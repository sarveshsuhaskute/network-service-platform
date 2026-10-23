# Lightweight Client-Server Network Service Platform

[![tests](https://github.com/sarveshsuhaskute/network-service-platform/actions/workflows/tests.yml/badge.svg)](https://github.com/sarveshsuhaskute/network-service-platform/actions/workflows/tests.yml)

BCSE308P – Computer Networks, Problem III. A TCP/UDP socket-programming platform that offers four
services from one server process, handles many concurrent clients, keeps communication logs, detects
and survives communication failures, and is evaluated on response time, throughput, reliability,
packet loss and resource utilisation.

| Service  | Transport | Port | What it does |
|----------|-----------|------|--------------|
| Echo     | TCP       | 9001 | 4-byte length-prefixed frames, echoed back unchanged |
| File     | TCP       | 9002 | `LIST`, `GET`, `PUT`, `STAT`, `STATS`, `PING`, `QUIT`; SHA-256 verified both ways |
| UDP echo | UDP       | 9003 | datagram echo, used to measure packet loss |
| DNS      | UDP       | 9053 | RFC 1035 wire format (A, AAAA, CNAME, MX, TXT) from `zone.json`; optional upstream forwarding + cache |

Python 3.9+ standard library only (no third-party packages needed to run, test or benchmark).

## Run

```bash
python3 server.py                      # all four services on 127.0.0.1 (use --host 0.0.0.0 for LAN)
python3 client.py echo "hello"
python3 client.py udp-echo "hello"
python3 client.py dns www.campus.lab            # also: dns campus.lab MX | TXT | AAAA
python3 client.py dns www.campus.lab --adaptive # adaptive (RFC 6298-style) retransmission timeout
python3 client.py file put notes.txt
python3 client.py file list
python3 client.py file get notes.txt copy.txt
python3 client.py file stats                    # live server counters (JSON)
dig @127.0.0.1 -p 9053 www.campus.lab           # the DNS service works with standard tools
```

Useful server options: `--max-clients`, `--idle-timeout`, `--loss 0.2` (simulate 20 % datagram loss on the
UDP services), `--send-mode sendfile`, `--digest-cache on|off`, `--upstream HOST:PORT`, `--log-level DEBUG`. See `python3 server.py -h`.
Logs go to `logs/server.log` (rotating) and stderr.

## Web dashboard

```bash
python3 web.py            # then open http://127.0.0.1:8080
```

Runs the platform itself plus a browser UI: echo / DNS / file / load-test / packet-loss controls, a terminal-style output
panel, a live server log, live statistics and the benchmark charts. Stop any separately started `server.py` first, or use
`python3 web.py --auto-ports`.

## Test, benchmark, demo

```bash
python3 tests/run_tests.py             # 34 functional / fault-injection / security tests
python3 bench/run_benchmarks.py        # full evaluation (~15 min) -> results/benchmarks.json  (--quick for a smoke run)
./demo.sh                              # scripted end-to-end demonstration
```

## Report

`report/` holds the project report (`CN_Project_Report.docx`) and the generator scripts. To rebuild it:

```bash
python3 -m venv .venv && .venv/bin/pip install -r report/requirements.txt
.venv/bin/python report/make_charts.py && .venv/bin/python report/make_report.py
```

## Layout

```
server.py        server: services, concurrency, logging, failure handling
client.py        client library + CLI
protocol.py      framing helpers shared by the TCP services
dns_wire.py      DNS encoder/decoder
zone.json        DNS zone data
tests/           unittest suite and runner
bench/           benchmark harness
report/          figures, report generator, final .docx
results/         benchmark JSON, test results, demo output
```
