import concurrent.futures
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ai_comm as ac

SCRIPT = Path(ac.__file__)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ai-comm-test-")
        self.root = Path(self.temp.name)
        self.path = self.root / "channel"
        ac.Channel.init(self.path, "Review a generic change", ["alpha", "beta"])
        self.channel = ac.Channel(self.path)
        self.channel.join("alpha", "codex", "alpha-session")
        self.channel.join("beta", "claude", "beta-session")

    def tearDown(self):
        self.temp.cleanup()

    def draft(self, act="ASK", to=None, **extra):
        return dict({"act": act, "to": to or ["beta"], "topic": "review",
                     "summary": "Review this precise change."}, **extra)

    def send(self, key="one", act="ASK", author="alpha", **extra):
        return self.channel.send(author, author + "-session", key,
                                 self.draft(act, to=["beta" if author == "alpha" else "alpha"], **extra))["message"]

    def cli(self, *args, data=None, cwd=None, script=SCRIPT):
        return subprocess.run([sys.executable, str(script), "--channel", str(self.path), *args],
                              input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              cwd=cwd, timeout=30)

    def test_send_snapshot_and_idempotent_retry(self):
        draft = self.draft(body="A value is approximately 2×, not greater than 2×.")
        first = self.channel.send("alpha", "alpha-session", "retry", draft)
        second = self.channel.send("alpha", "alpha-session", "retry", draft)
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["message"], second["message"])
        self.assertEqual(first["head"], second["head"])
        with self.assertRaises(ac.ProtocolError):
            self.channel.send("alpha", "alpha-session", "retry", dict(draft, summary="Changed"))
        self.assertEqual(1, len(self.channel.snapshot().messages))

    def test_concurrent_send_and_same_key_retry(self):
        def launch(index):
            return self.cli("send", "--as", "alpha", "--session", "alpha-session", "--key",
                            f"parallel-{index}", "--draft", "-", data=ac.canonical(self.draft()))
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(launch, range(6)))
        self.assertTrue(all(p.returncode == 0 for p in results), [p.stderr for p in results])
        self.assertEqual(list(range(1, 7)), [m["seq"] for m in self.channel.snapshot().messages])
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(launch, [99] * 4))
        self.assertTrue(all(p.returncode == 0 for p in results), [p.stderr for p in results])
        self.assertEqual(1, len({json.loads(p.stdout)["message"]["id"] for p in results}))
        self.assertEqual(7, len(self.channel.snapshot().messages))

    def test_killed_writer_does_not_publish_and_lock_recovers(self):
        before = self.channel.snapshot().head
        code = ("import sys,os;sys.path.insert(0,sys.argv[1]);import ai_comm as a;"
                "c=a.Channel(sys.argv[2]);c._before_publish=lambda commit:os._exit(78);"
                "c.send('alpha','alpha-session','crash',a.parse(sys.stdin.buffer.read()))")
        result = subprocess.run([sys.executable, "-c", code, str(SCRIPT.parent), str(self.path)],
                                input=ac.canonical(self.draft()), timeout=30)
        self.assertEqual(78, result.returncode)
        self.assertEqual(before, self.channel.snapshot().head)
        self.assertEqual([], self.channel.inbox("beta", "beta-session")["messages"])
        self.assertEqual(1, self.send(key="crash")["seq"])

    def test_restart_receipts_and_pagination(self):
        for n in range(4):
            self.send(key=f"n{n}")
        reopened = ac.Channel(self.path)
        one = reopened.wait("beta", "beta-session", timeout=0, limit=2)
        self.assertFalse(one["timed_out"])
        self.assertEqual([1, 2], [m["seq"] for m in one["messages"]])
        self.assertTrue(one["more"])
        reopened.ack("beta", "beta-session", one["through"])
        two = ac.Channel(self.path).inbox("beta", "beta-session")
        self.assertEqual([3, 4], [m["seq"] for m in two["messages"]])
        with self.assertRaises(ac.ProtocolError):
            reopened.ack("beta", "beta-session", 1)
        with self.assertRaises(ac.ProtocolError):
            reopened.ack("beta", "beta-session", 999)

    def test_ack_requires_scanning_and_no_topic_skip(self):
        self.send(key="first", topic="first")
        self.send(key="other", topic="other")
        with self.assertRaises(ac.ProtocolError):
            self.channel.ack("beta", "beta-session", 1)
        page = self.channel.inbox("beta", "beta-session", 1)
        self.assertEqual(1, page["through"])
        with self.assertRaises(ac.ProtocolError):
            self.channel.ack("beta", "beta-session", 2)
        self.channel.ack("beta", "beta-session", 1)
        self.assertEqual("other", self.channel.inbox("beta", "beta-session")["messages"][0]["topic"])

    def test_receipt_corruption_traversal_and_symlink(self):
        for method in (lambda: self.channel.ack("../outside", "x", 0),
                       lambda: self.channel.join("../outside", "codex", "x"),
                       lambda: self.channel.inbox("../outside", "x")):
            with self.assertRaises(ac.ProtocolError):
                method()
        self.assertFalse((self.path / "outside.json").exists())
        receipt = self.channel.runtime / "beta.json"
        receipt.write_bytes(b'{"v":')
        with self.assertRaises(ac.ProtocolError):
            self.channel.inbox("beta", "beta-session")
        receipt.unlink()
        target = self.root / "sentinel"
        target.write_text("untouched")
        receipt.symlink_to(target)
        with self.assertRaises(ac.ProtocolError):
            self.channel.inbox("beta", "beta-session")
        self.assertEqual("untouched", target.read_text())

    def test_takeover_stale_session_and_unchanged_history(self):
        old = self.send()
        with self.assertRaises(ac.ProtocolError):
            self.channel.join("alpha", "codex", "new-session")
        record = self.channel.join("alpha", "codex", "new-session", takeover=True)
        self.assertEqual(2, record["joined"]["generation"])
        for call in (lambda: self.send(key="old"),
                     lambda: self.channel.inbox("alpha", "alpha-session"),
                     lambda: self.channel.ack("alpha", "alpha-session", 0)):
            with self.assertRaises(ac.ProtocolError):
                call()
        fresh = self.channel.send("alpha", "new-session", "one", self.draft())
        self.assertEqual(2, fresh["message"]["generation"])
        self.assertEqual(old, self.channel.snapshot().by_id[old["id"]])

    def test_strict_json_and_input_bounds(self):
        for raw in (b'{"a":1,"a":2}', b'{"x":NaN}', b'[]', b'\xff', b'{}' * ac.MAX_DRAFT):
            with self.assertRaises(ac.ProtocolError):
                ac.parse(raw, ac.MAX_DRAFT)
        for change in ({"unknown": True}, {"act": "EXEC"}, {"summary": "a\nb"},
                       {"summary": "x" * 301}, {"to": ["../bad"]}, {"to": ["nobody"]},
                       {"body": "\x1b[31mbad"}, {"confidence": "certain"}):
            with self.subTest(change=list(change)):
                with self.assertRaises(ac.ProtocolError):
                    self.channel.send("alpha", "alpha-session", "bad", dict(self.draft(), **change))
        self.assertEqual([], self.channel.snapshot().messages)

    def test_takeover_cannot_reuse_current_or_historical_reference(self):
        self.assertTrue(self.channel.join("alpha", "codex", "alpha-session")["duplicate"])
        with self.assertRaises(ac.ProtocolError):
            self.channel.join("alpha", "different-platform", "alpha-session", takeover=True)
        self.channel.join("alpha", "codex", "alpha-new", takeover=True)
        with self.assertRaises(ac.ProtocolError):
            self.channel.join("alpha", "codex", "alpha-session", takeover=True)
        with self.assertRaises(ac.ProtocolError):
            self.send(key="old-process")
        self.assertEqual("alpha-new", self.channel.snapshot().current["alpha"]["session"])

    def test_unicode_formatting_controls_rejected(self):
        for character in ("\u202e", "\u202a", "\u2066", "\u2069", "\u200b", "\u200d", "\u200f", "\ufeff", "\u2028"):
            with self.subTest(codepoint=hex(ord(character))):
                with self.assertRaises(ac.ProtocolError):
                    self.send(key="unicode", body="before" + character + "after")
                with self.assertRaises(ac.ProtocolError):
                    self.channel.put_artifact("alpha", "alpha-session", ("before" + character).encode())
        self.send(body="Readable multilingual text: français, română, 日本語.\nNew line.")

    def test_malformed_stored_artifact_is_not_trusted(self):
        data = b"\xff"
        state = self.channel.snapshot()
        with self.channel.lock():
            self.channel.publish(state.head, {"artifacts/" + ac.sha(data): data}, "fixture", "alpha")
        with self.assertRaises(ac.ProtocolError):
            self.channel.snapshot()

    def test_handshake_nonce_relationships(self):
        ping = self.send(act="PING")
        pong = self.send(key="pong", act="PONG", author="beta", reply_to=ping["id"])
        ack = self.send(key="ack", act="ACK", reply_to=pong["id"])
        self.assertEqual(ping["nonce"], pong["echo"])
        self.assertEqual(pong["nonce"], ack["echo"])
        self.assertNotEqual(ping["nonce"], pong["nonce"])
        with self.assertRaises(ac.ProtocolError):
            self.send(key="self-pong", act="PONG", reply_to=ping["id"])
        with self.assertRaises(ac.ProtocolError):
            self.send(key="bad-pong", act="PONG", author="beta", reply_to=ack["id"])
        state = self.channel.snapshot()
        bad = dict(pong, echo="0" * 32)
        state.messages = state.messages[:1]
        state.by_id = {ping["id"]: ping}
        state.retries = {(ping["from"], ping["generation"], ping["key"]): ping}
        with self.assertRaises(ac.ProtocolError):
            state.add_message(bad)

    def test_correction_sources_questions_and_history_render(self):
        source = {"id": "reading", "kind": "measurement", "ref": "local-record-1", "detail": "Initial observation"}
        first = self.send(act="FACT", summary="Eleven cycles observed.", confidence="likely",
                          sources=[source], questions=[{"op": "open", "id": "count", "text": "Verify cycle count"}])
        with self.assertRaises(ac.ProtocolError):
            self.send(key="bad", act="CORRECT", summary="Nine cycles.")
        with self.assertRaises(ac.ProtocolError):
            self.send(key="bad", sources=[dict(source, detail="Changed")])
        self.send(key="correction", act="CORRECT", reply_to=first["id"], summary="Correction: nine cycles.",
                  confidence="verified", refs=["reading"], questions=[{"op": "close", "id": "count"}])
        with self.assertRaises(ac.ProtocolError):
            self.send(key="close-again", questions=[{"op": "close", "id": "count"}])
        state = self.channel.snapshot()
        self.assertEqual("closed", state.questions["count"]["state"])
        self.assertIn("closed_by", state.questions["count"])
        rendered = state.render()
        self.assertLess(rendered.index("Eleven cycles"), rendered.index("Correction: nine"))
        self.assertIn("Initial observation", rendered)
        self.assertEqual(first, state.by_id[first["id"]])

    def test_policy_confidence_and_sources(self):
        other = self.root / "policy-channel"
        ac.Channel.init(other, "Policy test", ["alpha", "beta"], ["FACT"], ["FACT"])
        c = ac.Channel(other)
        c.join("alpha", "codex", "alpha-session")
        c.join("beta", "claude", "beta-session")
        for draft in (self.draft("FACT"), self.draft("FACT", confidence="verified")):
            with self.assertRaises(ac.ProtocolError):
                c.send("alpha", "alpha-session", "fact", draft)
        draft = self.draft("FACT", confidence="unverified", sources=[{
            "id": "estimate", "kind": "inference", "ref": "draft-1", "detail": "Not measured"}])
        c.send("alpha", "alpha-session", "fact", draft)

    def test_exact_digest_consensus_not_ack(self):
        a = self.channel.put_artifact("alpha", "alpha-session", b"Candidate A\n")["artifact"]
        b = self.channel.put_artifact("alpha", "alpha-session", b"Candidate B\n")["artifact"]
        proposal = self.send(act="PROPOSE", artifact=a)
        args = {"reply_to": proposal["id"], "artifact": a}
        self.send(key="ack", act="ACK", author="beta", reply_to=proposal["id"])
        with self.assertRaises(ac.ProtocolError):
            self.send(key="decide", act="DECIDE", **args)
        self.send(key="accept-a", act="ACCEPT", **args)
        with self.assertRaises(ac.ProtocolError):
            self.send(key="wrong", act="ACCEPT", author="beta", reply_to=proposal["id"], artifact=b)
        self.send(key="reject", act="REJECT", author="beta", **args)
        with self.assertRaises(ac.ProtocolError):
            self.send(key="decide", act="DECIDE", **args)
        self.send(key="accept-b", act="ACCEPT", author="beta", **args)
        self.send(key="decide", act="DECIDE", **args)
        self.assertEqual("DECIDE", self.channel.snapshot().messages[-1]["act"])
        with self.assertRaises(ac.ProtocolError):
            self.send(key="late-reject", act="REJECT", author="beta", **args)

    def test_failed_publication_preserves_head_and_existing_record(self):
        first = self.send()
        state = self.channel.snapshot()
        with self.assertRaises(ac.ProtocolError):
            self.channel.publish(state.head, {f"messages/{first['id']}.json": b"{}\n"}, "replace", "alpha")
        with mock.patch.object(self.channel, "_before_publish", side_effect=ac.ProtocolError("injected")):
            with self.assertRaises(ac.ProtocolError):
                self.send(key="retry")
        self.assertEqual(state.head, self.channel.snapshot().head)
        self.assertEqual(2, self.send(key="retry")["seq"])

    def test_unrelated_index_environment_and_shell_text_untouched(self):
        app = self.root / "app"
        app.mkdir()
        subprocess.run(["git", "init", "--quiet", str(app)], check=True)
        (app / "keep.txt").write_text("user change")
        subprocess.run(["git", "-C", str(app), "add", "keep.txt"], check=True)
        index = app / ".git/index"
        before = index.read_bytes()
        payload = f"$(touch {self.root / 'executed'}) `touch {self.root / 'executed2'}`"
        with mock.patch.dict(os.environ, {"GIT_INDEX_FILE": str(index), "GIT_DIR": str(app / ".git")}):
            self.send(body=payload)
        self.assertEqual(before, index.read_bytes())
        self.assertFalse((self.root / "executed").exists())
        self.assertFalse((self.root / "executed2").exists())

    def test_state_overflow_is_visible_and_bounded(self):
        for batch in range(5):
            self.send(key=f"q{batch}", questions=[{
                "op": "open", "id": f"question-{batch}-{n}-" + "x" * 40,
                "text": "A bounded question"} for n in range(50)])
        state = self.channel.snapshot().state()
        self.assertLessEqual(len(ac.canonical(state)), 8192)
        self.assertGreater(state["omitted"]["open_questions"], 0)
        self.assertEqual(250, len(state["open_questions"]) + state["omitted"]["open_questions"])

    def test_relocated_cli_and_safe_render(self):
        moved = self.root / "relocated"
        moved.mkdir()
        for name in ("ai_comm.py", "PROTOCOL.md"):
            shutil.copy(SCRIPT.parent / name, moved / name)
        self.send(body='<script>alert("x")</script> [run](javascript:alert(1))')
        result = self.cli("validate", script=moved / "ai_comm.py", cwd=self.root)
        self.assertEqual(0, result.returncode, result.stderr)
        rendered = self.cli("render", script=moved / "ai_comm.py")
        self.assertEqual(0, rendered.returncode, rendered.stderr)
        self.assertNotIn(b"<script>", rendered.stdout)
        self.assertIn(b"&lt;script&gt;", rendered.stdout)
        before = self.channel.snapshot().head
        out = self.root / "history.md"
        self.assertEqual(0, self.cli("render", "--out", str(out)).returncode)
        self.assertNotEqual(0, self.cli("render", "--out", str(out)).returncode)
        self.assertEqual(before, self.channel.snapshot().head)

    def test_cli_invalid_input_exits_nonzero_without_echoing_payload(self):
        result = self.cli("send", "--as", "alpha", "--session", "alpha-session", "--key", "bad",
                          "--draft", "-", data=b'{"secret-value":NaN}')
        self.assertEqual(2, result.returncode)
        self.assertNotIn(b"secret-value", result.stderr)
        self.assertNotIn(b"Traceback", result.stderr)
        self.assertEqual([], self.channel.snapshot().messages)


if __name__ == "__main__":
    unittest.main()
