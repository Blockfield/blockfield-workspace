"""RCON packet boundaries must not truncate dumps or contaminate later replies."""

import struct
import unittest
from unittest.mock import patch

from rcon import Rcon


def packet(request, body):
    payload = struct.pack("<ii", request, 0) + body.encode() + b"\0\0"
    return struct.pack("<i", len(payload)) + payload


class Socket:
    def __init__(self, replies):
        self.replies = bytearray(b"".join(replies))
        self.sent = []

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, count):
        count = min(count, 7)
        value = bytes(self.replies[:count])
        del self.replies[:count]
        return value


class MultipartReplies(unittest.TestCase):
    def test_large_dump_then_another_command_stay_separate(self):
        first = '{"geometry":"' + "a" * 10000 + '"}'
        sock = Socket(
            [packet(1, "")]
            + [packet(2, first[i : i + 4096]) for i in range(0, len(first), 4096)]
            + [
                packet(3, "Unknown request 0"),
                packet(4, "20.0f"),
                packet(5, "Unknown request 0"),
            ]
        )
        with patch("rcon.socket.create_connection", return_value=sock):
            client = Rcon(pw="test-only")
        self.assertEqual(client.cmd("bf bodypose dump target"), first)
        self.assertEqual(client.cmd("data get entity target Health"), "20.0f")
        self.assertFalse(sock.replies)
        self.assertEqual(
            [struct.unpack("<ii", data[4:12])[1] for data in sock.sent],
            [3, 2, 0, 2, 0],
        )
        for data in sock.sent:
            self.assertEqual(struct.unpack("<i", data[:4])[0] + 4, len(data))

    def test_missing_response_boundary_fails_instead_of_returning_partial_data(self):
        sock = Socket([packet(1, ""), packet(2, "partial")])
        with patch("rcon.socket.create_connection", return_value=sock):
            client = Rcon(pw="test-only")
        with self.assertRaises(EOFError):
            client.cmd("dump")

    def test_unexpected_request_id_is_rejected(self):
        sock = Socket([packet(1, ""), packet(99, "stale")])
        with patch("rcon.socket.create_connection", return_value=sock):
            client = Rcon(pw="test-only")
        with self.assertRaisesRegex(RuntimeError, "request id"):
            client.cmd("dump")
