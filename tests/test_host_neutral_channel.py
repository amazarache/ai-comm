"""Any host label and any number of sessions per host work the same way."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ai_comm as ac  # noqa: E402


class HostNeutralChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ai-comm-neutral-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "channel"

    def draft(self, act, to, **extra):
        return dict({"act": act, "to": to, "topic": "handshake",
                     "summary": "Host-neutral handshake check."}, **extra)

    def handshake(self, channel, first, second):
        ping = channel.send(first, first + "-session", "ping-" + second,
                            self.draft("PING", [second]))["message"]
        pong = channel.send(second, second + "-session", "pong-" + first,
                            self.draft("PONG", [first], reply_to=ping["id"]))["message"]
        ack = channel.send(first, first + "-session", "ack-" + second,
                           self.draft("ACK", [second], reply_to=pong["id"]))["message"]
        self.assertEqual(ping["nonce"], pong["echo"])
        self.assertEqual(pong["nonce"], ack["echo"])
        page = channel.inbox(second, second + "-session")
        self.assertIn(ack["id"], [m["id"] for m in page["messages"]])
        channel.ack(second, second + "-session", page["through"])

    def test_third_participant_with_an_unseen_host_label(self):
        ac.Channel.init(self.path, "Three hosts", ["agent-a", "agent-b", "agent-c"])
        channel = ac.Channel(self.path)
        channel.join("agent-a", "host-a", "agent-a-session")
        channel.join("agent-b", "host-b", "agent-b-session")
        channel.join("agent-c", "some-new-host", "agent-c-session")
        current = channel.snapshot().current
        self.assertEqual({"agent-a": "host-a", "agent-b": "host-b", "agent-c": "some-new-host"},
                         {handle: record["platform"] for handle, record in current.items()})
        self.handshake(channel, "agent-a", "agent-c")
        self.handshake(channel, "agent-c", "agent-b")

    def test_two_sessions_on_the_same_host_use_distinct_handles(self):
        ac.Channel.init(self.path, "Same host twice", ["agent-a", "agent-b"])
        channel = ac.Channel(self.path)
        channel.join("agent-a", "host-a", "agent-a-session")
        channel.join("agent-b", "host-a", "agent-b-session")
        current = channel.snapshot().current
        self.assertEqual({"host-a"}, {record["platform"] for record in current.values()})
        self.handshake(channel, "agent-a", "agent-b")
        with self.assertRaises(ac.ProtocolError):
            channel.join("agent-a", "host-a", "another-session")


if __name__ == "__main__":
    unittest.main()
