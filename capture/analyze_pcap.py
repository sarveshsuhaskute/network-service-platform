#!/usr/bin/env python3
"""Decode a tcpdump capture of the platform (Wireshark-style) using only the standard library.

Reads classic pcap files (tcpdump -w), link types NULL/LOOP (macOS lo0) and Ethernet (Linux lo), IPv4,
TCP and UDP, and decodes DNS over UDP and TCP. Writes results/capture_analysis.json and a text report with:
  * a packet list (time, src -> dst, protocol, info) like Wireshark's top pane
  * TCP three-way handshakes and connection teardowns with their timing
  * every DNS transaction, including retransmissions (same ID sent again) and TC -> TCP fallback
Usage: python3 capture/analyze_pcap.py [results/capture.pcap]
"""
import json
import os
import socket
import struct
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
import dns_wire as dw  # noqa: E402

PORTS = {19001: "echo", 19002: "file", 19003: "udp-echo", 19053: "dns", 9001: "echo", 9002: "file", 9003: "udp-echo",
         9053: "dns"}
FLAG_NAMES = [(0x02, "SYN"), (0x10, "ACK"), (0x01, "FIN"), (0x04, "RST"), (0x08, "PSH")]


def read_pcap(path):
    with open(path, "rb") as f:
        data = f.read()
    magic = data[:4]
    if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
        end = "<"
    elif magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"):
        end = ">"
    else:
        raise ValueError("not a classic pcap file (pcapng is not supported; capture with tcpdump -w)")
    nano = magic in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d")
    linktype = struct.unpack(end + "I", data[20:24])[0]
    off, pkts = 24, []
    while off + 16 <= len(data):
        sec, frac, incl, _orig = struct.unpack(end + "IIII", data[off:off + 16])
        off += 16
        pkts.append((sec + frac / (1e9 if nano else 1e6), data[off:off + incl]))
        off += incl
    return linktype, pkts


def strip_link(linktype, frame):
    if linktype == 0:                                   # DLT_NULL: 4-byte address family, host byte order
        fam = struct.unpack("<I", frame[:4])[0]
        if fam not in (2,):
            fam = struct.unpack(">I", frame[:4])[0]
        return frame[4:] if fam == 2 else None
    if linktype == 108:                                 # DLT_LOOP: network byte order
        return frame[4:] if struct.unpack(">I", frame[:4])[0] == 2 else None
    if linktype == 1:                                   # Ethernet
        return frame[14:] if frame[12:14] == b"\x08\x00" else None
    if linktype in (101, 228):                          # raw IPv4
        return frame
    return None


def parse(path):
    linktype, raw = read_pcap(path)
    out, t0 = [], raw[0][0] if raw else 0
    for ts, frame in raw:
        ip = strip_link(linktype, frame)
        if not ip or ip[0] >> 4 != 4:
            continue
        ihl = (ip[0] & 0xF) * 4
        proto = ip[9]
        src, dst = socket.inet_ntoa(ip[12:16]), socket.inet_ntoa(ip[16:20])
        total = struct.unpack("!H", ip[2:4])[0]
        seg = ip[ihl:total]
        p = {"t": round(ts - t0, 6), "src": src, "dst": dst, "ip_len": total}
        if proto == 6 and len(seg) >= 20:
            sport, dport, seq, ack, offf, flags, win = struct.unpack("!HHIIBBH", seg[:16])
            hl = (offf >> 4) * 4
            p.update(proto="TCP", sport=sport, dport=dport, seq=seq, ack=ack, flags=flags, win=win, payload=seg[hl:])
        elif proto == 17 and len(seg) >= 8:
            sport, dport, ulen = struct.unpack("!HHH", seg[:6])
            p.update(proto="UDP", sport=sport, dport=dport, payload=seg[8:ulen])
        else:
            continue
        out.append(p)
    return out


def flag_str(f):
    return ",".join(n for b, n in FLAG_NAMES if f & b) or "-"


def service(p):
    return PORTS.get(p["dport"]) or PORTS.get(p["sport"]) or "?"


def dns_info(msg):
    try:
        qid, flags = struct.unpack("!HH", msg[:4])
        if flags & 0x8000:
            r = dw.parse_response(msg)
            tc = " TC" if r.tc else ""
            ans = ", ".join(f"{dw.QTYPE_NAMES.get(a.rtype, a.rtype)} {a.value}" for a in r.answers[:3])
            more = f" (+{len(r.answers) - 3} more)" if len(r.answers) > 3 else ""
            return qid, True, f"DNS response 0x{qid:04x} {dw.RCODE_NAMES[r.rcode]}{tc} {r.qname}: {ans}{more}".rstrip(": ")
        q = dw.parse_query(msg)
        return qid, False, f"DNS query 0x{qid:04x} {dw.QTYPE_NAMES.get(q.qtype, q.qtype)} {q.qname}"
    except Exception as e:                                 # malformed on purpose in some scenarios
        return None, None, f"DNS (malformed: {e})"


def analyze(pkts):
    rows, dns_events, conns = [], [], {}
    tcp_dns_buf = {}
    for i, p in enumerate(pkts, 1):
        svc = service(p)
        info = ""
        if p["proto"] == "TCP":
            key = tuple(sorted([(p["src"], p["sport"]), (p["dst"], p["dport"])]))
            c = conns.setdefault(key, {"service": svc, "client": None, "events": []})
            f = p["flags"]
            if f & 0x02 and not f & 0x10:
                c["client"] = (p["src"], p["sport"])
            c["events"].append({"no": i, "t": p["t"], "flags": flag_str(f), "len": len(p["payload"]),
                                "dir": "c->s" if c["client"] == (p["src"], p["sport"]) else "s->c"})
            info = f"{p['sport']} -> {p['dport']} [{flag_str(f)}] Seq={p['seq']} Ack={p['ack']} Win={p['win']} Len={len(p['payload'])}"
            if svc == "dns" and p["payload"]:
                buf = tcp_dns_buf.setdefault((p["src"], p["sport"]), b"") + p["payload"]
                while len(buf) >= 2 and len(buf) >= 2 + struct.unpack("!H", buf[:2])[0]:
                    n = struct.unpack("!H", buf[:2])[0]
                    qid, is_resp, txt = dns_info(buf[2:2 + n])
                    info += f"  | {txt} (over TCP)"
                    client = f"{p['dst']}:{p['dport']}" if is_resp else f"{p['src']}:{p['sport']}"
                    dns_events.append({"no": i, "t": p["t"], "id": qid, "response": is_resp, "transport": "tcp", "info": txt,
                                       "client": client})
                    buf = buf[2 + n:]
                tcp_dns_buf[(p["src"], p["sport"])] = buf
            elif svc == "echo" and len(p["payload"]) >= 4:
                info += f"  | echo frame len={struct.unpack('!I', p['payload'][:4])[0]}"
            elif svc == "file" and p["payload"][:8].isascii() and p["payload"]:
                info += "  | " + p["payload"][:60].decode("ascii", "replace").split("\n")[0]
        else:
            info = f"{p['sport']} -> {p['dport']} Len={len(p['payload'])}"
            if svc == "dns":
                qid, is_resp, txt = dns_info(p["payload"])
                info += "  | " + txt
                dns_events.append({"no": i, "t": p["t"], "id": qid, "response": is_resp, "transport": "udp", "info": txt,
                                   "client": f"{p['dst'] if is_resp else p['src']}:{p['dport'] if is_resp else p['sport']}"})
            elif svc == "udp-echo":
                info += "  | " + p["payload"][:40].decode("utf-8", "replace")
        rows.append({"no": i, "t": p["t"], "src": f"{p['src']}:{p['sport']}", "dst": f"{p['dst']}:{p['dport']}",
                     "proto": p["proto"], "service": svc, "len": p["ip_len"], "info": info})

    handshakes = []
    for c in conns.values():
        ev = c["events"]
        syn = next((e for e in ev if e["flags"] == "SYN"), None)
        synack = next((e for e in ev if e["flags"] == "SYN,ACK"), None)
        ack = next((e for e in ev if syn and synack and e["t"] >= synack["t"] and e["flags"] == "ACK" and e["dir"] == "c->s"), None)
        fins = [e for e in ev if "FIN" in e["flags"]]
        rsts = [e for e in ev if "RST" in e["flags"]]
        if syn and synack:
            handshakes.append({"service": c["service"], "syn_no": syn["no"], "synack_no": synack["no"],
                               "ack_no": ack["no"] if ack else None,
                               "handshake_ms": round(((ack or synack)["t"] - syn["t"]) * 1000, 3),
                               "data_packets": sum(1 for e in ev if e["len"]), "fin_packets": len(fins),
                               "rst_packets": len(rsts), "packets": len(ev)})

    # DNS transactions: a transaction is (ID, transport, client endpoint) – each query uses its own socket, so the
    # client port separates queries that happen to share an ID; >1 query in one transaction = retransmission
    tx = {}
    for e in dns_events:
        if e["id"] is None:
            continue
        key = (e["id"], e["transport"], e["client"])
        t = tx.setdefault(key, {"id": e["id"], "transport": e["transport"], "client": e["client"],
                                "queries": [], "responses": []})
        (t["responses"] if e["response"] else t["queries"]).append({"no": e["no"], "t": e["t"], "info": e["info"]})
    transactions = []
    for t in tx.values():
        q = t["queries"]
        transactions.append({**t, "retransmissions": max(0, len(q) - 1),
                             "gaps_ms": [round((b["t"] - a["t"]) * 1000, 1) for a, b in zip(q, q[1:])],
                             "answered": bool(t["responses"]),
                             "truncated": any(" TC " in r["info"] for r in t["responses"])})
    proto_counts = {}
    for r in rows:
        k = f"{r['proto']}/{r['service']}"
        pc = proto_counts.setdefault(k, {"packets": 0, "bytes": 0})
        pc["packets"] += 1
        pc["bytes"] += r["len"]
    return {"packets": rows, "handshakes": handshakes, "dns_transactions": transactions, "per_protocol": proto_counts}


def text_report(a):
    L = ["No.  Time(s)   Source                 Destination            Proto  Info"]
    for r in a["packets"]:
        L.append(f"{r['no']:<4} {r['t']:<9.4f} {r['src']:<22} {r['dst']:<22} {r['proto']:<6} {r['info'][:150]}")
    L += ["", "TCP connections (three-way handshake / teardown):"]
    for h in a["handshakes"]:
        L.append(f"  {h['service']:<5} SYN #{h['syn_no']} -> SYN,ACK #{h['synack_no']} -> ACK #{h['ack_no']}  "
                 f"handshake {h['handshake_ms']} ms, {h['data_packets']} data pkts, {h['fin_packets']} FIN, {h['rst_packets']} RST")
    L += ["", "DNS transactions:"]
    for t in sorted(a["dns_transactions"], key=lambda t: t["queries"][0]["t"] if t["queries"] else 0):
        first = t["queries"][0]["info"] if t["queries"] else "(no query seen)"
        L.append(f"  0x{t['id']:04x} {t['transport']}: {first}; retransmissions={t['retransmissions']} "
                 f"gaps_ms={t['gaps_ms']} answered={t['answered']} truncated={t['truncated']}")
    L += ["", "Per protocol / service:"]
    for k, v in sorted(a["per_protocol"].items()):
        L.append(f"  {k:<14} {v['packets']:>5} packets {v['bytes']:>9} bytes")
    return "\n".join(L)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "results", "capture.pcap")
    a = analyze(parse(path))
    with open(os.path.join(ROOT, "results", "capture_analysis.json"), "w") as f:
        json.dump(a, f, indent=1)
    txt = text_report(a)
    with open(os.path.join(ROOT, "results", "capture_report.txt"), "w") as f:
        f.write(txt + "\n")
    print(txt)


if __name__ == "__main__":
    main()
