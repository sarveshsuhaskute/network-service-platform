#!/usr/bin/env bash
# Capture real packets of every service with tcpdump on the loopback interface, then decode them.
# tcpdump needs root to open the packet filter, so run:   sudo ./capture/capture.sh
# The server and clients run as your normal user; only tcpdump runs as root.
# Output: results/capture.pcap (open it in Wireshark too), results/capture_report.txt, results/capture_analysis.json
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "$(id -u)" != 0 ]; then echo "tcpdump needs root: run  sudo $0"; exit 1; fi
U=${SUDO_USER:-$(whoami)}
PY=${PY:-$(sudo -u "$U" -i which python3 2>/dev/null | tail -1)}
PY=${PY:-python3}
IF=lo0; [ "$(uname)" = Linux ] && IF=lo
as_user() { sudo -u "$U" "$@"; }
PORTS="--echo-port 19001 --file-port 19002 --udp-echo-port 19003 --dns-port 19053"
WORK=$(as_user mktemp -d)
as_user mkdir -p "$WORK/storage" results logs
head -c 20000 /dev/urandom | as_user tee "$WORK/sample.bin" > /dev/null

tcpdump -i "$IF" -s 0 -U -w results/capture.pcap \
  "host 127.0.0.1 and (port 19001 or port 19002 or port 19003 or port 19053)" 2> "$WORK/tcpdump.err" &
TD=$!
sleep 1

echo "[1/2] normal traffic"
as_user "$PY" server.py $PORTS --root "$WORK/storage" --log-file logs/capture.log --stats-interval 0 > /dev/null 2>&1 &
SRV=$!; sleep 1
as_user "$PY" client.py echo "hello over TCP" --port 19001
as_user "$PY" client.py udp-echo "hello over UDP" --port 19003
as_user "$PY" client.py dns web.campus.lab --port 19053
as_user "$PY" client.py dns big.campus.lab --port 19053          # truncated over UDP -> TCP fallback
as_user "$PY" client.py dns nosuchhost.campus.lab --port 19053
(cd "$WORK" && as_user "$PY" "$OLDPWD/client.py" file put sample.bin --port 19002)
as_user "$PY" client.py file list --port 19002
kill $SRV; wait $SRV 2>/dev/null || true; sleep 0.5

echo "[2/2] 40% simulated datagram loss: DNS retransmissions with 200 ms timeout"
as_user "$PY" server.py $PORTS --root "$WORK/storage" --log-file logs/capture.log --stats-interval 0 --loss 0.4 --seed 7 > /dev/null 2>&1 &
SRV=$!; sleep 1
for n in 1 2 3 4 5 6; do as_user "$PY" client.py dns web.campus.lab --port 19053 --timeout 0.2 --retries 4 || true; done
kill $SRV; wait $SRV 2>/dev/null || true
sleep 0.5; kill -INT $TD; wait $TD 2>/dev/null || true
chown "$U" results/capture.pcap
rm -rf "$WORK"
echo; as_user "$PY" capture/analyze_pcap.py results/capture.pcap | tail -40
echo; echo "Done. Open results/capture.pcap in Wireshark, or read results/capture_report.txt"
