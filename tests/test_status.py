"""Tests for the read-only status view and its sidecar producer (docs/STATUS.md)."""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "ai_comm.py"
sys.path.insert(0, str(SCRIPT.parent))
import ai_comm as ac  # noqa: E402

PROBE = {"bound_cli_sha256": "0" * 64, "paused": False, "pending": False, "running": True}


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ai-comm-status-")
        self.root = Path(self.temp.name)
        self.path = self.root / "channel"
        ac.Channel.init(self.path, "Review a generic change", ["alpha", "beta"])
        self.channel = ac.Channel(self.path)
        self.channel.join("alpha", "host-a", "alpha-session")
        self.channel.join("beta", "host-b", "beta-session")
        self.identity = ac.channel_identity(self.channel)
        self.status_dir = self.root / ".ai-comm-local" / "status" / "v1" / self.identity[:16]
        self.keys = 0

    def tearDown(self):
        self.temp.cleanup()

    def send(self, author="alpha", **extra):
        self.keys += 1
        to = ["beta"] if author == "alpha" else ["alpha"]
        draft = dict({"act": "FACT", "to": to, "topic": "review", "summary": f"Message {self.keys}."}, **extra)
        return self.channel.send(author, author + "-session", f"k{self.keys}", draft)["message"]

    def cli(self, *args, stdin=b""):
        return subprocess.run([sys.executable, "-I", str(SCRIPT), "--channel", str(self.path), *args],
                              input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)

    def record(self, handle, dimension, state, lease=60):
        return ac.record_status(self.channel, None, handle, handle + "-session", dimension, state, "test", lease)

    def write_sidecar(self, who, dim, **changes):
        value = {"v": 1, "channel": self.identity, "handle": who, "session": who + "-session", "generation": 1,
                 "dimension": dim, "state": "ended" if dim == "turn" else "armed",
                 "source": "test", "observed_utc": ac.poll_now(), "lease_seconds": 60}
        value.update(changes)
        self.status_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.status_dir / f"{who}.{dim}.json").write_text(json.dumps(value))

    def row(self, handle, **kwargs):
        view = ac.status_view(self.channel, **kwargs)
        return next(r for r in view["participants"] if r["handle"] == handle)

    def tree_digest(self):
        digest = hashlib.sha256()
        for root, dirs, files in sorted(os.walk(self.root)):
            dirs.sort()
            digest.update(("dir:" + str(Path(root).relative_to(self.root))).encode())
            for name in sorted(files):
                full = Path(root) / name
                digest.update(str(full.relative_to(self.root)).encode())
                digest.update(full.read_bytes())
        return digest.hexdigest()

    # The view

    def test_without_signals_every_dimension_is_unknown(self):
        row = self.row("alpha")
        self.assertEqual(("unknown", "unknown", "unknown", None),
                         (row["summary"], row["turn"]["state"], row["wake"]["state"], row["tokens"]))

    def test_turn_ended_without_wake_and_with_mail_needs_nudge_never_working(self):
        self.send()
        self.record("beta", "turn", "ended")
        self.assertEqual("needs nudge", self.row("beta")["summary"])
        self.record("beta", "wake", "disarmed")
        self.assertEqual("needs nudge", self.row("beta")["summary"])
        self.record("beta", "wake", "uncertain")
        self.assertEqual("wake uncertain, needs nudge", self.row("beta")["summary"])
        self.record("beta", "wake", "armed")
        self.assertEqual("idle, wake armed", self.row("beta")["summary"])

    def test_turn_states_map_to_plain_words(self):
        self.record("alpha", "turn", "active")
        self.assertEqual("working", self.row("alpha")["summary"])
        self.record("alpha", "turn", "waiting")
        self.assertEqual("waiting for approval", self.row("alpha")["summary"])
        self.record("alpha", "turn", "ended")
        self.assertEqual("idle", self.row("alpha")["summary"], "nothing addressed to alpha")

    def test_mail_separates_new_fetched_unread_and_read(self):
        for _ in range(3):
            self.send()
        self.assertEqual((3, 0, 0), self.mail("beta"))
        self.channel.inbox("beta", "beta-session", limit=2)
        self.assertEqual((1, 2, 0), self.mail("beta"))
        self.channel.ack("beta", "beta-session", 1)
        self.assertEqual((1, 1, 1), self.mail("beta"))
        self.assertEqual(0, self.row("alpha")["mail"]["new"], "own messages are not mail")

    def mail(self, handle):
        mail = self.row(handle)["mail"]
        return mail["new"], mail["fetched_unread"], mail["read_through"]

    def test_mail_counts_owed_work_from_the_ledger(self):
        self.send(act="PING")
        alpha, beta = self.row("alpha")["mail"], self.row("beta")["mail"]
        self.assertEqual((1, 0), (beta["owes"], beta["owed_by_others"]))
        self.assertEqual((0, 1), (alpha["owes"], alpha["owed_by_others"]))

    def test_invalid_sidecars_read_as_unknown(self):
        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        old = (datetime.now(timezone.utc) - timedelta(seconds=61)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        cases = {"expired": {"observed_utc": old}, "future": {"observed_utc": future},
                 "other generation": {"generation": 2}, "other session": {"session": "old-session"},
                 "other handle": {"handle": "beta"}, "unknown state": {"state": "working"},
                 "extra key": {"note": "x"}, "other channel": {"channel": "f" * 40}, "bad version": {"v": 2}, "bool version": {"v": True},
                 "lease too long": {"lease_seconds": 86401}, "bad source": {"source": "Bad\u001b[31m"},
                 "bad time": {"observed_utc": "yesterday"}}
        for name, change in cases.items():
            with self.subTest(name):
                self.write_sidecar("alpha", "turn", **change)
                self.assertEqual("unknown", self.row("alpha")["turn"]["state"])
        self.write_sidecar("alpha", "turn")
        self.assertEqual("ended", self.row("alpha")["turn"]["state"], "the valid baseline is accepted")

    def test_symlinked_oversized_and_non_json_sidecars_read_as_unknown(self):
        self.write_sidecar("alpha", "wake")
        target = self.status_dir / "alpha.wake.json"
        real = self.root / "real.json"
        os.replace(target, real)
        os.symlink(real, target)
        self.assertEqual("unknown", self.row("alpha")["wake"]["state"])
        target.unlink()
        target.write_bytes(b"{" + b" " * 2000 + b"}")
        self.assertEqual("unknown", self.row("alpha")["wake"]["state"])
        target.write_bytes(b"\xff not json")
        self.assertEqual("unknown", self.row("alpha")["wake"]["state"])

    def test_sibling_channels_keep_their_observations_apart(self):
        # A copy has the same root commit, joins, sessions and generations: only its location differs.
        other_path = self.root / "channel-2"
        shutil.copytree(self.path, other_path, symlinks=True)
        other = ac.Channel(other_path)
        self.assertEqual(ac.git(self.path, "rev-list", "--max-parents=0", ac.REF),
                         ac.git(other_path, "rev-list", "--max-parents=0", ac.REF))
        self.record("alpha", "turn", "active")
        self.record("alpha", "wake", "armed")
        theirs = next(r for r in ac.status_view(other)["participants"] if r["handle"] == "alpha")
        self.assertEqual(("unknown", "unknown"), (theirs["turn"]["state"], theirs["wake"]["state"]))
        ac.record_status(other, None, "alpha", "alpha-session", "turn", "ended", "test", 60)
        self.assertEqual("active", self.row("alpha")["turn"]["state"], "one channel's write leaves the other alone")
        theirs = next(r for r in ac.status_view(other)["participants"] if r["handle"] == "alpha")
        self.assertEqual("ended", theirs["turn"]["state"])

    def test_an_unreadable_receipt_keeps_owed_work_and_never_says_idle(self):
        self.send(act="ASK", questions=[{"op": "open", "id": "q1", "text": "Which option?"}])
        self.record("beta", "turn", "ended")
        self.channel.runtime.mkdir(exist_ok=True)
        (self.channel.runtime / "beta.json").write_text("not json")
        row = self.row("beta")
        self.assertEqual(("unreadable", None, 1), (row["mail"]["receipt"], row["mail"]["new"], row["mail"]["owes"]))
        self.assertEqual("needs nudge", row["summary"])
        (self.channel.runtime / "alpha.json").write_text("not json")
        self.record("alpha", "turn", "ended")
        self.assertEqual("ended, mail unknown", self.row("alpha")["summary"], "nothing known to be owed")
        text = self.cli("status").stdout.decode()
        self.assertIn("mail receipt unreadable, owes 1", text)

    def test_a_rejoin_makes_older_observations_unknown(self):
        self.record("alpha", "turn", "active")
        self.channel.join("alpha", "host-a", "alpha-session-2", takeover=True)
        self.assertEqual("unknown", self.row("alpha")["turn"]["state"])

    def test_status_writes_nothing_and_creates_no_directory(self):
        self.send()
        self.channel.inbox("beta", "beta-session")
        before = self.tree_digest()
        for args in ([], ["--format", "json"], ["--me", "alpha", "--host-json", "-"]):
            self.assertEqual(0, self.cli("status", *args, stdin=b"{}").returncode)
        self.assertEqual(before, self.tree_digest())
        self.assertFalse((self.root / ".ai-comm-local").exists())

    def test_tokens_come_only_from_the_host_and_only_for_me(self):
        host = json.dumps({"context_window": {"used_percentage": 41.6, "total_input_tokens": 1000,
                                              "total_output_tokens": 234}}).encode()
        result = self.cli("status", "--me", "alpha", "--host-json", "-", "--format", "json", stdin=host)
        rows = {r["handle"]: r for r in json.loads(result.stdout)["participants"]}
        self.assertEqual({"context_used_percent": 42, "session_tokens": 1234,
                          "scope": "this session, reported by its host"}, rows["alpha"]["tokens"])
        self.assertIsNone(rows["beta"]["tokens"])
        self.assertTrue(rows["alpha"]["you"])
        text = self.cli("status", "--me", "alpha", "--host-json", "-", stdin=b"not json")
        self.assertEqual(0, text.returncode, text.stderr)
        self.assertEqual(2, text.stdout.decode().count("tokens n/a"))

    def test_text_shows_each_participant_and_the_last_message(self):
        self.send()
        self.record("alpha", "wake", "armed")
        lines = self.cli("status", "--me", "alpha").stdout.decode().splitlines()
        self.assertTrue(lines[0].startswith("ai-comm #1 as of "))
        self.assertRegex(lines[1], r"^alpha \(you\)  unknown: turn unknown · wake armed \(test, \d+s\) · mail new 0")
        self.assertTrue(lines[2].startswith("beta" + " " * 9 + "unknown: turn unknown · wake unknown · mail new 1"))
        self.assertEqual("last #1 alpha FACT [review] Message 1.", lines[3])

    def test_unknown_me_and_bad_interval_are_json_errors(self):
        for args, code in ((["--me", "gamma"], "unknown_participant"),
                           (["--interval", "0.1"], "invalid_status_interval"),
                           (["--format", "html"], "invalid_cli_arguments")):
            result = self.cli("status", *args)
            self.assertEqual(2, result.returncode)
            self.assertEqual(code, json.loads(result.stderr)["code"], args)

    def test_follow_rebuilds_the_projection_only_when_the_head_changes(self):
        self.send()
        built, sleeps = [], []
        real = self.channel.snapshot

        def counting():
            built.append(1)
            return real()

        def sleep(_seconds):
            sleeps.append(1)
            if len(sleeps) == 2:
                self.channel.snapshot = real
                self.send()
                self.channel.snapshot = counting
            if len(sleeps) == 4:
                raise KeyboardInterrupt
        self.channel.snapshot = counting
        out = io.StringIO()
        self.assertEqual(0, ac.run_status(self.channel, None, None, None, "text", True, 0.2, out=out, sleep=sleep))
        heads = [line.split()[1] for line in out.getvalue().splitlines() if line.startswith("ai-comm ")]
        self.assertEqual(["#1", "#1", "#2", "#2"], heads)
        self.assertEqual(2, len(built), "one projection at start, one after the new commit")

    def test_follow_ends_cleanly_when_the_reader_closes(self):
        read_end, write_end = os.pipe()
        os.close(read_end)
        process = subprocess.Popen([sys.executable, "-I", str(SCRIPT), "--channel", str(self.path), "status",
                                    "--follow", "--interval", "0.2"], stdout=write_end, stderr=subprocess.PIPE)
        os.close(write_end)
        try:
            code = process.wait(timeout=60)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=30)
        stderr = process.stderr.read()
        process.stderr.close()
        self.assertEqual((0, b""), (code, stderr))

    def test_a_closed_custom_output_leaves_the_real_stdout_alone(self):
        class Closed(io.StringIO):
            def write(self, _text):
                raise BrokenPipeError

        before = os.fstat(sys.stdout.fileno()) if hasattr(sys.stdout, "fileno") else None
        self.assertEqual(0, ac.run_status(self.channel, None, None, None, "text", False, 3.0, out=Closed()))
        if before is not None:
            after = os.fstat(sys.stdout.fileno())
            self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))

    # The producer

    def test_record_requires_the_current_session_and_writes_a_private_sidecar(self):
        with self.assertRaises(ac.ProtocolError) as caught:
            ac.record_status(self.channel, None, "alpha", "wrong-session", "turn", "active", "test", 60)
        self.assertEqual("unregistered_or_stale_session", caught.exception.details["code"])
        self.assertFalse(self.status_dir.exists())
        self.record("alpha", "turn", "active")
        self.assertEqual(0o700, self.status_dir.stat().st_mode & 0o777)
        self.assertEqual(0o700, self.status_dir.parent.stat().st_mode & 0o777)
        self.assertEqual(["alpha.turn.json"], sorted(p.name for p in self.status_dir.iterdir()))

    def test_record_rejects_bad_values(self):
        for args, code in ((("turn", "working", "test", 60), "invalid_status_state"),
                           (("mood", "active", "test", 60), "invalid_status_dimension"),
                           (("turn", "active", "Bad Source", 60), "invalid_status_source"),
                           (("turn", "active", "test", 0), "invalid_lease_seconds"),
                           (("turn", "active", "test", 86401), "invalid_lease_seconds"),
                           (("wake", "armed", "test", 3601), "invalid_lease_seconds")):
            with self.subTest(code), self.assertRaises(ac.ProtocolError) as caught:
                ac.record_status(self.channel, None, "alpha", "alpha-session", *args)
            self.assertEqual(code, caught.exception.details["code"])

    def test_probe_output_maps_to_wake_states(self):
        self.assertEqual("armed", ac.wake_from_probe(json.dumps(PROBE).encode()))
        self.assertEqual("disarmed", ac.wake_from_probe(json.dumps(dict(PROBE, running=False)).encode()))
        self.assertEqual("disarmed", ac.wake_from_probe(json.dumps(dict(PROBE, paused=True)).encode()))
        for bad in (b"", b"not json", json.dumps(dict(PROBE, running=1)).encode(),
                    json.dumps(dict(PROBE, bound_cli_sha256=123)).encode(),
                    json.dumps(dict(PROBE, bound_cli_sha256="0" * 63)).encode(),
                    json.dumps(dict(PROBE, bound_cli_sha256="Z" * 64)).encode(),
                    json.dumps(dict(PROBE, bound_cli_sha256=None)).encode(),
                    json.dumps(dict(PROBE, extra=True)).encode()):
            self.assertEqual("uncertain", ac.wake_from_probe(bad), bad)

    def test_record_from_probe_on_the_cli(self):
        result = self.cli("status-record", "--as", "alpha", "--session", "alpha-session", "--dimension", "wake",
                          "--from-probe", "-", "--source", "exit-wake", stdin=json.dumps(PROBE).encode())
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("armed", json.loads(result.stdout)["recorded"]["state"])
        self.assertEqual("armed", self.row("alpha")["wake"]["state"])
        empty = self.cli("status-record", "--as", "alpha", "--session", "alpha-session", "--dimension", "wake",
                         "--from-probe", "-", "--source", "exit-wake")
        self.assertEqual("uncertain", json.loads(empty.stdout)["recorded"]["state"], "a failed probe is uncertain")
        self.record("alpha", "wake", "armed")
        big = json.dumps(dict(PROBE, running=False)).encode() + b" " * 5000
        oversized = self.cli("status-record", "--as", "alpha", "--session", "alpha-session", "--dimension", "wake",
                             "--from-probe", "-", "--source", "exit-wake", stdin=big)
        self.assertEqual(0, oversized.returncode, oversized.stderr)
        self.assertEqual("uncertain", self.row("alpha")["wake"]["state"], "oversized output replaces a prior armed")
        turn = self.cli("status-record", "--as", "alpha", "--session", "alpha-session", "--dimension", "turn",
                        "--from-probe", "-", "--source", "exit-wake", stdin=json.dumps(PROBE).encode())
        self.assertEqual("probe_requires_wake", json.loads(turn.stderr)["code"])

    def test_record_never_touches_git_or_receipts(self):
        self.send()
        self.channel.inbox("beta", "beta-session")
        head = ac.git(self.path, "rev-parse", "--verify", ac.REF)
        receipt = (self.path / "ai-comm-runtime" / "beta.json").read_bytes()
        self.record("beta", "turn", "ended")
        self.assertEqual(head, ac.git(self.path, "rev-parse", "--verify", ac.REF))
        self.assertEqual(receipt, (self.path / "ai-comm-runtime" / "beta.json").read_bytes())


if __name__ == "__main__":
    unittest.main()
