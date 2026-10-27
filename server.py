#!/usr/bin/env python3
"""Lightweight client-server network service platform: server side.

Services
  echo      TCP  length-prefixed echo
  file      TCP  line-oriented file transfer (LIST / GET / PUT / STAT / STATS / PING / QUIT / AUTH)
  udp-echo  UDP  datagram echo (used for loss measurements)
  dns       UDP  authoritative DNS (RFC 1035 wire format) with optional upstream forwarding
  dns-tcp   TCP  DNS over TCP on the same port number (RFC 7766), used for truncated answers
Options add TLS + token authentication to the file service, an event-loop echo service, pre-forked
workers, and loss/delay emulation on the UDP services.
"""
import argparse
import json
import os
import random
import signal
import ssl
import threading

from core import Session, Stats, log, raise_fd_limit, setup_logging  # noqa: F401  (re-exported)
from tcp_services import DNSTCPService, EchoService, EventLoopEchoService, FileService, TCPService  # noqa: F401
from udp_services import DNSService, UDPEchoService, UDPService, Zone  # noqa: F401

__all__ = ["Platform", "DNSTCPService", "EventLoopEchoService", "Session", "Stats", "TCPService", "EchoService", "FileService", "UDPService",
           "UDPEchoService", "DNSService", "Zone", "log", "raise_fd_limit", "setup_logging"]


# --------------------------------------------------------------------------- platform
class Platform:
    def __init__(self, host="127.0.0.1", echo_port=9001, file_port=9002, udp_echo_port=9003, dns_port=9053,
                 root="storage", zone="zone.json", max_clients=200, idle_timeout=30.0, loss=0.0,
                 upstream=None, max_file=256 * 1024 * 1024, send_mode="chunked", workers=16, seed=None,
                 digest_cache=True, echo_io="threads", tls=None, auth_token=None,
                 delay=0.0, jitter=0.0):
        self.stats = Stats()
        rng = random.Random(seed)
        echo_cls = EventLoopEchoService if echo_io == "selectors" else EchoService
        self.echo = echo_cls(host, echo_port, self.stats, max_clients, idle_timeout)
        tls_ctx = None
        if tls:                                      # (certfile, keyfile): serve the file service over TLS 1.2+
            tls_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            tls_ctx.load_cert_chain(*tls)
        self.files = FileService(host, file_port, self.stats, max_clients, idle_timeout, root, max_file,
                                 send_mode, self.stats.snapshot, digest_cache, tls_ctx, auth_token)
        self.uecho = UDPEchoService(host, udp_echo_port, self.stats, workers, loss, rng, delay, jitter)
        self.dns = DNSService(host, dns_port, self.stats, workers, loss, rng, Zone(zone), upstream,
                              delay=delay, jitter=jitter)
        # DNS over TCP listens on the same port number as UDP DNS, as real servers do
        self.dns_tcp = DNSTCPService(host, self.dns.port, self.stats, max_clients, idle_timeout, self.dns)
        self.services = [self.echo, self.files, self.uecho, self.dns, self.dns_tcp]

    @property
    def ports(self):
        return {"echo": self.echo.port, "file": self.files.port, "udp-echo": self.uecho.port, "dns": self.dns.port,
                "dns-tcp": self.dns_tcp.port}

    def start(self):
        for s in self.services:
            s.start()
        return self

    def stop(self):
        for s in self.services:
            s.stop()
        log.info("event=shutdown stats=%s", json.dumps(self.stats.snapshot(), sort_keys=True))


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
    p.add_argument("--delay-ms", type=float, default=0.0, help="emulated extra round-trip delay on UDP services")
    p.add_argument("--jitter-ms", type=float, default=0.0, help="uniform +/- jitter added to --delay-ms")
    p.add_argument("--seed", type=int)
    p.add_argument("--tls-cert", help="PEM certificate: serve the file service over TLS (needs --tls-key)")
    p.add_argument("--tls-key", help="PEM private key for --tls-cert")
    p.add_argument("--auth-token", default=os.environ.get("NETPLATFORM_TOKEN"),
                   help="require AUTH <token> on the file service (default: $NETPLATFORM_TOKEN)")
    p.add_argument("--echo-io", choices=["threads", "selectors"], default="threads",
                   help="echo service concurrency model: thread per connection, or one selectors event loop")
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
                    digest_cache=a.digest_cache == "on", echo_io=a.echo_io,
                    tls=(a.tls_cert, a.tls_key) if a.tls_cert else None, auth_token=a.auth_token,
                    delay=a.delay_ms / 1000, jitter=a.jitter_ms / 1000)
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
