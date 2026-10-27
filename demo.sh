#!/usr/bin/env bash
# End-to-end demo. Prints each command before its output; sections are marked with "### name".
# Usage: ./demo.sh            (starts its own server on the default ports, stops it afterwards)
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
ROOT=$(pwd)
run() { echo "\$ $*"; "$@" 2>&1; echo; }
# client commands run inside the scratch dir so the printed paths stay short
cli() { echo "\$ python3 client.py $*"; (cd "$DEMO" && $PY "$ROOT/client.py" "$@") 2>&1; echo; }
DEMO=$(mktemp -d)
mkdir "$DEMO/storage"
head -c 3000000 /dev/urandom > "$DEMO/report.bin"
printf 'Computer Networks lab notes\n' > "$DEMO/notes.txt"

echo "### server"
rm -f logs/demo.log; mkdir -p logs
$PY server.py --log-file logs/demo.log --stats-interval 0 --root "$DEMO/storage" 2> "$DEMO/srv.err" > "$DEMO/srv.out" &
SRV=$!
sleep 1
echo "\$ python3 server.py --log-file logs/demo.log --root storage"
cat "$DEMO/srv.out"; cat "$DEMO/srv.err"; echo

echo "### echo"
cli echo "hello from the client"
cli udp-echo "datagram payload"

echo "### file"
cli file put notes.txt
cli file put report.bin
cli file list
cli file get report.bin report.copy
echo "$ cmp report.bin report.copy"; cmp "$DEMO/report.bin" "$DEMO/report.copy" && echo "files are identical"; echo
cli file get ../../etc/passwd
cli file get missing.txt

echo "### dns"
cli dns web.campus.lab
cli dns www.campus.lab
cli dns campus.lab MX
cli dns nosuchhost.campus.lab
run dig @127.0.0.1 -p 9053 +noall +comments +answer www.campus.lab
cli dns big.campus.lab | head -5; echo "   ... (40 A records in total)"; echo
echo "\$ dig +tcp +short @127.0.0.1 -p 9053 big.campus.lab | wc -l"; dig +tcp +short @127.0.0.1 -p 9053 big.campus.lab | wc -l; echo

echo "### concurrency"
echo "\$ 20 clients in parallel, each sending 5 echo messages"
PIDS=""
for i in $(seq 1 20); do ( for j in 1 2 3 4 5; do $PY client.py echo "client-$i msg-$j" >/dev/null || echo FAIL; done ) & PIDS="$PIDS $!"; done
wait $PIDS
echo "all clients finished"; echo
cli file stats

echo "### failure"
echo "\$ client aborts an upload half-way (simulated with a raw socket)"
$PY - <<'PYEOF'
import socket, time
s = socket.create_connection(("127.0.0.1", 9002))
s.sendall(b"PUT half.bin 1000000 " + b"a" * 64 + b"\n"); print(s.recv(20))
s.sendall(b"x" * 1000); s.close(); time.sleep(0.3)
PYEOF
echo
echo "\$ path-traversal and malformed DNS packet"
$PY - <<'PYEOF'
import socket
s = socket.create_connection(("127.0.0.1", 9002)); s.sendall(b"GET ../../etc/passwd\n"); print(s.recv(100)); s.close()
u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.settimeout(1)
u.sendto(b"\xab\xcd\x01\x00\x00\x05\x00\x00\x00\x00\x00\x00\x03abc", ("127.0.0.1", 9053)); r = u.recvfrom(512)[0]; print("DNS reply rcode =", r[3] & 15, "(1 = FORMERR)")
PYEOF
echo

echo "### tls"
echo "\$ tools/make_cert.sh certs && python3 server.py --file-port 9102 ... --tls-cert certs/server.crt --tls-key certs/server.key --auth-token lab-token"
tools/make_cert.sh "$DEMO/certs" > /dev/null
mkdir -p "$DEMO/tls-storage"
$PY server.py --echo-port 9101 --file-port 9102 --udp-echo-port 9103 --dns-port 9153 --root "$DEMO/tls-storage" \
  --tls-cert "$DEMO/certs/server.crt" --tls-key "$DEMO/certs/server.key" --auth-token lab-token \
  --log-file logs/demo.log --stats-interval 0 > /dev/null 2>&1 &
TLS=$!
sleep 1
cp "$DEMO/certs/server.crt" "$DEMO/server.crt"
cli file --port 9102 --tls --cafile server.crt --token lab-token put notes.txt
cli file --port 9102 --tls --cafile server.crt --token lab-token list
cli file --port 9102 --tls --cafile server.crt --token wrong-token list
cli file --port 9102 --tls --cafile server.crt list
cli file --port 9102 --tls list
kill $TLS; wait $TLS 2>/dev/null

echo "### log"
sleep 0.5
echo "\$ grep -E 'event=(put|get|list|query|aborted|malformed|auth|tls_established|tls_error)|WARNING' logs/demo.log | tail -n 24"
grep -E 'event=(put|get|list|query|aborted|malformed|auth|tls_established|tls_error)|WARNING' logs/demo.log | tail -n 24 | cut -c1-175
echo

kill $SRV; wait $SRV 2>/dev/null
rm -rf "$DEMO"
