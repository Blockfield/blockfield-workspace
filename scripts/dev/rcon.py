import socket
import struct
import sys


class Rcon:
    def __init__(self, host="127.0.0.1", port=25575, pw="leaks", timeout=120):
        self.s = socket.create_connection((host, port), timeout=timeout)
        self.i = 0
        if self._send(3, pw) is None:
            raise SystemExit("rcon auth failed")

    def _pack(self, t, body):
        self.i += 1
        p = struct.pack("<ii", self.i, t) + body.encode("utf8") + b"\x00\x00"
        return struct.pack("<i", len(p)) + p, self.i

    def _recv(self):
        ln = struct.unpack("<i", self._read(4))[0]
        d = self._read(ln)
        rid, t = struct.unpack("<ii", d[:8])
        return rid, t, d[8:-2].decode("utf8", "replace")

    def _read(self, n):
        b = b""
        while len(b) < n:
            c = self.s.recv(n - len(b))
            if not c:
                raise EOFError
            b += c
        return b

    def _send(self, t, body):
        pkt, rid = self._pack(t, body)
        self.s.sendall(pkt)
        r, _, payload = self._recv()
        return None if r == -1 else payload

    def cmd(self, c):
        command, rid = self._pack(2, c)
        # Minecraft replies to unknown type 0 after all chunks of the preceding command.
        boundary, end = self._pack(0, "")
        self.s.sendall(command)
        request, _, payload = self._recv()
        if request != rid:
            raise RuntimeError(f"unexpected RCON request id: {request}")
        # Wait for the first reply: Minecraft rejects coalesced request packets.
        self.s.sendall(boundary)
        chunks = [payload]
        while True:
            request, _, payload = self._recv()
            if request == end:
                return "".join(chunks)
            if request != rid:
                raise RuntimeError(f"unexpected RCON request id: {request}")
            chunks.append(payload)


if __name__ == "__main__":
    r = Rcon()
    for c in sys.argv[1:]:
        print(">", c)
        print(r.cmd(c))
