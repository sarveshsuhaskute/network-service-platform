# Lightweight Client-Server Network Service Platform

[![tests](https://github.com/sarveshsuhaskute/network-service-platform/actions/workflows/tests.yml/badge.svg)](https://github.com/sarveshsuhaskute/network-service-platform/actions/workflows/tests.yml)

BCSE308P – Computer Networks, Problem III. A TCP/UDP socket-programming platform that offers Echo, File
Transfer and DNS services from one server process, handles many concurrent clients, keeps communication
logs, detects and survives communication failures, and is evaluated on response time, throughput,
reliability, packet loss and resource utilisation.

| Service      | Transport | Port | What it does |
|--------------|-----------|------|--------------|
| Echo         | TCP       | 9001 | 4-byte length-prefixed frames, echoed back; thread per connection or a `selectors` event loop |
| File         | TCP (+TLS)| 9002 | `LIST`, `GET`, `PUT`, `STAT`, `STATS`, `PING`, `QUIT`, `AUTH`; SHA-256 verified both ways; optional TLS 1.3 and token auth |
| UDP echo     | UDP       | 9003 | datagram echo, used to measure packet loss |
| DNS          | UDP       | 9053 | RFC 1035 wire format (A, AAAA, CNAME, MX, TXT) from `zone.json`; TC bit on answers > 512 bytes; optional forwarding + cache |
| DNS over TCP | TCP       | 9053 | RFC 7766 2-byte framing, pipelined queries; the client falls back to it automatically on truncation |

Python 3.9+ standard library only (no third-party packages needed to run, test or benchmark).

## Run

```bash
python3 server.py                      # all services on 127.0.0.1 (use --host 0.0.0.0 for LAN)
python3 client.py echo "hello"
python3 client.py udp-echo "hello"
python3 client.py dns www.campus.lab            # also: dns campus.lab MX | TXT | AAAA
python3 client.py dns big.campus.lab            # 40 records: truncated over UDP, retried over TCP
python3 client.py dns www.campus.lab --tcp      # query over TCP directly
python3 client.py dns www.campus.lab --adaptive # adaptive (RFC 6298-style) retransmission timeout
python3 client.py file put notes.txt
python3 client.py file list
python3 client.py file get notes.txt copy.txt
python3 client.py file stats                    # live server counters (JSON)
dig @127.0.0.1 -p 9053 www.campus.lab           # works with standard tools (also dig +tcp)
```

Secure file service (TLS 1.3 + token):

```bash
tools/make_cert.sh                              # certs/server.crt + certs/server.key (self-signed, not committed)
python3 server.py --tls-cert certs/server.crt --tls-key certs/server.key --auth-token lab-token
python3 client.py file --tls --cafile certs/server.crt --token lab-token list
```

Useful server options: `--max-clients`, `--idle-timeout`, `--echo-io selectors` (event-loop echo),
`--workers N` (pre-forked processes; helps TCP, not UDP), `--loss 0.2` and `--delay-ms 20 --jitter-ms 5`
(emulate loss and delay on the UDP services), `--send-mode sendfile`, `--digest-cache on|off`,
`--upstream HOST:PORT`, `--log-level DEBUG`. See `python3 server.py -h`.
Logs go to `logs/server.log` (rotating) and stderr.

## Web dashboard

```bash
python3 web.py            # then open http://127.0.0.1:8080
```

Runs the platform plus a browser UI: echo / DNS (UDP, TCP, adaptive timeout) / file / load-test / packet-loss
controls, a terminal-style output panel, a live server log, live statistics and the benchmark charts. Stop any
separately started `server.py` first, or use `python3 web.py --auto-ports`.

## Packet capture

```bash
sudo ./capture/capture.sh                       # tcpdump on lo0 while a scripted session runs
```

Writes `results/capture.pcap` (open it in Wireshark) and decodes it with `capture/analyze_pcap.py`
(standard library only): packet list, TCP three-way handshakes and teardowns, every DNS transaction,
retransmissions under loss, and the UDP-truncation → TCP fallback. Only tcpdump runs as root.

## Test, benchmark, demo

```bash
python3 tests/run_tests.py             # functional / fault-injection / security / TLS tests
python3 bench/run_benchmarks.py        # full evaluation (~25 min) -> results/benchmarks.json  (--quick for a smoke run)
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
server.py         entry point: Platform, command-line options, pre-fork workers
core.py           logger, statistics, per-connection accounting
tcp_services.py   thread-per-connection TCP lifecycle, echo, event-loop echo, file (TLS/AUTH), DNS over TCP
udp_services.py   UDP receive loop + workers, loss/delay emulation, UDP echo, DNS resolver
client.py         client library + CLI (retries, adaptive RTO, TCP fallback, TLS)
protocol.py       framing helpers shared by the TCP services
dns_wire.py       DNS encoder/decoder
zone.json         DNS zone data
web.py, web/      browser dashboard
capture/          tcpdump capture script and pcap decoder
tools/            certificate generator
tests/            unittest suite and runner
bench/            benchmark harness
report/           figures, report generator, final .docx
results/          benchmark JSON, test results, demo output
```
