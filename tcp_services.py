"""TCP services: thread-per-connection lifecycle (TCPService), echo, file transfer, DNS over TCP, and an
event-loop echo service (EventLoopEchoService) for the threads-versus-event-loop comparison."""
import hashlib
import hmac
import json
import os
import queue
import re
import selectors
import socket
import ssl
import struct
import threading
import time
import uuid

from core import Session, log
from protocol import (CHUNK, HDR, MAX_FRAME, MAX_LINE, ConnectionClosed, ProtocolError,
                      recv_exact, sha256_file)

NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


# --------------------------------------------------------------------------- TCP base
class TCPService:
    name = "tcp"
    busy_reply = b""

    def __init__(self, host, port, stats, max_clients, idle_timeout):
        self.stats, self.idle_timeout = stats, idle_timeout
        self.tls_ctx = None                       # set to an ssl.SSLContext to serve this port over TLS
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
            if self.tls_ctx is not None:              # TLS handshake runs in this connection's own thread
                raw = conn
                conn = self.tls_ctx.wrap_socket(raw, server_side=True)
                with self.conns_lock:
                    self.conns.discard(raw)
                    self.conns.add(conn)
                log.info("svc=%s client=%s:%d event=tls_established version=%s cipher=%s",
                         self.name, *addr, conn.version(), conn.cipher()[0])
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
        except ssl.SSLError as e:
            outcome = "tls_error"
            self.stats.inc(f"{self.name}.tls_errors")
            log.warning("svc=%s client=%s:%d event=tls_error detail=%s", self.name, *addr, e)
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

    MAX_AUTH_FAILURES = 3

    def __init__(self, host, port, stats, max_clients, idle_timeout, root, max_file, send_mode, status_fn,
                 digest_cache=True, tls_ctx=None, auth_token=None):
        super().__init__(host, port, stats, max_clients, idle_timeout)
        self.tls_ctx, self.auth_token = tls_ctx, auth_token
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
        authed, failures = self.auth_token is None, 0

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
            if cmd == "AUTH":
                if self.auth_token is None:
                    send("OK authentication not required\n")
                elif len(parts) == 2 and hmac.compare_digest(parts[1].encode(), self.auth_token.encode()):
                    authed = True
                    send("OK authenticated\n")
                    log.info("svc=file client=%s:%d event=auth result=ok", *addr)
                else:
                    failures += 1
                    self.stats.inc("file.auth_failures")
                    log.warning("svc=file client=%s:%d event=auth result=denied attempt=%d", *addr, failures)
                    send("ERR 401 invalid token\n")
                    if failures >= self.MAX_AUTH_FAILURES:
                        return                               # brute-force limit: drop the connection
                continue
            if not authed and cmd not in ("PING", "QUIT"):
                self.stats.inc("file.unauthenticated")
                send("ERR 401 authentication required\n")
                continue
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


class DNSTCPService(TCPService):
    """DNS over TCP (RFC 1035 4.2.2, RFC 7766): each message carries a 2-byte length prefix and several
    queries may be sent on one connection. Answers are not truncated (64 KiB limit)."""
    name = "dns-tcp"

    def __init__(self, host, port, stats, max_clients, idle_timeout, resolver):
        super().__init__(host, port, stats, max_clients, idle_timeout)
        self.resolver = resolver

    def handle(self, conn, addr, sess):
        while True:
            (n,) = struct.unpack("!H", recv_exact(conn, 2))
            try:
                msg = recv_exact(conn, n)
            except ConnectionClosed as e:
                raise ConnectionClosed(str(e), partial=True)
            sess.requests += 1
            sess.bytes_in += 2 + n
            resp = self.resolver.answer(msg, addr, limit=65535, transport="tcp")
            if resp is None:
                return
            conn.sendall(struct.pack("!H", len(resp)) + resp)
            sess.bytes_out += 2 + len(resp)


class _LoopConn:
    __slots__ = ("addr", "inbuf", "outbuf", "last", "events", "sess", "t0")

    def __init__(self, addr):
        self.addr, self.inbuf, self.outbuf = addr, bytearray(), bytearray()
        self.last = self.t0 = time.monotonic()
        self.events, self.sess = selectors.EVENT_READ, Session()


class EventLoopEchoService:
    """Echo service on one non-blocking event loop (`selectors`: kqueue on macOS, epoll on Linux).

    Same wire protocol, statistics and log events as EchoService, but no thread per connection: a single
    thread multiplexes every socket. Idle timeout, client cap and frame-size checks are enforced in the loop,
    and reading stops while a client's unsent replies exceed OUT_LIMIT (back-pressure)."""
    name = "echo"
    OUT_LIMIT = 4 * 1024 * 1024

    def __init__(self, host, port, stats, max_clients, idle_timeout):
        self.stats, self.max_clients, self.idle_timeout = stats, max_clients, idle_timeout
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(512)
        self.sock.setblocking(False)
        self.port = self.sock.getsockname()[1]
        self.sel = selectors.DefaultSelector()
        self.conns = {}
        self.stop_evt = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="echo-loop", daemon=True)

    def start(self):
        self.sel.register(self.sock, selectors.EVENT_READ, None)
        self.thread.start()
        log.info("svc=echo event=listening port=%d io=event-loop", self.port)

    def stop(self):
        self.stop_evt.set()
        self.thread.join(timeout=2)

    def _loop(self):
        last_sweep = time.monotonic()
        try:
            while not self.stop_evt.is_set():
                for key, ev in self.sel.select(timeout=0.25):
                    if key.data is None:
                        self._accept()
                    else:
                        self._service(key.fileobj, key.data, ev)
                now = time.monotonic()
                if now - last_sweep >= 0.25:
                    last_sweep = now
                    for conn, st in list(self.conns.items()):
                        if now - st.last > self.idle_timeout:
                            self.stats.inc("echo.timeouts")
                            log.warning("svc=echo client=%s:%d event=timeout idle>%ss", *st.addr, self.idle_timeout)
                            self._close(conn, st, "idle_timeout")
        finally:
            for conn, st in list(self.conns.items()):
                self._close(conn, st, "shutdown")
            self.sel.close()
            self.sock.close()

    def _accept(self):
        for _ in range(128):                                  # drain the backlog in one go
            try:
                conn, addr = self.sock.accept()
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            if len(self.conns) >= self.max_clients:
                self.stats.inc("echo.rejected_busy")
                log.warning("svc=echo client=%s:%d event=rejected reason=max_clients", *addr)
                conn.close()
                continue
            conn.setblocking(False)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            st = _LoopConn(addr)
            self.conns[conn] = st
            self.sel.register(conn, selectors.EVENT_READ, st)
            self.stats.conn_open("echo")
            log.info("svc=echo client=%s:%d event=connect", *addr)

    def _service(self, conn, st, ev):
        if ev & selectors.EVENT_READ:
            try:
                data = conn.recv(262144)
            except (BlockingIOError, InterruptedError):
                data = None
            except OSError as e:
                self.stats.inc("echo.resets")
                log.warning("svc=echo client=%s:%d event=reset detail=%s", *st.addr, e)
                return self._close(conn, st, "reset")
            if data == b"":
                if st.inbuf:
                    self.stats.inc("echo.aborted")
                    log.warning("svc=echo client=%s:%d event=aborted detail=closed with %d bytes of a frame",
                                *st.addr, len(st.inbuf))
                return self._close(conn, st, "aborted" if st.inbuf else "closed")
            if data:
                st.last = time.monotonic()
                st.inbuf += data
                while len(st.inbuf) >= HDR.size:
                    (n,) = HDR.unpack_from(st.inbuf)
                    if n > MAX_FRAME:
                        self.stats.inc("echo.protocol_errors")
                        log.warning("svc=echo client=%s:%d event=protocol_error detail=frame length %d exceeds %d",
                                    *st.addr, n, MAX_FRAME)
                        return self._close(conn, st, "protocol_error")
                    if len(st.inbuf) < HDR.size + n:
                        break
                    st.outbuf += st.inbuf[:HDR.size + n]
                    del st.inbuf[:HDR.size + n]
                    st.sess.requests += 1
                    st.sess.bytes_in += HDR.size + n
                    st.sess.bytes_out += HDR.size + n
        if st.outbuf:
            try:
                sent = conn.send(st.outbuf)
                del st.outbuf[:sent]
            except (BlockingIOError, InterruptedError):
                pass
            except OSError as e:
                self.stats.inc("echo.resets")
                log.warning("svc=echo client=%s:%d event=reset detail=%s", *st.addr, e)
                return self._close(conn, st, "reset")
        want = (selectors.EVENT_READ if len(st.outbuf) < self.OUT_LIMIT else 0) | \
               (selectors.EVENT_WRITE if st.outbuf else 0)
        if want != st.events:
            self.sel.modify(conn, want, st)
            st.events = want

    def _close(self, conn, st, outcome):
        try:
            self.sel.unregister(conn)
        except (KeyError, ValueError):
            pass
        conn.close()
        self.conns.pop(conn, None)
        self.stats.conn_close("echo")
        self.stats.inc("echo.bytes_in", st.sess.bytes_in)
        self.stats.inc("echo.bytes_out", st.sess.bytes_out)
        self.stats.inc("echo.requests", st.sess.requests)
        log.info("svc=echo client=%s:%d event=disconnect outcome=%s requests=%d bytes_in=%d bytes_out=%d dur_ms=%.1f",
                 *st.addr, outcome, st.sess.requests, st.sess.bytes_in, st.sess.bytes_out,
                 (time.monotonic() - st.t0) * 1000)
