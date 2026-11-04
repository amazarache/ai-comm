"""Tests for scripts/wake.py (installer design v4). Isolated: a temporary project, a fake
trusted verifier, a fake channel state and a fake service manager. Nothing live is read,
installed, reloaded, enabled or started."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import wake  # noqa: E402

SESSION = "11111111-2222-4333-8444-555555555555"
OTHER = "66666666-7777-4888-9999-000000000000"
COMMIT = "c" * 40
SHA = "e" * 64


def git(directory, *args):
    subprocess.run(["git", "-C", str(directory), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})


def tree(*roots):
    """Every path under the roots with its mode and bytes (or a link target)."""
    result = {}
    for root in roots:
        if not os.path.lexists(root):
            continue
        for base, dirs, files in os.walk(root):
            for name in dirs + files:
                path = os.path.join(base, name)
                info = os.lstat(path)
                if stat.S_ISREG(info.st_mode):
                    content = Path(path).read_bytes()
                elif stat.S_ISLNK(info.st_mode):
                    content = os.readlink(path)
                else:
                    content = None
                result[path] = (stat.S_IMODE(info.st_mode), content, info.st_ino)
    return result


class FakeHost(wake.Host):
    def __init__(self, test):
        super().__init__({"HOME": str(test.base / "home")})
        self.test = test
        self.verified = True
        self.participants = [{"handle": "left", "session": SESSION, "generation": 1}]
        self.systemd = "systemd 255 (255.4)"
        self.show = {"LoadState": "not-found", "ActiveState": "inactive", "UnitFileState": "",
                     "FragmentPath": ""}
        self.show_raw = None
        self.calls = []

    def verify(self, project):
        self.calls.append(("verify",))
        if not self.verified:
            return {"status": "modified", "pin": str(self.test.pin)}
        return {"status": "verified", "pin": str(self.test.pin), "commit": COMMIT}

    def registry(self, cli, channel):
        self.calls.append(("registry",))
        return {"participants": self.participants}

    def systemctl(self, *args):
        self.calls.append(("systemctl", *args))
        if args == ("--version",):
            return 0, self.systemd + "\n"
        if args[:2] == ("--user", "show"):
            if self.show_raw is not None:
                return self.show_raw
            return 0, "".join(f"{k}={v}\n" for k, v in self.show.items())
        raise AssertionError("unexpected systemctl call")

    def systemctl_verbs(self):
        return [c[1:3] for c in self.calls if c[0] == "systemctl"]


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="wake-")
        self.addCleanup(temp.cleanup)
        self.base = Path(os.path.realpath(temp.name))
        self.project = self.base / "project"
        self.pin = self.project / ".tools" / "ai-comm"
        (self.pin / "adapters").mkdir(parents=True)
        channel = self.project / ".ai-comm"
        channel.mkdir()
        git(channel, "init", "-q")
        (channel / "README").write_text("channel\n")
        git(channel, "add", "README")
        git(channel, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "root")
        (self.project / ".ai-comm-local").mkdir()
        self.make_pin()
        self.host = FakeHost(self)
        self.unit_dir = self.base / "home" / ".config" / "systemd" / "user"

    def make_pin(self, names=None):
        entries = []
        files = {"ai_comm.py": b"# cli\n"}
        for name in names if names is not None else ("exit_wake.py", "common.py", "queue_wake.py", "queue-wake@.service",
                              "queue_wake.lock"):
            files["adapters/" + name] = (ROOT / "adapters" / name).read_bytes()
        for rel, data in files.items():
            (self.pin / rel).write_bytes(data)
            entries.append({"path": rel, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        manifest = {"format": "ai-comm-review-manifest/1", "files": entries,
                    "protocol_sha256": "7242b13f489a393492539b2e835ed69da65d57a0d39f4acce2e18df757ef3377"}
        (self.pin / "RELEASE-MANIFEST.json").write_text(json.dumps(manifest))

    def args(self, command, kind="exit", handle="left", session=SESSION, generation=1, extra=()):
        argv = [command, "--project", str(self.project), "--handle", handle, "--kind", kind,
                f"--session={session}", "--generation", str(generation)]
        if kind == "queue":
            argv += ["--python", sys.executable, "--socket", str(self.base / "runtime.sock"),
                     "--binary", str(self.base / "runtime"), "--binary-sha256", SHA, "--thread", session]
        return argv + list(extra)

    def run_wake(self, argv):
        out = io.StringIO()
        code = wake.main(argv, host=self.host, out=out)
        return code, json.loads(out.getvalue())

    def plan(self, **kwargs):
        code, plan = self.run_wake(self.args("plan", **kwargs))
        self.assertEqual(code, 0, plan)
        return plan

    def install(self, **kwargs):
        plan = self.plan(**kwargs)
        extra = ["--plan-sha256", plan["plan_sha256"], "--confirm"]
        return plan, self.run_wake(self.args("install", extra=extra, **kwargs))

    def remove(self, **kwargs):
        return self.run_wake(self.args("remove", extra=["--confirm"], **kwargs))

    def everything(self):
        return tree(str(self.project / ".ai-comm-local"), str(self.base / "home"))


class ExitTests(Base):
    def test_plan_is_read_only_and_exact(self):
        before = self.everything()
        plan = self.plan()
        self.assertEqual(self.everything(), before)
        key = plan["instance"][:16]
        self.assertEqual(plan["instance"], f"{key}-left")
        launch = plan["launch"]
        self.assertEqual(launch[:2], [sys.executable, "-I"])
        self.assertEqual(launch[2], str(Path(plan["bundle"]["dir"]) / "exit_wake.py"))
        self.assertIn(f"--session={SESSION}", launch)
        self.assertEqual(plan["steps"][0][:2], ["sh", str(wake.RELEASE / "install.sh")])
        self.assertEqual(plan["steps"][1], launch)
        self.assertNotIn("current", " ".join(plan["steps"][0]))

    def test_install_freezes_a_private_identical_bundle(self):
        plan, (code, result) = self.install()
        self.assertEqual((code, result["status"]), (0, "installed"))
        bundle = Path(plan["bundle"]["dir"])
        self.assertEqual(stat.S_IMODE(os.lstat(bundle).st_mode), 0o700)
        self.assertEqual(sorted(os.listdir(bundle)), ["BUNDLE.json", "common.py", "exit_wake.py"])
        for name in ("common.py", "exit_wake.py"):
            self.assertEqual(stat.S_IMODE(os.lstat(bundle / name).st_mode), 0o400)
            self.assertEqual((bundle / name).read_bytes(), (ROOT / "adapters" / name).read_bytes())
        self.assertEqual(hashlib.sha256((bundle / "BUNDLE.json").read_bytes()).hexdigest(), plan["bundle"]["sha256"])
        self.assertFalse(os.path.exists(self.project / ".ai-comm-local" / "wake"))  # no instance write
        again = self.run_wake(self.args("install", extra=["--plan-sha256", plan["plan_sha256"], "--confirm"]))
        self.assertEqual(again[0], 0)  # idempotent

    def test_plan_changed_and_confirmation(self):
        plan = self.plan()
        self.assertEqual(self.run_wake(self.args("install", extra=["--plan-sha256", "0" * 64, "--confirm"])),
                         (78, {"status": "stopped", "code": "plan_changed"}))
        self.assertEqual(self.run_wake(self.args("install", extra=["--plan-sha256", plan["plan_sha256"]])),
                         (78, {"status": "stopped", "code": "confirmation_required"}))
        self.assertFalse(os.path.exists(self.project / ".ai-comm-local" / "upgrade"))

    def test_refusals_before_any_write(self):
        before = self.everything()
        self.host.verified = False
        self.assertEqual(self.run_wake(self.args("plan"))[1]["code"], "pin_not_verified")
        self.host.verified = True
        self.host.participants = [{"handle": "left", "session": OTHER, "generation": 1}]
        self.assertEqual(self.run_wake(self.args("plan"))[1]["code"], "session_mismatch")
        self.host.participants = []
        self.assertEqual(self.run_wake(self.args("plan"))[1]["code"], "handle_not_joined")
        self.assertEqual(self.everything(), before)

    def test_pin_without_adapters_is_unsupported(self):
        shutil.rmtree(self.pin / "adapters")
        (self.pin / "adapters").mkdir()
        self.make_pin(names=())
        self.assertEqual(self.run_wake(self.args("plan")), (78, {"status": "stopped", "code": "adapters_unsupported"}))

    def test_changed_pin_file_is_refused(self):
        (self.pin / "adapters" / "exit_wake.py").write_bytes(b"changed")
        self.assertEqual(self.run_wake(self.args("plan"))[1]["code"], "pin_changed")

    def test_queue_instance_blocks_exit(self):
        state = self.project / ".ai-comm-local" / "wake" / self.plan()["instance"]
        state.mkdir(parents=True, mode=0o700)
        (state / "config.json").write_text("{}")
        self.assertEqual(self.run_wake(self.args("plan"))[1]["code"], "instance_kind_conflict")

    def test_different_existing_bundle_is_refused(self):
        plan = self.plan()
        bundle = Path(plan["bundle"]["dir"])
        for directory in (bundle.parents[1], bundle.parent, bundle):
            directory.mkdir(mode=0o700)
        (bundle / "exit_wake.py").write_text("other")
        os.chmod(bundle / "exit_wake.py", 0o400)
        code, result = self.run_wake(self.args("install", extra=["--plan-sha256", plan["plan_sha256"], "--confirm"]))
        self.assertEqual((code, result["code"]), (78, "bundle_conflict"))
        self.assertEqual(os.listdir(bundle), ["exit_wake.py"])

    def instance_with_data(self):
        plan = self.plan()
        state = self.project / ".ai-comm-local" / "wake" / plan["instance"]
        state.mkdir(parents=True, mode=0o700)
        os.chmod(state.parent, 0o700)
        for name in ("lock", "instance.lock", "phase.lock"):
            (state / name).write_bytes(b"")
        for name in ("cursor.json", "peer-alarm.json"):
            (state / name).write_text("{}")
        return state

    def test_remove_deletes_data_and_keeps_lock_inodes(self):
        state = self.instance_with_data()
        inodes = {n: os.lstat(state / n).st_ino for n in ("lock", "instance.lock", "phase.lock")}
        code, result = self.remove()
        self.assertEqual((code, sorted(result["removed"])), (0, ["cursor.json", "peer-alarm.json"]))
        self.assertEqual({n: os.lstat(state / n).st_ino for n in inodes}, inodes)
        self.assertEqual(self.remove()[1]["removed"], [])  # rerun is a no-op

    def test_remove_refuses_active_or_unexpected(self):
        state = self.instance_with_data()
        holder = wake.common.acquire_lock(str(state), name="lock")
        before = tree(str(state))
        self.assertEqual(self.remove()[1]["code"], "instance_active")
        os.close(holder)
        (state / "notes.txt").write_text("x")
        self.assertEqual(self.remove()[1]["code"], "instance_unexpected_files")
        before.update({str(state / "notes.txt"): tree(str(state))[str(state / "notes.txt")]})
        self.assertEqual(tree(str(state)), before)


class QueueTests(Base):
    def queue_module(self):
        return wake._load("test_queue_module", ROOT / "adapters" / "queue_wake.py")

    def test_plan_is_read_only_and_checks_systemd(self):
        before = self.everything()
        plan = self.plan(kind="queue")
        self.assertEqual(self.everything(), before)
        self.assertEqual(plan["config"]["service"], {"path": plan["unit_path"], "sha256": plan["unit_sha256"]})
        self.assertEqual(plan["unit_path"], str(self.unit_dir / f"queue-wake@{plan['instance']}.service"))
        self.assertEqual(plan["steps"][1][3:5], [str(Path(plan["bundle"]["dir"]) / "queue_wake.py"), "init"])
        self.assertEqual(plan["steps"][2:], [[wake.SYSTEMCTL, "--user", "daemon-reload"],
                                             [wake.SYSTEMCTL, "--user", "enable", "--now",
                                              f"queue-wake@{plan['instance']}.service"]])
        self.host.systemd = "systemd 253 (253.1)"
        self.assertEqual(self.run_wake(self.args("plan", kind="queue"))[1]["code"], "service_manager_unsupported")
        self.assertEqual(self.everything(), before)

    def test_render_is_identical_from_null_and_completed_service(self):
        plan = self.plan(kind="queue")
        queue = self.queue_module()
        template = (ROOT / "adapters" / "queue-wake@.service").read_text()
        args = (Path(plan["config_path"]), sys.executable, str(Path(plan["bundle"]["dir"]) / "queue_wake.py"),
                template)
        null = queue.render_service(dict(plan["config"], service=None), *args)
        full = queue.render_service(plan["config"], *args)
        self.assertEqual(null, full)
        self.assertEqual(hashlib.sha256(null).hexdigest(), plan["unit_sha256"])

    def test_install_publishes_config_then_unit_and_activates_nothing(self):
        plan, (code, result) = self.install(kind="queue")
        self.assertEqual((code, result["status"]), (0, "installed"), result)
        config = Path(plan["config_path"])
        self.assertEqual(config.read_bytes(), wake.canonical(plan["config"]))
        self.assertEqual(stat.S_IMODE(os.lstat(config).st_mode), 0o600)
        unit = Path(plan["unit_path"])
        self.assertEqual(hashlib.sha256(unit.read_bytes()).hexdigest(), plan["unit_sha256"])
        self.assertEqual(sorted(os.listdir(unit.parent)), [unit.name])  # no temporary left
        verbs = self.host.systemctl_verbs()
        self.assertTrue(all(v == ("--version",) for v in verbs), verbs)  # no reload, enable or start
        self.assertFalse(os.path.exists(config.parent / "state.json"))  # init is the owner's step
        again = self.run_wake(self.args("install", kind="queue",
                                        extra=["--plan-sha256", plan["plan_sha256"], "--confirm"]))
        self.assertEqual(again[0], 0)

    def test_interrupted_install_resumes_from_the_config(self):
        plan = self.plan(kind="queue")
        argv = self.args("install", kind="queue", extra=["--plan-sha256", plan["plan_sha256"], "--confirm"])
        with mock.patch.object(wake, "publish_unit", side_effect=OSError(5, "fault")):
            self.assertEqual(self.run_wake(argv)[0], 75)
        self.assertTrue(os.path.exists(plan["config_path"]))
        self.assertFalse(os.path.exists(plan["unit_path"]))
        self.assertEqual(self.run_wake(argv)[0], 0)
        self.assertTrue(os.path.exists(plan["unit_path"]))

    def test_orphan_and_foreign_objects_change_nothing(self):
        plan = self.plan(kind="queue")
        argv = self.args("install", kind="queue", extra=["--plan-sha256", plan["plan_sha256"], "--confirm"])
        self.unit_dir.mkdir(parents=True, mode=0o700)
        unit = Path(plan["unit_path"])
        unit.write_bytes(b"[Unit]\n")
        os.chmod(unit, 0o644)
        before = self.everything()
        self.assertEqual(self.run_wake(argv)[1]["code"], "unit_exists")
        self.assertEqual(self.everything(), before)
        unit.unlink()
        self.install(kind="queue")
        os.unlink(plan["config_path"])
        before = self.everything()
        self.assertEqual(self.run_wake(argv)[1]["code"], "unit_orphan")
        self.assertEqual(self.everything(), before)

    def test_exit_instance_blocks_queue(self):
        plan = self.plan(kind="queue")
        state = Path(plan["config_path"]).parent
        state.mkdir(parents=True, mode=0o700)
        (state / "cursor.json").write_text("{}")
        self.assertEqual(self.run_wake(self.args("plan", kind="queue"))[1]["code"], "instance_kind_conflict")

    def test_invalid_renderer_input_is_a_fixed_error(self):
        argv = self.args("plan", kind="queue")
        argv[argv.index("--socket") + 1] = str(self.base / "run\x01time.sock")
        out = io.StringIO()
        code = wake.main(argv, host=self.host, out=out)
        self.assertEqual((code, json.loads(out.getvalue())), (78, {"status": "stopped",
                                                                   "code": "queue_render_refused"}))

    def installed_with_state(self, pending=None):
        plan, _ = self.install(kind="queue")
        queue = self.queue_module()
        state = {"v": 1, "binding": queue.digest(queue.canonical(plan["config"])), "scanned": 5, "read": 5,
                 "wanted": 5, "pending": pending, "last_dispatch": 0, "status": "new"}
        directory = Path(plan["config_path"]).parent
        wake.common.write_private_json(str(directory), "state.json", state)
        return plan, directory

    def test_remove_completes_and_keeps_locks(self):
        plan, directory = self.installed_with_state()
        inode = os.lstat(directory / "instance.lock").st_ino
        code, result = self.remove(kind="queue")
        self.assertEqual(code, 0, result)
        self.assertFalse(os.path.exists(plan["unit_path"]))
        self.assertEqual(sorted(os.listdir(directory)), ["guided.lock", "instance.lock", "observer.lock"])
        self.assertEqual(os.lstat(directory / "instance.lock").st_ino, inode)
        self.assertEqual(self.remove(kind="queue")[1]["removed"], [])

    def test_remove_refusals_change_nothing(self):
        plan, directory = self.installed_with_state(pending={"through": 5, "phase": "uncertain"})
        before = self.everything()
        self.assertEqual(self.remove(kind="queue")[1]["code"], "pending_attempt")
        after = self.everything()
        self.assertEqual({k: v for k, v in after.items() if not k.endswith(".lock")},
                         {k: v for k, v in before.items() if not k.endswith(".lock")})
        self.host.show = {"LoadState": "loaded", "ActiveState": "active", "UnitFileState": "enabled",
                          "FragmentPath": plan["unit_path"]}
        self.assertEqual(self.remove(kind="queue")[1]["code"], "instance_active")

    def test_state_missing_refuses_before_any_lock_exists(self):
        plan, _ = self.install(kind="queue")
        before = self.everything()
        self.assertEqual(self.remove(kind="queue"), (78, {"status": "stopped", "code": "state_missing"}))
        self.assertEqual(self.everything(), before)  # no lock file created

    def test_recovery_data_blocks_removal(self):
        plan, directory = self.installed_with_state()
        (directory / "rebind-0123456789ab").mkdir(mode=0o700)
        self.assertEqual(self.remove(kind="queue")[1]["code"], "recovery_data_present")
        self.assertTrue(os.path.exists(plan["config_path"]))

    def test_every_deletion_cut_resumes(self):
        for cut in range(5):  # the unit, paused.json, state.json, config.json, the intent
            with self.subTest(cut=cut):
                self.setUp()
                plan, directory = self.installed_with_state()
                wake.common.write_private_json(str(directory), "paused.json", {"v": 1})
                real = os.unlink
                count = {"n": 0}

                def failing(path, *a, **k):
                    if Path(path).parent in (directory, Path(plan["unit_path"]).parent) \
                            and not str(path).endswith((".lock",)) and ".queue-wake." not in str(path):
                        if count["n"] == cut:
                            raise OSError(5, "fault")
                        count["n"] += 1
                    return real(path, *a, **k)

                with mock.patch.object(wake.os, "unlink", failing):
                    self.assertEqual(self.remove(kind="queue")[0], 75)
                self.assertTrue(os.path.exists(directory / "remove-intent.json"))
                self.assertEqual(self.remove(kind="queue")[0], 0)
                self.assertEqual(sorted(os.listdir(directory)), ["guided.lock", "instance.lock", "observer.lock"])

    def test_changed_state_after_interruption_is_refused(self):
        plan, directory = self.installed_with_state()
        with mock.patch.object(wake.os, "unlink", side_effect=OSError(5, "fault")):
            self.assertEqual(self.remove(kind="queue")[0], 75)
        state = json.loads((directory / "state.json").read_text())
        state["scanned"] = 6
        wake.common.write_private_json(str(directory), "state.json", state)
        before = self.everything()
        self.assertEqual(self.remove(kind="queue")[1]["code"], "removal_state_changed")
        self.assertEqual({k: v for k, v in self.everything().items() if not k.endswith(".lock")},
                         {k: v for k, v in before.items() if not k.endswith(".lock")})

    def test_kind_switch_keeps_and_accepts_the_other_kinds_locks(self):
        exit_plan = self.plan()
        directory = self.project / ".ai-comm-local" / "wake" / exit_plan["instance"]
        directory.mkdir(parents=True, mode=0o700)
        os.chmod(directory.parent, 0o700)
        for name in ("lock", "phase.lock", "instance.lock"):
            (directory / name).write_bytes(b"")
        (directory / "cursor.json").write_text("{}")
        self.assertEqual(self.remove()[0], 0)
        plan, directory = self.installed_with_state()
        self.assertEqual(self.remove(kind="queue")[0], 0)
        self.assertEqual(sorted(os.listdir(directory)),
                         ["guided.lock", "instance.lock", "lock", "observer.lock", "phase.lock"])


class CorrectionTests(Base):
    """Review 1371 R1-R5 and review 1373 (plan 051)."""

    queue_module = QueueTests.queue_module
    installed_with_state = QueueTests.installed_with_state

    def snapshot_all(self):
        return tree(str(self.project / ".ai-comm-local"), str(self.base / "home"))

    # R6 ------------------------------------------------------------------------------------
    def test_symlinked_wake_root_is_refused_and_nothing_outside_is_deleted(self):
        """Review 1379: a symlinked .ai-comm-local/wake must not lead removal elsewhere."""
        for kind in ("exit", "queue"):
            with self.subTest(kind=kind):
                self.setUp()
                instance = self.plan(kind=kind)["instance"]
                outside = self.base / "outside"
                (outside / instance).mkdir(parents=True, mode=0o700)
                os.chmod(outside, 0o700)
                names = ("cursor.json",) if kind == "exit" else ("config.json", "state.json")
                for name in names:
                    wake.common.write_private_json(str(outside / instance), name, {"v": 1})
                os.symlink(outside, self.project / ".ai-comm-local" / "wake")
                before = tree(str(outside))
                code, result = self.remove(kind=kind)
                self.assertEqual(code, 78, result)
                self.assertEqual(tree(str(outside)), before)  # nothing outside the project deleted
                self.codes = getattr(self, "codes", []) + [result["code"]]

    def test_dangling_or_empty_symlinked_wake_root_is_refused(self):
        """Plan 054: a symlinked ancestor is a refusal, never a 'nothing to remove' no-op."""
        for kind in ("exit", "queue"):
            for target in ("missing", "empty"):
                with self.subTest(kind=kind, target=target):
                    self.setUp()
                    self.plan(kind=kind)
                    destination = self.base / target
                    if target == "empty":
                        destination.mkdir(mode=0o700)
                    os.symlink(destination, self.project / ".ai-comm-local" / "wake")
                    before = tree(str(self.base / "empty"))
                    self.assertEqual(self.remove(kind=kind),
                                     (78, {"status": "stopped", "code": "instance_dir_unsafe"}))
                    self.assertEqual(tree(str(self.base / "empty")), before)

    def test_interrupted_queue_removal_retry_refuses_a_symlinked_path(self):
        plan, directory = self.installed_with_state()
        with mock.patch.object(wake.os, "unlink", side_effect=OSError(5, "fault")):
            self.assertEqual(self.remove(kind="queue")[0], 75)
        self.assertTrue(os.path.exists(directory / "remove-intent.json"))
        wake_root = directory.parent
        outside = self.base / "moved-wake"
        os.rename(wake_root, outside)
        os.symlink(outside, wake_root)
        before = tree(str(outside), str(self.unit_dir))
        self.assertEqual(self.remove(kind="queue"), (78, {"status": "stopped", "code": "instance_dir_unsafe"}))
        self.assertEqual(tree(str(outside), str(self.unit_dir)), before)  # unit, data and intent kept

    def test_symlinked_wake_root_reports_its_own_code(self):
        self.test_symlinked_wake_root_is_refused_and_nothing_outside_is_deleted()
        self.assertEqual(self.codes, ["instance_dir_unsafe", "instance_dir_unsafe"])

    # R1 ------------------------------------------------------------------------------------
    def test_unsafe_existing_bundle_is_refused_without_publishing(self):
        plan = self.plan(kind="queue")
        bundle = Path(plan["bundle"]["dir"])
        for directory in (bundle.parents[1], bundle.parent, bundle):
            directory.mkdir(mode=0o700)
        for name, data in {"queue_wake.py": None, "common.py": None, "queue-wake@.service": None,
                           "queue_wake.lock": None}.items():
            (bundle / name).write_bytes((ROOT / "adapters" / name).read_bytes())
        doc = {"kind": "queue", "package": {"commit": COMMIT, "manifest_sha256": hashlib.sha256(
            (self.pin / "RELEASE-MANIFEST.json").read_bytes()).hexdigest()}, "files": plan["bundle"]["files"]}
        (bundle / "BUNDLE.json").write_bytes(wake.canonical(doc))
        for directory in (bundle.parents[1], bundle.parent, bundle):
            os.chmod(directory, 0o777)
        for name in os.listdir(bundle):
            os.chmod(bundle / name, 0o666)
        before = self.snapshot_all()
        argv = self.args("install", kind="queue", extra=["--plan-sha256", plan["plan_sha256"], "--confirm"])
        self.assertEqual(self.run_wake(argv), (78, {"status": "stopped", "code": "bundle_unsafe"}))
        self.assertEqual(self.snapshot_all(), before)  # modes unchanged, no config, no unit
        self.assertFalse(os.path.exists(plan["unit_path"]))

    def test_unsafe_unit_destinations_are_refused(self):
        plan = self.plan(kind="queue")
        argv = self.args("install", kind="queue", extra=["--plan-sha256", plan["plan_sha256"], "--confirm"])
        self.unit_dir.mkdir(parents=True, mode=0o700)
        os.chmod(self.unit_dir, 0o777)
        self.assertEqual(self.run_wake(argv)[1]["code"], "unit_dir_unsafe")
        self.assertFalse(os.path.exists(plan["config_path"]))
        shutil.rmtree(self.base / "home")
        (self.base / "home").mkdir(mode=0o700)
        os.symlink(self.base / "elsewhere", self.base / "home" / ".config")
        (self.base / "elsewhere").mkdir(mode=0o700)
        self.assertEqual(self.run_wake(self.args("plan", kind="queue"))[0], 0)  # plan reads nothing there
        self.assertEqual(self.run_wake(argv)[1]["code"], "unit_dir_unsafe")
        os.unlink(self.base / "home" / ".config")
        os.chmod(self.base / "home", 0o777)
        self.assertEqual(self.run_wake(argv)[1]["code"], "unit_dir_unsafe")  # writable parent of a missing dir
        self.assertFalse(os.path.exists(self.base / "home" / ".config"))

    def test_user_owned_traversable_unit_dir_is_accepted(self):
        self.unit_dir.mkdir(parents=True, mode=0o755)
        for directory in (self.unit_dir, self.unit_dir.parent, self.unit_dir.parent.parent):
            os.chmod(directory, 0o755)
        plan, (code, result) = self.install(kind="queue")
        self.assertEqual(code, 0, result)
        self.assertTrue(os.path.exists(plan["unit_path"]))

    # R2 ------------------------------------------------------------------------------------
    def test_unknown_service_state_refuses_before_any_change(self):
        plan, directory = self.installed_with_state()
        good = {"LoadState": "loaded", "ActiveState": "inactive", "UnitFileState": "disabled",
                "FragmentPath": plan["unit_path"]}
        lines = lambda values: "".join(f"{k}={v}\n" for k, v in values.items())  # noqa: E731
        cases = [
            ((0, ""), "service_state_invalid"),
            ((0, "LoadState=loaded\nActiveState=inactive\n"), "service_state_invalid"),
            ((0, lines(good) + "ActiveState=inactive\n"), "service_state_invalid"),
            ((0, lines(dict(good, LoadState="masked"))), "service_state_invalid"),
            ((0, lines(dict(good, ActiveState="activating"))), "instance_active"),
            ((0, lines(dict(good, UnitFileState="enabled"))), "instance_active"),
            ((0, lines(dict(good, FragmentPath="/elsewhere.service"))), "wrong_service_fragment"),
            ((1, lines(dict(good))), "service_manager_unavailable"),
        ]
        for raw, code in cases:
            self.host.show_raw = raw
            before = self.snapshot_all()
            self.assertEqual(self.remove(kind="queue")[1]["code"], code, raw)
            data_only = lambda t: {k: v for k, v in t.items() if not k.endswith(".lock")}  # noqa: E731
            self.assertEqual(data_only(self.snapshot_all()), data_only(before), raw)
            self.assertFalse(os.path.exists(directory / "remove-intent.json"))
        self.host.show_raw = (0, lines(good))
        self.assertEqual(self.remove(kind="queue")[0], 0)

    # R3 ------------------------------------------------------------------------------------
    def rebind(self, plan, directory):
        """What a guided rebind does: a new package binding, the old frozen unit kept."""
        queue = self.queue_module()
        config = json.loads(Path(plan["config_path"]).read_text())
        config["package"] = dict(config["package"], commit="d" * 40, manifest_sha256="f" * 64)
        wake.common.write_private_json(str(directory), "config.json", config)
        state = json.loads((directory / "state.json").read_text())
        state["binding"] = queue.digest(queue.canonical(config))
        wake.common.write_private_json(str(directory), "state.json", state)

    def test_remove_after_a_guided_rebind(self):
        plan, directory = self.installed_with_state()
        self.rebind(plan, directory)
        code, result = self.remove(kind="queue")
        self.assertEqual(code, 0, result)
        self.assertFalse(os.path.exists(plan["unit_path"]))
        self.assertEqual(sorted(os.listdir(directory)), ["guided.lock", "instance.lock", "observer.lock"])

    def test_interrupted_remove_after_rebind_resumes(self):
        plan, directory = self.installed_with_state()
        self.rebind(plan, directory)
        real = os.unlink
        state = {"n": 0}

        def second_fails(path, *a, **k):
            if not str(path).endswith(".lock") and ".queue-wake." not in str(path):
                state["n"] += 1
                if state["n"] == 2:
                    raise OSError(5, "fault")
            return real(path, *a, **k)

        with mock.patch.object(wake.os, "unlink", second_fails):
            self.assertEqual(self.remove(kind="queue")[0], 75)
        self.assertEqual(self.remove(kind="queue")[0], 0)

    def test_config_for_another_binding_is_refused(self):
        plan, directory = self.installed_with_state()
        before = self.snapshot_all()
        argv = self.args("remove", kind="queue", extra=["--confirm"])
        argv[argv.index("--socket") + 1] = str(self.base / "other.sock")
        self.assertEqual(self.run_wake(argv)[1]["code"], "config_binding_mismatch")
        self.assertEqual({k: v for k, v in self.snapshot_all().items() if not k.endswith(".lock")},
                         {k: v for k, v in before.items() if not k.endswith(".lock")})

    def test_exit_remove_keeps_the_current_registry_check(self):
        exit_plan = self.plan()
        directory = self.project / ".ai-comm-local" / "wake" / exit_plan["instance"]
        directory.mkdir(parents=True, mode=0o700)
        os.chmod(directory.parent, 0o700)
        wake.common.write_private_json(str(directory), "cursor.json", {"v": 1, "notification_through": 9})
        before = tree(str(directory))
        self.assertEqual(self.remove(session=OTHER)[1]["code"], "session_mismatch")
        self.host.participants = [{"handle": "left", "session": SESSION, "generation": 2}]
        self.assertEqual(self.remove(generation=1)[1]["code"], "generation_mismatch")
        self.assertEqual(tree(str(directory)), before)

    # R4 ------------------------------------------------------------------------------------
    def test_malformed_intent_is_refused_even_when_its_files_are_gone(self):
        plan, directory = self.installed_with_state()
        with mock.patch.object(wake.os, "unlink", side_effect=OSError(5, "fault")):
            self.remove(kind="queue")
        original = json.loads((directory / "remove-intent.json").read_text())
        os.unlink(directory / "config.json")
        os.unlink(directory / "state.json")
        cases = [dict(original, config_sha256=123, state_sha256=456), dict(original, v=True),
                 dict(original, state_sha256=None), dict(original, unit={"path": "/x.service",
                                                                         "sha256": original["unit"]["sha256"]}),
                 dict(original, extra=1), dict(original, pending={"through": 1, "phase": "queued"})]
        for intent in cases:
            wake.common.write_private_json(str(directory), "remove-intent.json", intent)
            before = self.snapshot_all()
            self.assertEqual(self.remove(kind="queue")[1]["code"], "removal_intent_invalid", intent)
            self.assertEqual(self.snapshot_all(), before)
            self.assertTrue(os.path.exists(plan["unit_path"]))

    # R5 ------------------------------------------------------------------------------------
    def test_changed_unit_refuses_before_any_intent(self):
        plan, directory = self.installed_with_state()
        unit = Path(plan["unit_path"])
        os.chmod(unit, 0o600)
        with open(unit, "ab") as handle:
            handle.write(b"# local note\n")
        before = self.snapshot_all()
        self.assertEqual(self.remove(kind="queue")[1]["code"], "unit_changed")
        after = self.snapshot_all()
        self.assertFalse(os.path.exists(directory / "remove-intent.json"))
        self.assertEqual({k: v for k, v in after.items() if not k.endswith(".lock")},
                         {k: v for k, v in before.items() if not k.endswith(".lock")})


class RegistryReaderTests(Base):
    """The real Host.registry reader against a scripted CLI (review 1345 Q1)."""

    def scripted(self, pages):
        host = wake.Host({"HOME": str(self.base / "home")})
        calls = []

        def cli_json(argv):
            calls.append(argv)
            return pages[len(calls) - 1]
        host.cli_json = cli_json
        return host, calls

    def test_pages_history_and_takes_highest_generation(self):
        items = [{"handle": f"p{i}", "session": f"s{i}", "generation": 1} for i in range(100)]
        pages = [{"head": "h", "items": items, "next": 100, "more": True},
                 {"head": "h", "items": [{"handle": "left", "session": "old", "generation": 1},
                                         {"handle": "left", "session": SESSION, "generation": 2}],
                  "next": 102, "more": False}]
        host, calls = self.scripted(pages)
        result = host.registry(self.pin / "ai_comm.py", self.project / ".ai-comm")
        left = [p for p in result["participants"] if p["handle"] == "left"]
        self.assertEqual(left, [{"handle": "left", "session": SESSION, "generation": 2}])
        self.assertEqual(len(result["participants"]), 101)
        self.assertEqual([a[a.index("--offset") + 1] for a in calls], ["0", "100"])
        self.assertIn("registry", calls[0])
        self.assertNotIn("state", calls[0])

    def test_head_change_and_bad_pages(self):
        one = {"head": "h", "items": [{"handle": "a", "session": "s", "generation": 1}], "next": 1, "more": True}
        moved = {"head": "g", "items": [], "next": 1, "more": False}
        host, _ = self.scripted([one, moved])
        with self.assertRaises(wake.WakeError) as ctx:
            host.registry(self.pin / "ai_comm.py", self.project)
        self.assertEqual((ctx.exception.code, ctx.exception.exit_code), ("registry_changed", 75))
        gap = {"head": "h", "items": [], "next": 5, "more": False}
        host, _ = self.scripted([gap])
        with self.assertRaises(wake.WakeError) as ctx:
            host.registry(self.pin / "ai_comm.py", self.project)
        self.assertEqual(ctx.exception.code, "channel_state_invalid")


class DispatchTests(unittest.TestCase):
    def test_install_user_routes_wake(self):
        import install_user
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = install_user.main(["wake", "plan", "--handle", "left"])
        self.assertEqual((code, json.loads(out.getvalue())), (78, {"status": "stopped", "code": "arguments_invalid"}))


if __name__ == "__main__":
    unittest.main()
