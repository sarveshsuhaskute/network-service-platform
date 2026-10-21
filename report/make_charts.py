#!/usr/bin/env python3
"""Build report figures from results/benchmarks.json, results/test_results.json and results/demo_output.txt."""
import json
import os
import re

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FIG = os.path.join(ROOT, "report", "figures")
os.makedirs(FIG, exist_ok=True)
R = json.load(open(os.path.join(ROOT, "results", "benchmarks.json")))

BLUE, ORANGE, GREEN, RED, GREY, PURPLE = "#2b6cb0", "#dd6b20", "#2f855a", "#c53030", "#718096", "#6b46c1"
plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
                     "grid.alpha": 0.25, "figure.dpi": 130, "savefig.bbox": "tight", "legend.frameon": False})


def save(name):
    plt.savefig(os.path.join(FIG, name))
    plt.close()


# 1. response time vs payload size
def fig_latency():
    t, u = R["e1"]["tcp_persistent"], R["e1"]["udp"]
    fig, ax = plt.subplots(1, 2, figsize=(10, 3.8))
    ax[0].plot([r["size"] for r in t], [r["p50_ms"] for r in t], "o-", color=BLUE, label="TCP echo p50")
    ax[0].plot([r["size"] for r in t], [r["p95_ms"] for r in t], "o--", color=BLUE, alpha=.55, label="TCP echo p95")
    ax[0].plot([r["size"] for r in u], [r["p50_ms"] for r in u], "s-", color=ORANGE, label="UDP echo p50")
    ax[0].plot([r["size"] for r in u], [r["p95_ms"] for r in u], "s--", color=ORANGE, alpha=.55, label="UDP echo p95")
    ax[0].set_xscale("log"); ax[0].set_yscale("log")
    ax[0].set_xlabel("payload size (bytes)"); ax[0].set_ylabel("round-trip time (ms)")
    ax[0].set_title("Response time vs payload size"); ax[0].legend(fontsize=8)
    ax[1].plot([r["size"] for r in t], [r["goodput_MBps"] for r in t], "o-", color=GREEN)
    ax[1].set_xscale("log"); ax[1].set_xlabel("payload size (bytes)"); ax[1].set_ylabel("echo goodput (MB/s)")
    ax[1].set_title("TCP echo goodput (payload / RTT)")
    save("fig_latency.png")


# 2. concurrency
def fig_concurrency():
    rows = R["e2"]
    c = [r["clients"] for r in rows]
    fig, ax = plt.subplots(1, 3, figsize=(12, 3.6))
    ax[0].plot(c, [r["req_per_s"] for r in rows], "o-", color=BLUE)
    ax[0].set_ylabel("requests / s"); ax[0].set_title("Aggregate throughput")
    ax[1].plot(c, [r["p50_ms"] for r in rows], "o-", color=GREEN, label="p50")
    ax[1].plot(c, [r["p95_ms"] for r in rows], "o-", color=ORANGE, label="p95")
    ax[1].plot(c, [r["p99_ms"] for r in rows], "o-", color=RED, label="p99")
    ax[1].set_yscale("log"); ax[1].set_ylabel("latency (ms)"); ax[1].set_title("Response time"); ax[1].legend(fontsize=8)
    ax[2].plot(c, [r["cpu_pct"] for r in rows], "o-", color=PURPLE)
    ax[2].set_ylabel("server CPU (% of one core)"); ax[2].set_title("Server CPU utilisation")
    for a in ax:
        a.set_xscale("log"); a.set_xlabel("concurrent clients")
    save("fig_concurrency.png")


# 3. file transfer
VARIANTS = [("baseline (read+send, hash pass)", BLUE), ("sendfile()", ORANGE), ("digest cache", GREEN), ("digest cache + sendfile()", PURPLE)]


def file_variants():
    return [(R["e3"], R["e3b"]), (R["e3c"]["files"], R["e3c"]["concurrent"]),
            (R["e8"]["files"], R["e8"]["concurrent"]), (R["e8b"]["files"], R["e8b"]["concurrent"])]


def fig_files():
    import numpy as np
    var = file_variants()
    fig, ax = plt.subplots(1, 3, figsize=(15, 3.8))
    fig.subplots_adjust(wspace=0.3)
    x = np.arange(len(var[0][0])); w = 0.2
    for i, ((files, _), (label, col)) in enumerate(zip(var, VARIANTS)):
        ax[0].bar(x + (i - 1.5) * w, [r["download_MBps"] for r in files], w, color=col, label=label)
    ax[0].plot(x, [r["upload_MBps"] for r in var[0][0]], "k_", ms=22, mew=2, label="upload (PUT) for reference")
    ax[0].set_xticks(x, [f"{r['size_mb']} MB" for r in var[0][0]]); ax[0].set_ylabel("download throughput (MB/s)")
    ax[0].set_title("Single-client download"); ax[0].legend(fontsize=7, loc="upper left", ncol=1)
    ax[0].set_ylim(0, 2700)
    for (_, conc), (label, col) in zip(var, VARIANTS):
        ax[1].plot([r["clients"] for r in conc], [r["aggregate_MBps"] for r in conc], "o-", color=col, label=label)
        ax[2].plot([r["clients"] for r in conc], [r["cpu_pct"] for r in conc], "o-", color=col, label=label)
    for a in ax[1:]:
        a.set_xticks([1, 2, 4, 8, 16]); a.set_xlabel("concurrent downloads (20 MB each)")
    ax[1].set_ylabel("aggregate throughput (MB/s)"); ax[1].set_title("Concurrent downloads: throughput")
    ax[2].set_ylabel("server CPU (% of one core)"); ax[2].set_title("Concurrent downloads: server CPU")
    ax[1].legend(fontsize=7)
    save("fig_files.png")


# 4. DNS
def fig_dns():
    lat, q = R["e4"]["latency"], R["e4"]["qps"]
    fig, ax = plt.subplots(1, 2, figsize=(10, 3.6))
    labels = list(lat)
    import numpy as np
    x = np.arange(len(labels)); w = 0.27
    ax[0].bar(x - w, [lat[k]["p50_ms"] for k in labels], w, color=GREEN, label="p50")
    ax[0].bar(x, [lat[k]["p95_ms"] for k in labels], w, color=ORANGE, label="p95")
    ax[0].bar(x + w, [lat[k]["p99_ms"] for k in labels], w, color=RED, label="p99")
    ax[0].set_xticks(x, labels, fontsize=8); ax[0].set_ylabel("response time (ms)"); ax[0].set_title("DNS response time by query type")
    ax[0].legend(fontsize=8)
    ax[1].plot([r["clients"] for r in q], [r["req_per_s"] for r in q], "o-", color=BLUE)
    ax1b = ax[1].twinx(); ax1b.plot([r["clients"] for r in q], [r["p95_ms"] for r in q], "s--", color=ORANGE)
    ax1b.set_ylabel("p95 latency (ms)", color=ORANGE); ax1b.grid(False); ax1b.spines["right"].set_visible(True)
    fig.subplots_adjust(wspace=0.5)
    ax[1].set_xscale("log"); ax[1].set_xlabel("concurrent resolvers"); ax[1].set_ylabel("queries / s", color=BLUE)
    ax[1].set_title("DNS query rate and p95 latency")
    save("fig_dns.png")


# 5. loss and reliability
def fig_loss():
    rows = R["e5"]
    p = [r["loss_per_datagram"] * 100 for r in rows]
    fig, ax = plt.subplots(1, 3, figsize=(13, 3.8))
    ax[0].plot(p, [r["udp_theory_loss"] * 100 for r in rows], "--", color=GREY, label="theory 1-(1-p)^2")
    ax[0].plot(p, [r["udp_observed_loss"] * 100 for r in rows], "o-", color=RED, label="measured (no retransmission)")
    ax[0].set_xlabel("simulated loss per datagram (%)"); ax[0].set_ylabel("round-trip loss (%)")
    ax[0].set_title("UDP round-trip packet loss"); ax[0].legend(fontsize=8)
    cols = {0: RED, 1: ORANGE, 2: GREEN, 4: BLUE}
    for i, r0 in enumerate([0, 1, 2, 4]):
        ax[1].plot(p, [[d for d in r["dns"] if d["retries"] == r0][0]["success"] * 100 for r in rows], "o-", color=cols[r0],
                   label=f"{r0} retries")
        ax[1].plot(p, [[d for d in r["dns"] if d["retries"] == r0][0]["theory_success"] * 100 for r in rows], ":", color=cols[r0])
    ax[1].plot(p, [r["tcp_echo_success"] * 100 for r in rows], "k-", lw=1, label="TCP echo")
    ax[1].set_xlabel("simulated loss per datagram (%)"); ax[1].set_ylabel("successful requests (%)")
    ax[1].set_title("DNS success vs retries (dotted = theory)"); ax[1].legend(fontsize=8); ax[1].set_ylim(30, 102)
    for r0 in [0, 1, 2, 4]:
        ys = [[d for d in r["dns"] if d["retries"] == r0][0]["mean_ms_success"] for r in rows]
        ax[2].plot(p, ys, "o-", color=cols[r0], label=f"{r0} retries")
    ax[2].set_xlabel("simulated loss per datagram (%)"); ax[2].set_ylabel("mean response time of successes (ms)")
    ax[2].set_title("Retransmission cost (50 ms timeout)"); ax[2].legend(fontsize=8)
    save("fig_loss.png")


# 6. resources
def fig_resources():
    e6 = R["e6"]
    fig, ax = plt.subplots(1, 3, figsize=(14, 3.8))
    fig.subplots_adjust(wspace=0.55)
    ax[0].plot([r["clients"] for r in R["e2"]], [r["rss_peak_mb"] for r in R["e2"]], "o-", color=BLUE)
    ax[0].set_xscale("log"); ax[0].set_xlabel("active echo clients"); ax[0].set_ylabel("server RSS (MB)"); ax[0].set_title("Memory under load")
    n = [r["idle_connections"] for r in e6]
    ax[1].plot(n, [r["rss_mb"] for r in e6], "o-", color=GREEN)
    ax[1].set_xlabel("open idle connections"); ax[1].set_ylabel("server RSS (MB)"); ax[1].set_title("Memory vs idle connections")
    ax2 = ax[1].twinx(); ax2.plot(n, [r["threads"] for r in e6], "s--", color=GREY); ax2.set_ylabel("server threads", color=GREY)
    ax2.grid(False); ax2.spines["right"].set_visible(True)
    ax[2].plot(n, [r["echo_p95_ms"] for r in e6], "o-", color=ORANGE, label="p95")
    ax[2].plot(n, [r["echo_p50_ms"] for r in e6], "o-", color=GREEN, label="p50")
    ax[2].set_xlabel("open idle connections"); ax[2].set_ylabel("echo latency of one active client (ms)")
    ax[2].set_title("Latency of one active client"); ax[2].legend(fontsize=8)
    save("fig_resources.png")


# 7. architecture diagram
def fig_architecture():
    fig, ax = plt.subplots(figsize=(11, 6))
    ax.set_xlim(0, 11); ax.set_ylim(0, 6.4); ax.axis("off"); ax.grid(False)

    def box(x, y, w, h, text, fc, ec="#2d3748", fs=9, bold=False):
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.08", fc=fc, ec=ec, lw=1.1))
        ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=fs, fontweight="bold" if bold else "normal")

    def arrow(a, b, text="", both=True, color="#4a5568"):
        ax.add_patch(FancyArrowPatch(a, b, arrowstyle="<->" if both else "->", mutation_scale=11, color=color, lw=1.2))
        if text:
            ax.text((a[0] + b[0]) / 2, (a[1] + b[1]) / 2 + 0.12, text, ha="center", fontsize=7.5, color=color)

    ax.text(1.2, 6.05, "CLIENTS", fontsize=10, fontweight="bold", ha="center")
    for i, (n, c) in enumerate([("Echo client", "#ebf4ff"), ("File client", "#ebf4ff"), ("UDP / DNS client\n(timeout + retransmit)", "#fffaf0")]):
        box(0.2, 4.55 - i * 1.55, 2.0, 1.0, n, c)
    ax.add_patch(FancyBboxPatch((3.4, 0.35), 7.3, 5.45, boxstyle="round,pad=0.02,rounding_size=0.1", fc="#f7fafc", ec="#2d3748", lw=1.4))
    ax.text(7.05, 5.5, "SERVER PROCESS", fontsize=10, fontweight="bold", ha="center")
    box(3.65, 4.15, 2.2, 0.9, "TCP :9001\nEcho service", "#ebf4ff", bold=True)
    box(3.65, 2.85, 2.2, 0.9, "TCP :9002\nFile service", "#ebf4ff", bold=True)
    box(3.65, 1.55, 2.2, 0.9, "UDP :9003\nUDP echo", "#fffaf0", bold=True)
    box(3.65, 0.55, 2.2, 0.75, "UDP :9053\nDNS service", "#fffaf0", bold=True)
    box(6.2, 3.55, 2.1, 1.5, "TCP accept loop\n+ dispatcher\n→ 1 thread / connection\nidle timeout, max clients", "#e6fffa", fs=8)
    box(6.2, 0.55, 2.1, 1.9, "UDP receive loop\n→ worker thread pool\nsimulated loss hook\n(--loss)", "#e6fffa", fs=8)
    box(8.6, 3.55, 1.95, 1.5, "Failure handling\ntimeouts · resets\nprotocol errors\nsize / path checks", "#fff5f5", fs=8)
    box(8.6, 2.15, 1.95, 1.1, "Stats counters\n+ rotating log\nlogs/server.log", "#f0fff4", fs=8)
    box(8.6, 0.55, 1.95, 1.3, "Storage dir\nSHA-256 verified\natomic rename\nzone.json (DNS)", "#faf5ff", fs=8)
    arrow((2.2, 5.05), (3.65, 4.6), "frames"); arrow((2.2, 3.5), (3.65, 3.3), "LIST/GET/PUT")
    arrow((2.2, 1.95), (3.65, 1.95), "datagrams"); arrow((2.2, 1.7), (3.65, 0.95), "DNS wire fmt")
    arrow((5.85, 4.6), (6.2, 4.4), both=False); arrow((5.85, 3.3), (6.2, 3.9), both=False)
    arrow((5.85, 1.95), (6.2, 1.8), both=False); arrow((5.85, 0.9), (6.2, 1.1), both=False)
    arrow((8.3, 4.3), (8.6, 4.3), both=False); arrow((8.3, 1.5), (8.6, 1.1), both=False)
    arrow((9.55, 3.55), (9.55, 3.25), both=False)
    save("fig_architecture.png")


# 8. terminal screenshots from demo output
def terminal(text, name, title="Terminal", maxcols=118):
    lines = []
    for l in text.rstrip("\n").split("\n"):
        l = l.replace("\t", "    ")
        while len(l) > maxcols:
            lines.append(l[:maxcols]); l = "    " + l[maxcols:]
        lines.append(l)
    cols = max(max(len(l) for l in lines), 40)
    w, h = 0.0735 * cols + 0.5, 0.185 * len(lines) + 0.85
    fig = plt.figure(figsize=(w, h))
    fig.patch.set_facecolor("#1a1b26")
    ax = fig.add_axes([0, 0, 1, 1]); ax.axis("off"); ax.set_xlim(0, w); ax.set_ylim(0, h)
    ax.add_patch(plt.Rectangle((0, h - 0.4), w, 0.4, fc="#2a2c3d", ec="none"))
    for i, c in enumerate(["#ff5f56", "#ffbd2e", "#27c93f"]):
        ax.add_patch(plt.Circle((0.25 + i * 0.22, h - 0.2), 0.065, fc=c, ec="none"))
    ax.text(w / 2, h - 0.2, title, color="#a9b1d6", ha="center", va="center", fontsize=8.5, family="monospace")
    for i, l in enumerate(lines):
        col = "#9ece6a" if l.startswith("$") else ("#f7768e" if re.search(r"ERR|error:|FAIL|WARNING|Traceback", l) else "#c0caf5")
        ax.text(0.2, h - 0.65 - i * 0.185, l, color=col, family="monospace", fontsize=7.6, va="center")
    fig.savefig(os.path.join(FIG, name), facecolor=fig.get_facecolor())
    plt.close(fig)


def screenshots():
    p = os.path.join(ROOT, "results", "demo_output.txt")
    if os.path.exists(p):
        parts = re.split(r"^### (\w+)\n", open(p).read(), flags=re.M)
        sec = {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}
        titles = {"server": "Terminal 1 — starting the server", "echo": "Terminal 2 — TCP and UDP echo", "file": "Terminal 2 — file transfer",
                  "dns": "Terminal 2 — DNS service (own client and `dig`)", "concurrency": "Terminal 2 — 20 concurrent clients",
                  "failure": "Terminal 2 — fault injection", "log": "Server log excerpt (logs/demo.log)"}
        for k, t in titles.items():
            if k in sec:
                terminal(sec[k], f"term_{k}.png", t)
    p = os.path.join(ROOT, "results", "test_output.txt")
    if os.path.exists(p):
        txt = open(p).read().strip().split("\n")
        body = [l for l in txt if re.search(r"\.\.\. (ok|FAIL|ERROR|skipped)|^Ran |^OK|^FAILED|^-{20}", l)]
        terminal("$ python3 tests/run_tests.py\n" + "\n".join(body), "term_tests.png", "Test suite", 130)


if __name__ == "__main__":
    fig_latency(); fig_concurrency(); fig_files(); fig_dns(); fig_loss(); fig_resources(); fig_architecture(); screenshots()
    print("figures written to", FIG)
