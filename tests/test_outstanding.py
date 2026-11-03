"""Tests for the read-only `outstanding` delivery-status command."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "ai_comm.py"
sys.path.insert(0, str(SCRIPT.parent))
import ai_comm as ac  # noqa: E402


class OutstandingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ai-comm-outstanding-")
        self.path = Path(self.temp.name) / "channel"
        ac.Channel.init(self.path, "Review a generic change", ["alpha", "beta"])
        self.channel = ac.Channel(self.path)
        self.channel.join("alpha", "host-a", "alpha-session")
        self.channel.join("beta", "host-b", "beta-session")
        self.keys = 0

    def tearDown(self):
        self.temp.cleanup()

    def send(self, author="alpha", act="ASK", **extra):
        self.keys += 1
        to = ["beta"] if author == "alpha" else ["alpha"]
        draft = dict({"act": act, "to": to, "topic": "review", "summary": "Review this change."}, **extra)
        return self.channel.send(author, author + "-session", f"k{self.keys}", draft)["message"]

    def out(self, **kwargs):
        return self.channel.outstanding("alpha", "alpha-session", "beta", **kwargs)

    def cli(self, *args):
        return subprocess.run([sys.executable, "-I", str(SCRIPT), "--channel", str(self.path), *args],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)

    def code(self, call):
        with self.assertRaises(ac.ProtocolError) as caught:
            call()
        return caught.exception.details["code"]

    def test_absent_receipt_reports_every_message_unseen(self):
        first = self.send()
        self.send()
        result = self.out()
        self.assertEqual({"handle": "beta", "generation": 1,
                          "receipt": {"present": False, "generation": 1, "read_through": 0, "seen_through": 0}},
                         result["peer"])
        self.assertEqual((2, 2), (result["count"], result["matched"]))
        self.assertEqual([first["id"], "00000002-alpha"], [m["id"] for m in result["messages"]])
        self.assertEqual({"unseen"}, {m["state"] for m in result["messages"]})
        self.assertEqual({"id", "seq", "act", "ts", "state", "replied"}, set(result["messages"][0]))
        self.assertRegex(result["as_of"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_seen_then_acknowledged(self):
        self.send()
        self.channel.inbox("beta", "beta-session")
        result = self.out()
        self.assertEqual(["seen"], [m["state"] for m in result["messages"]])
        self.assertEqual((0, 1), (result["peer"]["receipt"]["read_through"],
                                  result["peer"]["receipt"]["seen_through"]))
        self.channel.ack("beta", "beta-session", 1)
        result = self.out()
        self.assertEqual((0, 0, []), (result["count"], result["matched"], result["messages"]))

    def test_takeover_normalizes_seen_without_rewriting_the_receipt(self):
        self.send()
        self.send()
        self.channel.inbox("beta", "beta-session")
        self.channel.ack("beta", "beta-session", 1)
        self.send()
        self.channel.inbox("beta", "beta-session")
        receipt = self.channel.runtime / "beta.json"
        stored = receipt.read_bytes()
        self.channel.join("beta", "host-b", "beta-session-2", takeover=True)
        result = self.out()
        self.assertEqual(2, result["peer"]["generation"])
        self.assertEqual({"present": True, "generation": 2, "read_through": 1, "seen_through": 1},
                         result["peer"]["receipt"])
        self.assertEqual(["unseen", "unseen"], [m["state"] for m in result["messages"]])
        self.assertEqual(stored, receipt.read_bytes())

    def test_corrupt_or_symlinked_receipt_is_an_error(self):
        self.send()
        receipt = self.channel.runtime / "beta.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text('{"v": 2, "generation": 1, "read": 5, "seen": 1}')
        self.assertEqual("corrupt_receipt_bounds", self.code(self.out))
        receipt.unlink()
        target = Path(self.temp.name) / "elsewhere.json"
        target.write_text('{"v": 2, "generation": 1, "read": 0, "seen": 0}')
        receipt.symlink_to(target)
        self.assertEqual("receipt_symlink_refused", self.code(self.out))

    def test_identity_peer_and_argument_validation(self):
        self.assertEqual("unknown_peer", self.code(
            lambda: self.channel.outstanding("alpha", "alpha-session", "gamma")))
        self.assertEqual("peer_is_caller", self.code(
            lambda: self.channel.outstanding("alpha", "alpha-session", "alpha")))
        self.assertEqual("unregistered_or_stale_session", self.code(
            lambda: self.channel.outstanding("alpha", "old-session", "beta")))
        self.assertEqual("invalid_handle", self.code(
            lambda: self.channel.outstanding("alpha", "alpha-session", "Beta!")))
        self.assertEqual("invalid_outstanding_state", self.code(lambda: self.out(state_filter="read")))
        self.assertEqual("invalid_limit", self.code(lambda: self.out(limit=0)))
        self.assertEqual("invalid_limit", self.code(lambda: self.out(limit=101)))
        self.assertEqual("invalid_after", self.code(lambda: self.out(after=1)))
        self.send()
        self.assertEqual(1, self.out(after=1)["count"])
        self.assertEqual("invalid_after", self.code(lambda: self.out(after=-1)))

    def test_peer_polling_never_changes_the_result(self):
        self.send()
        before = self.out()
        self.channel.wait("beta", "beta-session", timeout=0.1, new_since=0)
        self.channel.notifications("beta", "beta-session", 0)
        self.channel.snapshot().state(actor="beta", session="beta-session")
        self.channel._record_poll("beta", "beta-session", "state")
        after = self.out()
        self.assertEqual(before["peer"], after["peer"])
        self.assertEqual(before["messages"], after["messages"])

    def test_later_sends_keep_the_oldest_entry_and_other_traffic_is_excluded(self):
        first = self.send()
        self.send(author="beta")
        self.channel.send("alpha", "alpha-session", "to-both",
                          {"act": "ASK", "to": ["alpha", "beta"], "topic": "review", "summary": "Both."})
        self.send()
        result = self.out(limit=1)
        self.assertEqual(first["id"], result["messages"][0]["id"])
        self.assertEqual((3, 3), (result["count"], result["matched"]))
        reverse = self.channel.outstanding("beta", "beta-session", "alpha")
        self.assertEqual(["00000002-beta"], [m["id"] for m in reverse["messages"]])

    def test_replied_classification_and_filters(self):
        asked = self.send()
        self.send()
        self.send(author="beta", reply_to=asked["id"])
        self.channel.inbox("beta", "beta-session", limit=1)
        result = self.out()
        self.assertEqual([(1, "seen", True), (2, "unseen", False)],
                         [(m["seq"], m["state"], m["replied"]) for m in result["messages"]])
        self.assertEqual([2], [m["seq"] for m in self.out(unreplied=True)["messages"]])
        self.assertEqual([1], [m["seq"] for m in self.out(state_filter="seen")["messages"]])
        self.assertEqual([2], [m["seq"] for m in self.out(state_filter="unseen")["messages"]])
        self.assertEqual([], self.out(state_filter="seen", unreplied=True)["messages"])
        self.assertEqual([2], [m["seq"] for m in self.out(after=1)["messages"]])
        combined = self.out(state_filter="unseen", unreplied=True, after=2)
        self.assertEqual((2, 0, []), (combined["count"], combined["matched"], combined["messages"]))

    def test_reading_writes_nothing(self):
        self.send()
        self.channel.inbox("beta", "beta-session")
        receipt = self.channel.runtime / "beta.json"
        poll_dir = self.channel.runtime / "poll"
        stored = receipt.read_bytes()
        polls = sorted((p.name, p.read_bytes()) for p in poll_dir.iterdir())
        head = self.channel.snapshot().head
        self.out()
        result = self.cli("outstanding", "--as", "alpha", "--session", "alpha-session", "--to", "beta")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(stored, receipt.read_bytes())
        self.assertFalse((self.channel.runtime / "alpha.json").exists())
        self.assertEqual(polls, sorted((p.name, p.read_bytes()) for p in poll_dir.iterdir()))
        self.assertEqual(head, self.channel.snapshot().head)

    def test_cli_output_and_argument_errors(self):
        self.send()
        result = self.cli("outstanding", "--as", "alpha", "--session", "alpha-session", "--to", "beta",
                          "--state", "unseen", "--unreplied", "--after", "0", "--limit", "5")
        self.assertEqual(0, result.returncode, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual({"head", "as_of", "peer", "count", "matched", "messages"}, set(value))
        self.assertEqual(["00000001-alpha"], [m["id"] for m in value["messages"]])
        bad = self.cli("outstanding", "--as", "alpha", "--session", "alpha-session", "--to", "beta",
                       "--state", "read")
        self.assertEqual(2, bad.returncode)
        self.assertEqual("invalid_cli_arguments", json.loads(bad.stderr)["code"])
        missing = self.cli("outstanding", "--as", "alpha", "--session", "alpha-session")
        self.assertEqual("invalid_cli_arguments", json.loads(missing.stderr)["code"])

    def test_excluded_acts_leave_the_set_before_the_limit(self):
        self.send(act="ASK")
        ping = self.channel.send("alpha", "alpha-session", "ping", {"act": "PING", "to": ["beta"], "topic": "review",
                                                                    "summary": "Ping."})["message"]
        pong = self.channel.send("beta", "beta-session", "pong", {"act": "PONG", "to": ["alpha"], "topic": "review",
                                                                  "summary": "Pong.", "reply_to": ping["id"]})["message"]
        for i in range(12):
            self.channel.send("alpha", "alpha-session", f"ack{i}", {"act": "ACK", "to": ["beta"], "topic": "review",
                                                                    "summary": "Ack.", "reply_to": pong["id"]})
        asked = self.send(act="ASK")
        everything = self.out(limit=100)
        self.assertEqual(15, everything["count"])
        self.assertEqual(12, sum(1 for m in everything["messages"] if m["act"] == "ACK"))
        filtered = self.out(exclude_acts=["ACK"], unreplied=True, after=1, limit=1)
        # The PING is replied (PONG), so --unreplied skips it; the first match lies beyond 12 ACKs.
        self.assertEqual((3, 1, asked["id"]), (filtered["count"], filtered["matched"], filtered["messages"][0]["id"]))
        filtered = self.out(exclude_acts=["ACK", "PING"], after=1, limit=1)
        self.assertEqual((2, 1, asked["id"]), (filtered["count"], filtered["matched"], filtered["messages"][0]["id"]))
        result = self.cli("outstanding", "--as", "alpha", "--session", "alpha-session", "--to", "beta",
                          "--exclude-act", "ACK", "--exclude-act", "PING", "--limit", "5")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["ASK", "ASK"], [m["act"] for m in json.loads(result.stdout)["messages"]])
        self.assertEqual("unknown_act", self.code(lambda: self.out(exclude_acts=["NOPE"])))
        bad = self.cli("outstanding", "--as", "alpha", "--session", "alpha-session", "--to", "beta",
                       "--exclude-act", "nope")
        self.assertEqual("unknown_act", json.loads(bad.stderr)["code"])

    def test_terminal_ack_alone_is_excluded(self):
        ping = self.channel.send("alpha", "alpha-session", "ping", {"act": "PING", "to": ["beta"], "topic": "review",
                                                                    "summary": "Ping."})["message"]
        self.channel.inbox("beta", "beta-session")
        self.channel.ack("beta", "beta-session", ping["seq"])
        pong = self.channel.send("beta", "beta-session", "pong", {"act": "PONG", "to": ["alpha"], "topic": "review",
                                                                  "summary": "Pong.", "reply_to": ping["id"]})["message"]
        self.channel.send("alpha", "alpha-session", "ack", {"act": "ACK", "to": ["beta"], "topic": "review",
                                                            "summary": "Ack.", "reply_to": pong["id"]})
        self.assertEqual(1, self.out()["count"])
        self.assertEqual((0, 0, []), tuple(self.out(exclude_acts=["ACK"])[k] for k in ("count", "matched", "messages")))

    def test_long_history_has_no_window_and_filters_search_everything(self):
        for _ in range(301):
            self.send()
        replies = [self.send(author="beta", reply_to=f"{seq:08d}-alpha")["seq"] for seq in range(1, 13)]
        self.assertEqual(list(range(302, 314)), replies)
        self.channel.inbox("beta", "beta-session", limit=100)
        result = self.out(limit=1)
        self.assertEqual(("00000001-alpha", 301, 301), (result["messages"][0]["id"], result["count"],
                                                         result["matched"]))
        self.assertEqual(13, self.out(unreplied=True, limit=1)["messages"][0]["seq"])
        self.assertEqual(101, self.out(state_filter="unseen", limit=1)["messages"][0]["seq"])
        found = self.out(state_filter="unseen", unreplied=True, after=250, limit=1)
        self.assertEqual((251, 51), (found["messages"][0]["seq"], found["matched"]))
        page = self.out(limit=100)
        self.assertEqual(100, len(page["messages"]))
        self.assertLess(len(ac.canonical(page)), 32 * 1024)


if __name__ == "__main__":
    unittest.main()
