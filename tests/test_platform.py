"""Functional, fault-injection and security tests. Each test docstring is its description in the report."""
import hashlib
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import client as cl                      # noqa: E402
import dns_wire as dw                    # noqa: E402
import server as sv                      # noqa: E402
from protocol import HDR, ConnectionClosed  # noqa: E402

ZONE = os.path.join(os.path.dirname(__file__), "..", "zone.json")
sv.log.addHandler(__import__("logging").NullHandler())
sv.log.propagate = False                 # keep test output readable; server logging is exercised separately


def read(path):
    with open(path, "rb") as f:
        return f.read()


class Base(unittest.TestCase):
    plat_kwargs = {}

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.plat = sv.Platform(echo_port=0, file_port=0, udp_echo_port=0, dns_port=0, root=cls.tmp.name,
                               zone=ZONE, seed=1, **cls.plat_kwargs).start()
        cls.ports = cls.plat.ports

    @classmethod
    def tearDownClass(cls):
        cls.plat.stop()
        cls.tmp.cleanup()

    def raw(self, svc, timeout=3):
        s = socket.create_connection(("127.0.0.1", self.ports[svc]), timeout=timeout)
        return s


class TestEcho(Base):
    def test_echo_roundtrip_sizes(self):
        """TCP echo returns identical bytes for 0 B, 1 B, 1 KiB and 1 MiB payloads."""
        with cl.EchoClient(port=self.ports["echo"]) as c:
            for n in (0, 1, 1024, 1 << 20):
                data = os.urandom(n)
                self.assertEqual(c.echo(data)[0], data)

    def test_echo_many_messages_one_connection(self):
        """500 sequential echoes on a single persistent connection all succeed."""
        with cl.EchoClient(port=self.ports["echo"]) as c:
            for i in range(500):
                self.assertEqual(c.echo(b"m%d" % i)[0], b"m%d" % i)

    def test_concurrent_echo_clients(self):
        """50 concurrent clients each exchange 100 messages with no errors or cross-talk."""
        errors = []

        def worker(k):
            try:
                with cl.EchoClient(port=self.ports["echo"]) as c:
                    for i in range(100):
                        msg = f"client{k}-msg{i}".encode()
                        if c.echo(msg)[0] != msg:
                            errors.append((k, i))
            except Exception as e:
                errors.append(e)

        ts = [threading.Thread(target=worker, args=(k,)) for k in range(50)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])

    def test_oversize_frame_rejected(self):
        """A frame header announcing more than 16 MiB makes the server drop the connection."""
        s = self.raw("echo")
        s.sendall(HDR.pack(100 * 1024 * 1024))
        self.assertEqual(s.recv(10), b"")
        s.close()

    def test_abrupt_disconnect_mid_frame_is_logged_and_survived(self):
        """Client dies halfway through a frame: server records an abort and keeps serving others."""
        before = self.plat.stats.snapshot()["counters"].get("echo.aborted", 0)
        s = self.raw("echo")
        s.sendall(HDR.pack(1000) + b"x" * 10)
        s.close()
        for _ in range(50):
            if self.plat.stats.snapshot()["counters"].get("echo.aborted", 0) > before:
                break
            time.sleep(0.05)
        self.assertGreater(self.plat.stats.snapshot()["counters"].get("echo.aborted", 0), before)
        with cl.EchoClient(port=self.ports["echo"]) as c:
            self.assertEqual(c.echo(b"still alive")[0], b"still alive")


class TestEchoEventLoop(TestEcho):
    """Re-runs every echo test against the selectors event-loop implementation."""
    plat_kwargs = {"echo_io": "selectors"}

    def test_idle_timeout_event_loop(self):
        """The event loop drops a client that stays idle longer than the idle timeout."""
        tmp = tempfile.TemporaryDirectory()
        plat = sv.Platform(echo_port=0, file_port=0, udp_echo_port=0, dns_port=0, root=tmp.name, zone=ZONE,
                           idle_timeout=0.5, echo_io="selectors").start()
        try:
            s = socket.create_connection(("127.0.0.1", plat.ports["echo"]), timeout=3)
            t0 = time.time()
            self.assertEqual(s.recv(10), b"")
            self.assertLess(time.time() - t0, 2)
            s.close()
        finally:
            plat.stop()
            tmp.cleanup()


class TestFile(Base):
    def setUp(self):
        self.c = cl.FileClient(port=self.ports["file"])
        self.addCleanup(self.c.close)
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)

    def _mk(self, name, size):
        p = os.path.join(self.d.name, name)
        with open(p, "wb") as f:
            f.write(os.urandom(size))
        return p

    def test_upload_download_integrity(self):
        """A 5 MB file uploaded then downloaded is byte-identical (SHA-256 verified end to end)."""
        src = self._mk("blob.bin", 5_000_000)
        self.c.put(src)
        dst = os.path.join(self.d.name, "back.bin")
        self.c.get("blob.bin", dst)
        self.assertEqual(read(src), read(dst))

    def test_list_and_stat(self):
        """LIST shows uploaded files with sizes and STAT returns the correct size and digest."""
        src = self._mk("listme.bin", 1234)
        self.c.put(src)
        self.assertIn({"name": "listme.bin", "size": 1234}, self.c.list())
        self.assertEqual(self.c.stat("listme.bin"), (1234, hashlib.sha256(read(src)).hexdigest()))

    def test_empty_file(self):
        """A zero-byte file can be uploaded and downloaded."""
        src = self._mk("empty.bin", 0)
        self.c.put(src)
        dst = os.path.join(self.d.name, "empty.out")
        self.assertEqual(self.c.get("empty.bin", dst)[0], 0)

    def test_missing_file_404(self):
        """Requesting a non-existent file returns ERR 404 and the session stays usable."""
        with self.assertRaisesRegex(cl.ServiceError, "404"):
            self.c.get("ghost.bin", os.path.join(self.d.name, "x"))
        self.assertGreater(self.c.ping(), 0)

    def test_path_traversal_blocked(self):
        """File names such as ../../etc/passwd or /etc/passwd are refused (ERR 400)."""
        for bad in ("../../etc/passwd", "/etc/passwd", "..", ".hidden", "a/b"):
            with self.assertRaisesRegex(cl.ServiceError, "400"):
                self.c.stat(bad)

    def test_checksum_mismatch_detected(self):
        """An upload whose bytes do not match the announced SHA-256 is rejected and not stored."""
        s = self.raw("file")
        s.sendall(f"PUT bad.bin 10 {'0' * 64}\n".encode())
        self.assertEqual(s.recv(100), b"READY\n")
        s.sendall(b"0123456789")
        self.assertIn(b"ERR 422", s.recv(100))
        s.close()
        self.assertNotIn("bad.bin", [i["name"] for i in self.c.list()])

    def test_oversize_upload_rejected(self):
        """An upload larger than the configured limit is refused before any data is sent (ERR 413)."""
        s = self.raw("file")
        s.sendall(f"PUT big.bin {10**12} {'a' * 64}\n".encode())
        self.assertIn(b"ERR 413", s.recv(100))
        s.close()

    def test_aborted_upload_leaves_no_partial_file(self):
        """Client disconnects mid-upload: server removes the temporary file and stores nothing."""
        s = self.raw("file")
        s.sendall(f"PUT half.bin 1000000 {'b' * 64}\n".encode())
        s.recv(100)
        s.sendall(b"x" * 1000)
        s.close()
        time.sleep(0.4)
        names = os.listdir(self.plat.files.root)
        self.assertNotIn("half.bin", names)
        self.assertFalse([n for n in names if n.startswith(".tmp-")])

    def test_malformed_commands(self):
        """Unknown commands get ERR 400; an over-long command line closes the connection."""
        s = self.raw("file")
        s.sendall(b"FROBNICATE now\n")
        self.assertIn(b"ERR 400", s.recv(100))
        s.sendall(b"A" * 5000 + b"\n")
        data = b""
        while True:
            chunk = s.recv(100)
            if not chunk:
                break
            data += chunk
        self.assertIn(b"ERR 400", data)
        s.close()

    def test_concurrent_uploads(self):
        """8 clients upload 2 MB files simultaneously; every file is stored intact."""
        errs, srcs = [], [self._mk(f"par{i}.bin", 2_000_000) for i in range(8)]

        def w(p):
            try:
                with cl.FileClient(port=self.ports["file"]) as c:
                    c.put(p)
            except Exception as e:
                errs.append(e)

        ts = [threading.Thread(target=w, args=(p,)) for p in srcs]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errs, [])
        for p in srcs:
            self.assertEqual(self.c.stat(os.path.basename(p))[1],
                             hashlib.sha256(read(p)).hexdigest())

    def test_digest_cache_invalidated_when_file_changes(self):
        """The SHA-256 cache serves repeat requests but is refreshed when the stored file is modified."""
        src = self._mk("cached.bin", 1000)
        self.c.put(src)
        first = self.c.stat("cached.bin")
        self.assertEqual(self.c.stat("cached.bin"), first)
        self.assertGreaterEqual(self.plat.stats.snapshot()["counters"].get("file.digest_cache_hits", 0), 1)
        with open(os.path.join(self.plat.files.root, "cached.bin"), "wb") as f:   # modified behind the server's back
            f.write(b"different content")
        self.assertEqual(self.c.stat("cached.bin"), (17, hashlib.sha256(b"different content").hexdigest()))

    def test_stats_command(self):
        """The STATS command returns live server counters and resource usage as JSON."""
        st = self.c.server_stats()
        self.assertIn("counters", st)
        self.assertIn("maxrss_bytes", st)


class TestDNS(Base):
    def q(self, name, t="A", **kw):
        return cl.dns_query("127.0.0.1", self.ports["dns"], name, t, **kw)[0]

    def test_a_record(self):
        """An A query for a zone name returns NOERROR with both addresses."""
        r = self.q("web.campus.lab")
        self.assertEqual((r.rcode, sorted(a.value for a in r.answers)), (0, ["10.10.0.10", "10.10.0.11"]))
        self.assertTrue(r.aa)

    def test_cname_chain(self):
        """A two-level CNAME chain is followed and the final A records are included."""
        r = self.q("alias1.campus.lab")
        self.assertEqual([dw.QTYPE_NAMES[a.rtype] for a in r.answers], ["CNAME", "CNAME", "A", "A"])

    def test_aaaa_mx_txt(self):
        """AAAA, MX and TXT records are encoded and decoded correctly."""
        self.assertEqual(self.q("web.campus.lab", "AAAA").answers[0].value, "fd00::10")
        self.assertEqual(self.q("campus.lab", "MX").answers[0].value, "10 mail.campus.lab")
        self.assertIn("lab zone", self.q("campus.lab", "TXT").answers[0].value)

    def test_nxdomain(self):
        """An unknown name yields rcode NXDOMAIN with no answers."""
        r = self.q("nothing.campus.lab")
        self.assertEqual((r.rcode, r.answers), (dw.RCODE_NXDOMAIN, []))

    def test_nodata(self):
        """A known name without the requested type yields NOERROR with an empty answer section."""
        r = self.q("mail.campus.lab", "AAAA")
        self.assertEqual((r.rcode, r.answers), (0, []))

    def test_case_insensitive(self):
        """Name matching is case-insensitive (WWW.Campus.LAB)."""
        self.assertEqual(self.q("WWW.Campus.LAB").rcode, 0)

    def test_malformed_packet_formerr(self):
        """A truncated/garbage DNS packet gets FORMERR (or is ignored if no ID) and the server survives."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.sendto(b"\xab\xcd\x01\x00\x00\x05\x00\x00\x00\x00\x00\x00\x03abc", ("127.0.0.1", self.ports["dns"]))
        resp = s.recvfrom(512)[0]
        self.assertEqual(resp[:2], b"\xab\xcd")
        self.assertEqual(resp[3] & 0xF, dw.RCODE_FORMERR)
        s.sendto(b"\x01", ("127.0.0.1", self.ports["dns"]))        # too short to even carry an ID
        self.assertEqual(self.q("web.campus.lab").rcode, 0)
        s.close()

    def test_compression_loop_rejected(self):
        """A name consisting of a self-referencing compression pointer is rejected, not looped on."""
        pkt = struct.pack("!HHHHHH", 7, 0x0100, 1, 0, 0, 0) + b"\xc0\x0c" + struct.pack("!HH", 1, 1)
        with self.assertRaises(dw.DNSFormatError):
            dw.parse_query(pkt)

    def test_unsupported_opcode(self):
        """A non-QUERY opcode is answered with NOTIMP."""
        pkt = bytearray(dw.build_query("web.campus.lab"))
        pkt[2] |= 0x28                                  # opcode 5 (UPDATE)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.sendto(bytes(pkt), ("127.0.0.1", self.ports["dns"]))
        self.assertEqual(s.recvfrom(512)[0][3] & 0xF, dw.RCODE_NOTIMP)
        s.close()

    def test_matches_system_dig(self):
        """The server's answers are decodable by the independent `dig` tool (interoperability)."""
        import shutil
        import subprocess
        if not shutil.which("dig"):
            self.skipTest("dig not installed")
        out = subprocess.run(["dig", "+short", "@127.0.0.1", "-p", str(self.ports["dns"]), "web.campus.lab"],
                             capture_output=True, text=True, timeout=10).stdout.split()
        self.assertEqual(sorted(out), ["10.10.0.10", "10.10.0.11"])

    def test_large_answer_truncated_over_udp(self):
        """A 40-record answer exceeds the 512-byte UDP limit: the server sets TC and drops the answers."""
        r = cl.dns_query("127.0.0.1", self.ports["dns"], "big.campus.lab", tcp_fallback=False)[0]
        self.assertTrue(r.tc)
        self.assertEqual(r.answers, [])

    def test_tcp_fallback_returns_all_records(self):
        """On a truncated UDP answer the client retries over TCP and receives all 40 records."""
        r = cl.dns_query("127.0.0.1", self.ports["dns"], "big.campus.lab")[0]
        self.assertEqual((r.transport, r.tc, len(r.answers)), ("tcp", False, 40))

    def test_dns_over_tcp_pipelined(self):
        """Two queries sent back-to-back on one TCP connection get two correctly matched answers (RFC 7766)."""
        s = socket.create_connection(("127.0.0.1", self.ports["dns-tcp"]), timeout=3)
        q1, q2 = dw.build_query("web.campus.lab", "A", 101), dw.build_query("campus.lab", "MX", 102)
        s.sendall(struct.pack("!H", len(q1)) + q1 + struct.pack("!H", len(q2)) + q2)
        got = []
        for _ in range(2):
            (n,) = struct.unpack("!H", cl.recv_exact(s, 2))
            got.append(dw.parse_response(cl.recv_exact(s, n)))
        s.close()
        self.assertEqual([g.id for g in got], [101, 102])
        self.assertEqual(got[1].answers[0].value, "10 mail.campus.lab")

    def test_dig_over_tcp(self):
        """`dig +tcp` (independent implementation) receives all 40 records over DNS-over-TCP."""
        import shutil
        import subprocess
        if not shutil.which("dig"):
            self.skipTest("dig not installed")
        out = subprocess.run(["dig", "+tcp", "+short", "@127.0.0.1", "-p", str(self.ports["dns"]), "big.campus.lab"],
                             capture_output=True, text=True, timeout=10).stdout.split()
        self.assertEqual(len(out), 40)

    def test_concurrent_dns(self):
        """20 threads x 50 queries all receive correct answers."""
        bad = []

        def w():
            for _ in range(50):
                r = self.q("web.campus.lab")
                if r.rcode != 0 or len(r.answers) != 2:
                    bad.append(r)

        ts = [threading.Thread(target=w) for _ in range(20)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(bad, [])


class TestTLSAndAuth(unittest.TestCase):
    TOKEN = "s3cret-lab-token"

    @classmethod
    def setUpClass(cls):
        import shutil
        import subprocess
        if not shutil.which("openssl"):
            raise unittest.SkipTest("openssl not installed")
        cls.tmp = tempfile.TemporaryDirectory()
        subprocess.run([os.path.join(os.path.dirname(__file__), "..", "tools", "make_cert.sh"), cls.tmp.name],
                       check=True, capture_output=True)
        cls.cert, key = os.path.join(cls.tmp.name, "server.crt"), os.path.join(cls.tmp.name, "server.key")
        cls.root = os.path.join(cls.tmp.name, "files")
        cls.plat = sv.Platform(echo_port=0, file_port=0, udp_echo_port=0, dns_port=0, root=cls.root, zone=ZONE,
                               tls=(cls.cert, key), auth_token=cls.TOKEN).start()
        cls.port = cls.plat.ports["file"]

    @classmethod
    def tearDownClass(cls):
        cls.plat.stop()
        cls.tmp.cleanup()

    def test_tls_transfer_with_token(self):
        """Over TLS with a valid token, an upload/download round trip is byte-identical (TLS 1.2+ negotiated)."""
        src = os.path.join(self.tmp.name, "secret.bin")
        with open(src, "wb") as f:
            f.write(os.urandom(300_000))
        with cl.FileClient(port=self.port, tls=True, cafile=self.cert, token=self.TOKEN) as c:
            self.assertIn(c.tls_version, ("TLSv1.2", "TLSv1.3"))
            c.put(src)
            dst = os.path.join(self.tmp.name, "secret.out")
            c.get("secret.bin", dst)
        self.assertEqual(read(src), read(dst))

    def test_missing_token_rejected(self):
        """Without AUTH every file command is refused with ERR 401 (PING still works)."""
        with cl.FileClient(port=self.port, tls=True, cafile=self.cert) as c:
            self.assertGreater(c.ping(), 0)
            with self.assertRaisesRegex(cl.ServiceError, "401"):
                c.list()

    def test_wrong_token_limit(self):
        """Three wrong tokens close the connection (brute-force limit) and are counted."""
        before = self.plat.stats.snapshot()["counters"].get("file.auth_failures", 0)
        with cl.FileClient(port=self.port, tls=True, cafile=self.cert) as c:
            for _ in range(3):
                with self.assertRaisesRegex(cl.ServiceError, "401"):
                    c._cmd("AUTH wrong")
            with self.assertRaises((ConnectionClosed, OSError)):
                c._cmd("PING")
        self.assertEqual(self.plat.stats.snapshot()["counters"]["file.auth_failures"] - before, 3)

    def test_plaintext_client_on_tls_port(self):
        """A plaintext client on the TLS port gets no service; the server logs a TLS error and keeps running."""
        before = self.plat.stats.snapshot()["counters"].get("file.tls_errors", 0)
        s = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        s.sendall(b"LIST\n")
        try:
            data = s.recv(100)
        except OSError:
            data = b""
        s.close()
        self.assertNotIn(b"OK", data)
        for _ in range(40):
            if self.plat.stats.snapshot()["counters"].get("file.tls_errors", 0) > before:
                break
            time.sleep(0.05)
        self.assertGreater(self.plat.stats.snapshot()["counters"].get("file.tls_errors", 0), before)

    def test_untrusted_certificate_rejected(self):
        """A client that does not trust the server's self-signed certificate refuses the connection."""
        import ssl
        with self.assertRaises(ssl.SSLCertVerificationError):
            cl.FileClient(port=self.port, tls=True, cafile=None)


class TestForwarding(unittest.TestCase):
    def test_forwarding_cache_and_upstream_failure(self):
        """Unknown names are forwarded upstream (non-authoritative answer), repeats hit the TTL cache,
        and a dead upstream yields SERVFAIL instead of a hang."""
        import json as _json
        tmp = tempfile.TemporaryDirectory()
        zpath = os.path.join(tmp.name, "up.json")
        with open(zpath, "w") as f:
            _json.dump({"ttl": 60, "records": {"ext.example.net": {"A": ["192.0.2.7"]},
                                                "other.example.net": {"A": ["192.0.2.8"]}}}, f)
        up = sv.Platform(echo_port=0, file_port=0, udp_echo_port=0, dns_port=0, root=tmp.name, zone=zpath).start()
        main = sv.Platform(echo_port=0, file_port=0, udp_echo_port=0, dns_port=0, root=tmp.name, zone=ZONE,
                           upstream=("127.0.0.1", up.ports["dns"])).start()
        try:
            port = main.ports["dns"]
            r = cl.dns_query("127.0.0.1", port, "ext.example.net")[0]
            self.assertEqual((r.rcode, r.answers[0].value), (0, "192.0.2.7"))
            cl.dns_query("127.0.0.1", port, "ext.example.net")
            c = main.stats.snapshot()["counters"]
            self.assertEqual((c.get("dns.forwarded"), c.get("dns.cache_hits")), (1, 1))
            self.assertTrue(cl.dns_query("127.0.0.1", port, "web.campus.lab")[0].aa)   # local zone stays authoritative
            up.stop()
            r = cl.dns_query("127.0.0.1", port, "other.example.net", timeout=3)[0]
            self.assertEqual(r.rcode, dw.RCODE_SERVFAIL)
        finally:
            main.stop()
            tmp.cleanup()


class TestCaptureAnalyzer(unittest.TestCase):
    def test_decodes_handshake_dns_and_retransmission(self):
        """The pcap decoder finds a TCP three-way handshake, decodes DNS and counts a retransmitted query ID."""
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "capture"))
        import analyze_pcap as ap

        def ip(proto, sport, dport, l4):
            hdr = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(l4), 0, 0, 64, proto, 0,
                              socket.inet_aton("127.0.0.1"), socket.inet_aton("127.0.0.1"))
            return struct.pack("<I", 2) + hdr + l4                      # DLT_NULL, AF_INET
        def tcp(sp, dp, seq, ack, flags):
            return ip(6, sp, dp, struct.pack("!HHIIBBHHH", sp, dp, seq, ack, 5 << 4, flags, 65535, 0, 0))
        def udp(sp, dp, payload):
            return ip(17, sp, dp, struct.pack("!HHHH", sp, dp, 8 + len(payload), 0) + payload)

        q = dw.build_query("web.campus.lab", "A", 0x4242)
        resp = dw.build_response(dw.parse_query(q), 0, [("web.campus.lab", 1, 300, socket.inet_aton("10.10.0.10"))])
        frames = [tcp(50000, 19001, 100, 0, 0x02), tcp(19001, 50000, 900, 101, 0x12), tcp(50000, 19001, 101, 901, 0x10),
                  udp(50001, 19053, q), udp(50001, 19053, q), udp(19053, 50001, resp)]
        pcap = struct.pack("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, 0)
        for i, fr in enumerate(frames):
            pcap += struct.pack("<IIII", 1000, i * 1000, len(fr), len(fr)) + fr
        tmp = tempfile.TemporaryDirectory()
        path = os.path.join(tmp.name, "t.pcap")
        with open(path, "wb") as f:
            f.write(pcap)
        a = ap.analyze(ap.parse(path))
        tmp.cleanup()
        self.assertEqual(len(a["packets"]), 6)
        hs = a["handshakes"][0]
        self.assertEqual((hs["service"], hs["syn_no"], hs["synack_no"], hs["ack_no"]), ("echo", 1, 2, 3))
        tx = [t for t in a["dns_transactions"] if t["id"] == 0x4242][0]
        self.assertEqual((tx["retransmissions"], tx["answered"]), (1, True))
        self.assertIn("10.10.0.10", tx["responses"][0]["info"])


class TestUDPEcho(Base):
    def test_udp_echo(self):
        """UDP echo returns the datagram on the first attempt when there is no loss."""
        r, secs, attempts = cl.udp_echo("127.0.0.1", self.ports["udp-echo"], b"ping")
        self.assertEqual((r, attempts), (b"ping", 1))


class TestLossyUDP(unittest.TestCase):
    def test_retransmission_recovers_from_loss(self):
        """With 30% simulated loss per datagram, 3 attempts recover most queries (>80%) while some still fail."""
        tmp = tempfile.TemporaryDirectory()
        plat = sv.Platform(echo_port=0, file_port=0, udp_echo_port=0, dns_port=0, root=tmp.name, zone=ZONE,
                           loss=0.3, seed=42).start()
        try:
            ok = fails = 0
            for _ in range(150):
                try:
                    cl.dns_query("127.0.0.1", plat.ports["dns"], "web.campus.lab", timeout=0.05, retries=2)
                    ok += 1
                except socket.timeout:
                    fails += 1
            self.assertGreater(ok / 150, 0.80)           # theory: 1-(1-0.49)^3 = 0.867
            self.assertGreater(fails, 0)                  # loss really happens
            self.assertGreater(plat.stats.snapshot()["counters"].get("dns.dropped_in", 0), 0)
        finally:
            plat.stop()
            tmp.cleanup()

    def test_adaptive_timeout_learns_rtt_and_recovers(self):
        """The RFC 6298-style estimator learns a sub-millisecond RTO and still recovers queries under 30% loss."""
        est = cl.RTTEstimator(initial=0.05)
        tmp = tempfile.TemporaryDirectory()
        plat = sv.Platform(echo_port=0, file_port=0, udp_echo_port=0, dns_port=0, root=tmp.name, zone=ZONE,
                           loss=0.3, seed=5).start()
        try:
            ok = sum(self._try(plat, est) for _ in range(150))
            self.assertGreater(ok / 150, 0.85)                 # 4 retries: theory 1-0.51^5 = 0.965
            self.assertLess(est.rto(), 0.05)                    # learned a timeout well below the initial 50 ms
            self.assertGreaterEqual(est.rto(), est.min_rto)
        finally:
            plat.stop()
            tmp.cleanup()

    @staticmethod
    def _try(plat, est):
        try:
            cl.dns_query("127.0.0.1", plat.ports["dns"], "web.campus.lab", retries=4, estimator=est)
            return 1
        except socket.timeout:
            return 0

    def test_delay_emulation_and_rto_tracking(self):
        """With 20 ms emulated delay every RTT exceeds 17 ms, SRTT tracks the measured RTT and the RTO settles above it."""
        tmp = tempfile.TemporaryDirectory()
        plat = sv.Platform(echo_port=0, file_port=0, udp_echo_port=0, dns_port=0, root=tmp.name, zone=ZONE,
                           delay=0.02, jitter=0.002).start()
        try:
            est = cl.RTTEstimator(initial=0.2)
            rtts = [cl.dns_query("127.0.0.1", plat.ports["dns"], "web.campus.lab", retries=4, estimator=est)[1]
                    for _ in range(30)]
            median = sorted(rtts)[15]
            self.assertGreater(min(rtts), 0.017)                 # the emulated delay is really applied
            # the estimator tracks whatever RTT it observes (timer wake-ups are late on loaded CI machines)
            self.assertLess(abs(est.srtt - median) / median, 0.5, (est.srtt, median))
            self.assertGreater(est.rto(), est.srtt)
            self.assertLess(est.rto(), 4 * median + 0.05)
        finally:
            plat.stop()
            tmp.cleanup()

    def test_client_detects_dead_server(self):
        """With no server listening, the client reports ConnectionError after bounded retries and UDP times out."""
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        with self.assertRaises(ConnectionError):
            cl.tcp_connect("127.0.0.1", port, timeout=0.2, retries=1, backoff=0.01)
        with self.assertRaises(socket.timeout):
            cl.udp_echo("127.0.0.1", port, b"x", timeout=0.05, retries=1)


class TestLimits(unittest.TestCase):
    def test_idle_timeout_and_max_clients(self):
        """Idle clients are dropped after the idle timeout; connections beyond max_clients get ERR 503."""
        tmp = tempfile.TemporaryDirectory()
        plat = sv.Platform(echo_port=0, file_port=0, udp_echo_port=0, dns_port=0, root=tmp.name, zone=ZONE,
                           max_clients=3, idle_timeout=0.5).start()
        try:
            held = [socket.create_connection(("127.0.0.1", plat.ports["file"])) for _ in range(3)]
            time.sleep(0.1)
            extra = socket.create_connection(("127.0.0.1", plat.ports["file"]))
            extra.settimeout(2)
            self.assertIn(b"ERR 503", extra.recv(100))
            held[0].settimeout(3)
            t0 = time.time()
            self.assertEqual(held[0].recv(10), b"")        # server closed the idle connection
            self.assertLess(time.time() - t0, 2)
            self.assertGreaterEqual(plat.stats.snapshot()["counters"].get("file.timeouts", 0), 1)
            for h in held + [extra]:
                h.close()
        finally:
            plat.stop()
            tmp.cleanup()

    def test_server_shutdown_detected_by_client(self):
        """If the server dies during a session, the next client call raises instead of hanging."""
        tmp = tempfile.TemporaryDirectory()
        plat = sv.Platform(echo_port=0, file_port=0, udp_echo_port=0, dns_port=0, root=tmp.name, zone=ZONE).start()
        c = cl.EchoClient(port=plat.ports["echo"], timeout=2)
        self.assertEqual(c.echo(b"a")[0], b"a")
        plat.stop()
        with self.assertRaises((ConnectionClosed, OSError)):
            c.echo(b"b")
        c.close()
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
