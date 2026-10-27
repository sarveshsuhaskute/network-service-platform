"""UDP services: receive loop + worker pool, UDP echo, authoritative DNS with optional forwarding."""
import heapq
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import dns_wire as dw
from core import log


# --------------------------------------------------------------------------- UDP
class UDPService:
    name = "udp"

    def __init__(self, host, port, stats, workers, loss, rng, delay=0.0, jitter=0.0):
        self.stats, self.loss, self.rng = stats, loss, rng
        self.delay, self.jitter = delay, jitter     # emulated extra round-trip delay (seconds), like tc netem
        self._dq, self._dcv, self._dseq = [], threading.Condition(), 0
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
        threading.Thread(target=self._delay_loop, name=f"{self.name}-delay", daemon=True).start()
        log.info("svc=%s event=listening port=%d loss_sim=%.2f delay_ms=%.1f jitter_ms=%.1f",
                 self.name, self.port, self.loss, self.delay * 1000, self.jitter * 1000)

    def stop(self):
        self.stop_evt.set()
        with self._dcv:
            self._dcv.notify_all()
        try:
            self.sock.close()
        except OSError:
            pass
        self.pool.shutdown(wait=False, cancel_futures=True)

    def _send(self, reply, addr):
        if self.delay > 0 or self.jitter > 0:          # hold the reply in a timer queue instead of blocking a worker
            due = time.monotonic() + max(0.0, self.delay + self.rng.uniform(-self.jitter, self.jitter))
            with self._dcv:
                self._dseq += 1
                heapq.heappush(self._dq, (due, self._dseq, reply, addr))
                self._dcv.notify()
            return
        self.sock.sendto(reply, addr)
        self.stats.inc(f"{self.name}.datagrams_out")

    def _delay_loop(self):
        while not self.stop_evt.is_set():
            with self._dcv:
                if not self._dq:
                    self._dcv.wait(0.25)
                    continue
                due, _, reply, addr = self._dq[0]
                wait = due - time.monotonic()
                if wait > 0:
                    self._dcv.wait(wait)
                    continue
                heapq.heappop(self._dq)
            try:
                self.sock.sendto(reply, addr)
                self.stats.inc(f"{self.name}.datagrams_out")
            except OSError as e:
                self.stats.inc(f"{self.name}.os_errors")
                log.warning("svc=%s client=%s:%d event=os_error detail=%s", self.name, *addr, e)

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
            self._send(reply, addr)
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

    def __init__(self, host, port, stats, workers, loss, rng, zone, upstream, upstream_timeout=1.0,
                 delay=0.0, jitter=0.0):
        super().__init__(host, port, stats, workers, loss, rng, delay, jitter)
        self.zone, self.upstream, self.upstream_timeout = zone, upstream, upstream_timeout
        self.cache, self.cache_lock = {}, threading.Lock()

    def handle(self, data, addr):
        return self.answer(data, addr)

    def answer(self, data, addr, limit=dw.UDP_LIMIT, transport="udp"):
        """Resolve one DNS message; shared by the UDP service and the TCP service (DNSTCPService)."""
        t0 = time.perf_counter()
        try:
            q = dw.parse_query(data)
        except dw.DNSFormatError as e:
            self.stats.inc("dns.malformed")
            log.warning("svc=dns client=%s:%d transport=%s event=malformed detail=%s", *addr, transport, e)
            return dw.header_only(int.from_bytes(data[:2], "big"), dw.RCODE_FORMERR) if len(data) >= 2 else None
        tname = dw.QTYPE_NAMES.get(q.qtype, str(q.qtype))
        if q.opcode != 0:
            rcode, resp = dw.RCODE_NOTIMP, dw.build_response(q, dw.RCODE_NOTIMP, [], aa=False, limit=limit)
        elif q.qclass != dw.CLASS_IN:
            rcode, resp = dw.RCODE_REFUSED, dw.build_response(q, dw.RCODE_REFUSED, [], aa=False, limit=limit)
        else:
            known, answers = self.zone.resolve(q.qname, q.qtype)
            if known:
                rcode, resp = dw.RCODE_NOERROR, dw.build_response(q, dw.RCODE_NOERROR, answers, limit=limit)
            elif self.upstream:
                resp = self._forward(q, data)
                rcode = resp[3] & 0xF
            else:
                rcode, resp = dw.RCODE_NXDOMAIN, dw.build_response(q, dw.RCODE_NXDOMAIN, [], limit=limit)
        truncated = bool(resp[2] & 0x02)
        self.stats.inc(f"dns.rcode.{dw.RCODE_NAMES[rcode]}")
        self.stats.inc(f"dns.{transport}_queries")
        if truncated:
            self.stats.inc("dns.truncated")
        log.info("svc=dns client=%s:%d transport=%s event=query name=%s type=%s rcode=%s%s dur_us=%.0f",
                 *addr, transport, q.qname, tname, dw.RCODE_NAMES[rcode], " tc=1" if truncated else "",
                 (time.perf_counter() - t0) * 1e6)
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
