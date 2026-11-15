"""Channel selection regressions: temporary projects and fake services only."""
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
from types import SimpleNamespace
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
            for channel in (str(self.project / ".ai-comm"), ".ai-comm", "./.ai-comm/",
                            "/" + str(self.project / ".ai-comm")):
                with self.subTest(kind=kind, channel=channel):
                    self.assertEqual(self.plan(kind=kind, extra=["--channel", channel]), expected)
                    self.assertNotIn("--channel", expected["steps"][0])
        plan = self.install_channel(".ai-comm")
        expected = self.integration_channel(plan)
        for channel in (str(self.project / ".ai-comm"), ".ai-comm", "./.ai-comm/",
                        "/" + str(self.project / ".ai-comm")):
            self.assertEqual(self.integration_channel(plan, channel), expected)
        self.assertNotIn("--channel", plan["launch"])

    def test_leading_slashes_on_project_preserve_default_identity(self):
        expected = self.plan()
        for prefix in ("/", "//"):
            args = self.args("plan")
            args[args.index("--project") + 1] = prefix + str(self.project)
            self.assertEqual(self.run_wake(args), (0, expected))

    def test_bare_channel_root_is_accepted_but_its_subdirectory_is_not(self):
        bare = self.project / ".ai-comm-bare"
        installer.git(self.project, "clone", "--bare", "-q", str(self.project / ".ai-comm"), str(bare))
        self.assertEqual(self.plan(extra=["--channel", bare.name])["project"]["channel_path"], str(bare))
        code, result = self.run_wake(self.args("plan", extra=["--channel", str(bare / "objects")]))
        self.assertEqual((code, result["code"]), (78, "channel_path_invalid"))

    def test_alternate_only_install_remove_and_frozen_exit_phases(self):
        live = self.project / ".ai-comm-v3"
        (self.project / ".ai-comm").rename(live)
        self.assertEqual(self.run_wake(self.args("plan"))[1]["code"], "channel_path_invalid")
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
        # Both parent repositories have history: accidental Git discovery must not pass.
        for repo in (self.project, self.pin):
            installer.git(repo, "init", "-q")
            installer.git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                          "commit", "-q", "--allow-empty", "-m", "parent")
        (live / "sub").mkdir()
        cases = [str(alias), str(alias / ".." / live.name), str(self.base),
                 str(self.project), str(self.pin), str(file), str(file / ".." / live.name),
                 "/" + str(self.project), "/" + str(self.pin), str(self.pin / "adapters"),
                 str(self.project / ".tools"), str(self.project / ".ai-comm-local"),
                 str(live / "sub"), "", ".", "..", "missing", "x" * 300, "a\x00b"]
        before = self.everything()
        for selection in cases:
            with self.subTest(channel=selection):
                code, result = self.run_wake(self.args("plan", extra=["--channel", selection]))
                self.assertEqual((code, result["code"]), (78, "channel_path_invalid"))
                with self.assertRaises(exit_tests.common.WakeError) as caught:
                    layout = exit_tests.exit_wake.Layout(str(self.project), "0" * 16, "left", selection)
                    exit_tests.exit_wake.project_identity(layout)
                self.assertEqual(caught.exception.code, "channel_path_invalid")
        self.assertEqual(self.everything(), before)

    def test_unsafe_channel_ownership_and_modes_are_refused_by_bundled_check(self):
        live = self.channel()
        # Keep ancestors non-group-writable so each injected defect is reached;
        # a fake group membership must not make an earlier ancestor fail instead.
        os.chmod(self.project, 0o755)
        os.chmod(live, 0o755)
        real_lstat = os.lstat
        for module in (installer.wake.common, exit_tests.common):
            self.assertEqual(module.channel_path(str(self.project), str(live)), str(live))
            for attributes in ({"st_uid": os.getuid() + 100000},
                               {"st_mode": stat.S_IFDIR | 0o777},
                               {"st_mode": stat.S_IFDIR | 0o770, "st_gid": os.getgid() + 100000},
                               {"st_mode": stat.S_IFDIR | 0o770, "st_gid": os.getgid()}):
                with self.subTest(module=module.__name__, attributes=attributes):
                    def unsafe(path):
                        info = real_lstat(path)
                        if os.fspath(path) != str(live):
                            return info
                        values = {name: getattr(info, name) for name in ("st_mode", "st_uid", "st_gid")}
                        return SimpleNamespace(**{**values, **attributes})
                    with mock.patch.object(module.os, "lstat", side_effect=unsafe), \
                            mock.patch.object(module.grp, "getgrgid", return_value=SimpleNamespace(
                                gr_mem=["another-fixture-user"])):
                        with self.assertRaises(module.WakeError) as caught:
                            module.channel_path(str(self.project), str(live))
                    self.assertEqual(caught.exception.code, "channel_path_invalid")

    def test_unreadable_channel_ancestry_has_fixed_refusal(self):
        live = self.channel()
        real_lstat = os.lstat

        def denied(path):
            if os.fspath(path) == str(live):
                raise PermissionError("fixture denial")
            return real_lstat(path)

        before = self.everything()
        with mock.patch.object(os, "lstat", side_effect=denied):
            code, result = self.run_wake(self.args("plan", extra=["--channel", live.name]))
            self.assertEqual((code, result["code"]), (78, "channel_path_invalid"))
            with self.assertRaises(exit_tests.common.WakeError) as caught:
                exit_tests.exit_wake.Layout(str(self.project), "0" * 16, "left", live.name)
            self.assertEqual(caught.exception.code, "channel_path_invalid")
        self.assertEqual(self.everything(), before)

    def test_channel_selection_marker_requires_exact_integer_one(self):
        live = self.channel()
        for value in ("0", "2", "True", "'1'"):
            with self.subTest(value=value):
                self.make_pin(override={"exit_wake.py": ("CHANNEL_SELECTION = " + value + "\n").encode()})
                code, result = self.run_wake(self.args("plan", extra=["--channel", live.name]))
                self.assertEqual((code, result.get("code")), (78, "channel_selection_unsupported"))

    def test_exit_roundtrip_preserves_other_channel_pin_and_instance(self):
        live = self.channel()
        other = self.install_channel(".ai-comm")
        other_dir = self.project / ".ai-comm-local" / "wake" / other["instance"]
        other_dir.mkdir(parents=True, mode=0o700)
        os.chmod(other_dir.parent, 0o700)
        exit_tests.exit_wake.write_cursor(str(other_dir), 7)
        roots = (str(self.project / ".ai-comm"), str(self.pin), str(other_dir))
        before = installer.tree(*roots)
        plan = self.install_channel(live.name)
        directory = self.project / ".ai-comm-local" / "wake" / plan["instance"]
        directory.mkdir(mode=0o700)
        exit_tests.exit_wake.write_cursor(str(directory), 3)
        for name in installer.wake.LOCK_NAMES:
            os.close(installer.wake.common.acquire_lock(str(directory), name=name))
        locks = {name: os.lstat(directory / name).st_ino for name in installer.wake.LOCK_NAMES
                 if (directory / name).exists()}
        code, result = self.run_wake(self.args("remove", extra=["--channel", live.name, "--confirm"]))
        self.assertEqual(code, 0, result)
        self.assertEqual(installer.tree(*roots), before)
        for name, inode in locks.items():
            self.assertEqual(os.lstat(directory / name).st_ino, inode)

    def test_same_root_exit_remove_uses_selected_copy_binding_and_shared_cursor(self):
        copy = self.channel(copy=True)
        old = self.install_channel(".ai-comm")
        directory = self.project / ".ai-comm-local" / "wake" / old["instance"]
        directory.mkdir(parents=True, mode=0o700)
        os.chmod(directory.parent, 0o700)
        exit_tests.exit_wake.write_cursor(str(directory), 5)

        def registry(cli, channel):
            self.assertEqual(str(channel), str(copy))
            return {"participants": [{"handle": "left", "session": installer.OTHER, "generation": 1}]}

        before = installer.tree(str(self.project / ".ai-comm"), str(copy))
        with mock.patch.object(self.host, "registry", registry):
            code, result = self.run_wake(self.args("remove", session=installer.OTHER,
                                                 extra=["--channel", copy.name, "--confirm"]))
        self.assertEqual(code, 0, result)
        self.assertIn("cursor.json", result["removed"])
        self.assertFalse((directory / "cursor.json").exists())
        self.assertEqual(installer.tree(str(self.project / ".ai-comm"), str(copy)), before)

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

    def test_double_slash_project_and_channel_use_canonical_identity(self):
        cli = exit_tests.FakeCli("left", exit_tests.SESSION_A)
        args = self.argv(project="/" + self.project, extra=["--channel", "/" + str(self.live)])
        self.assertEqual(self.run_main(args, cli)[0], 0)
        for phase in ("probe", "pause", "rebind", "check", "resume"):
            out = io.StringIO()
            code = exit_tests.exit_wake.main([phase, *args], runner=cli, out=out, sleep=lambda s: None)
            self.assertEqual(code, 0, out.getvalue())
        self.assertEqual({c[c.index("--channel") + 1] for c in cli.calls}, {str(self.live)})

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
