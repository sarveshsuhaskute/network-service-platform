#!/usr/bin/env python3
"""Web dashboard for the network service platform.

Starts the real platform (echo / file / udp-echo / dns) in this process and serves a browser UI on
http://127.0.0.1:8080. Every button goes through client.py over real sockets, so the page shows the same
behaviour as the command-line demo, with a terminal-style output panel, live server log and live statistics.
"""
import argparse
import collections
import json
import logging
import os
import statistics
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import client as cl
import dns_wire as dw
import server as sv

ROOT = os.path.dirname(os.path.abspath(__file__))
MAX_UPLOAD = 50 * 1024 * 1024


class RingLog(logging.Handler):
    """Keeps the last 500 server log lines in memory for the live-log panel."""

    def __init__(self):
        super().__init__()
        self.lines, self.n, self.buf_lock = collections.deque(maxlen=500), 0, threading.Lock()
        self.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d | %(levelname)-7s | %(message)s", "%H:%M:%S"))

    def emit(self, record):
        with self.buf_lock:
            self.n += 1
            self.lines.append((self.n, self.format(record)))

    def since(self, n):
        with self.buf_lock:
            return [l for l in self.lines if l[0] > n], self.n


RING = RingLog()
PLAT = None
ESTIMATOR = cl.RTTEstimator(initial=0.05)       # shared adaptive-RTO state for the dashboard's DNS client


def result(cmd, output, ok=True, ms=None):
    return {"cmd": cmd, "output": output, "ok": ok, "ms": ms}


def api_echo(b):
    text, proto = b.get("text", "hello"), b.get("proto", "tcp")
    ports = PLAT.ports
    try:
        if proto == "udp":
            r, secs, n = cl.udp_echo("127.0.0.1", ports["udp-echo"], text.encode(), float(b.get("timeout", 0.3)), int(b.get("retries", 2)))
            return result(f'python3 client.py udp-echo "{text}"', f"{r.decode(errors='replace')!r}  ({secs * 1000:.3f} ms, {n} attempt(s))", ms=secs * 1000)
        with cl.EchoClient(port=ports["echo"]) as c:
            r, rtt = c.echo(text.encode())
        return result(f'python3 client.py echo "{text}"', f"{r.decode(errors='replace')!r}  ({rtt * 1000:.3f} ms)", ms=rtt * 1000)
    except Exception as e:
        return result(f'python3 client.py {"udp-echo" if proto == "udp" else "echo"} "{text}"', f"error: {type(e).__name__}: {e}", ok=False)


def api_dns(b):
    name, qtype = b.get("name", "web.campus.lab").strip(), b.get("type", "A")
    adaptive = bool(b.get("adaptive"))
    cmd = f"python3 client.py dns {name} {qtype}" + (" --adaptive" if adaptive else "")
    try:
        r, secs, n = cl.dns_query("127.0.0.1", PLAT.ports["dns"], name, qtype, float(b.get("timeout", 0.3)), int(b.get("retries", 2)),
                                  ESTIMATOR if adaptive else None)
        rto = f" rto={ESTIMATOR.rto() * 1000:.1f} ms" if adaptive else ""
        out = [f";; rcode={dw.RCODE_NAMES[r.rcode]} aa={int(r.aa)} tc={int(r.tc)} time={secs * 1000:.3f} ms attempts={n}{rto}"]
        out += [f"{a.name}\t{a.ttl}\tIN\t{dw.QTYPE_NAMES.get(a.rtype, a.rtype)}\t{a.value}" for a in r.answers]
        return result(cmd, "\n".join(out), ms=secs * 1000)
    except Exception as e:
        return result(cmd, f"error: {type(e).__name__}: {e}  (all attempts lost or invalid name)", ok=False)


def api_list():
    try:
        with cl.FileClient(port=PLAT.ports["file"]) as c:
            items = c.list()
        out = "\n".join(f"{i['size']:>12}  {i['name']}" for i in items) or "(no files)"
        return result("python3 client.py file list", out), items
    except Exception as e:
        return result("python3 client.py file list", f"error: {e}", ok=False), []


def api_put(name, data):
    cmd = f"python3 client.py file put {name}"
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "upload.bin")
        with open(path, "wb") as f:
            f.write(data)
        try:
            with cl.FileClient(port=PLAT.ports["file"], timeout=60) as c:
                n, s = c.put(path, name)
            return result(cmd, f"uploaded {n} bytes in {s:.3f} s ({cl.rate(n, s)}), SHA-256 verified by server", ms=s * 1000)
        except Exception as e:
            return result(cmd, f"error: {type(e).__name__}: {e}", ok=False)


def api_get(name):
    with tempfile.TemporaryDirectory() as d:
        dest = os.path.join(d, "dl.bin")
        try:
            with cl.FileClient(port=PLAT.ports["file"], timeout=60) as c:
                n, s = c.get(name, dest)
            with open(dest, "rb") as f:
                return result(f"python3 client.py file get {name}", f"downloaded {n} bytes in {s:.3f} s ({cl.rate(n, s)}), SHA-256 verified", ms=s * 1000), f.read()
        except Exception as e:
            return result(f"python3 client.py file get {name}", f"error: {type(e).__name__}: {e}", ok=False), None


def api_load(b):
    clients, reqs, size = max(1, min(int(b.get("clients", 20)), 200)), max(1, min(int(b.get("requests", 50)), 200)), 1024
    lats, errs, lock = [], [0], threading.Lock()
    payload = os.urandom(size)

    def worker():
        loc, e = [], 0
        try:
            with cl.EchoClient(port=PLAT.ports["echo"], timeout=15) as c:
                for _ in range(reqs):
                    loc.append(c.echo(payload)[1])
        except Exception:
            e += 1
        with lock:
            lats.extend(loc)
            errs[0] += e

    ts = [threading.Thread(target=worker) for _ in range(clients)]
    t0 = time.perf_counter()
    for t in ts:
        t.start()
        time.sleep(0.002)
    for t in ts:
        t.join()
    wall = time.perf_counter() - t0
    out = [f"clients={clients}  requests/client={reqs}  payload={size} B",
           f"total requests : {len(lats)}   failed clients: {errs[0]}",
           f"wall time      : {wall:.3f} s   throughput: {len(lats) / wall:,.0f} req/s"]
    if lats:
        ms = sorted(x * 1000 for x in lats)
        out.append(f"latency (ms)   : p50={ms[len(ms) // 2]:.3f}  p95={ms[int(len(ms) * .95)]:.3f}  p99={ms[int(len(ms) * .99)]:.3f}  max={ms[-1]:.3f}")
    return result(f"load test: {clients} concurrent clients x {reqs} echo requests", "\n".join(out), ok=errs[0] == 0)


def stats():
    s = PLAT.stats.snapshot()
    c = s["counters"]
    return {"uptime": s["uptime_s"], "threads": s["threads"], "rss_mb": s["maxrss_bytes"] / 2**20,
            "cpu_s": s["cpu_user_s"] + s["cpu_sys_s"], "active": s["active"], "peak": s["peak_active"], "counters": c,
            "loss": PLAT.dns.loss, "ports": PLAT.ports}


class H(BaseHTTPRequestHandler):
    server_version = "CNDashboard/1.0"

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(ROOT, "web", "index.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if u.path.startswith("/figures/"):
            p = os.path.join(ROOT, "report", "figures", os.path.basename(u.path))
            if os.path.isfile(p):
                with open(p, "rb") as f:
                    return self.send(200, f.read(), "image/png")
            return self.send(404, {"error": "no such figure"})
        if u.path == "/api/stats":
            return self.send(200, stats())
        if u.path == "/api/logs":
            lines, n = RING.since(int(q.get("since", ["0"])[0]))
            return self.send(200, {"lines": [l[1] for l in lines], "n": n})
        if u.path == "/api/file/list":
            r, items = api_list()
            return self.send(200, {**r, "items": items})
        if u.path == "/api/file/get":
            name = q.get("name", [""])[0]
            r, data = api_get(name)
            if data is None:
                return self.send(404, r)
            return self.send(200, data, "application/octet-stream",
                             {"Content-Disposition": f'attachment; filename="{name}"', "X-Result": urllib.parse.quote(json.dumps(r))})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0))
        if n > MAX_UPLOAD:
            return self.send(413, result("upload", "error: file larger than the 50 MB web limit", ok=False))
        raw = self.rfile.read(n) if n else b""
        if u.path == "/api/file/put":
            name = urllib.parse.parse_qs(u.query).get("name", [""])[0]
            return self.send(200, api_put(name, raw))
        try:
            b = json.loads(raw or b"{}")
        except ValueError:
            return self.send(400, {"error": "bad json"})
        if u.path == "/api/echo":
            return self.send(200, api_echo(b))
        if u.path == "/api/dns":
            return self.send(200, api_dns(b))
        if u.path == "/api/load":
            return self.send(200, api_load(b))
        if u.path == "/api/loss":
            p = max(0.0, min(float(b.get("p", 0)), 0.9))
            PLAT.uecho.loss = PLAT.dns.loss = p
            sv.log.info("event=config simulated_loss=%.2f (set from dashboard)", p)
            return self.send(200, result(f"server --loss {p:.2f}", f"simulated per-datagram loss set to {p * 100:.0f}% on UDP echo and DNS"))
        self.send(404, {"error": "not found"})


def main():
    global PLAT
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8080, help="dashboard port")
    ap.add_argument("--auto-ports", action="store_true", help="use free ports instead of 9001/9002/9003/9053 (if a server is already running)")
    ap.add_argument("--root", default=os.path.join(ROOT, "storage"))
    a = ap.parse_args()
    sv.log.setLevel(logging.INFO)
    sv.log.addHandler(RING)
    sv.raise_fd_limit()
    extra = dict(echo_port=0, file_port=0, udp_echo_port=0, dns_port=0) if a.auto_ports else {}
    PLAT = sv.Platform(**extra, root=a.root, zone=os.path.join(ROOT, "zone.json"), idle_timeout=60).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    print(f"Dashboard: http://127.0.0.1:{a.port}   (platform ports: {json.dumps(PLAT.ports)})   Ctrl+C to stop", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        PLAT.stop()


if __name__ == "__main__":
    main()
