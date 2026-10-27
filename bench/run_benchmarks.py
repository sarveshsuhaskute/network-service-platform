#!/usr/bin/env python3
"""Performance / reliability evaluation. Writes results/benchmarks.json.

Usage: python3 bench/run_benchmarks.py [--quick]
"""
import argparse
import json
import multiprocessing as mp
import os
import platform
import resource
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
import client as cl  # noqa: E402

PY = sys.executable


def raise_limit():
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(8192, hard if hard != resource.RLIM_INFINITY else 8192), hard))
    except (ValueError, OSError):
        pass


# ----------------------------------------------------------------------------- helpers
def pct(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))]


def lat_stats(v):
    ms = [x * 1000 for x in v]
    return {"n": len(ms), "mean_ms": statistics.fmean(ms), "p50_ms": pct(ms, 50), "p95_ms": pct(ms, 95),
            "p99_ms": pct(ms, 99), "min_ms": min(ms), "max_ms": max(ms),
            "stdev_ms": statistics.pstdev(ms)}


class Server:
    def __init__(self, tag="bench", client_kwargs=None, **opts):
        self.client_kwargs = client_kwargs or {}
        self.root = tempfile.mkdtemp(prefix="cn-storage-")
        args = [PY, os.path.join(ROOT, "server.py"), "--host", "127.0.0.1", "--echo-port", "0", "--file-port", "0",
                "--udp-echo-port", "0", "--dns-port", "0", "--root", self.root, "--zone", os.path.join(ROOT, "zone.json"),
                "--log-file", os.path.join(ROOT, "logs", f"{tag}.log"), "--stats-interval", "0",
                "--max-clients", str(opts.pop("max_clients", 4000))]
        for k, v in opts.items():
            args += [f"--{k.replace('_', '-')}", str(v)]
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, cwd=ROOT)
        line = self.proc.stdout.readline()
        assert line.startswith("READY"), line
        self.ports = json.loads(line[6:])

    def stats(self):
        with cl.FileClient(port=self.ports["file"], **self.client_kwargs) as c:
            return c.server_stats()

    def rss_mb(self):
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(self.proc.pid)], capture_output=True, text=True).stdout
        return int(out.strip() or 0) / 1024

    def measure(self, fn):
        """Run fn() while sampling server RSS; return (fn result, resource dict)."""
        s0, t0 = self.stats(), time.perf_counter()
        samples, stop = [], threading.Event()

        def sample():
            while not stop.is_set():
                samples.append(self.rss_mb())
                stop.wait(0.1)

        th = threading.Thread(target=sample, daemon=True)
        th.start()
        res = fn()
        wall = time.perf_counter() - t0
        stop.set(); th.join()
        s1 = self.stats()
        cpu = (s1["cpu_user_s"] + s1["cpu_sys_s"]) - (s0["cpu_user_s"] + s0["cpu_sys_s"])
        return res, {"cpu_pct": 100 * cpu / wall, "cpu_s": cpu, "wall_s": wall,
                     "rss_mean_mb": statistics.fmean(samples) if samples else 0, "rss_peak_mb": max(samples or [0]),
                     "threads": s1["threads"]}

    def stop(self):
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


# ----------------------------------------------------------------------------- load generators
def _client_threads(kind, port, n_clients, n_req, size, start_at, extra):
    lats, errs, spans = [], [0], []
    lock = threading.Lock()

    def run():
        local, e = [], 0
        try:
            if kind == "echo":
                c = cl.EchoClient(port=port, timeout=30)
            payload = os.urandom(size)
            while time.time() < start_at:
                time.sleep(0.001)
            t_first = time.time()
            for i in range(n_req):
                try:
                    if kind == "echo":
                        local.append(c.echo(payload)[1])
                    elif kind == "dns":
                        _, secs, _ = cl.dns_query("127.0.0.1", port, "web.campus.lab", timeout=1.0, retries=1)
                        local.append(secs)
                    elif kind == "udp-echo":
                        _, secs, _ = cl.udp_echo("127.0.0.1", port, payload, timeout=1.0, retries=1)
                        local.append(secs)
                except Exception:
                    e += 1
            t_last = time.time()
            if kind == "echo":
                c.close()
        except Exception:
            e += n_req
            t_first = t_last = time.time()
        with lock:
            lats.extend(local); errs[0] += e; spans.append((t_first, t_last))

    ts = [threading.Thread(target=run) for _ in range(n_clients)]
    for t in ts:                         # ramp up: 2 ms between connects, like real arrivals
        t.start()
        time.sleep(0.002)
    [t.join() for t in ts]
    return lats, errs[0], spans


def _proc_entry(args, q):
    q.put(_client_threads(*args))


def load(kind, port, n_clients, n_req, size=1024, procs=4):
    """Spread n_clients threads over several processes (avoids a single-GIL client bottleneck)."""
    procs = max(1, min(procs, n_clients))
    shares = [n_clients // procs + (1 if i < n_clients % procs else 0) for i in range(procs)]
    start_at = time.time() + 1.5 + 0.003 * n_clients
    ctx, q = mp.get_context("spawn"), mp.get_context("spawn").Queue()
    ps = [ctx.Process(target=_proc_entry, args=((kind, port, s, n_req, size, start_at, None), q)) for s in shares]
    [p.start() for p in ps]
    lats, errs, spans = [], 0, []
    for _ in ps:
        l, e, sp = q.get()
        lats += l; errs += e; spans += sp
    [p.join() for p in ps]
    wall = max(s[1] for s in spans) - min(s[0] for s in spans)
    out = {"clients": n_clients, "requests": len(lats) + errs, "errors": errs, "wall_s": wall,
           "req_per_s": len(lats) / wall if wall > 0 else 0}
    if lats:
        out.update(lat_stats(lats))
    return out


# ----------------------------------------------------------------------------- experiments
def e1_latency(srv, quick):
    res = {"tcp_persistent": [], "udp": [], "tcp_new_conn": None}
    sizes = [16, 256, 1024, 4096, 16384, 65536, 262144, 1048576]
    with cl.EchoClient(port=srv.ports["echo"]) as c:
        for _ in range(3000):                                   # warm-up: CPU clocks, caches, allocator
            c.echo(b"w" * 256)
        for size in sizes:
            n = (50 if quick else 100) if size >= 262144 else (200 if quick else 1000)
            payload = os.urandom(size)
            for _ in range(20):
                c.echo(payload)
            lat = [c.echo(payload)[1] for _ in range(n)]
            st = lat_stats(lat); st["size"] = size
            st["goodput_MBps"] = size / statistics.fmean(lat) / 1e6
            res["tcp_persistent"].append(st)
    for size in [16, 256, 1024, 1400, 8192]:
        payload = os.urandom(size)
        for _ in range(20):
            cl.udp_echo("127.0.0.1", srv.ports["udp-echo"], payload)
        lat = [cl.udp_echo("127.0.0.1", srv.ports["udp-echo"], payload, timeout=1)[1] for _ in range(200 if quick else 1000)]
        st = lat_stats(lat); st["size"] = size
        res["udp"].append(st)
    lat = []
    for _ in range(200 if quick else 500):
        t0 = time.perf_counter()
        with cl.EchoClient(port=srv.ports["echo"]) as c:
            c.echo(b"x" * 64)
        lat.append(time.perf_counter() - t0)
    res["tcp_new_conn"] = lat_stats(lat)
    return res


def e2_concurrency(srv, quick):
    rows = []
    for c in ([1, 10, 50, 100] if quick else [1, 2, 5, 10, 25, 50, 100, 200, 400]):
        n_req = max(50, min(500, 20000 // c)) if not quick else 100
        r, rs = srv.measure(lambda: load("echo", srv.ports["echo"], c, n_req, 1024))
        r.update(rs)
        rows.append(r)
        print(f"  echo clients={c:<4} {r['req_per_s']:.0f} req/s p95={r.get('p95_ms', 0):.2f} ms err={r['errors']} cpu={r['cpu_pct']:.0f}%", flush=True)
    return rows


def e3_files(srv, quick, tag=""):
    sizes = [1, 10, 50] if quick else [1, 10, 50, 100]
    reps = 2 if quick else 3
    rows = []
    d = tempfile.mkdtemp(prefix="cn-client-")
    with cl.FileClient(port=srv.ports["file"], timeout=120) as c:
        for mb in sizes:
            src = os.path.join(d, f"f{mb}.bin")
            with open(src, "wb") as f:
                for _ in range(mb):
                    f.write(os.urandom(1024 * 1024))
            up, down = [], []
            for _ in range(reps):
                n, s = c.put(src, f"f{mb}.bin"); up.append(n / s / 1e6)
                n, s = c.get(f"f{mb}.bin", os.path.join(d, "dl.bin")); down.append(n / s / 1e6)
            rows.append({"size_mb": mb, "upload_MBps": statistics.fmean(up), "download_MBps": statistics.fmean(down),
                         "upload_min": min(up), "upload_max": max(up), "download_min": min(down), "download_max": max(down)})
            print(f"  file {tag}{mb} MB up={rows[-1]['upload_MBps']:.0f} MB/s down={rows[-1]['download_MBps']:.0f} MB/s", flush=True)
    return rows


def _dl_worker(port, name, dest, q):
    with cl.FileClient(port=port, timeout=120) as c:
        n, s = c.get(name, dest)
    q.put((n, s))


def e3b_file_concurrent(srv, quick):
    d = tempfile.mkdtemp(prefix="cn-client-")
    src = os.path.join(d, "shared.bin")
    mb = 20
    with open(src, "wb") as f:
        for _ in range(mb):
            f.write(os.urandom(1024 * 1024))
    with cl.FileClient(port=srv.ports["file"], timeout=120) as c:
        c.put(src, "shared.bin")
    rows = []
    for n in [1, 2, 4, 8, 16]:
        def run():
            ctx = mp.get_context("spawn")
            q = ctx.Queue()
            ps = [ctx.Process(target=_dl_worker, args=(srv.ports["file"], "shared.bin", os.path.join(d, f"o{i}.bin"), q)) for i in range(n)]
            t0 = time.perf_counter()
            [p.start() for p in ps]
            got = [q.get() for _ in ps]
            [p.join() for p in ps]
            return time.perf_counter() - t0, got
        (wall, got), rs = srv.measure(run)
        total = sum(g[0] for g in got)
        # wall includes process spawn; use per-client transfer times for the rate
        per = [g[0] / g[1] / 1e6 for g in got]
        rows.append({"clients": n, "aggregate_MBps": total / max(g[1] for g in got) / 1e6,
                     "per_client_MBps": statistics.fmean(per), **rs})
        print(f"  file concurrent clients={n} aggregate={rows[-1]['aggregate_MBps']:.0f} MB/s cpu={rs['cpu_pct']:.0f}%", flush=True)
    return rows


def e4_dns(srv, quick):
    out = {"latency": {}, "qps": []}
    names = {"A (2 records)": ("web.campus.lab", "A"), "CNAME chain": ("alias1.campus.lab", "A"),
             "TXT": ("campus.lab", "TXT"), "NXDOMAIN": ("missing.campus.lab", "A")}
    for label, (n, t) in names.items():
        for _ in range(30):
            cl.dns_query("127.0.0.1", srv.ports["dns"], n, t)
        lat = [cl.dns_query("127.0.0.1", srv.ports["dns"], n, t, timeout=1)[1] for _ in range(500 if quick else 3000)]
        out["latency"][label] = lat_stats(lat)
    for c in ([1, 8, 32] if quick else [1, 2, 4, 8, 16, 32, 64]):
        r, rs = srv.measure(lambda: load("dns", srv.ports["dns"], c, 200 if quick else 500, procs=4))
        r.update(rs)
        out["qps"].append(r)
        print(f"  dns clients={c:<3} {r['req_per_s']:.0f} q/s p95={r.get('p95_ms', 0):.2f} ms err={r['errors']}", flush=True)
    return out


def e5_loss(quick):
    losses = [0.0, 0.05, 0.10, 0.20, 0.30, 0.40]
    retry_set = [0, 1, 2, 4]
    N = 120 if quick else 1000
    rows = []
    for p in losses:
        srv = Server(tag="bench-loss", loss=p, seed=11)
        try:
            # raw datagram loss (no retransmission)
            sent = 600 if quick else 2000
            got = 0
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(0.02)
            for i in range(sent):
                s.sendto(i.to_bytes(4, "big"), ("127.0.0.1", srv.ports["udp-echo"]))
                try:
                    s.recvfrom(100); got += 1
                except socket.timeout:
                    pass
            s.close()
            row = {"loss_per_datagram": p, "udp_sent": sent, "udp_received": got,
                   "udp_observed_loss": 1 - got / sent, "udp_theory_loss": 1 - (1 - p) ** 2, "dns": []}
            for r in retry_set:
                ok, times, attempts = 0, [], []
                for _ in range(N):
                    try:
                        _, secs, a = cl.dns_query("127.0.0.1", srv.ports["dns"], "web.campus.lab", timeout=0.05, retries=r)
                        ok += 1; times.append(secs); attempts.append(a)
                    except socket.timeout:
                        attempts.append(r + 1)
                per_attempt = (1 - p) ** 2
                row["dns"].append({"retries": r, "queries": N, "success": ok / N,
                                   "theory_success": 1 - (1 - per_attempt) ** (r + 1),
                                   "mean_attempts": statistics.fmean(attempts),
                                   "mean_ms_success": statistics.fmean(times) * 1000 if times else None,
                                   "p95_ms_success": pct([t * 1000 for t in times], 95) if times else None})
            # TCP echo against the same server (TCP reliability is provided by the kernel)
            tcp_ok = 0
            with cl.EchoClient(port=srv.ports["echo"]) as c:
                for _ in range(N):
                    tcp_ok += c.echo(b"z" * 64)[0] == b"z" * 64
            row["tcp_echo_success"] = tcp_ok / N
            st = srv.stats()["counters"]
            row["server_dropped_in"] = st.get("dns.dropped_in", 0) + st.get("udp-echo.dropped_in", 0)
            row["server_dropped_out"] = st.get("dns.dropped_out", 0) + st.get("udp-echo.dropped_out", 0)
            rows.append(row)
            print(f"  loss={p:.2f}: raw UDP loss obs={row['udp_observed_loss']:.3f} theory={row['udp_theory_loss']:.3f}; "
                  + ", ".join(f"r{d['retries']}={d['success']:.3f}" for d in row["dns"]), flush=True)
        finally:
            srv.stop()
    return rows


def e9_adaptive_rto(quick):
    """Fixed 50 ms retransmission timeout vs RFC 6298-style adaptive RTO (both: 4 retries, same seeded loss)."""
    rows = []
    N = 150 if quick else 500
    for p in [0.0, 0.1, 0.2, 0.3, 0.4]:
        row = {"loss_per_datagram": p}
        for mode in ("fixed", "adaptive"):
            srv = Server(tag="bench-rto", loss=p, seed=23)
            try:
                est = cl.RTTEstimator(initial=0.05) if mode == "adaptive" else None
                ok, times, attempts, rtos = 0, [], [], []
                t0 = time.perf_counter()
                for _ in range(N):
                    try:
                        _, secs, a = cl.dns_query("127.0.0.1", srv.ports["dns"], "web.campus.lab", timeout=0.05,
                                                  retries=4, estimator=est)
                        ok += 1; times.append(secs * 1000); attempts.append(a)
                    except socket.timeout:
                        attempts.append(5)
                    if est:
                        rtos.append(est.rto() * 1000)
                row[mode] = {"success": ok / N, "mean_ms": statistics.fmean(times) if times else None,
                             "p95_ms": pct(times, 95) if times else None, "mean_attempts": statistics.fmean(attempts),
                             "total_s": time.perf_counter() - t0, "final_rto_ms": rtos[-1] if rtos else 50.0,
                             "median_rto_ms": pct(rtos, 50) if rtos else 50.0}
            finally:
                srv.stop()
        rows.append(row)
        print(f"  loss={p:.1f} fixed: succ={row['fixed']['success']:.3f} mean={row['fixed']['mean_ms'] or 0:.2f} ms | "
              f"adaptive: succ={row['adaptive']['success']:.3f} mean={row['adaptive']['mean_ms'] or 0:.2f} ms "
              f"rto~{row['adaptive']['median_rto_ms']:.2f} ms", flush=True)
    return {"queries": N, "rows": rows}


def e10_workers(quick):
    """Pre-forked worker processes vs the single-process server (same ports, kernel distributes connections)."""
    rows = []
    for w in [1, 2, 4, 6]:
        srv = Server(tag="bench-workers", workers=w)
        try:
            time.sleep(0.5)
            row = {"workers": w}
            for c in ([50] if quick else [50, 200]):
                r = load("echo", srv.ports["echo"], c, 100 if quick else 300, 1024, procs=6)
                row[f"echo_{c}"] = r
            row["dns_32"] = load("dns", srv.ports["dns"], 32, 100 if quick else 400, procs=6)
            rows.append(row)
            print(f"  workers={w}: " + "  ".join(f"{k}: {v['req_per_s']:,.0f}/s p95={v['p95_ms']:.2f}ms err={v['errors']}"
                                                 for k, v in row.items() if k != "workers"), flush=True)
        finally:
            srv.stop()
    return rows


def e11_event_loop(quick):
    """Thread-per-connection echo vs a single selectors event loop: memory per connection, threads, throughput."""
    out = {}
    for io in ("threads", "selectors"):
        res = {"idle": [], "load": []}
        for n in ([0, 500] if quick else [0, 500, 1000, 2000]):
            srv = Server(tag="bench-io", echo_io=io)
            try:
                time.sleep(0.3)
                base = srv.rss_mb()
                socks = []
                for i in range(n):
                    socks.append(socket.create_connection(("127.0.0.1", srv.ports["echo"])))
                    if i % 10 == 9:
                        time.sleep(0.003)
                time.sleep(0.8)
                st = srv.stats()
                with cl.EchoClient(port=srv.ports["echo"]) as c:
                    lat = lat_stats([c.echo(b"p" * 64)[1] for _ in range(300)])
                rss = srv.rss_mb()
                res["idle"].append({"idle_connections": n, "rss_mb": rss, "threads": st["threads"],
                                    "kb_per_conn": (rss - base) * 1024 / n if n else 0, "echo_p50_ms": lat["p50_ms"]})
                for s_ in socks:
                    s_.close()
            finally:
                srv.stop()
        srv = Server(tag="bench-io", echo_io=io)
        try:
            for c in ([1, 50] if quick else [1, 10, 50, 200]):
                r, rs = srv.measure(lambda: load("echo", srv.ports["echo"], c, 100 if quick else 300, 1024))
                r.update(rs)
                res["load"].append(r)
        finally:
            srv.stop()
        out[io] = res
        print(f"  {io}: idle " + ", ".join(f"{x['idle_connections']}->{x['rss_mb']:.0f}MB/{x['threads']}thr" for x in res["idle"])
              + " | load " + ", ".join(f"{x['clients']}c {x['req_per_s']:,.0f}/s p95={x['p95_ms']:.2f} cpu={x['cpu_pct']:.0f}%" for x in res["load"]),
              flush=True)
    return out


def e12_tls(quick):
    """Cost of TLS on the file service: connection set-up latency and bulk throughput."""
    certdir = tempfile.mkdtemp(prefix="cn-cert-")
    subprocess.run([os.path.join(ROOT, "tools", "make_cert.sh"), certdir], check=True, capture_output=True)
    cert, key = os.path.join(certdir, "server.crt"), os.path.join(certdir, "server.key")
    d = tempfile.mkdtemp(prefix="cn-client-")
    src = os.path.join(d, "f.bin")
    mb = 20 if quick else 100
    with open(src, "wb") as f:
        for _ in range(mb):
            f.write(os.urandom(1024 * 1024))
    out = {}
    for mode in ("plain", "tls"):
        ck = {"tls": True, "cafile": cert} if mode == "tls" else {}
        opts = {"tls_cert": cert, "tls_key": key} if mode == "tls" else {}
        srv = Server(tag="bench-tls", client_kwargs=ck, digest_cache="on", **opts)
        try:
            setup = []
            for _ in range(50 if quick else 300):
                t0 = time.perf_counter()
                with cl.FileClient(port=srv.ports["file"], **ck) as c:
                    c.ping()
                setup.append(time.perf_counter() - t0)
            up, down = [], []

            def transfer():
                with cl.FileClient(port=srv.ports["file"], timeout=120, **ck) as c:
                    for _ in range(3):
                        n, t = c.put(src, "f.bin"); up.append(n / t / 1e6)
                        n, t = c.get("f.bin", os.path.join(d, "dl.bin")); down.append(n / t / 1e6)
            _, rs = srv.measure(transfer)
            out[mode] = {"connect_ping": lat_stats(setup), "upload_MBps": statistics.fmean(up),
                         "download_MBps": statistics.fmean(down), "cpu_pct": rs["cpu_pct"], "size_mb": mb}
            print(f"  {mode}: connect+PING p50={out[mode]['connect_ping']['p50_ms']:.3f} ms  up={out[mode]['upload_MBps']:.0f} MB/s "
                  f"down={out[mode]['download_MBps']:.0f} MB/s cpu={rs['cpu_pct']:.0f}%", flush=True)
        finally:
            srv.stop()
    return out


def e13_delay_rto(quick):
    """20 +/- 5 ms emulated RTT: fixed 50 ms, fixed 20 ms (too aggressive) and adaptive RTO, 4 retries."""
    N = 80 if quick else 200
    rows = []
    for p in [0.0, 0.1, 0.2, 0.3]:
        row = {"loss_per_datagram": p}
        for mode in ("fixed50", "fixed20", "adaptive"):
            srv = Server(tag="bench-delay", loss=p, seed=31, delay_ms=20, jitter_ms=5)
            try:
                est = cl.RTTEstimator(initial=0.2) if mode == "adaptive" else None
                to = {"fixed50": 0.05, "fixed20": 0.02, "adaptive": 0.2}[mode]
                before = srv.stats()["counters"].get("dns.datagrams_in", 0)
                ok, times = 0, []
                for _ in range(N):
                    try:
                        _, secs, _a = cl.dns_query("127.0.0.1", srv.ports["dns"], "web.campus.lab", timeout=to,
                                                   retries=4, estimator=est)
                        ok += 1; times.append(secs * 1000)
                    except socket.timeout:
                        pass
                sent = srv.stats()["counters"].get("dns.datagrams_in", 0) - before
                row[mode] = {"success": ok / N, "mean_ms": statistics.fmean(times) if times else None,
                             "p95_ms": pct(times, 95) if times else None, "datagrams_per_query": sent / N,
                             "final_rto_ms": est.rto() * 1000 if est else to * 1000}
            finally:
                srv.stop()
        rows.append(row)
        print(f"  loss={p:.1f} " + " | ".join(f"{m}: succ={row[m]['success']:.3f} mean={row[m]['mean_ms']:.1f}ms "
                                                f"dgram/q={row[m]['datagrams_per_query']:.2f} rto={row[m]['final_rto_ms']:.1f}"
                                                for m in ("fixed50", "fixed20", "adaptive")), flush=True)
    return {"queries": N, "delay_ms": 20, "jitter_ms": 5, "rows": rows}


def e6_idle_connections(quick):
    rows = []
    for n in ([0, 100, 400] if quick else [0, 100, 250, 500, 1000, 2000]):
        srv = Server(tag="bench-idle")
        try:
            time.sleep(0.3)
            base = srv.rss_mb()
            socks = []
            t0 = time.perf_counter()
            for i in range(n):
                socks.append(socket.create_connection(("127.0.0.1", srv.ports["echo"])))
                if i % 10 == 9:
                    time.sleep(0.003)             # ramp: 10 connects per 3 ms
            time.sleep(0.8)
            conn_time = time.perf_counter() - t0
            st = srv.stats()
            with cl.EchoClient(port=srv.ports["echo"]) as c:
                lat = lat_stats([c.echo(b"p" * 64)[1] for _ in range(300)])
            rows.append({"idle_connections": n, "rss_mb": srv.rss_mb(), "rss_base_mb": base, "threads": st["threads"],
                         "kb_per_conn": (srv.rss_mb() - base) * 1024 / n if n else 0, "echo_p50_ms": lat["p50_ms"],
                         "echo_p95_ms": lat["p95_ms"], "connect_all_s": conn_time})
            print(f"  idle={n:<5} rss={rows[-1]['rss_mb']:.1f} MB threads={st['threads']} echo p95={lat['p95_ms']:.2f} ms", flush=True)
            for s in socks:
                s.close()
        finally:
            srv.stop()
    return rows


def e6b_burst_limit():
    """How many connections can be opened back-to-back (no pause) before the kernel accept queue overflows?"""
    rows = []
    for trial in range(3):
        srv = Server(tag="bench-burst")
        socks, err = [], None
        try:
            for _ in range(3000):
                try:
                    socks.append(socket.create_connection(("127.0.0.1", srv.ports["echo"])))
                except OSError as e:
                    err = type(e).__name__
                    break
            rows.append({"trial": trial + 1, "connections_before_failure": len(socks), "error": err})
            for s in socks:
                s.close()
        finally:
            srv.stop()
    print("  burst limit:", [r["connections_before_failure"] for r in rows], flush=True)
    return {"trials": rows, "somaxconn": subprocess.run(["sysctl", "-n", "kern.ipc.somaxconn"], capture_output=True, text=True).stdout.strip()}


def e7_failure_detection():
    out = {}
    srv = Server(tag="bench-fail")
    try:
        c = cl.EchoClient(port=srv.ports["echo"], timeout=1.0)
        c.echo(b"warm")
        os.kill(srv.proc.pid, signal.SIGSTOP)                 # server hangs: no FIN, no RST
        t0 = time.perf_counter()
        try:
            c.echo(b"x")
        except Exception as e:
            out["hung_server"] = {"exception": type(e).__name__, "detected_after_s": time.perf_counter() - t0, "client_timeout_s": 1.0}
        os.kill(srv.proc.pid, signal.SIGCONT)
        c.close()
        c = cl.EchoClient(port=srv.ports["echo"], timeout=1.0)
        c.echo(b"warm")
        os.kill(srv.proc.pid, signal.SIGKILL)                 # server crashes: kernel sends FIN/RST
        srv.proc.wait()
        t0 = time.perf_counter()
        try:
            c.echo(b"x")
        except Exception as e:
            out["crashed_server"] = {"exception": type(e).__name__, "detected_after_s": time.perf_counter() - t0}
        t0 = time.perf_counter()
        try:
            cl.tcp_connect("127.0.0.1", srv.ports["echo"], timeout=0.5, retries=2, backoff=0.2)
        except ConnectionError:
            out["connect_refused"] = {"exception": "ConnectionError", "detected_after_s": time.perf_counter() - t0, "attempts": 3}
        t0 = time.perf_counter()
        try:
            cl.udp_echo("127.0.0.1", srv.ports["udp-echo"], b"x", timeout=0.2, retries=2)
        except socket.timeout:
            out["udp_dead_server"] = {"exception": "timeout", "detected_after_s": time.perf_counter() - t0, "attempts": 3,
                                      "note": "UDP gives no error: only the retry budget bounds the wait"}
    finally:
        srv.stop()
    srv = Server(tag="bench-idle-timeout", idle_timeout=1.0)
    try:
        s = socket.create_connection(("127.0.0.1", srv.ports["echo"]))
        t0 = time.perf_counter()
        s.settimeout(5)
        data = s.recv(1)
        out["server_idle_drop"] = {"closed_after_s": time.perf_counter() - t0, "configured_idle_timeout_s": 1.0, "eof": data == b""}
        s.close()
    finally:
        srv.stop()
    print("  " + json.dumps({k: round(list(v.values())[1], 3) if isinstance(list(v.values())[1], float) else v for k, v in out.items()})[:300], flush=True)
    return out


def machine():
    def sysctl(k):
        try:
            return subprocess.run(["sysctl", "-n", k], capture_output=True, text=True).stdout.strip()
        except OSError:
            return ""
    return {"platform": platform.platform(), "python": platform.python_version(), "cpu": sysctl("machdep.cpu.brand_string") or platform.processor(),
            "cores": os.cpu_count(), "ram_gb": round(int(sysctl("hw.memsize") or 0) / 2**30, 1),
            "date": time.strftime("%Y-%m-%d %H:%M")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--only", help="comma list of experiments, e.g. e1,e5")
    a = ap.parse_args()
    raise_limit()
    want = set(a.only.split(",")) if a.only else None
    path = os.path.join(ROOT, "results", "benchmarks.json")
    results = json.load(open(path)) if (want and os.path.exists(path)) else {}
    results["machine"] = machine()
    results["quick"] = a.quick

    def run(key, label, fn, server=True, **srv_opts):
        if want and key not in want:
            return
        print(f"[{key}] {label}", flush=True)
        t0 = time.time()
        if server:
            srv = Server(tag=f"bench-{key}", **srv_opts)
            try:
                results[key] = fn(srv, a.quick)
            finally:
                srv.stop()
        else:
            results[key] = fn(a.quick) if fn.__code__.co_argcount else fn()
        print(f"    done in {time.time() - t0:.1f}s", flush=True)
        json.dump(results, open(path, "w"), indent=2)

    def sendfile_runs_named(label):
        def f(srv, quick):
            return {"files": e3_files(srv, quick, label + " "), "concurrent": e3b_file_concurrent(srv, quick)}
        return f

    def sendfile_runs(srv, quick):
        return {"files": e3_files(srv, quick, "sendfile "), "concurrent": e3b_file_concurrent(srv, quick)}

    run("e1", "response time vs payload size (TCP vs UDP)", e1_latency)
    run("e2", "echo scalability vs concurrent clients", e2_concurrency)
    run("e3", "baseline file transfer (chunked send, no digest cache)", e3_files, digest_cache="off")
    run("e3b", "baseline concurrent downloads", e3b_file_concurrent, digest_cache="off")
    run("e3c", "sendfile (no digest cache)", sendfile_runs, send_mode="sendfile", digest_cache="off")
    run("e8", "digest cache (chunked send)", sendfile_runs_named("chunked"), digest_cache="on")
    run("e8b", "digest cache + sendfile", sendfile_runs_named("sendfile"), digest_cache="on", send_mode="sendfile")
    run("e4", "DNS latency and query rate", e4_dns)
    run("e5", "packet loss, retransmission and reliability", e5_loss, server=False)
    run("e9", "adaptive vs fixed retransmission timeout", e9_adaptive_rto, server=False)
    run("e10", "pre-forked worker processes", e10_workers, server=False)
    run("e11", "threads vs event loop", e11_event_loop, server=False)
    run("e12", "TLS cost", e12_tls, server=False)
    run("e13", "adaptive RTO under emulated delay", e13_delay_rto, server=False)
    run("e6", "resource use with many idle connections", e6_idle_connections, server=False)
    run("e6b", "burst connection limit", e6b_burst_limit, server=False)
    run("e7", "failure detection", e7_failure_detection, server=False)
    print("saved", path)


if __name__ == "__main__":
    mp.freeze_support()
    main()
