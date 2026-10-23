#!/usr/bin/env python3
"""Lightweight client-server network service platform: server side.

Services
  echo      TCP  length-prefixed echo
  file      TCP  line-oriented file transfer (LIST / GET / PUT / STAT / STATS / PING / QUIT)
  udp-echo  UDP  datagram echo (used for loss measurements)
  dns       UDP  authoritative DNS (RFC 1035 wire format) with optional upstream forwarding
"""
import argparse
import collections
import hashlib
import json
import logging
import os
import queue
import random
import re
import resource
import signal
import socket
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler

import dns_wire as dw
from protocol import (CHUNK, HDR, MAX_FRAME, MAX_LINE, ConnectionClosed, ProtocolError,
                      recv_exact, sha256_file)

log = logging.getLogger("netplatform")
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


# --------------------------------------------------------------------------- stats
class Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self.counters = collections.Counter()
        self.active = collections.Counter()
        self.peak = collections.Counter()
        self.started = time.time()

    def inc(self, key, n=1):
        with self._lock:
            self.counters[key] += n

    def conn_open(self, svc):
        with self._lock:
            self.counters[f"{svc}.connections"] += 1
            self.active[svc] += 1
            self.peak[svc] = max(self.peak[svc], self.active[svc])

    def conn_close(self, svc):
        with self._lock:
            self.active[svc] -= 1

    def snapshot(self):
        ru = resource.getrusage(resource.RUSAGE_SELF)
        with self._lock:
            return {
                "uptime_s": round(time.time() - self.started, 3),
                "counters": dict(self.counters),
                "active": dict(self.active),
                "peak_active": dict(self.peak),
                "threads": threading.active_count(),
                "cpu_user_s": ru.ru_utime,
                "cpu_sys_s": ru.ru_stime,
                "maxrss_bytes": ru.ru_maxrss if sys.platform == "darwin" else ru.ru_maxrss * 1024,
            }


class Session:
    def __init__(self):
        self.bytes_in = self.bytes_out = self.requests = 0


# --------------------------------------------------------------------------- TCP base
class TCPService:
    name = "tcp"
    busy_reply = b""

    def __init__(self, host, port, stats, max_clients, idle_timeout):
        self.stats, self.idle_timeout = stats, idle_timeout
        self.slots = threading.BoundedSemaphore(max_clients)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(512)
        self.port = self.sock.getsockname()[1]
        self.stop_evt = threading.Event()
        self.conns, self.conns_lock = set(), threading.Lock()
        self.pending = queue.SimpleQueue()
        self.thread = threading.Thread(target=self._accept_loop, name=f"{self.name}-accept", daemon=True)
        self.dispatcher = threading.Thread(target=self._dispatch_loop, name=f"{self.name}-dispatch", daemon=True)

    def start(self):
        self.thread.start()
        self.dispatcher.start()
        log.info("svc=%s event=listening port=%d", self.name, self.port)

    def stop(self):
        self.stop_evt.set()
        try:
            self.sock.close()
        except OSError:
            pass
        with self.conns_lock:
            for c in list(self.conns):
                try:
                    c.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def _accept_loop(self):
        """Only accept() here so the kernel backlog drains fast; threads are started by _dispatch_loop."""
        self.sock.settimeout(0.5)
        while not self.stop_evt.is_set():
            try:
                self.pending.put(self.sock.accept())
            except socket.timeout:
                continue
            except OSError:
                break

    def _dispatch_loop(self):
        while not self.stop_evt.is_set():
            try:
                conn, addr = self.pending.get(timeout=0.5)
            except queue.Empty:
                continue
            if not self.slots.acquire(blocking=False):
                self.stats.inc(f"{self.name}.rejected_busy")
                log.warning("svc=%s client=%s:%d event=rejected reason=max_clients", self.name, *addr)
                try:
                    conn.sendall(self.busy_reply)
                except OSError:
                    pass
                conn.close()
                continue
            threading.Thread(target=self._serve, args=(conn, addr), daemon=True,
                             name=f"{self.name}-{addr[1]}").start()

    def _serve(self, conn, addr):
        sess, t0, outcome = Session(), time.perf_counter(), "closed"
        conn.settimeout(self.idle_timeout)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        with self.conns_lock:
            self.conns.add(conn)
        self.stats.conn_open(self.name)
        log.info("svc=%s client=%s:%d event=connect", self.name, *addr)
        try:
            self.handle(conn, addr, sess)
        except socket.timeout:
            outcome = "idle_timeout"
            self.stats.inc(f"{self.name}.timeouts")
            log.warning("svc=%s client=%s:%d event=timeout idle>%ss", self.name, *addr, self.idle_timeout)
        except ConnectionClosed as e:
            if e.partial:
                outcome = "aborted"
                self.stats.inc(f"{self.name}.aborted")
                log.warning("svc=%s client=%s:%d event=aborted detail=%s", self.name, *addr, e)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError) as e:
            outcome = "reset"
            self.stats.inc(f"{self.name}.resets")
            log.warning("svc=%s client=%s:%d event=reset detail=%s", self.name, *addr, e)
        except ProtocolError as e:
            outcome = "protocol_error"
            self.stats.inc(f"{self.name}.protocol_errors")
            log.warning("svc=%s client=%s:%d event=protocol_error detail=%s", self.name, *addr, e)
            self.on_protocol_error(conn, e)
        except OSError as e:
            outcome = "os_error"
            self.stats.inc(f"{self.name}.os_errors")
            log.warning("svc=%s client=%s:%d event=os_error detail=%s", self.name, *addr, e)
        except Exception:
            outcome = "internal_error"
            self.stats.inc(f"{self.name}.internal_errors")
            log.exception("svc=%s client=%s:%d event=internal_error", self.name, *addr)
        finally:
            with self.conns_lock:
                self.conns.discard(conn)
            try:
                conn.close()
            except OSError:
                pass
            self.slots.release()
            self.stats.conn_close(self.name)
            self.stats.inc(f"{self.name}.bytes_in", sess.bytes_in)
            self.stats.inc(f"{self.name}.bytes_out", sess.bytes_out)
            self.stats.inc(f"{self.name}.requests", sess.requests)
            log.info("svc=%s client=%s:%d event=disconnect outcome=%s requests=%d bytes_in=%d bytes_out=%d dur_ms=%.1f",
                     self.name, *addr, outcome, sess.requests, sess.bytes_in, sess.bytes_out,
                     (time.perf_counter() - t0) * 1000)

    def on_protocol_error(self, conn, err):
        pass

    def handle(self, conn, addr, sess):
        raise NotImplementedError


class EchoService(TCPService):
    name = "echo"

    def handle(self, conn, addr, sess):
        while True:
            hdr = recv_exact(conn, HDR.size)
            (n,) = HDR.unpack(hdr)
            if n > MAX_FRAME:
                raise ProtocolError(f"frame length {n} exceeds {MAX_FRAME}")
            try:
                payload = recv_exact(conn, n)
            except ConnectionClosed as e:
                raise ConnectionClosed(str(e), partial=True)
            conn.sendall(hdr + payload)
            sess.requests += 1
            sess.bytes_in += HDR.size + n
            sess.bytes_out += HDR.size + n
            log.debug("svc=echo client=%s:%d event=echo bytes=%d", *addr, n)


class FileService(TCPService):
    name = "file"
    busy_reply = b"ERR 503 server busy\n"

    def __init__(self, host, port, stats, max_clients, idle_timeout, root, max_file, send_mode, status_fn,
                 digest_cache=True):
        super().__init__(host, port, stats, max_clients, idle_timeout)
        self.root, self.max_file, self.send_mode, self.status_fn = root, max_file, send_mode, status_fn
        self.digest_cache, self._digests = digest_cache, {}
        os.makedirs(root, exist_ok=True)

    def _digest(self, path):
        """SHA-256 of a stored file; cached per (mtime, size) so repeated GET/STAT skip the extra read pass."""
        if not self.digest_cache:
            return sha256_file(path)
        st = os.stat(path)
        hit = self._digests.get(path)
        if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
            self.stats.inc("file.digest_cache_hits")
            return hit[2]
        digest = sha256_file(path)
        self._digests[path] = (st.st_mtime_ns, st.st_size, digest)
        return digest

    def on_protocol_error(self, conn, err):
        try:
            conn.sendall(f"ERR 400 {err}\n".encode())
        except OSError:
            pass

    def _path(self, name):
        if not NAME_RE.match(name):
            return None
        return os.path.join(self.root, name)

    def handle(self, conn, addr, sess):
        f = conn.makefile("rb", CHUNK)

        def send(text):
            b = text.encode()
            conn.sendall(b)
            sess.bytes_out += len(b)

        while True:
            line = f.readline(MAX_LINE + 1)
            if not line:
                return
            if len(line) > MAX_LINE or not line.endswith(b"\n"):
                raise ProtocolError("command line too long or unterminated")
            sess.bytes_in += len(line)
            sess.requests += 1
            parts = line.decode("utf-8", "replace").split()
            cmd = parts[0].upper() if parts else ""
            t0 = time.perf_counter()
            if cmd == "PING":
                send("OK pong\n")
            elif cmd == "QUIT":
                send("OK bye\n")
                return
            elif cmd == "STATS":
                body = json.dumps(self.status_fn()).encode()
                send(f"OK {len(body)}\n")
                conn.sendall(body)
                sess.bytes_out += len(body)
            elif cmd == "LIST":
                items = []
                for n in sorted(os.listdir(self.root)):
                    try:                                 # a file may be replaced/removed between listdir and stat
                        if not n.startswith(".") and os.path.isfile(os.path.join(self.root, n)):
                            items.append({"name": n, "size": os.path.getsize(os.path.join(self.root, n))})
                    except OSError:
                        continue
                body = json.dumps(items).encode()
                send(f"OK {len(body)}\n")
                conn.sendall(body)
                sess.bytes_out += len(body)
                log.info("svc=file client=%s:%d event=list files=%d", *addr, len(items))
            elif cmd in ("GET", "STAT") and len(parts) == 2:
                path = self._path(parts[1])
                if path is None:
                    send("ERR 400 invalid file name\n")
                elif not os.path.isfile(path):
                    send("ERR 404 no such file\n")
                    log.info("svc=file client=%s:%d event=%s name=%s result=not_found", *addr, cmd.lower(), parts[1])
                else:
                    size, digest = os.path.getsize(path), self._digest(path)
                    send(f"OK {size} {digest}\n")
                    if cmd == "GET":
                        self._send_file(conn, path, size, sess)
                        log.info("svc=file client=%s:%d event=get name=%s bytes=%d dur_ms=%.1f",
                                 *addr, parts[1], size, (time.perf_counter() - t0) * 1000)
            elif cmd == "PUT" and len(parts) == 4:
                self._receive_file(conn, f, addr, sess, parts[1], parts[2], parts[3].lower(), send, t0)
            else:
                send("ERR 400 unknown or malformed command\n")
                log.warning("svc=file client=%s:%d event=bad_command line=%r", *addr, line[:60])

    def _send_file(self, conn, path, size, sess):
        with open(path, "rb") as fh:
            if self.send_mode == "sendfile":
                conn.sendfile(fh)
            else:
                while True:
                    b = fh.read(CHUNK)
                    if not b:
                        break
                    conn.sendall(b)
        sess.bytes_out += size

    def _receive_file(self, conn, f, addr, sess, name, size_s, want_sha, send, t0):
        path = self._path(name)
        if path is None or not size_s.isdigit() or not SHA_RE.match(want_sha):
            send("ERR 400 invalid PUT arguments\n")
            return
        size = int(size_s)
        if size > self.max_file:
            send(f"ERR 413 file exceeds {self.max_file} bytes\n")
            log.warning("svc=file client=%s:%d event=put name=%s result=too_large size=%d", *addr, name, size)
            return
        send("READY\n")
        tmp = os.path.join(self.root, f".tmp-{uuid.uuid4().hex}")
        h, left = hashlib.sha256(), size
        try:
            with open(tmp, "wb") as out:
                while left:
                    b = f.read(min(CHUNK, left))
                    if not b:
                        raise ConnectionClosed(f"upload of {name} aborted with {left} bytes missing", partial=True)
                    out.write(b)
                    h.update(b)
                    left -= len(b)
                    sess.bytes_in += len(b)
            if h.hexdigest() != want_sha:
                send("ERR 422 checksum mismatch\n")
                self.stats.inc("file.checksum_failures")
                log.warning("svc=file client=%s:%d event=put name=%s result=checksum_mismatch", *addr, name)
                return
            os.replace(tmp, path)
            tmp = None
            if self.digest_cache:                        # we just verified these bytes: remember the digest
                st = os.stat(path)
                self._digests[path] = (st.st_mtime_ns, st.st_size, want_sha)
            send(f"OK {size}\n")
            log.info("svc=file client=%s:%d event=put name=%s bytes=%d dur_ms=%.1f",
                     *addr, name, size, (time.perf_counter() - t0) * 1000)
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)                       # never leave partial uploads behind


# --------------------------------------------------------------------------- UDP
class UDPService:
    name = "udp"

    def __init__(self, host, port, stats, workers, loss, rng):
        self.stats, self.loss, self.rng = stats, loss, rng
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        self.sock.bind((host, port))
        self.port = self.sock.getsockname()[1]
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=self.name)
        self.stop_evt = threading.Event()
        self.thread = threading.Thread(target=self._loop, name=f"{self.name}-recv", daemon=True)

    def start(self):
        self.thread.start()
        log.info("svc=%s event=listening port=%d loss_sim=%.2f", self.name, self.port, self.loss)

    def stop(self):
        self.stop_evt.set()
        try:
            self.sock.close()
        except OSError:
            pass
        self.pool.shutdown(wait=False, cancel_futures=True)

    def _drop(self):
        return self.loss > 0 and self.rng.random() < self.loss

    def _loop(self):
        while not self.stop_evt.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
            except OSError:
                break
            self.stats.inc(f"{self.name}.datagrams_in")
            if self._drop():
                self.stats.inc(f"{self.name}.dropped_in")
                log.info("svc=%s client=%s:%d event=drop dir=in bytes=%d simulated=1", self.name, *addr, len(data))
                continue
            try:
                self.pool.submit(self._work, data, addr)
            except RuntimeError:
                break

    def _work(self, data, addr):
        try:
            reply = self.handle(data, addr)
            if reply is None:
                return
            if self._drop():
                self.stats.inc(f"{self.name}.dropped_out")
                log.info("svc=%s client=%s:%d event=drop dir=out bytes=%d simulated=1", self.name, *addr, len(reply))
                return
            self.sock.sendto(reply, addr)
            self.stats.inc(f"{self.name}.datagrams_out")
        except OSError as e:
            self.stats.inc(f"{self.name}.os_errors")
            log.warning("svc=%s client=%s:%d event=os_error detail=%s", self.name, *addr, e)
        except Exception:
            self.stats.inc(f"{self.name}.internal_errors")
            log.exception("svc=%s client=%s:%d event=internal_error", self.name, *addr)

    def handle(self, data, addr):
        raise NotImplementedError


class UDPEchoService(UDPService):
    name = "udp-echo"

    def handle(self, data, addr):
        log.debug("svc=udp-echo client=%s:%d event=echo bytes=%d", *addr, len(data))
        return data


class Zone:
    def __init__(self, path):
        with open(path) as fh:
            cfg = json.load(fh)
        self.ttl = cfg.get("ttl", 300)
        self.records = {k.lower(): v for k, v in cfg["records"].items()}

    def resolve(self, qname, qtype):
        """Return (known, answers). known=False means the name is not in the zone."""
        name = qname.lower().rstrip(".")
        if name not in self.records:
            return False, []
        answers = []
        for _ in range(8):                                   # follow CNAME chains
            rec = self.records.get(name, {})
            if "CNAME" in rec and qtype != dw.QTYPES["CNAME"]:
                answers.append((name, dw.QTYPES["CNAME"], self.ttl, dw.encode_rdata(dw.QTYPES["CNAME"], rec["CNAME"])))
                name = rec["CNAME"].lower().rstrip(".")
                if name not in self.records:
                    break
                continue
            tname = dw.QTYPE_NAMES.get(qtype)
            vals = rec.get(tname, []) if tname else []
            if isinstance(vals, str):
                vals = [vals]
            for v in vals:
                answers.append((name, qtype, self.ttl, dw.encode_rdata(qtype, v)))
            break
        return True, answers


class DNSService(UDPService):
    name = "dns"

    def __init__(self, host, port, stats, workers, loss, rng, zone, upstream, upstream_timeout=1.0):
        super().__init__(host, port, stats, workers, loss, rng)
        self.zone, self.upstream, self.upstream_timeout = zone, upstream, upstream_timeout
        self.cache, self.cache_lock = {}, threading.Lock()

    def handle(self, data, addr):
        t0 = time.perf_counter()
        try:
            q = dw.parse_query(data)
        except dw.DNSFormatError as e:
            self.stats.inc("dns.malformed")
            log.warning("svc=dns client=%s:%d event=malformed detail=%s", *addr, e)
            return dw.header_only(int.from_bytes(data[:2], "big"), dw.RCODE_FORMERR) if len(data) >= 2 else None
        tname = dw.QTYPE_NAMES.get(q.qtype, str(q.qtype))
        if q.opcode != 0:
            rcode, resp = dw.RCODE_NOTIMP, dw.build_response(q, dw.RCODE_NOTIMP, [], aa=False)
        elif q.qclass != dw.CLASS_IN:
            rcode, resp = dw.RCODE_REFUSED, dw.build_response(q, dw.RCODE_REFUSED, [], aa=False)
        else:
            known, answers = self.zone.resolve(q.qname, q.qtype)
            if known:
                rcode, resp = dw.RCODE_NOERROR, dw.build_response(q, dw.RCODE_NOERROR, answers)
            elif self.upstream:
                resp = self._forward(q, data)
                rcode = resp[3] & 0xF
            else:
                rcode, resp = dw.RCODE_NXDOMAIN, dw.build_response(q, dw.RCODE_NXDOMAIN, [])
        self.stats.inc(f"dns.rcode.{dw.RCODE_NAMES[rcode]}")
        log.info("svc=dns client=%s:%d event=query name=%s type=%s rcode=%s dur_us=%.0f",
                 *addr, q.qname, tname, dw.RCODE_NAMES[rcode], (time.perf_counter() - t0) * 1e6)
        return resp

    def _forward(self, q, data):
        key = (q.qname.lower(), q.qtype, q.qclass)
        now = time.time()
        with self.cache_lock:
            hit = self.cache.get(key)
        if hit and hit[1] > now:
            self.stats.inc("dns.cache_hits")
            return data[:2] + hit[0][2:]
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(self.upstream_timeout)
        try:
            s.sendto(data, self.upstream)
            resp, _ = s.recvfrom(4096)
            if resp[:2] != data[:2]:
                raise OSError("upstream id mismatch")
            ttl = min([a.ttl for a in dw.parse_response(resp).answers] or [30])
            with self.cache_lock:
                self.cache[key] = (resp, now + min(ttl, 300))
            self.stats.inc("dns.forwarded")
            return resp
        except (OSError, dw.DNSFormatError) as e:
            self.stats.inc("dns.upstream_failures")
            log.warning("svc=dns event=upstream_failure name=%s detail=%s", q.qname, e)
            return dw.build_response(q, dw.RCODE_SERVFAIL, [], aa=False, ra=True)
        finally:
            s.close()


# --------------------------------------------------------------------------- platform
class Platform:
    def __init__(self, host="127.0.0.1", echo_port=9001, file_port=9002, udp_echo_port=9003, dns_port=9053,
                 root="storage", zone="zone.json", max_clients=200, idle_timeout=30.0, loss=0.0,
                 upstream=None, max_file=256 * 1024 * 1024, send_mode="chunked", workers=16, seed=None,
                 digest_cache=True):
        self.stats = Stats()
        rng = random.Random(seed)
        self.echo = EchoService(host, echo_port, self.stats, max_clients, idle_timeout)
        self.files = FileService(host, file_port, self.stats, max_clients, idle_timeout, root, max_file,
                                 send_mode, self.stats.snapshot, digest_cache)
        self.uecho = UDPEchoService(host, udp_echo_port, self.stats, workers, loss, rng)
        self.dns = DNSService(host, dns_port, self.stats, workers, loss, rng, Zone(zone), upstream)
        self.services = [self.echo, self.files, self.uecho, self.dns]

    @property
    def ports(self):
        return {"echo": self.echo.port, "file": self.files.port, "udp-echo": self.uecho.port, "dns": self.dns.port}

    def start(self):
        for s in self.services:
            s.start()
        return self

    def stop(self):
        for s in self.services:
            s.stop()
        log.info("event=shutdown stats=%s", json.dumps(self.stats.snapshot(), sort_keys=True))


def setup_logging(path, level):
    log.setLevel(level)
    fmt = logging.Formatter("%(asctime)s.%(msecs)03d | %(levelname)-7s | %(message)s", "%Y-%m-%d %H:%M:%S")
    handlers = [logging.StreamHandler(sys.stderr)]
    if path:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        handlers.append(RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3))
    for h in handlers:
        h.setFormatter(fmt)
        log.addHandler(h)


def raise_fd_limit(target=8192):
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = target if hard == resource.RLIM_INFINITY else min(target, hard)
    if soft < want:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
        except (ValueError, OSError):
            pass


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1", help="bind address (use 0.0.0.0 for LAN access)")
    p.add_argument("--echo-port", type=int, default=9001)
    p.add_argument("--file-port", type=int, default=9002)
    p.add_argument("--udp-echo-port", type=int, default=9003)
    p.add_argument("--dns-port", type=int, default=9053)
    p.add_argument("--root", default="storage", help="directory served by the file service")
    p.add_argument("--zone", default="zone.json")
    p.add_argument("--upstream", help="forward unknown DNS names to HOST:PORT")
    p.add_argument("--max-clients", type=int, default=200, help="concurrent TCP connections per service")
    p.add_argument("--idle-timeout", type=float, default=30.0, help="seconds before an idle TCP client is dropped")
    p.add_argument("--max-file-mb", type=int, default=256)
    p.add_argument("--send-mode", choices=["chunked", "sendfile"], default="chunked")
    p.add_argument("--digest-cache", choices=["on", "off"], default="on", help="cache SHA-256 of stored files")
    p.add_argument("--loss", type=float, default=0.0, help="simulated per-datagram loss probability (UDP services)")
    p.add_argument("--seed", type=int)
    p.add_argument("--workers", type=int, default=1,
                   help="pre-forked worker processes sharing the listening sockets (bypasses the one-core GIL limit; stats are per process)")
    p.add_argument("--log-file", default="logs/server.log")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--stats-interval", type=float, default=30.0, help="seconds between stats log lines (0 = off)")
    a = p.parse_args()
    raise_fd_limit()
    setup_logging(a.log_file, a.log_level)
    up = None
    if a.upstream:
        h, _, pt = a.upstream.rpartition(":")
        up = (h, int(pt))
    plat = Platform(a.host, a.echo_port, a.file_port, a.udp_echo_port, a.dns_port, a.root, a.zone, a.max_clients,
                    a.idle_timeout, a.loss, up, a.max_file_mb * 1024 * 1024, a.send_mode, seed=a.seed,
                    digest_cache=a.digest_cache == "on")
    # Pre-fork: sockets are already bound and listening, so every worker inherits them and the kernel hands each
    # new connection / datagram to whichever worker is waiting. Fork happens before any thread is started.
    children, is_worker = [], False
    for _ in range(max(1, a.workers) - 1):
        pid = os.fork()
        if pid == 0:
            is_worker, children = True, []
            break
        children.append(pid)
    plat.start()
    if not is_worker:
        print("READY " + json.dumps(plat.ports), flush=True)
        if children:
            log.info("event=prefork workers=%d pids=%s", len(children) + 1, children)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.wait(a.stats_interval or 3600):
        if a.stats_interval:
            log.info("event=stats %s", json.dumps(plat.stats.snapshot(), sort_keys=True))
    for pid in children:
        try:
            os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, 0)
        except OSError:
            pass
    plat.stop()


if __name__ == "__main__":
    main()
