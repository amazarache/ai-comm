"""Channel selection regressions: temporary projects and fake services only."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
from unittest import mock

from tests import test_wake_install as installer
from tests import test_exit_wake as exit_tests


class InstallerChannelTests(installer.Base):
    def channel(self, name=".ai-comm-v3", copy=False):
        path = self.project / name
        if copy:
            shutil.copytree(self.project / ".ai-comm", path)
        else:
            path.mkdir()
            installer.git(path, "init", "-q")
            installer.git(path, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                          "commit", "-q", "--allow-empty", "-m", name)
        return path

    def install_channel(self, channel, kind="exit"):
        extra = ["--channel", str(channel)]
        plan = self.plan(kind=kind, extra=extra)
        code, result = self.run_wake(self.args("install", kind=kind, extra=extra + [
            "--plan-sha256", plan["plan_sha256"], "--confirm"]))
        self.assertEqual(code, 0, result)
        return plan

    def integration_channel(self, plan, channel=None):
        extra = ["--bundle-sha256", plan["bundle"]["sha256"], "--pair", "1" * 40, "2" * 40]
        if channel is not None:
            extra += ["--channel", str(channel)]
        code, result = self.run_wake(self.args("integration", extra=extra))
        self.assertEqual(code, 0, result)
        return result

    def queue_state(self, plan):
        queue = installer.QueueTests.queue_module(self)
        directory = Path(plan["config_path"]).parent
        queue.Store(directory, plan["config"]).save(queue.initial_state(plan["config"]))
        return directory

    def test_frozen_default_does_not_hide_selected_live_channel(self):
        default = self.project / ".ai-comm"
        live = self.project / ".ai-comm-v3"
        live.mkdir()
        installer.git(live, "init", "-q")
        installer.git(live, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                      "commit", "-q", "--allow-empty", "-m", "successor")
        selected = []

        def registry(cli, channel):
            selected.append(str(channel))
            if str(channel) == str(default):
                raise installer.wake.WakeError("channel_state_unavailable", "transient")
            self.assertEqual(str(channel), str(live))
            return {"participants": self.host.participants}

        before = self.everything()
        with mock.patch.object(self.host, "registry", registry):
            self.assertEqual(self.run_wake(self.args("plan")),
                             (75, {"status": "stopped", "code": "channel_state_unavailable"}))
            self.assertEqual(selected, [str(default)])
            selected.clear()
            code, plan = self.run_wake(self.args("plan", extra=["--channel", str(live)]))
            self.assertEqual(code, 0, plan)
            self.assertEqual(plan["project"]["channel_path"], str(live))
            self.assertEqual(selected, [str(live)])
        self.assertEqual(self.everything(), before)

    def test_default_forms_have_identical_plans_and_items(self):
        for kind in ("exit", "queue"):
            expected = self.plan(kind=kind)
            for channel in (str(self.project / ".ai-comm"), ".ai-comm", "./.ai-comm/"):
                with self.subTest(kind=kind, channel=channel):
                    self.assertEqual(self.plan(kind=kind, extra=["--channel", channel]), expected)
                    self.assertNotIn("--channel", expected["steps"][0])
        plan = self.install_channel(".ai-comm")
        expected = self.integration_channel(plan)
        for channel in (str(self.project / ".ai-comm"), ".ai-comm", "./.ai-comm/"):
            self.assertEqual(self.integration_channel(plan, channel), expected)
        self.assertNotIn("--channel", plan["launch"])

    def test_alternate_only_install_remove_and_frozen_exit_phases(self):
        live = self.project / ".ai-comm-v3"
        (self.project / ".ai-comm").rename(live)
        self.assertEqual(self.run_wake(self.args("plan"))[1]["code"], "project_invalid")
        plan = self.install_channel(live.name)
        for argv in (plan["launch"], plan["steps"][0]):
            self.assertEqual(argv[argv.index("--channel") + 1], str(live))
        item = self.integration_channel(plan, live.name)["item"]
        for argv in [item["probe"]["argv"], *item["steps"].values()]:
            self.assertEqual(argv[argv.index("--channel") + 1], str(live))
        # Execute the actual frozen adapter, with a fixture CLI and no controller import.
        (self.pin / "ai_comm.py").write_text(
            "import json, sys\n"
            f"assert sys.argv[1:4] == ['--channel', {str(live)!r}, 'registry']\n"
            f"print(json.dumps({{'head':'h','next':1,'more':False,'items':"
            f"[{{'handle':'left','session':{installer.SESSION!r},'generation':1}}]}}))\n")
        runtime = live / "ai-comm-runtime"
        runtime.mkdir()
        (runtime / "left.json").write_text('{"v":2,"generation":1,"read":0,"seen":0}')
        for phase in ("probe", "pause", "rebind", "check", "resume"):
            argv = item["probe"]["argv"] if phase == "probe" else item["steps"][phase]
            done = subprocess.run(argv, capture_output=True, timeout=15)
            self.assertEqual(done.returncode, 0, (phase, done.stdout, done.stderr))
        code, result = self.run_wake(self.args("remove", extra=["--channel", live.name, "--confirm"]))
        self.assertEqual(code, 0, result)
        directory = self.project / ".ai-comm-local" / "wake" / plan["instance"]
        self.assertTrue(set(os.listdir(directory)) <= installer.wake.LOCK_NAMES)
        self.assertTrue(Path(plan["bundle"]["dir"]).is_dir())

    def test_relative_path_does_not_depend_on_working_directory(self):
        live = self.channel()
        absolute = self.plan(extra=["--channel", str(live)])
        with mock.patch.object(os, "getcwd", return_value=str(self.base)):
            self.assertEqual(self.plan(extra=["--channel", live.name]), absolute)

    def test_binding_and_plan_digest_use_selected_channel(self):
        live = self.channel()
        old = self.plan()
        selected = self.plan(extra=["--channel", str(live)])
        self.assertNotEqual(old["plan_sha256"], selected["plan_sha256"])
        before = self.everything()
        code, result = self.run_wake(self.args("install", extra=["--channel", str(live),
            "--plan-sha256", old["plan_sha256"], "--confirm"]))
        self.assertEqual((code, result["code"]), (78, "plan_changed"))
        self.assertEqual(self.everything(), before)

        def registry(cli, channel):
            session = installer.SESSION if str(channel) == str(live) else installer.OTHER
            return {"participants": [{"handle": "left", "session": session, "generation": 1}]}

        with mock.patch.object(self.host, "registry", registry):
            self.assertEqual(self.run_wake(self.args("plan"))[1]["code"], "session_mismatch")
            self.assertEqual(self.plan(extra=["--channel", live.name]), selected)

    def test_queue_roundtrip_preserves_other_channel_pin_and_locks(self):
        live = self.channel()
        other = self.install_channel(".ai-comm", "queue")
        self.queue_state(other)
        old_paths = (str(self.project / ".ai-comm"), str(self.pin),
                     str(Path(other["config_path"]).parent))
        before = installer.tree(*old_paths)
        plan = self.install_channel(live, "queue")
        self.assertEqual(plan["config"]["project"]["channel_path"], str(live))
        directory = self.queue_state(plan)
        lock = os.lstat(directory / "instance.lock").st_ino
        code, result = self.run_wake(self.args("remove", kind="queue",
            extra=["--channel", live.name, "--confirm"]))
        self.assertEqual(code, 0, result)
        self.assertEqual(set(os.listdir(directory)), {"guided.lock", "observer.lock", "instance.lock"})
        self.assertEqual(os.lstat(directory / "instance.lock").st_ino, lock)
        self.assertFalse(Path(plan["unit_path"]).exists())
        self.assertTrue(Path(plan["bundle"]["dir"]).is_dir())
        self.assertEqual(installer.tree(*old_paths), before)

    def test_same_root_queue_paths_cannot_overwrite_or_remove_each_other(self):
        live = self.channel(copy=True)
        old = self.install_channel(".ai-comm", "queue")
        self.queue_state(old)
        selected = self.plan(kind="queue", extra=["--channel", live.name])
        self.assertEqual(old["instance"], selected["instance"])
        self.assertNotEqual(old["config_sha256"], selected["config_sha256"])
        self.assertNotEqual(old["plan_sha256"], selected["plan_sha256"])
        before = self.everything()
        for command, extra, expected in (
                ("install", ["--plan-sha256", selected["plan_sha256"]], "config_exists"),
                ("remove", [], "config_binding_mismatch")):
            code, result = self.run_wake(self.args(command, kind="queue",
                extra=["--channel", live.name, "--confirm", *extra]))
            self.assertEqual(code, 78, result)
            self.assertEqual(result["code"], expected)
        self.assertEqual(self.everything(), before)

    def test_path_refusals_match_installer_and_adapter_without_writes(self):
        live = self.channel()
        alias = self.project / "alias"
        alias.symlink_to(live, target_is_directory=True)
        file = self.project / "file"
        file.write_text("not a directory")
        cases = [str(alias), str(alias / ".." / live.name), str(self.base),
                 str(self.project), str(self.pin), str(file), str(file / ".." / live.name)]
        before = self.everything()
        for selection in cases:
            with self.subTest(channel=selection):
                self.assertEqual(self.run_wake(self.args("plan", extra=["--channel", selection]))[0], 78)
                with self.assertRaises(exit_tests.common.WakeError):
                    exit_tests.exit_wake.Layout(str(self.project), "0" * 16, "left", selection)
        self.assertEqual(self.everything(), before)

    def test_old_exit_bundle_cannot_receive_a_new_channel_argument(self):
        live = self.channel()
        self.make_pin(override={"exit_wake.py": b"# Legacy adapter: no channel selection contract.\n"})
        plan = self.install_channel(".ai-comm")
        before = self.everything()
        code, result = self.run_wake(self.args("plan", extra=["--channel", live.name]))
        self.assertEqual((code, result["code"]), (78, "channel_selection_unsupported"))
        # Advancing the pin does not make an old frozen bundle gain the option.
        self.make_pin()
        code, result = self.run_wake(self.args("integration", extra=["--channel", live.name,
            "--bundle-sha256", plan["bundle"]["sha256"], "--pair", "1" * 40, "2" * 40]))
        self.assertEqual((code, result["code"]), (78, "channel_selection_unsupported"))
        self.assertEqual(self.everything(), before)


class ExitChannelTests(exit_tests.Base):
    def setUp(self):
        super().setUp()
        self.live = Path(self.project) / ".ai-comm-v3"
        shutil.copytree(Path(self.project) / ".ai-comm", self.live)
        Path(self.project, ".tools", "ai-comm", "ai_comm.py").write_text("# fixture CLI\n")
        runtime = self.live / "ai-comm-runtime"
        runtime.mkdir()
        (runtime / "left.json").write_text('{"v":2,"generation":1,"read":0,"seen":0}')

    def phase(self, phase, cli, channel=None):
        out = io.StringIO()
        args = [phase, *self.argv(extra=["--channel", str(channel or self.live)])]
        code = exit_tests.exit_wake.main(args, runner=cli, out=out, sleep=lambda s: None)
        return code, json.loads(out.getvalue())

    def test_watch_and_every_phase_use_selected_path_and_receipt(self):
        cli = exit_tests.FakeCli("left", exit_tests.SESSION_A)
        self.assertEqual(self.run_main(self.argv(extra=["--channel", self.live.name]), cli)[0], 0)
        for phase in ("probe", "pause", "rebind", "check", "resume"):
            code, result = self.phase(phase, cli)
            self.assertEqual(code, 0, (phase, result))
        self.assertTrue(cli.calls)
        self.assertEqual({c[c.index("--channel") + 1] for c in cli.calls}, {str(self.live)})
        # The default has no receipt: a phase accidentally using it would fail.
        self.assertFalse(Path(self.project, ".ai-comm", "ai-comm-runtime").exists())

    def test_wrong_key_and_binding_keep_existing_refusal_contracts(self):
        cli = exit_tests.FakeCli("left", exit_tests.SESSION_A)
        code, line = self.run_main(self.argv(key="0" * 16, extra=["--channel", self.live.name]), cli)
        self.assertEqual((code, line), (78, "WAKE-ERROR project_key_mismatch"))
        self.assertFalse(Path(self.state_dir()).exists())
        cli = exit_tests.FakeCli("left", "different session")
        code, line = self.run_main(self.argv(extra=["--channel", self.live.name]), cli)
        self.assertEqual((code, line), (78, "WAKE-ERROR session_mismatch"))
        self.assertTrue(Path(self.state_dir(), "lock").exists())
        self.assertFalse(Path(self.state_dir(), "run.json").exists())
        self.assertFalse(Path(self.state_dir(), "cursor.json").exists())

    def test_same_root_phase_refuses_another_paths_live_run(self):
        cli = exit_tests.FakeCli("left", exit_tests.SESSION_A)
        result = []
        cli.on_wait = lambda: result.append(self.phase("pause", exit_tests.FakeCli(
            "left", exit_tests.SESSION_A)))
        self.assertEqual(self.run_main(self.argv(), cli)[0], 0)
        self.assertEqual(result, [(78, {"error": "run_identity_unverified"})] * 2)
        self.assertFalse(Path(self.state_dir(), "pause").exists())
