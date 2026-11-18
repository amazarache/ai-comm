"""Tests for the read-only `watch` view (docs/WATCH.md)."""
import hashlib
import io
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "ai_comm.py"
sys.path.insert(0, str(SCRIPT.parent))
import ai_comm as ac  # noqa: E402


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ai-comm-watch-")
        self.path = Path(self.temp.name) / "channel"
        ac.Channel.init(self.path, "Review a generic change", ["alpha", "beta"])
        self.channel = ac.Channel(self.path)
        self.channel.join("alpha", "host-a", "alpha-session")
        self.channel.join("beta", "host-b", "beta-session")
        self.keys = 0

    def tearDown(self):
        self.temp.cleanup()

    def send(self, author="alpha", **extra):
        self.keys += 1
        to = ["beta"] if author == "alpha" else ["alpha"]
        draft = dict({"act": "FACT", "to": to, "topic": "review", "summary": f"Message {self.keys}."}, **extra)
        return self.channel.send(author, author + "-session", f"k{self.keys}", draft)["message"]

    def cli(self, *args, **kwargs):
        return subprocess.run([sys.executable, "-I", str(SCRIPT), "--channel", str(self.path), *args],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, **kwargs)

    def tree_digest(self):
        digest = hashlib.sha256()
        for root, dirs, files in sorted(os.walk(self.path)):
            dirs.sort()
            digest.update(("dir:" + str(Path(root).relative_to(self.path))).encode())
            for name in sorted(files):
                full = Path(root) / name
                digest.update(str(full.relative_to(self.path)).encode())
                digest.update(full.read_bytes())
        return digest.hexdigest()

    def test_text_line_shows_time_seq_route_act_topic_reply_and_summary(self):
        first = self.send()
        reply = self.send(author="beta", reply_to=first["id"])
        out = io.StringIO()
        self.assertEqual(0, ac.run_watch(self.channel, 0, False, 2.0, "text", out=out))
        lines = out.getvalue().splitlines()
        self.assertEqual(2, len(lines))
        stored = self.channel.snapshot().by_id[reply["id"]]
        self.assertEqual(f"{stored['ts']} #2 beta -> alpha FACT [review] re:#1 Message 2.", lines[1])
        self.assertNotIn("re:", lines[0])

    def test_jsonl_lines_are_json_with_routing_fields_only(self):
        self.send(body="body text is not shown")
        result = self.cli("watch", "--after", "0", "--format", "jsonl")
        self.assertEqual(0, result.returncode, result.stderr)
        record = json.loads(result.stdout.decode().splitlines()[0])
        self.assertEqual({"seq", "id", "ts", "from", "to", "act", "topic", "summary"}, set(record))
        self.assertNotIn("body text", result.stdout.decode())

    def test_default_shows_the_last_ten_and_after_selects(self):
        for _ in range(12):
            self.send()
        self.assertEqual(10, len(self.cli("watch").stdout.decode().splitlines()))
        lines = self.cli("watch", "--after", "10").stdout.decode().splitlines()
        self.assertEqual(["#11", "#12"], [line.split()[1] for line in lines])

    def test_watch_writes_nothing(self):
        self.send()
        self.channel.inbox("beta", "beta-session")
        before = self.tree_digest()
        self.assertEqual(0, self.cli("watch", "--after", "0").returncode)
        self.assertEqual(0, self.cli("watch", "--format", "jsonl").returncode)
        self.assertEqual(before, self.tree_digest())

    def test_follow_rebuilds_the_snapshot_only_when_the_head_changes(self):
        self.send()
        calls, sleeps = [], []
        real = self.channel.snapshot

        sending = []

        def counting():
            if not sending:
                calls.append(1)
            return real()

        def sleep(_seconds):
            sleeps.append(1)
            if len(sleeps) == 2:
                sending.append(1)
                self.send()
                sending.clear()
            if len(sleeps) == 4:
                raise KeyboardInterrupt
        self.channel.snapshot = counting
        out = io.StringIO()
        self.assertEqual(0, ac.run_watch(self.channel, 0, True, 0.2, "text", out=out, sleep=sleep))
        self.assertEqual(["#1", "#2"], [line.split()[1] for line in out.getvalue().splitlines()])
        self.assertEqual(2, len(calls), "one snapshot at start, one after the new commit")

    def test_follow_prints_a_message_committed_by_another_process(self):
        self.send()
        process = subprocess.Popen([sys.executable, "-I", str(SCRIPT), "--channel", str(self.path), "watch",
                                    "--after", "0", "--follow", "--interval", "0.2"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        lines = queue.Queue()
        reader = threading.Thread(target=lambda: [lines.put(line) for line in process.stdout], daemon=True)
        reader.start()

        def next_line():
            try:
                return lines.get(timeout=30)
            except queue.Empty:
                self.fail("watch printed no line within 30 seconds")
        try:
            self.assertIn("#1 ", next_line())
            self.send(author="beta")
            self.assertIn("#2 beta -> alpha FACT", next_line())
        finally:
            process.kill()
            process.wait(timeout=30)
            process.stdout.close()
            process.stderr.close()

    def test_a_reader_that_closes_early_ends_watch_cleanly(self):
        self.send()
        self.send()
        for extra in ([], ["--follow", "--interval", "0.2"]):
            read_end, write_end = os.pipe()
            os.close(read_end)
            process = subprocess.Popen([sys.executable, "-I", str(SCRIPT), "--channel", str(self.path), "watch",
                                        "--after", "0", *extra], stdout=write_end, stderr=subprocess.PIPE)
            os.close(write_end)
            try:
                code = process.wait(timeout=60)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=30)
            stderr = process.stderr.read()
            process.stderr.close()
            self.assertEqual((0, b""), (code, stderr), extra)

    def test_invalid_arguments_are_json_errors(self):
        for args, code in ((["--interval", "0.01"], "invalid_watch_interval"),
                           (["--after", "-1"], "invalid_after"),
                           (["--format", "html"], "invalid_cli_arguments")):
            result = self.cli("watch", *args)
            self.assertEqual(2, result.returncode)
            self.assertEqual(code, json.loads(result.stderr)["code"], args)


if __name__ == "__main__":
    unittest.main()
