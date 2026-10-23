#!/usr/bin/env python3
"""Client library and CLI for the network service platform."""
import argparse
import hashlib
import json
import os
import socket
import struct
import sys
import time

import dns_wire as dw
from protocol import CHUNK, HDR, ConnectionClosed, ProtocolError, recv_exact, recv_frame, send_frame


class ServiceError(Exception):
    """Server answered with an ERR line."""


def tcp_connect(host, port, timeout=5.0, retries=2, backoff=0.2):
    """Connect with bounded retries and exponential back-off (failure detection on connect)."""
    last = None
    for i in range(retries + 1):
        try:
            s = socket.create_connection((host, port), timeout=timeout)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return s
        except OSError as e:
            last = e
            if i < retries:
                time.sleep(backoff * 2 ** i)
    raise ConnectionError(f"cannot connect to {host}:{port} after {retries + 1} attempts: {last}")


class EchoClient:
    def __init__(self, host="127.0.0.1", port=9001, timeout=5.0):
        self.sock = tcp_connect(host, port, timeout)
        self.sock.settimeout(timeout)

    def echo(self, payload):
        """Send payload, return (reply, rtt_seconds)."""
        t0 = time.perf_counter()
        send_frame(self.sock, payload)
        reply = recv_frame(self.sock)
        rtt = time.perf_counter() - t0
        if reply != payload:
            raise ProtocolError("echo reply differs from request")
        return reply, rtt

    def close(self):
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class FileClient:
    def __init__(self, host="127.0.0.1", port=9002, timeout=10.0):
        self.sock = tcp_connect(host, port, timeout)
        self.sock.settimeout(timeout)
        self.f = self.sock.makefile("rb", CHUNK)

    def _line(self):
        line = self.f.readline(2048)
        if not line:
            raise ConnectionClosed("server closed the connection")
        text = line.decode().rstrip("\n")
        if text.startswith("ERR"):
            raise ServiceError(text)
        return text

    def _cmd(self, text):
        self.sock.sendall(text.encode() + b"\n")
        return self._line()

    def ping(self):
        t0 = time.perf_counter()
        self._cmd("PING")
        return time.perf_counter() - t0

    def _json(self, cmd):
        n = int(self._cmd(cmd).split()[1])
        return json.loads(self.f.read(n))

    def list(self):
        return self._json("LIST")

    def server_stats(self):
        return self._json("STATS")

    def stat(self, name):
        _, size, digest = self._cmd(f"STAT {name}").split()
        return int(size), digest

    def get(self, name, dest):
        """Download name to dest; verifies SHA-256. Returns (bytes, seconds)."""
        t0 = time.perf_counter()
        _, size, digest = self._cmd(f"GET {name}").split()
        size, left, h = int(size), int(size), hashlib.sha256()
        tmp = dest + ".part"
        try:
            with open(tmp, "wb") as out:
                while left:
                    b = self.f.read(min(CHUNK, left))
                    if not b:
                        raise ConnectionClosed(f"download interrupted, {left} bytes missing", partial=True)
                    out.write(b)
                    h.update(b)
                    left -= len(b)
            if h.hexdigest() != digest:
                raise ProtocolError("downloaded file failed SHA-256 verification")
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        return size, time.perf_counter() - t0

    def put(self, path, name=None):
        """Upload path; the server verifies the SHA-256. Returns (bytes, seconds)."""
        name = name or os.path.basename(path)
        size = os.path.getsize(path)
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for b in iter(lambda: fh.read(CHUNK * 4), b""):
                h.update(b)
        t0 = time.perf_counter()
        resp = self._cmd(f"PUT {name} {size} {h.hexdigest()}")
        if resp != "READY":
            raise ProtocolError(f"unexpected reply {resp!r}")
        with open(path, "rb") as fh:
            self.sock.sendfile(fh)
        self._line()                                     # "OK <size>" or raises ServiceError
        return size, time.perf_counter() - t0

    def close(self):
        try:
            self.sock.sendall(b"QUIT\n")
        except OSError:
            pass
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class RTTEstimator:
    """Adaptive retransmission timeout in the style of RFC 6298 (Jacobson/Karels).

    SRTT and RTTVAR are smoothed from RTT samples; RTO = SRTT + 4*RTTVAR, clamped to
    [min_rto, max_rto]. Only replies to first transmissions are sampled (Karn's rule), and
    each retransmission doubles the timeout (exponential back-off).
    """

    def __init__(self, initial=0.5, min_rto=0.005, max_rto=2.0, alpha=0.125, beta=0.25):
        self.srtt = self.rttvar = None
        self.initial, self.min_rto, self.max_rto, self.alpha, self.beta = initial, min_rto, max_rto, alpha, beta

    def sample(self, rtt):
        if self.srtt is None:
            self.srtt, self.rttvar = rtt, rtt / 2
        else:
            self.rttvar = (1 - self.beta) * self.rttvar + self.beta * abs(self.srtt - rtt)
            self.srtt = (1 - self.alpha) * self.srtt + self.alpha * rtt

    def rto(self):
        if self.srtt is None:
            return self.initial
        return min(self.max_rto, max(self.min_rto, self.srtt + 4 * self.rttvar))


def udp_request(host, port, payload, match, timeout=0.5, retries=2, estimator=None):
    """Send a datagram and wait for a reply accepted by match(reply).

    Retransmits up to `retries` times. With an RTTEstimator the per-attempt timeout is
    adaptive (RTO, doubled on every retransmission); otherwise it is the fixed `timeout`.
    Returns (reply, total_seconds, attempts); raises socket.timeout when every attempt is lost.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        t0 = time.perf_counter()
        wait = estimator.rto() if estimator else timeout
        for attempt in range(1, retries + 2):
            sent = time.perf_counter()
            s.sendto(payload, (host, port))
            deadline = sent + wait
            while True:
                left = deadline - time.perf_counter()
                if left <= 0:
                    break
                s.settimeout(left)
                try:
                    data, _ = s.recvfrom(65535)
                except socket.timeout:
                    break
                if match(data):
                    if estimator and attempt == 1:          # Karn: never sample retransmitted requests
                        estimator.sample(time.perf_counter() - sent)
                    return data, time.perf_counter() - t0, attempt
            if estimator:
                wait = min(wait * 2, estimator.max_rto)     # exponential back-off
        raise socket.timeout(f"no reply after {retries + 1} attempts")
    finally:
        s.close()


def udp_echo(host, port, payload, timeout=0.5, retries=2, estimator=None):
    return udp_request(host, port, payload, lambda d: d == payload, timeout, retries, estimator)


_txid = 0


def dns_query(host, port, name, qtype="A", timeout=0.5, retries=2, estimator=None):
    """Resolve name. Returns (Response, total_seconds, attempts)."""
    global _txid
    _txid = (_txid + 1) & 0xFFFF
    qid = _txid
    pkt = dw.build_query(name, qtype, qid)

    def match(d):
        return len(d) >= 12 and struct.unpack("!H", d[:2])[0] == qid

    data, secs, attempts = udp_request(host, port, pkt, match, timeout, retries, estimator)
    return dw.parse_response(data), secs, attempts


def rate(n, secs):
    bps = n / max(secs, 1e-9)
    return f"{bps / 1e6:.1f} MB/s" if bps >= 1e6 else f"{bps / 1e3:.1f} kB/s"


def main():
    p = argparse.ArgumentParser(description="Client for the lightweight network service platform")
    p.add_argument("--host", default="127.0.0.1")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("echo", help="TCP echo")
    e.add_argument("text", nargs="?")
    e.add_argument("--port", type=int, default=9001)
    e.add_argument("-i", "--interactive", action="store_true")
    u = sub.add_parser("udp-echo", help="UDP echo")
    u.add_argument("text")
    u.add_argument("--port", type=int, default=9003)
    u.add_argument("--timeout", type=float, default=0.5)
    u.add_argument("--retries", type=int, default=2)
    d = sub.add_parser("dns", help="DNS lookup")
    d.add_argument("name")
    d.add_argument("type", nargs="?", default="A", choices=list(dw.QTYPES))
    d.add_argument("--port", type=int, default=9053)
    d.add_argument("--timeout", type=float, default=0.5)
    d.add_argument("--retries", type=int, default=2)
    d.add_argument("--adaptive", action="store_true", help="adaptive (RFC 6298-style) retransmission timeout")
    f = sub.add_parser("file", help="file transfer")
    f.add_argument("--port", type=int, default=9002)
    fs = f.add_subparsers(dest="op", required=True)
    fs.add_parser("list")
    fs.add_parser("stats")
    g = fs.add_parser("get"); g.add_argument("name"); g.add_argument("dest", nargs="?")
    pu = fs.add_parser("put"); pu.add_argument("path"); pu.add_argument("name", nargs="?")
    a = p.parse_args()
    try:
        if a.cmd == "echo":
            with EchoClient(a.host, a.port) as c:
                msgs = sys.stdin if a.interactive else [a.text or "hello"]
                for m in msgs:
                    r, rtt = c.echo(m.rstrip("\n").encode())
                    print(f"{r.decode()!r}  ({rtt * 1000:.3f} ms)")
        elif a.cmd == "udp-echo":
            r, secs, n = udp_echo(a.host, a.port, a.text.encode(), a.timeout, a.retries)
            print(f"{r.decode()!r}  ({secs * 1000:.3f} ms, {n} attempt(s))")
        elif a.cmd == "dns":
            r, secs, n = dns_query(a.host, a.port, a.name, a.type, a.timeout, a.retries,
                                   RTTEstimator(initial=a.timeout) if a.adaptive else None)
            print(f";; rcode={dw.RCODE_NAMES[r.rcode]} aa={int(r.aa)} tc={int(r.tc)} time={secs * 1000:.3f} ms attempts={n}")
            for ans in r.answers:
                print(f"{ans.name}\t{ans.ttl}\tIN\t{dw.QTYPE_NAMES.get(ans.rtype, ans.rtype)}\t{ans.value}")
        else:
            with FileClient(a.host, a.port) as c:
                if a.op == "list":
                    for it in c.list():
                        print(f"{it['size']:>12}  {it['name']}")
                elif a.op == "stats":
                    print(json.dumps(c.server_stats(), indent=2, sort_keys=True))
                elif a.op == "get":
                    n, s = c.get(a.name, a.dest or a.name)
                    print(f"downloaded {n} bytes in {s:.3f} s ({rate(n, s)}), SHA-256 verified")
                else:
                    n, s = c.put(a.path, a.name)
                    print(f"uploaded {n} bytes in {s:.3f} s ({rate(n, s)}), SHA-256 verified by server")
    except (ServiceError, ConnectionError, ConnectionClosed, ProtocolError, socket.timeout, OSError) as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
