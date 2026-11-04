"""Tests for adapters/exit_wake.py. Fakes only: no real channel, host or peer."""
import io
import json
import os
import random
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "adapters"))
import common  # noqa: E402
import exit_wake  # noqa: E402

SESSION_A = "11111111-2222-4333-8444-555555555555"
SESSION_B = "session two / tab 3"
SEED = 20261104


def git(directory, *args):
    subprocess.run(["git", "-C", str(directory), *args], check=True, capture_output=True,
                   env=exit_wake.git_env())


def make_project(base, name="project"):
    """A project root with a one-commit channel repository and an empty pin directory."""
    project = os.path.join(os.path.realpath(base), name)
    os.makedirs(os.path.join(project, ".ai-comm"))
    os.makedirs(os.path.join(project, ".tools", "ai-comm"))
    os.makedirs(os.path.join(project, ".ai-comm-local"))
    channel = os.path.join(project, ".ai-comm")
    git(channel, "init", "-q")
    Path(channel, "README").write_text("channel\n")
    git(channel, "add", "README")
    git(channel, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "root")
    return project


def key_for(project):
    layout = exit_wake.Layout(project, "0" * 16, "x")
    return common.project_key(exit_wake.project_identity(layout))


class FakeCli:
    """Answers state/wait/outstanding calls; records every argv."""

    def __init__(self, handle, session, generation=1, waits=None, participants=None, outstanding=None):
        self.calls = []
        self.waits = list(waits or [])
        self.participants = participants if participants is not None else [
            {"handle": handle, "session": session, "generation": generation}]
        self.outstanding = outstanding
        self.on_wait = None
        self.head = "h" * 40

    def __call__(self, cmd, timeout):
        self.calls.append(list(cmd))
        sub = cmd[cmd.index("--channel") + 2]
        if sub == "registry":
            assert cmd[cmd.index("registry") + 1] == "sessions"
            offset = int(cmd[cmd.index("--offset") + 1])
            limit = int(cmd[cmd.index("--limit") + 1])
            page = self.participants[offset:offset + limit]
            return {"head": self.head, "items": page, "next": offset + len(page),
                    "more": offset + len(page) < len(self.participants)}
        if sub == "wait":
            if self.on_wait:
                self.on_wait()
            cursor = int(cmd[cmd.index("--new-since") + 1])
            item = self.waits.pop(0) if self.waits else None
            if isinstance(item, Exception):
                raise item
            if item is None:
                return {"notification_through": cursor, "messages": []}
            return {"notification_through": item, "messages": [{"seq": item}]}
        if sub == "outstanding":
            return self.outstanding(cmd)
        raise AssertionError("unexpected subcommand")

    def subcommands(self):
        return [c[c.index("--channel") + 2] for c in self.calls]


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = os.path.realpath(self._tmp.name)
        self.project = make_project(self.base)
        self.key = key_for(self.project)

    def argv(self, handle="left", session=SESSION_A, generation=1, project=None, key=None, extra=()):
        return ["--project", project or self.project, "--project-key", key or self.key, "--handle", handle,
                f"--session={session}", "--generation", str(generation), "--rounds", "2", "--wait-timeout", "1",
                *extra]

    def run_main(self, argv, cli):
        out = io.StringIO()
        code = exit_wake.main(argv, runner=cli, out=out, sleep=lambda s: None)
        return code, out.getvalue().strip()

    def state_dir(self, handle="left", project=None, key=None):
        return os.path.join(project or self.project, ".ai-comm-local", "wake", f"{key or self.key}-{handle}")


class ArgumentTests(Base):
    def test_handles_are_opaque(self):
        rng = random.Random(SEED)
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789-"
        handles = ["left", "right", "a", "x" * 32]
        handles += ["".join([rng.choice("abcdefghijklmnopqrstuvwxyz")]
                            + [rng.choice(alphabet) for _ in range(rng.randrange(0, 32))]) for _ in range(20)]
        for handle in handles:
            args = exit_wake.parse_args(self.argv(handle=handle))
            self.assertEqual(args.handle, handle, f"seed {SEED}")

    def test_refusals(self):
        cases = [
            (self.argv(project="relative/p"), "project_path_invalid"),
            (self.argv(project=self.project + "/"), "project_path_invalid"),
            (self.argv(project="/"), "project_path_invalid"),
            (self.argv(key="XYZ"), "project_key_invalid"),
            (self.argv(handle="Left"), "handle_invalid"),
            (self.argv(session="a\nb"), "session_invalid"),
            (self.argv(generation=0), "generation_invalid"),
            (self.argv(generation="01"), "generation_invalid"),
            (self.argv(extra=["--peer", "left"]), "peer_invalid"),
            (self.argv(extra=["--rounds", "0"]), "bounds_invalid"),
            (self.argv(extra=["--overdue-after", "700", "--seen-overdue-after", "650"]), "bounds_invalid"),
            (self.argv() + ["--unknown"], "arguments_invalid"),
            (["--handle", "left"], "arguments_invalid"),
        ]
        for argv, code in cases:
            with self.assertRaises(common.WakeError, msg=code) as ctx:
                exit_wake.parse_args(argv)
            self.assertEqual(ctx.exception.code, code)
            self.assertEqual(ctx.exception.exit_code, 78)

    def test_session_starting_with_dash_is_passed_safely(self):
        cli = FakeCli("left", "-x --as right")
        code, line = self.run_main(self.argv(session="-x --as right"), cli)
        self.assertEqual(code, 0, line)
        wait = cli.calls[1]
        self.assertIn("--session=-x --as right", wait)
        self.assertEqual(wait.count("--as"), 1)


class IdentityTests(Base):
    def test_wrong_project_key_stops(self):
        cli = FakeCli("left", SESSION_A)
        code, line = self.run_main(self.argv(key="0" * 16), cli)
        self.assertEqual((code, line), (78, "WAKE-ERROR project_key_mismatch"))
        self.assertEqual(cli.calls, [])

    def test_identical_channel_history_in_two_paths_gives_two_keys(self):
        other = os.path.join(self.base, "copy")
        shutil.copytree(self.project, other, symlinks=True)
        self.assertNotEqual(key_for(other), self.key)
        code, line = self.run_main(self.argv(project=other), FakeCli("left", SESSION_A))
        self.assertEqual((code, line), (78, "WAKE-ERROR project_key_mismatch"))

    def test_channel_without_history_stops(self):
        bare = os.path.join(self.base, "empty")
        for sub in (".ai-comm", ".tools/ai-comm", ".ai-comm-local"):
            os.makedirs(os.path.join(bare, sub))
        git(os.path.join(bare, ".ai-comm"), "init", "-q")
        code, line = self.run_main(self.argv(project=bare), FakeCli("left", SESSION_A))
        self.assertEqual((code, line), (78, "WAKE-ERROR channel_identity_unavailable"))

    def test_git_hooks_and_user_config_are_ignored(self):
        env = exit_wake.git_env()
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual((env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]), ("core.hooksPath", os.devnull))

    def test_binding_mismatches_fail_closed(self):
        cases = [
            ([], "handle_not_joined"),
            ([{"handle": "left", "session": SESSION_B, "generation": 1}], "session_mismatch"),
            ([{"handle": "left", "session": SESSION_A, "generation": 2}], "generation_mismatch"),
        ]
        for participants, code in cases:
            cli = FakeCli("left", SESSION_A, participants=participants)
            result = self.run_main(self.argv(), cli)
            self.assertEqual(result, (78, f"WAKE-ERROR {code}"))
            self.assertEqual(cli.subcommands(), ["registry"])  # never waits on a stale binding

    def test_takeover_during_run_fails_closed(self):
        error = exit_wake.CliError("session_not_current", "permanent", "unregistered_or_stale_session")
        cli = FakeCli("left", SESSION_A, waits=[None, error])
        self.assertEqual(self.run_main(self.argv(extra=["--rounds", "3"]), cli),
                         (78, "WAKE-ERROR session_not_current"))

    def test_binding_comes_from_the_complete_paged_registry(self):
        """Review 1345 Q1: many participants and history; the current binding is the highest generation."""
        participants = [{"handle": f"peer-{i}", "session": "x" * 150 + str(i), "generation": 1} for i in range(250)]
        participants += [{"handle": "left", "session": "old-session", "generation": 1},
                         {"handle": "left", "session": SESSION_A, "generation": 2}]
        cli = FakeCli("left", SESSION_A, participants=participants, waits=[8])
        self.assertEqual(self.run_main(self.argv(generation=2), cli),
                         (0, "WAKE new messages for left; notification cursor 8"))
        self.assertEqual(cli.subcommands()[:3], ["registry"] * 3)  # 252 records, pages of 100
        stale = FakeCli("left", "old-session", participants=participants)
        self.assertEqual(self.run_main(self.argv(session="old-session", generation=1), stale),
                         (78, "WAKE-ERROR session_mismatch"))

    def test_registry_changing_between_pages_is_transient(self):
        participants = [{"handle": f"peer-{i}", "session": f"s{i}", "generation": 1} for i in range(150)]
        participants.append({"handle": "left", "session": SESSION_A, "generation": 1})
        cli = FakeCli("left", SESSION_A, participants=participants)
        real = FakeCli.__call__

        def moving(cmd, timeout):
            result = real(cli, cmd, timeout)
            cli.head = "g" * 40
            return result

        self.assertEqual(self.run_main(self.argv(), moving), (75, "WAKE-ERROR registry_changed"))

    def test_third_participant_is_tolerated(self):
        participants = [{"handle": h, "session": f"s-{h}", "generation": 1} for h in ("right", "third")]
        participants.append({"handle": "left", "session": SESSION_A, "generation": 1})
        cli = FakeCli("left", SESSION_A, participants=participants, waits=[7])
        self.assertEqual(self.run_main(self.argv(), cli), (0, "WAKE new messages for left; notification cursor 7"))


class RunTests(Base):
    def test_wake_records_cursor_and_cleans_run_record(self):
        cli = FakeCli("left", SESSION_A, waits=[None, 42])
        code, line = self.run_main(self.argv(), cli)
        self.assertEqual((code, line), (0, "WAKE new messages for left; notification cursor 42"))
        state = self.state_dir()
        self.assertEqual(common.read_small_json(os.path.join(state, "cursor.json")),
                         {"v": 1, "notification_through": 42})
        self.assertFalse(os.path.exists(os.path.join(state, "run.json")))
        self.assertEqual(stat.S_IMODE(os.lstat(state).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.lstat(os.path.dirname(state)).st_mode), 0o700)
        # The next run continues from the stored cursor.
        cli2 = FakeCli("left", SESSION_A)
        self.run_main(self.argv(), cli2)
        self.assertIn("42", cli2.calls[1][cli2.calls[1].index("--new-since") + 1])

    def test_expiry(self):
        code, line = self.run_main(self.argv(), FakeCli("left", SESSION_A))
        self.assertEqual((code, line), (0, "WAKE-EXPIRED re-arm; notification cursor 0"))

    def test_run_record_names_full_identity(self):
        seen = {}
        cli = FakeCli("left", SESSION_A)
        cli.on_wait = lambda: seen.setdefault("run", common.read_small_json(
            os.path.join(self.state_dir(), "run.json")))
        self.run_main(self.argv(generation=1), cli)
        run = seen["run"]
        self.assertEqual(run["adapter"], "exit-wake")
        self.assertEqual((run["handle"], run["session"], run["generation"]), ("left", SESSION_A, 1))
        self.assertEqual(run["instance"], f"{self.key}-left")
        self.assertEqual(set(run["project"]), {"path", "channel", "pin_path", "channel_path"})
        self.assertEqual(run["project"]["path"], self.project)

    def test_transient_and_permanent_cli_failures(self):
        cli = FakeCli("left", SESSION_A, waits=[exit_wake.CliError("cli_timed_out")])
        self.assertEqual(self.run_main(self.argv(), cli), (75, "WAKE-ERROR cli_timed_out"))
        cli = FakeCli("left", SESSION_A, waits=[exit_wake.CliError("cli_not_runnable", "permanent")])
        self.assertEqual(self.run_main(self.argv(), cli), (78, "WAKE-ERROR cli_not_runnable"))

    def test_cursor_regression_is_refused(self):
        cli = FakeCli("left", SESSION_A, waits=[5])
        self.run_main(self.argv(), cli)
        bad = FakeCli("left", SESSION_A)
        bad.waits = []

        def regress(cmd, timeout):
            if cmd[cmd.index("--channel") + 2] == "wait":
                return {"notification_through": 3, "messages": []}
            return FakeCli.__call__(bad, cmd, timeout)

        self.assertEqual(self.run_main(self.argv(), regress), (75, "WAKE-ERROR notification_cursor_invalid"))

    def test_commit_uncertain_cursor_is_accepted_after_read_back(self):
        real_write = common.write_private_json

        def uncertain(directory, name, value):
            real_write(directory, name, value)
            if name == "cursor.json":
                raise common.WakeError("state_commit_uncertain", "transient")

        common.write_private_json = uncertain
        try:
            code, line = self.run_main(self.argv(), FakeCli("left", SESSION_A, waits=[9]))
        finally:
            common.write_private_json = real_write
        self.assertEqual((code, line), (0, "WAKE new messages for left; notification cursor 9"))

    def test_corrupt_cursor_is_permanent(self):
        self.run_main(self.argv(), FakeCli("left", SESSION_A))
        Path(self.state_dir(), "cursor.json").write_text('{"v":1,"notification_through":-1}')
        self.assertEqual(self.run_main(self.argv(), FakeCli("left", SESSION_A)), (78, "WAKE-ERROR cursor_invalid"))

    def test_symlinked_state_is_refused(self):
        os.makedirs(os.path.join(self.base, "elsewhere"))
        os.symlink(os.path.join(self.base, "elsewhere"), os.path.join(self.project, ".ai-comm-local", "wake"))
        cli = FakeCli("left", SESSION_A)
        self.assertEqual(self.run_main(self.argv(), cli), (78, "WAKE-ERROR state_path_symlink"))
        self.assertEqual(cli.calls, [])


class IsolationTests(Base):
    def test_second_instance_of_same_handle_is_refused(self):
        state = self.state_dir()
        os.makedirs(state, mode=0o700)
        os.chmod(os.path.dirname(state), 0o700)
        holder = common.acquire_lock(state)
        self.addCleanup(os.close, holder)
        self.assertEqual(self.run_main(self.argv(), FakeCli("left", SESSION_A)),
                         (78, "WAKE-ERROR another_instance_running"))

    def test_left_and_right_run_concurrently_in_one_project(self):
        barrier = threading.Barrier(2, timeout=10)
        results = {}

        def run(handle, session, seq):
            cli = FakeCli(handle, session, waits=[seq])
            cli.on_wait = barrier.wait  # both hold their locks at the same moment
            results[handle] = self.run_main(self.argv(handle=handle, session=session), cli)

        threads = [threading.Thread(target=run, args=("left", SESSION_A, 3)),
                   threading.Thread(target=run, args=("right", SESSION_B, 4))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        self.assertEqual(results["left"], (0, "WAKE new messages for left; notification cursor 3"))
        self.assertEqual(results["right"], (0, "WAKE new messages for right; notification cursor 4"))
        self.assertEqual(sorted(os.listdir(os.path.join(self.project, ".ai-comm-local", "wake"))),
                         sorted([f"{self.key}-left", f"{self.key}-right"]))

    def test_same_handle_in_two_projects_is_independent(self):
        second = make_project(self.base, "second")
        key2 = key_for(second)
        self.assertNotEqual(key2, self.key)
        self.run_main(self.argv(), FakeCli("left", SESSION_A, waits=[11]))
        self.run_main(self.argv(project=second, key=key2), FakeCli("left", SESSION_A, waits=[22]))
        read = common.read_small_json
        self.assertEqual(read(os.path.join(self.state_dir(), "cursor.json"))["notification_through"], 11)
        self.assertEqual(read(os.path.join(self.state_dir(project=second, key=key2), "cursor.json"))
                         ["notification_through"], 22)

    def test_same_handle_name_for_two_sessions_needs_its_own_binding(self):
        # Two sessions of one host may use any labels; the binding, not the name, decides.
        cli = FakeCli("left", SESSION_B)
        self.assertEqual(self.run_main(self.argv(session=SESSION_A), cli), (78, "WAKE-ERROR session_mismatch"))


class PauseTests(Base):
    def test_pause_is_acknowledged_without_cli_calls(self):
        self.run_main(self.argv(), FakeCli("left", SESSION_A))  # creates the state directory
        state = self.state_dir()
        nonce = "a" * 32
        common.write_private_json(state, "pause", {"v": 1, "nonce": nonce})
        cli = FakeCli("left", SESSION_A)
        out = io.StringIO()
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 3:
                ack = common.read_small_json(os.path.join(state, "pause-ack"))
                self.assertEqual(ack["nonce"], nonce)
                self.assertEqual(cli.subcommands(), ["registry"])  # no wait while paused
                os.unlink(os.path.join(state, "pause"))

        code = exit_wake.main(self.argv(), runner=cli, out=out, clock=lambda: 0.0, sleep=sleep)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(os.path.join(state, "pause-ack")))
        self.assertIn("wait", cli.subcommands())

    def test_expiry_while_paused_keeps_the_acknowledgement(self):
        self.run_main(self.argv(), FakeCli("left", SESSION_A))
        state = self.state_dir()
        common.write_private_json(state, "pause", {"v": 1, "nonce": "b" * 32})
        ticks = iter(range(1000))
        cli = FakeCli("left", SESSION_A)
        out = io.StringIO()
        code = exit_wake.main(self.argv(), runner=cli, out=out, clock=lambda: next(ticks), sleep=lambda s: None)
        self.assertEqual((code, out.getvalue().strip()), (0, "WAKE-EXPIRED re-arm; notification cursor 0"))
        self.assertEqual(cli.subcommands(), ["registry"])
        self.assertEqual(common.read_small_json(os.path.join(state, "pause-ack"))["nonce"], "b" * 32)

    def test_malformed_pause_is_permanent(self):
        self.run_main(self.argv(), FakeCli("left", SESSION_A))
        common.write_private_json(self.state_dir(), "pause", {"v": 1, "nonce": "short"})
        self.assertEqual(self.run_main(self.argv(), FakeCli("left", SESSION_A)),
                         (78, "WAKE-ERROR pause_request_invalid"))


class PeerAlarmTests(Base):
    def outstanding(self, ages):
        def answer(cmd):
            cls = cmd[cmd.index("--state") + 1]
            after = int(cmd[cmd.index("--after") + 1])
            info = {"handle": "right", "generation": 1,
                    "receipt": {"present": True, "generation": 1, "read_through": 0, "seen_through": 5}}
            seq = {"unseen": 9, "seen": 3}[cls]
            if cls not in ages or seq <= after:
                return {"peer": info, "count": 2, "matched": 0, "messages": []}
            entry = {"id": f"{seq:08d}-left", "seq": seq, "act": "PROPOSE", "ts": ages[cls], "state": cls,
                     "replied": False}
            return {"peer": info, "count": 2, "matched": 1, "messages": [entry]}
        return answer

    def test_overdue_fires_once(self):
        argv = self.argv(extra=["--peer", "right"])
        wall = lambda: 1_800_000_000.0  # noqa: E731
        old = "2027-01-15T07:00:00Z"  # 3600 s before wall
        cli = FakeCli("left", SESSION_A, outstanding=self.outstanding({"unseen": old}))
        out = io.StringIO()
        code = exit_wake.main(argv, runner=cli, out=out, sleep=lambda s: None, wall=wall)
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().strip(),
                         "PEER-OVERDUE right outstanding 2 unseen 00000009-left sent 2027-01-15T07:00:00Z age 3600s")
        out = io.StringIO()
        exit_wake.main(argv, runner=FakeCli("left", SESSION_A, outstanding=self.outstanding({"unseen": old})),
                       out=out, sleep=lambda s: None, wall=wall)
        self.assertTrue(out.getvalue().startswith("WAKE-EXPIRED"))  # watermark advanced: no repeat



class KindGateTests(Base):
    """Decision 1314: one adapter kind per handle, serialized by instance.lock."""

    def prepare_dir(self):
        state = self.state_dir()
        os.makedirs(state, mode=0o700)
        os.chmod(os.path.dirname(state), 0o700)
        return state

    def test_queue_marker_refuses_startup(self):
        state = self.prepare_dir()
        common.write_private_json(state, "config.json", {"v": 1})
        cli = FakeCli("left", SESSION_A)
        self.assertEqual(self.run_main(self.argv(), cli), (78, "WAKE-ERROR instance_kind_conflict"))
        self.assertEqual(cli.subcommands(), ["registry"])
        self.assertEqual(sorted(os.listdir(state)), ["config.json", "instance.lock", "lock"])  # nothing removed

    def test_dangling_link_marker_counts_and_is_not_followed(self):
        state = self.prepare_dir()
        os.symlink(os.path.join(self.base, "nowhere"), os.path.join(state, "config.json"))
        self.assertEqual(self.run_main(self.argv(), FakeCli("left", SESSION_A)),
                         (78, "WAKE-ERROR instance_kind_conflict"))
        self.assertFalse(os.path.exists(os.path.join(self.base, "nowhere")))

    def test_expiry_only_run_leaves_a_persistent_marker(self):
        self.assertEqual(self.run_main(self.argv(), FakeCli("left", SESSION_A))[0], 0)
        self.assertEqual(common.read_small_json(os.path.join(self.state_dir(), "cursor.json")),
                         {"v": 1, "notification_through": 0})
        self.assertFalse(os.path.exists(os.path.join(self.state_dir(), "run.json")))

    def test_existing_cursor_is_preserved(self):
        state = self.prepare_dir()
        common.write_private_json(state, "cursor.json", {"v": 1, "notification_through": 17})
        cli = FakeCli("left", SESSION_A)
        self.run_main(self.argv(), cli)
        self.assertEqual(cli.calls[1][cli.calls[1].index("--new-since") + 1], "17")
        self.assertEqual(common.read_small_json(os.path.join(state, "cursor.json"))["notification_through"], 17)

    def test_forced_interleaving_never_starts_both_kinds(self):
        """A queue installer holds the gate, publishes its marker and releases while exit-wake waits."""
        state = self.prepare_dir()
        installer = common.acquire_lock(state, name="instance.lock")
        events = []

        def sleep(seconds):
            if installer is not None and not events:
                common.write_private_json(state, "config.json", {"v": 1})
                os.close(installer)
                events.append("queue published")

        cli = FakeCli("left", SESSION_A)
        out = io.StringIO()
        code = exit_wake.main(self.argv(), runner=cli, out=out, sleep=sleep)
        self.assertEqual(events, ["queue published"])
        self.assertEqual((code, out.getvalue().strip()), (78, "WAKE-ERROR instance_kind_conflict"))
        self.assertNotIn("wait", cli.subcommands())
        self.assertFalse(os.path.exists(os.path.join(state, "run.json")))
        self.assertFalse(os.path.exists(os.path.join(state, "cursor.json")))

    def test_removal_intent_blocks_startup_without_changes(self):
        """Decision 1335: an interrupted removal must be completed before a run may start."""
        state = self.prepare_dir()
        common.write_private_json(state, "cursor.json", {"v": 1, "notification_through": 3})
        marker = os.path.join(state, "remove-intent.json")
        for dangling in (False, True):
            if dangling:
                os.symlink(os.path.join(self.base, "missing-intent"), marker)
            else:
                common.write_private_json(state, "remove-intent.json", {"fixture": "installer-owned"})
            before = {n: Path(state, n).read_bytes() for n in os.listdir(state)
                      if not os.path.islink(os.path.join(state, n))}
            cli = FakeCli("left", SESSION_A)
            self.assertEqual(self.run_main(self.argv(), cli), (78, "WAKE-ERROR removal_in_progress"))
            self.assertNotIn("wait", cli.subcommands())
            self.assertFalse(os.path.exists(os.path.join(state, "run.json")))
            after = {n: Path(state, n).read_bytes() for n in os.listdir(state)
                     if not os.path.islink(os.path.join(state, n))}
            self.assertEqual(after, before)  # no lock file created either
            self.assertTrue(os.path.lexists(marker))
            os.unlink(marker)

    def test_intent_published_while_startup_waits_on_the_gate(self):
        state = self.prepare_dir()
        holder = common.acquire_lock(state, name="instance.lock")
        published = []

        def sleep(seconds):
            if not published:
                common.write_private_json(state, "remove-intent.json", {"fixture": "new-intent"})
                os.close(holder)
                published.append(True)

        cli = FakeCli("left", SESSION_A)
        out = io.StringIO()
        code = exit_wake.main(self.argv(), runner=cli, out=out, sleep=sleep)
        self.assertEqual((code, out.getvalue().strip()), (78, "WAKE-ERROR removal_in_progress"))
        self.assertEqual(published, [True])
        self.assertNotIn("wait", cli.subcommands())
        self.assertFalse(os.path.exists(os.path.join(state, "run.json")))

    def test_gate_held_too_long_is_reported(self):
        state = self.prepare_dir()
        holder = common.acquire_lock(state, name="instance.lock")
        self.addCleanup(os.close, holder)
        self.assertEqual(self.run_main(self.argv(), FakeCli("left", SESSION_A)),
                         (78, "WAKE-ERROR another_instance_running"))


class PhaseTests(Base):
    def phase(self, name, generation=1, key=None, clock=lambda: 0.0, sleep=lambda s: None, session=SESSION_A,
              participants=None):
        argv = [name] + self.argv(generation=generation, key=key, session=session)
        out = io.StringIO()
        cli = FakeCli("left", SESSION_A, participants=participants)
        code = exit_wake.main(argv, runner=cli, out=out, clock=clock, sleep=sleep)
        return code, json.loads(out.getvalue())

    def snapshot(self):
        """Every entry of the instance directory with its mode and bytes, or None if absent."""
        state = self.state_dir()
        if not os.path.lexists(state):
            return None
        entries = {".": stat.S_IMODE(os.lstat(state).st_mode)}
        for name in sorted(os.listdir(state)):
            path = os.path.join(state, name)
            entries[name] = (stat.S_IMODE(os.lstat(path).st_mode), Path(path).read_bytes())
        return entries

    def receipt(self, read, seen=None, generation=1):
        runtime = os.path.join(self.project, ".ai-comm", "ai-comm-runtime")
        os.makedirs(runtime, exist_ok=True)
        Path(runtime, "left.json").write_text(json.dumps(
            {"v": 2, "generation": generation, "read": read, "seen": read if seen is None else seen}))

    def setUp(self):
        super().setUp()
        Path(self.project, ".tools", "ai-comm", "ai_comm.py").write_text("# cli\n")
        self.cli_digest = exit_wake.hashlib.sha256(b"# cli\n").hexdigest()

    def test_probe_creates_nothing(self):
        self.receipt(0)
        code, value = self.phase("probe")
        self.assertEqual((code, value), (0, {"running": False, "paused": False, "pending": False,
                                             "bound_cli_sha256": self.cli_digest}))
        self.assertFalse(os.path.exists(os.path.join(self.project, ".ai-comm-local", "wake")))

    def test_probe_sees_a_running_instance(self):
        self.receipt(0)
        seen = {}
        cli = FakeCli("left", SESSION_A)
        cli.on_wait = lambda: seen.setdefault("probe", self.phase("probe"))
        self.run_main(self.argv(), cli)
        self.assertEqual(seen["probe"], (0, {"running": True, "paused": False, "pending": False,
                                             "bound_cli_sha256": self.cli_digest}))

    def test_pause_resume_cycle_while_stopped(self):
        self.receipt(0)
        code, value = self.phase("pause")
        self.assertEqual((code, value["paused"], value["pending"]), (0, True, False))
        self.assertIsNotNone(exit_wake.read_pause_request(self.state_dir()))
        code, value = self.phase("check")
        self.assertEqual((code, value["paused"]), (0, True))
        code, value = self.phase("resume")
        self.assertEqual((code, value["paused"], value["running"]), (0, False, False))
        self.assertEqual(sorted(os.listdir(self.state_dir())), ["phase.lock"])

    def test_pause_with_unread_messages_does_not_complete(self):
        self.receipt(0)
        self.run_main(self.argv(), FakeCli("left", SESSION_A, waits=[4]))  # cursor 4, read 0
        ticks = iter(range(0, 10000, 10))
        code, value = self.phase("pause", clock=lambda: next(ticks))
        self.assertEqual((code, value), (75, {"error": "run_did_not_pause"}))
        self.assertIsNotNone(exit_wake.read_pause_request(self.state_dir()))  # the request stays

    def test_pause_is_honoured_by_a_running_instance(self):
        self.receipt(0)
        started = threading.Event()
        cli = FakeCli("left", SESSION_A)

        def slow_wait():
            started.set()
            time.sleep(0.05)

        cli.on_wait = slow_wait
        result = {}
        run = threading.Thread(target=lambda: result.setdefault("run", exit_wake.main(
            self.argv(extra=["--rounds", "20"]), runner=cli, out=io.StringIO(), sleep=lambda s: time.sleep(0.01))))
        run.start()
        self.assertTrue(started.wait(10))
        code, value = self.phase("pause", sleep=lambda s: time.sleep(0.02))
        self.assertEqual((code, value["paused"], value["running"], value["pending"]), (0, True, True, False))
        waits = cli.subcommands().count("wait")
        time.sleep(0.3)
        self.assertEqual(cli.subcommands().count("wait"), waits)  # no CLI call while paused
        code, value = self.phase("resume")
        self.assertEqual((code, value["paused"]), (0, False))
        run.join(30)
        self.assertEqual(result["run"], 0)
        self.assertGreater(cli.subcommands().count("wait"), waits)

    def test_identity_and_receipt_refusals(self):
        self.receipt(0)
        self.assertEqual(self.phase("probe", key="0" * 16), (78, {"error": "project_key_mismatch"}))
        self.assertEqual(self.phase("probe", generation=2), (78, {"error": "generation_mismatch"}))
        os.unlink(os.path.join(self.project, ".ai-comm", "ai-comm-runtime", "left.json"))
        self.assertEqual(self.phase("probe"), (78, {"error": "receipt_missing"}))

    def test_phase_refuses_queue_instance(self):
        self.receipt(0)
        state = self.state_dir()
        os.makedirs(state, mode=0o700)
        os.chmod(os.path.dirname(state), 0o700)
        common.write_private_json(state, "config.json", {"v": 1})
        self.assertEqual(self.phase("probe"), (78, {"error": "instance_kind_conflict"}))
        self.assertEqual(self.phase("pause"), (78, {"error": "instance_kind_conflict"}))
        self.assertFalse(os.path.exists(os.path.join(state, "pause")))

    def test_non_private_state_is_refused_by_probe(self):
        self.receipt(0)
        self.run_main(self.argv(), FakeCli("left", SESSION_A))
        os.chmod(self.state_dir(), 0o755)
        self.assertEqual(self.phase("probe"), (78, {"error": "state_dir_not_private"}))

    def test_binding_is_checked_before_any_change_when_stopped(self):
        """Review 1326: a stopped instance cannot be probed, paused or resumed under another binding."""
        self.receipt(0)
        self.run_main(self.argv(), FakeCli("left", SESSION_A))  # an existing, stopped instance
        common.write_private_json(self.state_dir(), "pause", {"v": 1, "nonce": "c" * 32})
        cases = [
            ({"session": "wrong-session"}, "session_mismatch"),
            ({"participants": [{"handle": "left", "session": SESSION_A, "generation": 2}]}, "generation_mismatch"),
            ({"participants": [{"handle": "right", "session": SESSION_B, "generation": 1}]}, "handle_not_joined"),
        ]
        for kwargs, code in cases:
            for phase in exit_wake.PHASES:
                before = self.snapshot()
                self.assertEqual(self.phase(phase, **kwargs), (78, {"error": code}), (phase, code))
                self.assertEqual(self.snapshot(), before, (phase, code))

    def test_absent_instance_is_not_created_by_a_refused_phase(self):
        self.receipt(0)
        for phase in ("pause", "resume", "check"):
            self.assertEqual(self.phase(phase, session="wrong-session"), (78, {"error": "session_mismatch"}))
        self.assertFalse(os.path.exists(os.path.join(self.project, ".ai-comm-local", "wake")))

    def test_receipt_problems_refuse_before_writing(self):
        self.run_main(self.argv(), FakeCli("left", SESSION_A))
        before = self.snapshot()
        self.assertEqual(self.phase("pause"), (78, {"error": "receipt_missing"}))
        self.receipt(0, generation=2)
        self.assertEqual(self.phase("pause"), (78, {"error": "receipt_invalid"}))
        self.assertEqual(self.snapshot(), before)

    def run_in_background(self, rounds=20):
        self.receipt(0)
        started = threading.Event()
        cli = FakeCli("left", SESSION_A)

        def slow_wait():
            started.set()
            time.sleep(0.05)

        cli.on_wait = slow_wait
        result = {}
        thread = threading.Thread(target=lambda: result.setdefault("run", exit_wake.main(
            self.argv(extra=["--rounds", str(rounds)]), runner=cli, out=io.StringIO(),
            sleep=lambda s: time.sleep(0.01))))
        thread.start()
        self.assertTrue(started.wait(10))
        return cli, thread, result

    def test_wrong_session_cannot_pause_a_live_run(self):
        cli, thread, result = self.run_in_background()
        before = self.snapshot()
        self.assertEqual(self.phase("pause", session="wrong-session"), (78, {"error": "session_mismatch"}))
        self.assertFalse(os.path.exists(os.path.join(self.state_dir(), "pause")))
        self.assertEqual({k: v for k, v in self.snapshot().items() if k not in ("cursor.json", "run.json")},
                         {k: v for k, v in before.items() if k not in ("cursor.json", "run.json")})
        thread.join(30)
        self.assertEqual(result["run"], 0)

    def test_wrong_session_cannot_resume_a_paused_run(self):
        cli, thread, result = self.run_in_background(rounds=40)
        self.assertEqual(self.phase("pause", sleep=lambda s: time.sleep(0.02))[1]["paused"], True)
        waits = cli.subcommands().count("wait")
        self.assertEqual(self.phase("resume", session="wrong-session"), (78, {"error": "session_mismatch"}))
        time.sleep(0.3)
        self.assertTrue(os.path.exists(os.path.join(self.state_dir(), "pause")))
        self.assertTrue(os.path.exists(os.path.join(self.state_dir(), "pause-ack")))
        self.assertEqual(cli.subcommands().count("wait"), waits)  # still paused
        self.assertEqual(self.phase("resume")[0], 0)
        thread.join(30)
        self.assertEqual(result["run"], 0)

    def test_mismatched_live_run_record_refuses_before_pause(self):
        cli, thread, result = self.run_in_background()
        run_path = os.path.join(self.state_dir(), "run.json")
        record = common.read_small_json(run_path)
        record["generation"] = 2
        common.write_private_json(self.state_dir(), "run.json", record)
        self.assertEqual(self.phase("pause"), (78, {"error": "run_identity_unverified"}))
        self.assertFalse(os.path.exists(os.path.join(self.state_dir(), "pause")))
        thread.join(30)

    def test_removal_intent_blocks_every_phase(self):
        self.receipt(0)
        self.run_main(self.argv(), FakeCli("left", SESSION_A))
        common.write_private_json(self.state_dir(), "remove-intent.json", {"fixture": "installer-owned"})
        for phase in exit_wake.PHASES:
            before = self.snapshot()
            self.assertEqual(self.phase(phase), (78, {"error": "removal_in_progress"}), phase)
            self.assertEqual(self.snapshot(), before, phase)

    def test_bad_arguments_are_json(self):
        out = io.StringIO()
        self.assertEqual(exit_wake.main(["probe", "--handle", "Left"], out=out), 78)
        self.assertEqual(json.loads(out.getvalue()), {"error": "arguments_invalid"})

class LaunchTests(unittest.TestCase):
    """The real launch line: python3 -I <dir>/exit_wake.py, run from a copy outside the package."""

    def test_isolated_launch_imports_its_sibling_and_writes_no_bytecode(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.realpath(tmp)
            bundle = os.path.join(base, "bundle")
            os.makedirs(bundle, 0o700)
            for name in ("exit_wake.py", "common.py"):
                shutil.copy2(ROOT / "adapters" / name, bundle)
            project = make_project(base)
            Path(project, ".tools", "ai-comm", "ai_comm.py").write_text(textwrap.dedent("""\
                import json, sys
                sub = sys.argv[3]
                if sub == "registry":
                    print(json.dumps({"head": "h", "next": 1, "more": False,
                                      "items": [{"handle": "left", "session": "s-1", "generation": 1}]}))
                elif sub == "wait":
                    print(json.dumps({"notification_through": 5, "messages": [{"seq": 5}]}))
                else:
                    sys.exit(2)
                """))
            key = key_for(project)
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": base}
            result = subprocess.run(
                [sys.executable, "-I", os.path.join(bundle, "exit_wake.py"), "--project", project,
                 "--project-key", key, "--handle", "left", "--session=s-1", "--generation", "1",
                 "--rounds", "1", "--wait-timeout", "1"],
                capture_output=True, text=True, timeout=60, cwd=base, env=env)
            self.assertEqual((result.returncode, result.stdout.strip(), result.stderr),
                             (0, "WAKE new messages for left; notification cursor 5", ""))
            self.assertEqual(sorted(os.listdir(bundle)), ["common.py", "exit_wake.py"])
            runtime = os.path.join(project, ".ai-comm", "ai-comm-runtime")
            os.makedirs(runtime)
            Path(runtime, "left.json").write_text('{"v":2,"generation":1,"read":5,"seen":5}')
            probe = subprocess.run(
                [sys.executable, "-I", os.path.join(bundle, "exit_wake.py"), "probe", "--project", project,
                 "--project-key", key, "--handle", "left", "--session=s-1", "--generation", "1"],
                capture_output=True, text=True, timeout=60, cwd=base, env=env)
            self.assertEqual((probe.returncode, probe.stderr), (0, ""))
            self.assertEqual(json.loads(probe.stdout)["pending"], False)
            self.assertEqual(sorted(os.listdir(bundle)), ["common.py", "exit_wake.py"])


class RealCliTests(unittest.TestCase):
    """Design 022 section 5 with the real CLI and a real channel: no fake runner, no fake binding."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="wake-real-")
        self.addCleanup(temp.cleanup)
        self.project = os.path.join(os.path.realpath(temp.name), "project")
        self.pin = Path(self.project, ".tools", "ai-comm")
        self.pin.mkdir(parents=True)
        for name in ("ai_comm.py", "PROTOCOL.md", "RELEASE-MANIFEST.json"):
            shutil.copy2(ROOT / name, self.pin / name)
        os.mkdir(os.path.join(self.project, ".ai-comm-local"), 0o700)
        self.env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
                    "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
                    "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        self.cli("init", "--task", "wake test", "--reviewers", "left", "right")

    def cli(self, *args):
        done = subprocess.run([sys.executable, "-I", "-B", str(self.pin / "ai_comm.py"), "--channel",
                               os.path.join(self.project, ".ai-comm"), *args], capture_output=True, text=True,
                              env=self.env, timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout[-300:] + done.stderr[-300:])
        return done.stdout

    def join(self, handle, session):
        self.cli("join", "--as", handle, "--platform", "host", "--session", session)

    def send(self, sender, session, to, key):
        path = os.path.join(self.project, ".ai-comm-local", key + ".json")
        Path(path).write_text(json.dumps({"act": "ASK", "to": to, "topic": "t", "summary": "ping", "body": "b"}))
        self.cli("send", "--as", sender, "--session", session, "--key", key, "--draft", path)

    def argv(self, handle, session, rounds=1):
        return ["--project", self.project, "--project-key", key_for(self.project), "--handle", handle,
                f"--session={session}", "--generation", "1", "--rounds", str(rounds), "--wait-timeout", "1"]

    def run_adapter(self, handle, session, rounds=1):
        out = io.StringIO()
        return exit_wake.main(self.argv(handle, session, rounds), out=out), out.getvalue().strip()

    def test_solo_then_a_late_join(self):
        self.join("left", SESSION_A)
        self.assertEqual(self.run_adapter("left", SESSION_A), (0, "WAKE-EXPIRED re-arm; notification cursor 0"))
        self.join("right", SESSION_B)
        self.send("right", SESSION_B, ["left"], "late")
        code, line = self.run_adapter("left", SESSION_A)
        self.assertEqual(code, 0, line)
        self.assertTrue(line.startswith("WAKE new messages for left"), line)

    def test_simultaneous_delivery_to_two_handles(self):
        self.join("left", SESSION_A)
        self.join("right", SESSION_B)
        self.join("third", "third-session")
        self.send("third", "third-session", ["left", "right"], "both")
        results = {}
        threads = [threading.Thread(target=lambda h=h, s=s: results.setdefault(h, self.run_adapter(h, s)))
                   for h, s in (("left", SESSION_A), ("right", SESSION_B))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        for handle in ("left", "right"):
            self.assertEqual(results[handle][0], 0, results[handle])
            self.assertTrue(results[handle][1].startswith(f"WAKE new messages for {handle}"), results[handle])

    def test_a_real_takeover_stops_a_live_run_and_the_old_binding(self):
        self.join("left", SESSION_A)
        live = subprocess.Popen([sys.executable, "-I", str(ROOT / "adapters" / "exit_wake.py"),
                                 *self.argv("left", SESSION_A, rounds=30)], stdout=subprocess.PIPE, text=True)
        run_record = os.path.join(self.project, ".ai-comm-local", "wake", f"{key_for(self.project)}-left", "run.json")
        for _ in range(200):
            if os.path.exists(run_record):
                break
            time.sleep(0.05)
        self.cli("join", "--as", "left", "--platform", "host", "--session", "new-session", "--takeover")
        output, _ = live.communicate(timeout=60)
        self.assertEqual((live.returncode, output.strip()), (78, "WAKE-ERROR session_not_current"))
        self.assertEqual(self.run_adapter("left", SESSION_A), (78, "WAKE-ERROR session_mismatch"))


class NoContentTests(unittest.TestCase):
    def test_never_reads_inbox_acknowledges_or_sends(self):
        source = (ROOT / "adapters" / "exit_wake.py").read_text(encoding="utf-8")
        for sub in ('"inbox"', '"ack"', '"send"', '"join"'):
            self.assertNotIn(sub, source)


if __name__ == "__main__":
    unittest.main()
