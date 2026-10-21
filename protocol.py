"""Shared wire helpers for the TCP services (framing, exact reads, errors)."""
import hashlib
import struct

HDR = struct.Struct("!I")          # 4-byte big-endian length prefix
MAX_FRAME = 16 * 1024 * 1024       # largest echo frame accepted (16 MiB)
MAX_LINE = 1024                    # longest command line in the file protocol
CHUNK = 64 * 1024


class ProtocolError(Exception):
    """Peer sent something that violates the protocol."""


class ConnectionClosed(Exception):
    """Peer closed the connection. `partial` is True if it happened mid-message."""

    def __init__(self, msg="connection closed", partial=False):
        super().__init__(msg)
        self.partial = partial


def recv_exact(sock, n):
    """Read exactly n bytes or raise ConnectionClosed."""
    if n == 0:
        return b""
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
        k = sock.recv_into(view[got:], n - got)
        if k == 0:
            raise ConnectionClosed(f"peer closed after {got}/{n} bytes", partial=got > 0)
        got += k
    return bytes(buf)


def send_frame(sock, payload):
    sock.sendall(HDR.pack(len(payload)) + payload)


def recv_frame(sock):
    (n,) = HDR.unpack(recv_exact(sock, HDR.size))
    if n > MAX_FRAME:
        raise ProtocolError(f"frame of {n} bytes exceeds limit {MAX_FRAME}")
    try:
        return recv_exact(sock, n)
    except ConnectionClosed as e:
        raise ConnectionClosed(str(e), partial=True)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK * 4)
            if not b:
                break
            h.update(b)
    return h.hexdigest()
