"""Shared server infrastructure: logger, statistics, per-connection accounting, logging setup, fd limit."""
import collections
import logging
import os
import resource
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

log = logging.getLogger("netplatform")


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
