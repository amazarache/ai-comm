"""Tests for turn observations recorded from host hook events (docs/STATUS.md, "Turn from host hooks")."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "ai_comm.py"
EXAMPLE = SCRIPT.parent / "docs" / "turn-hooks.example.json"
sys.path.insert(0, str(SCRIPT.parent))
import ai_comm as ac  # noqa: E402

SECRET = "PROMPT-TEXT-MUST-NOT-BE-STORED"


class TurnHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ai-comm-hooks-")
        self.root = Path(self.temp.name)
        self.path = self.root / "channel"
        ac.Channel.init(self.path, "Review a generic change", ["alpha", "beta"])
        self.channel = ac.Channel(self.path)
        self.channel.join("alpha", "host-a", "alpha-session")
        self.channel.join("beta", "host-b", "beta-session")
        self.status_dir = self.root / ".ai-comm-local" / "status" / "v1" / ac.channel_identity(self.channel)[:16]

    def tearDown(self):
        self.temp.cleanup()

    def hook(self, event, session="alpha-session", generation="1", extra_args=(), **fields):
        payload = dict({"hook_event_name": event, "session_id": session, "cwd": "/somewhere",
                        "prompt": SECRET, "last_assistant_message": SECRET, "tool_input": {"command": SECRET}},
                       **fields)
        return self.run_hook(json.dumps(payload).encode(), generation=generation, extra_args=extra_args)

    def run_hook(self, stdin, generation="1", extra_args=()):
        args = [sys.executable, "-I", str(SCRIPT), "--channel", str(self.path), "status-record", "--as", "alpha",
                "--session", "alpha-session", "--generation", generation, "--dimension", "turn", "--from-hook", "-",
                "--source", "host-hooks", *extra_args]
        return subprocess.run(args, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)

    def turn(self):
        row = next(r for r in ac.status_view(self.channel)["participants"] if r["handle"] == "alpha")
        return row["turn"]["state"], row["turn"].get("lease_seconds"), row["summary"]

    def sidecar_text(self):
        target = self.status_dir / "alpha.turn.json"
        return target.read_text() if target.exists() else ""

    def assert_silent(self, result):
        self.assertEqual((0, b"", b""), (result.returncode, result.stdout, result.stderr))

    def test_events_map_to_turn_states_with_their_leases(self):
        cases = [("UserPromptSubmit", {}, "active", 900), ("PostToolUse", {}, "active", 900),
                 ("PostToolUseFailure", {}, "active", 900), ("PermissionRequest", {}, "waiting", 86400),
                 ("Notification", {"notification_type": "permission_prompt"}, "waiting", 86400),
                 ("Notification", {"notification_type": "elicitation_dialog"}, "waiting", 86400),
                 ("Stop", {}, "ended", 86400), ("StopFailure", {}, "ended", 86400),
                 ("Interrupt", {}, "ended", 86400), ("SessionEnd", {}, "ended", 86400)]
        cases += [("SessionStart", {"source": s}, "ended", 86400) for s in ("startup", "resume", "clear", "fork")]
        for event, fields, state, lease in cases:
            with self.subTest(event=event, **fields):
                self.assert_silent(self.hook("UserPromptSubmit" if state != "active" else "Stop"))
                self.assert_silent(self.hook(event, **fields))
                self.assertEqual((state, lease), self.turn()[:2])

    def test_events_that_change_nothing_keep_the_current_state(self):
        self.assert_silent(self.hook("UserPromptSubmit"))
        for event, fields in (("SessionStart", {"source": "compact"}), ("SessionStart", {"source": "other"}),
                              ("SessionStart", {}), ("Notification", {"notification_type": "idle_prompt"}),
                              ("Notification", {"notification_type": ["permission_prompt"]}),
                              ("Notification", {}), ("PreToolUse", {}), ("PreCompact", {}), ("SubagentStop", {}),
                              ("Unknown", {})):
            with self.subTest(event=event, **{k: str(v) for k, v in fields.items()}):
                self.assert_silent(self.hook(event, **fields))
                self.assertEqual("active", self.turn()[0], "a mid-turn compaction or other event keeps active")

    def test_waiting_reads_as_waiting_for_approval_and_clears(self):
        self.assert_silent(self.hook("UserPromptSubmit"))
        self.assert_silent(self.hook("Notification", notification_type="permission_prompt"))
        self.assertEqual(("waiting", "waiting for approval"), (self.turn()[0], self.turn()[2]))
        self.assert_silent(self.hook("PostToolUse"))
        self.assertEqual(("active", "working"), (self.turn()[0], self.turn()[2]))
        self.assert_silent(self.hook("PermissionRequest"))
        self.assert_silent(self.hook("Stop"))
        self.assertEqual("ended", self.turn()[0], "a denial ends at the next Stop")

    def test_other_sessions_and_wrong_bindings_record_nothing(self):
        self.assert_silent(self.hook("Stop"))
        before = self.sidecar_text()
        for session in ("beta-session", "other-session", "", None, 7):
            self.assert_silent(self.hook("UserPromptSubmit", session=session))
            self.assertEqual(before, self.sidecar_text())
        self.assertEqual("ended", self.turn()[0])

    def test_a_stale_binding_writes_but_shows_unknown(self):
        """The fast path stamps the declared binding; the view checks it against the current join."""
        self.assert_silent(self.hook("UserPromptSubmit", generation="2"))
        self.assertEqual(2, json.loads(self.sidecar_text())["generation"])
        self.assertEqual("unknown", self.turn()[0])

    def test_content_fields_are_never_written(self):
        for event in ("UserPromptSubmit", "Stop", "PostToolUse"):
            self.assert_silent(self.hook(event))
            self.assertNotIn(SECRET, self.sidecar_text())
            self.assertEqual({"v", "channel", "handle", "session", "generation", "dimension", "state", "source",
                              "observed_utc", "lease_seconds"}, set(json.loads(self.sidecar_text())))

    def test_malformed_oversized_and_invalid_calls_are_silent_and_exit_zero(self):
        self.assert_silent(self.hook("Stop"))
        before = self.sidecar_text()
        bad_inputs = (b"", b"not json", b"[]", b"\xff\xfe", b'{"hook_event_name": "Stop"}',
                      json.dumps({"hook_event_name": "Stop", "session_id": "alpha-session",
                                  "pad": "x" * (ac.HOOK_INPUT_LIMIT + 10)}).encode())
        for data in bad_inputs:
            with self.subTest(data=data[:30]):
                self.assert_silent(self.run_hook(data))
                self.assertEqual(before, self.sidecar_text())
        for generation, extra in (("zero", ()), ("0", ()), ("1", ("--unknown-flag",)), ("1", ("--help",))):
            with self.subTest(generation=generation, extra=extra):
                self.assert_silent(self.hook("UserPromptSubmit", generation=generation, extra_args=extra))
                self.assertEqual(before, self.sidecar_text())

    def test_a_missing_channel_or_unwritable_directory_is_still_silent(self):
        args = [sys.executable, "-I", str(SCRIPT), "--channel", str(self.root / "absent"), "status-record",
                "--as", "alpha", "--session", "alpha-session", "--generation", "1", "--dimension", "turn",
                "--from-hook", "-", "--source", "host-hooks"]
        payload = json.dumps({"hook_event_name": "Stop", "session_id": "alpha-session"}).encode()
        result = subprocess.run(args, input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assert_silent(result)
        self.status_dir.parent.mkdir(mode=0o700, parents=True)
        self.status_dir.mkdir(mode=0o500)
        try:
            self.assert_silent(self.hook("Stop"))
        finally:
            self.status_dir.chmod(0o700)

    def main_hook(self, event="Stop"):
        """Run the real CLI entry point in-process, as a hook would, capturing both streams."""
        argv = ["--channel", str(self.path), "status-record", "--as", "alpha", "--session", "alpha-session",
                "--generation", "1", "--dimension", "turn", "--from-hook", "-", "--source", "host-hooks"]
        payload = json.dumps({"hook_event_name": event, "session_id": "alpha-session"}).encode()
        out, err = io.StringIO(), io.StringIO()
        saved = sys.stdin, sys.stdout, sys.stderr
        sys.stdin = io.TextIOWrapper(io.BytesIO(payload))
        sys.stdout, sys.stderr = out, err
        try:
            code = ac.main(argv)
            restored = sys.stdout is out and sys.stderr is err
        finally:
            sys.stdin, sys.stdout, sys.stderr = saved
        return code, out.getvalue(), err.getvalue(), restored

    def test_hook_mode_writes_without_reading_the_ledger(self):
        def trap(_self):
            raise AssertionError("the hook path read the ledger")
        with mock.patch.object(ac.Channel, "snapshot", trap):
            self.assertEqual((0, "", "", True), self.main_hook("Stop"))
        self.assertEqual("ended", json.loads(self.sidecar_text())["state"], "the observation was really written")

    def test_a_sink_that_fails_to_open_or_close_is_still_silent(self):
        real_open = open

        class ClosingFails(io.StringIO):
            def close(self):
                raise OSError("close failed")

        def failing(kind):
            def fake(path, *args, **kwargs):
                if path == os.devnull:
                    if kind == "open":
                        raise OSError("open failed")
                    return ClosingFails()
                return real_open(path, *args, **kwargs)
            return fake

        with mock.patch.object(ac, "open", failing("open"), create=True):
            self.assertEqual((0, "", "", True), self.main_hook("Stop"))
        self.assertEqual("", self.sidecar_text(), "no observation when the sink cannot be set up")
        with mock.patch.object(ac, "open", failing("close"), create=True):
            self.assertEqual((0, "", "", True), self.main_hook("Stop"))
        self.assertEqual("ended", json.loads(self.sidecar_text())["state"], "a cleanup failure loses nothing")

    def test_host_order_replays(self):
        """Synchronous hooks are delivered in event order; each replay ends in the state of its last event."""
        sequences = ((["UserPromptSubmit", "PostToolUse", "Stop"], "ended"),
                     (["Stop", "UserPromptSubmit"], "active"),
                     (["UserPromptSubmit", "PermissionRequest", "PostToolUse"], "active"),
                     (["UserPromptSubmit", "Interrupt"], "ended"))
        for events, final in sequences:
            with self.subTest(events=events):
                for event in events:
                    self.assert_silent(self.hook(event))
                self.assertEqual(final, self.turn()[0])

    def test_why_hooks_must_not_run_in_the_background(self):
        """An older event whose recorder only runs after a newer state was written does overwrite it. Nothing in
        the recorder can tell, so the configuration must keep every hook synchronous (next test)."""
        self.assert_silent(self.hook("Stop"))  # newer event, already recorded
        self.assert_silent(self.hook("PostToolUse"))  # an older event's delayed recorder
        self.assertEqual("active", self.turn()[0])

    def test_the_example_configuration_is_synchronous_and_complete(self):
        config = json.loads(EXAMPLE.read_text())
        events = set(config["hooks"])
        self.assertEqual(set(ac.HOOK_EVENT_STATES) | {"Notification", "SessionStart"}, events)
        for event, groups in config["hooks"].items():
            for group in groups:
                for handler in group["hooks"]:
                    with self.subTest(event=event):
                        self.assertNotIn("async", handler)
                        self.assertNotIn("asyncRewake", handler)
                        self.assertEqual("command", handler["type"])
                        self.assertLessEqual(handler["timeout"], 5)
                        self.assertIn("status-record", handler["command"])
                        self.assertIn("--from-hook -", handler["command"])
                        self.assertIn("--generation", handler["command"])
        notification = config["hooks"]["Notification"][0]["matcher"].split("|")
        self.assertEqual(set(ac.HOOK_NOTIFICATION_STATES), set(notification))

    def test_generation_is_refused_outside_hook_mode(self):
        result = subprocess.run([sys.executable, "-I", str(SCRIPT), "--channel", str(self.path), "status-record",
                                 "--as", "alpha", "--session", "alpha-session", "--generation", "1",
                                 "--dimension", "turn", "--state", "active", "--source", "test"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(2, result.returncode)
        self.assertEqual("generation_requires_hook", json.loads(result.stderr)["code"])

    def test_a_hook_call_takes_well_under_a_second(self):
        import time
        start = time.monotonic()
        self.assert_silent(self.hook("Stop"))
        self.assertLess(time.monotonic() - start, 2.0)


if __name__ == "__main__":
    unittest.main()
