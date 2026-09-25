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
import threading
import unittest
from unittest import mock
import hashlib
import ast

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

    def test_send_dry_run_has_no_durable_side_effects_and_does_not_reserve_key(self):
        before = self.channel.snapshot().head
        receipt = self.channel.runtime / "beta.json"
        self.assertFalse(receipt.exists())
        dry = self.channel.send("alpha", "alpha-session", "dry-ping",
                                self.draft("PING"), dry_run=True)
        self.assertTrue(dry["dry_run"])
        self.assertFalse(dry["published"])
        self.assertTrue(dry["message"]["provisional"])
        self.assertEqual(1, dry["message"]["seq"])
        self.assertRegex(dry["message"]["nonce"], r"^[0-9a-f]{32}$")
        self.assertEqual(before, self.channel.snapshot().head)
        self.assertEqual([], self.channel.snapshot().messages)
        self.assertFalse(receipt.exists())
        real = self.channel.send("alpha", "alpha-session", "dry-ping",
                                 self.draft("PING"))
        self.assertFalse(real["duplicate"])
        self.assertEqual(1, real["message"]["seq"])
        self.assertNotEqual(dry["message"]["nonce"], real["message"]["nonce"])

    def test_dry_run_validates_and_returns_structured_safe_errors(self):
        bad = self.draft("ASK", confidence="secret-payload")
        with self.assertRaises(ac.ProtocolError) as caught:
            self.channel.send("alpha", "alpha-session", "dry-bad", bad, dry_run=True)
        details = caught.exception.details
        self.assertEqual("confidence_not_allowed", details["code"])
        self.assertEqual("confidence", details["field"])
        self.assertEqual("ASK", details["got"])
        self.assertNotIn("secret-payload", json.dumps(details))
        self.assertEqual([], self.channel.snapshot().messages)

    def test_unregistered_recipients_report_only_a_count(self):
        draft = self.draft("ASK", to=["ghost-one", "ghost-two"])
        with self.assertRaises(ac.ProtocolError) as caught:
            self.channel.send("alpha", "alpha-session", "unregistered", draft, dry_run=True)
        self.assertEqual("unregistered_recipients", caught.exception.details["code"])
        self.assertEqual(2, caught.exception.details["got"])
        self.assertNotIn("ghost-one", json.dumps(caught.exception.details))
        self.assertNotIn("ghost-two", json.dumps(caught.exception.details))
        result = self.cli("send", "--as", "alpha", "--session", "alpha-session",
                          "--key", "cli-dry-run", "--draft", "-", "--dry-run",
                          data=ac.canonical(self.draft("PING")))
        self.assertEqual(0, result.returncode, result.stderr)
        response = json.loads(result.stdout)
        self.assertTrue(response["dry_run"])
        self.assertFalse(response["published"])
        self.assertTrue(response["message"]["provisional"])
        self.assertEqual([], self.channel.snapshot().messages)

    def test_all_literal_require_rules_have_structured_codes_fields_and_hints(self):
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        rules = {node.args[1].value for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                 and node.func.id == "require" and len(node.args) > 1
                 and isinstance(node.args[1], ast.Constant)
                 and isinstance(node.args[1].value, str)}
        self.assertGreater(len(rules), 60)
        for rule in rules:
            with self.subTest(rule=rule), self.assertRaises(ac.ProtocolError) as caught:
                ac.require(False, rule)
            details = caught.exception.details
            self.assertRegex(details["code"], r"^[a-z][a-z0-9_]{0,63}$")
            self.assertRegex(details["field"], r"^[a-z_]{1,32}$")
            self.assertTrue(details["hint"])
            self.assertNotIn("secret-payload", json.dumps(details))
        unsafe = ac.ProtocolError("safe rejection", field="secret-payload",
                                  got="secret-payload", allowed=["secret-payload"])
        self.assertEqual("input", unsafe.details["field"])
        self.assertNotIn("got", unsafe.details)
        self.assertNotIn("allowed", unsafe.details)
        self.assertNotIn("secret-payload", json.dumps(unsafe.details))

    def test_body_warning_counts_unicode_codepoints_and_is_actionable(self):
        result = self.channel.send("alpha", "alpha-session", "long-body",
                                   self.draft(body="😀" * 2001))
        self.assertIn("body is 2001 characters", result["warnings"][0])
        self.assertIn("Unicode code points", result["warnings"][0])
        self.assertIn("artifact --file PATH", result["warnings"][0])

    def test_release_range_and_fixed_in_boundaries(self):
        parsed, upper, inclusive = ac.parse_affects(">=1.0.0,<1.2.0")
        self.assertEqual((1, 2, 0), upper)
        self.assertFalse(inclusive)
        self.assertTrue(ac.range_matches(parsed, (1, 1, 9)))
        self.assertFalse(ac.range_matches(parsed, (1, 2, 0)))
        base = {"kind": "ai-comm-release", "package_version": "1.2.0",
                "commit": "a" * 40, "tag": "v1.2.0", "manifest_sha256": "b" * 64,
                "protocol_sha256": "c" * 64, "advisory": "required", "reason": "Data loss risk.",
                "affects": ">=1.0.0,<1.2.0", "fixed_in": "1.2.0"}
        ac.validate_release_record(base)
        with self.assertRaises(ac.ProtocolError):
            ac.validate_release_record(dict(base, fixed_in="0.9.0"))
        with self.assertRaises(ac.ProtocolError):
            ac.validate_release_record(dict(base, affects=">=1.0.0", fixed_in="2.0.0"))
        with self.assertRaises(ac.ProtocolError):
            ac.parse_affects(">=01.0.0")

    def test_upgrade_record_is_strict_and_surfaces_advisory_in_state_and_inbox(self):
        package = SCRIPT.parent
        record = {"kind": "ai-comm-release", "package_version": ac.VERSION,
                  "commit": "a" * 40, "tag": "v" + ac.VERSION,
                  "manifest_sha256": ac.sha((package / "RELEASE-MANIFEST.json").read_bytes()),
                  "protocol_sha256": ac.sha((package / "PROTOCOL.md").read_bytes()),
                  "advisory": "required", "reason": "A protocol safety defect affects this range.",
                  "affects": ">=0.1.0,<0.3.0", "fixed_in": "0.3.0"}
        digest = self.channel.put_artifact("alpha", "alpha-session", ac.canonical(record))["artifact"]
        summary = f"Release v{ac.VERSION} at {record['commit']}; required advisory."
        self.channel.send("alpha", "alpha-session", "upgrade-notice",
                          self.draft("FACT", topic="upgrade", artifact=digest,
                                     summary=summary))
        notice = self.channel.snapshot().state()["release_notices"][0]
        self.assertEqual("same", notice["status"])
        self.assertEqual("required", notice["advisory"])
        self.assertTrue(notice["advisory_applies"])
        inbox = self.channel.inbox("beta", "beta-session")
        self.assertEqual("required", inbox["messages"][0]["release_status"]["advisory"])

    def test_offline_release_status_checks_local_index_and_annotated_tag(self):
        repo = self.root / "release-repo"
        repo.mkdir()

        def git(*args):
            env = os.environ.copy()
            env.update(GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="test@example.invalid",
                       GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="test@example.invalid")
            return subprocess.run(["git", "-C", str(repo), *args], check=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=30).stdout

        subprocess.run(["git", "init", "--initial-branch=main", str(repo)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        manifest = b'{"release":"fixture"}\n'
        protocol = b"fixture protocol\n"
        (repo / "ai_comm.py").write_text('VERSION = "1.0.0"\n', encoding="utf-8")
        (repo / "RELEASE-MANIFEST.json").write_bytes(manifest)
        (repo / "PROTOCOL.md").write_bytes(protocol)
        git("add", "ai_comm.py", "RELEASE-MANIFEST.json", "PROTOCOL.md")
        git("commit", "-m", "release fixture")
        commit = git("rev-parse", "HEAD").decode().strip()
        manifest_digest = hashlib.sha256(manifest).hexdigest()
        protocol_digest = hashlib.sha256(protocol).hexdigest()
        git("tag", "-a", "v1.0.0", "-m", "Release 1.0.0 manifest " + manifest_digest)
        (repo / "RELEASES.json").write_bytes(ac.canonical({"format": "ai-comm-releases/1", "releases": [{
            "package_version": "1.0.0", "commit": commit, "tag": "v1.0.0",
            "manifest_sha256": manifest_digest, "protocol_sha256": protocol_digest,
            "summary": "Initial release", "advisory": "required", "reason": "Protocol safety defect.",
            "affects": ">=1.0.0,<1.2.0", "fixed_in": "1.2.0"}]}))
        git("switch", "--orphan", "release-index")
        for name in ("ai_comm.py", "RELEASE-MANIFEST.json", "PROTOCOL.md"):
            (repo / name).unlink(missing_ok=True)
        git("add", "RELEASES.json")
        git("commit", "-m", "release index")
        git("update-ref", "refs/remotes/origin/release-index", "HEAD")
        self.assertEqual("RELEASES.json", git("ls-tree", "--name-only", "HEAD").decode().strip())
        git("switch", "main")
        result = ac.release_status(repo)
        self.assertEqual("ok", result["status"])
        self.assertEqual("1.0.0", result["pinned_version"])
        self.assertEqual("1.0.0", result["latest_version"])
        self.assertEqual("not verified (no attestation)", result["authenticity"])
        self.assertEqual("required", result["active_advisories"][0]["advisory"])
        self.assertEqual("1.2.0", result["active_advisories"][0]["fixed_in"])

        wrong = {"format": "ai-comm-releases/1", "releases": [{
            "package_version": "1.0.0", "commit": "0" * 40, "tag": "v1.0.0",
            "manifest_sha256": manifest_digest, "protocol_sha256": protocol_digest,
            "summary": "Initial release", "advisory": "required", "reason": "Protocol safety defect.",
            "affects": ">=1.0.0,<1.2.0", "fixed_in": "1.2.0"}]}
        git("switch", "release-index")
        (repo / "RELEASES.json").write_bytes(ac.canonical(wrong))
        git("add", "RELEASES.json")
        git("commit", "-m", "inconsistent index")
        git("update-ref", "refs/remotes/origin/release-index", "HEAD")
        git("switch", "main")
        result = ac.release_status(repo)
        self.assertEqual("index_inconsistent", result["status"])
        self.assertEqual([], result["advice"])

        git("update-ref", "-d", "refs/remotes/origin/release-index")
        result = ac.release_status(repo)
        self.assertEqual("index_missing", result["status"])
        self.assertEqual([], result["advice"])

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
        with self.assertRaises(ac.ProtocolError) as caught:
            self.channel.ack("beta", "beta-session", 1)
        self.assertEqual("ack_out_of_range", caught.exception.details["code"])
        self.assertEqual(0, caught.exception.details["read_through"])
        self.assertEqual(0, caught.exception.details["scanned_through"])
        page = self.channel.inbox("beta", "beta-session", 1)
        self.assertEqual(1, page["through"])
        with self.assertRaises(ac.ProtocolError):
            self.channel.ack("beta", "beta-session", 2)
        self.channel.ack("beta", "beta-session", 1)
        self.assertEqual("other", self.channel.inbox("beta", "beta-session")["messages"][0]["topic"])

    def test_notification_cursor_suppresses_alerts_without_consuming_inbox(self):
        self.send()
        args = ("wait", "--as", "beta", "--session", "beta-session", "--timeout", "0")
        first = self.cli(*args, "--new-since", "0")
        self.assertEqual(0, first.returncode, first.stderr)
        page = json.loads(first.stdout)
        self.assertEqual([1], [m["seq"] for m in page["messages"]])
        self.assertEqual(1, page["notification_through"])
        self.assertNotIn("through", page)
        self.assertEqual("notifications", page["mode"])
        self.assertFalse((self.channel.runtime / "beta.json").exists())
        with self.assertRaises(ac.ProtocolError):
            self.channel.ack("beta", "beta-session", 1)
        duplicate = self.cli(*args, "--new-since", "1")
        self.assertEqual(0, duplicate.returncode, duplicate.stderr)
        self.assertTrue(json.loads(duplicate.stdout)["timed_out"])
        self.assertEqual([], json.loads(duplicate.stdout)["messages"])
        # Default wait still replays unread backlog, including after process restart.
        ordinary = json.loads(self.cli(*args).stdout)
        self.assertEqual([1], [m["seq"] for m in ordinary["messages"]])
        receipt = (self.channel.runtime / "beta.json").read_bytes()
        self.channel.wait("beta", "beta-session", timeout=0, new_since=1)
        self.assertEqual(receipt, (self.channel.runtime / "beta.json").read_bytes())

    def test_notification_pagination_retains_unread_topics_and_byte_bound(self):
        self.send(key="first", topic="first")
        self.send(key="outgoing", author="beta")
        self.send(key="second", topic="second")
        first = self.channel.wait("beta", "beta-session", timeout=0, limit=1, new_since=0)
        self.assertEqual([1], [m["seq"] for m in first["messages"]])
        self.assertEqual(2, first["notification_through"])
        self.assertTrue(first["more"])
        second = self.channel.wait("beta", "beta-session", timeout=0, limit=1,
                                   new_since=first["notification_through"])
        self.assertEqual([3], [m["seq"] for m in second["messages"]])
        self.assertFalse(second["more"])
        unread = self.channel.inbox("beta", "beta-session")
        self.assertEqual([1, 3], [m["seq"] for m in unread["messages"]])
        self.channel.ack("beta", "beta-session", unread["through"])
        self.assertEqual([], self.channel.wait("beta", "beta-session", timeout=0,
                                               new_since=0)["messages"])
        for n in range(5):
            self.send(key=f"large-{n}", body="x" * 60000)
        page = self.channel.wait("beta", "beta-session", timeout=0, new_since=3)
        self.assertEqual(4, len(page["messages"]))
        self.assertTrue(page["more"])
        self.assertLessEqual(sum(len(ac.canonical(m)) for m in page["messages"]), 256 * 1024)

    def test_notification_rejects_invalid_cursor_stale_identity_and_corrupt_receipt(self):
        for cursor in (-1, True, 0.5, "0", 1, 999999999):
            with self.subTest(cursor=cursor), self.assertRaises(ac.ProtocolError):
                self.channel.wait("beta", "beta-session", timeout=0, new_since=cursor)
        self.channel.join("beta", "claude", "beta-new", takeover=True)
        with self.assertRaises(ac.ProtocolError):
            self.channel.wait("beta", "beta-session", timeout=0, new_since=0)
        receipt = self.channel.runtime / "beta.json"
        receipt.write_bytes(b'{"v":')
        with self.assertRaises(ac.ProtocolError):
            self.channel.wait("beta", "beta-new", timeout=0, new_since=0)
        receipt.unlink()
        target = self.root / "sentinel"
        target.write_text("unchanged")
        receipt.symlink_to(target)
        with self.assertRaises(ac.ProtocolError):
            self.channel.wait("beta", "beta-new", timeout=0, new_since=0)
        self.assertEqual("unchanged", target.read_text())

    def test_notification_wait_observes_later_arrival_without_ack(self):
        self.send()
        scanned = threading.Event()
        notifications = self.channel.notifications

        def observe_empty_page(*args, **kwargs):
            page = notifications(*args, **kwargs)
            if not page["messages"]:
                scanned.set()
            return page

        def send_after_scan():
            self.assertTrue(scanned.wait(5), "wait did not reach its first empty notification scan")
            return self.send(key="later")

        # Do not patch time.sleep: it is also used by subprocess waits and lock
        # backoff, including while Git operations hold the channel lock.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(send_after_scan)
            with mock.patch.object(self.channel, "notifications", side_effect=observe_empty_page):
                page = self.channel.wait("beta", "beta-session", timeout=10, new_since=1)
            self.assertEqual(2, pending.result(timeout=10)["seq"])
        self.assertEqual([2], [m["seq"] for m in page["messages"]])
        self.assertFalse(page["timed_out"])
        with self.assertRaises(ac.ProtocolError):
            self.channel.ack("beta", "beta-session", 2)
        self.assertEqual([1, 2], [m["seq"] for m in self.channel.inbox("beta", "beta-session")["messages"]])

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

    def test_text_validation_preserves_control_ranges_and_multiline_rules(self):
        for codepoint in range(256):
            character = chr(codepoint)
            for multiline in (False, True):
                permitted = not (codepoint < 32 or 127 <= codepoint <= 159 or codepoint == 173)
                permitted = permitted or (multiline and codepoint == 10)
                value = "before" + character + "after"
                if permitted:
                    self.assertEqual(value, ac.text_field(value, "test", 100, multiline))
                else:
                    with self.subTest(codepoint=codepoint, multiline=multiline), self.assertRaises(ac.ProtocolError):
                        ac.text_field(value, "test", 100, multiline)
        for value in ("ASCII prose", "français 日本語", "Words\nwith lines"):
            self.assertEqual(value, ac.text_field(value, "test", 100, True))
        for character in ("\u061c", "\u202e", "\ud800", "\u2028", "\u2029", "\U000e0001"):
            with self.assertRaises(ac.ProtocolError):
                ac.text_field("before" + character + "after", "test", 100, True)

    def test_text_validation_matches_v010_for_every_unicode_codepoint(self):
        # Frozen v0.1.0 oracle: a security-sensitive optimization must preserve
        # its complete rejection set, including lone surrogates and unassigned text.
        def previous(value, name, maximum, multiline=False):
            ac.require(isinstance(value, str) and 0 < len(value) <= maximum,
                       f"invalid {name} length or type")
            ac.require(value.strip() != "", f"empty {name}")
            ac.require(not any((ord(c) < 32 and not (multiline and c == "\n"))
                               or 127 <= ord(c) <= 159
                               or ac.unicodedata.category(c) in {"Cf", "Cs", "Zl", "Zp"}
                               for c in value), f"invalid control character in {name}")
            return value

        def accepted(function, value, maximum, multiline):
            try:
                function(value, "test", maximum, multiline)
                return True
            except ac.ProtocolError:
                return False

        for point in range(0x110000):
            value = "a" + chr(point) + "b"
            for multiline in (False, True):
                old = accepted(previous, value, 3, multiline)
                new = accepted(ac.text_field, value, 3, multiline)
                if old != new:
                    self.fail(f"v0.1.0 mismatch at U+{point:06X}, multiline={multiline}")
        for value in (None, 0, b"a", "", " ", "\t", "\n", "\u00a0", "\u3000", "a", "ab", "a\nb"):
            for maximum in (0, 1, 2, 3):
                for multiline in (False, True):
                    self.assertEqual(accepted(previous, value, maximum, multiline),
                                     accepted(ac.text_field, value, maximum, multiline))

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

    def test_render_deduplicates_sources_within_each_export(self):
        source = {"id": "record", "kind": "measurement", "ref": "local-reading",
                  "detail": "Uncertain: <script>7 ± 2 units</script> `literal`."}
        first = self.send(act="FACT", summary="Original estimate.", sources=[source], refs=["record"])
        second = self.send(key="second", act="CORRECT", reply_to=first["id"], refs=["record"],
                           summary="Correction remains uncertain.")
        self.send(key="third", sources=[source], refs=["record"])
        state = self.channel.snapshot()
        rendered = state.render()
        detail = "Uncertain: &lt;script&gt;7 ± 2 units&lt;/script&gt; &#96;literal&#96;."
        self.assertEqual(1, rendered.count(detail))
        self.assertEqual(2, rendered.count(f"Source record: see {first['id']} above."))
        self.assertNotIn("<script>", rendered)
        self.assertEqual(rendered, state.render())
        # A partial export resolves its first citation even if declaration was omitted.
        partial = state.render(since=1)
        self.assertEqual(1, partial.count(detail))
        self.assertIn(f"Source record: see {second['id']} above.", partial)
        self.assertNotIn(f"Source record: see {first['id']}", partial)
        self.assertIn("Original estimate.", rendered)
        self.assertIn("Correction remains uncertain.", partial)
        self.assertEqual(first, state.by_id[first["id"]])

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
        error = json.loads(result.stderr)
        self.assertTrue(error["code"])
        self.assertTrue(error["field"])
        self.assertTrue(error["hint"])
        self.assertEqual([], self.channel.snapshot().messages)


if __name__ == "__main__":
    unittest.main()
