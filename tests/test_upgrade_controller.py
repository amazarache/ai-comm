"""Tests for upgrade_controller.py (design 013 v3). Fakes only: no network, no real channel."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import upgrade_controller as uc  # noqa: E402

GIT_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid", "HOME": "/nonexistent"}

FAKE_CLI = r'''
import json, os, subprocess, sys
VERSION = "@VERSION@"
RELEASE_ORIGIN = "https://example.invalid/ai-comm.git"
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
fake = os.environ.get("UC_FAKE_DIR", "")
def tagcommit(tag):
    return subprocess.run(["git", "-C", here, "rev-parse", tag + "^{commit}"], capture_output=True, text=True).stdout.strip()
if args[:1] == ["release-status"]:
    print(json.dumps({"status": "ok", "pinned_version": VERSION, "latest_version": "1.1.0"})); sys.exit(0)
if args[:1] == ["upgrade-plan"]:
    if VERSION == "1.1.0":
        print(json.dumps({"error": "x", "code": "upgrade_target_not_newer"})); sys.exit(2)
    print(json.dumps({"from": VERSION, "to": "1.1.0", "commit": tagcommit("v1.1.0"), "tag": "v1.1.0",
                      "channel_action": "unchanged", "applicable_advisory": {"advisory": os.environ.get("UC_ADVISORY", "optional")},
                      "owner_notice": "notice"})); sys.exit(0)
if args[:1] == ["--channel"]:
    cmd = args[2]
    if cmd == "state":
        print(open(os.path.join(fake, "state.json")).read()); sys.exit(0)
    if cmd == "show":
        path = os.path.join(fake, "messages", args[3] + ".json")
        if not os.path.exists(path): sys.exit(2)
        print(open(path).read()); sys.exit(0)
    if cmd == "show-artifact":
        path = os.path.join(fake, "artifacts", args[3])
        if not os.path.exists(path): sys.exit(2)
        sys.stdout.write(open(path).read()); sys.exit(0)
sys.exit(2)
'''

LISTENER = r'''
import hashlib, json, os, sys
root = os.path.dirname(os.path.abspath(__file__))
name, cmd = sys.argv[1], sys.argv[2]
state_path = os.path.join(root, name + ".state.json")
src = os.path.join(root, name + "_src.py")
cli = os.path.join(root, ".tools", "ai-comm", "ai_comm.py")
def sha(p): return hashlib.sha256(open(p, "rb").read()).hexdigest()
st = json.load(open(state_path)) if os.path.exists(state_path) else {"paused": False, "running": True, "bound": sha(cli)}
fail = os.environ.get("UC_FAIL_STEP")
if fail == name + ":" + cmd:
    sys.exit(3)
if cmd == "pause": st.update(paused=True, running=False)
elif cmd == "resume": st.update(paused=False, running=True)
elif cmd == "rebind": st["bound"] = sha(cli)
elif cmd in ("install_source", "restore_source"):
    which = sys.argv[3]
    open(src, "wb").write(open(os.path.join(root, "staged", name + "-" + which + ".py"), "rb").read())
    if os.environ.get("UC_WRITE_THEN_FAIL") == name + ":" + cmd:
        sys.exit(3)
elif cmd == "check": pass
elif cmd == "big": print("x" * 70000); sys.exit(0)
json.dump(st, open(state_path, "w"))
if cmd == "probe":
    print(json.dumps({"paused": st["paused"], "running": st["running"], "pending": False,
                      "source_sha256": sha(src), "bound_cli_sha256": st["bound"]}))
else:
    print("{}")
'''


def git(cwd, *args):
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=env).stdout.decode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Fixture:
    def __init__(self, base, fail_target_tests=False):
        self.base = Path(base)
        self.repo = self.base / "pkg"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "--initial-branch=main")
        self.from_commit = self._release("1.0.0", controller=False, fail_tests=False)
        self.to_commit = self._release("1.1.0", controller=True, fail_tests=fail_target_tests)
        self.project = self.base / "project"
        self.project.mkdir()
        (self.project / ".tools").mkdir()
        self.pin = self.project / ".tools" / "ai-comm"
        subprocess.run(["git", "clone", "-q", str(self.repo), str(self.pin)], check=True,
                       env={**os.environ, **GIT_ENV})
        git(self.pin, "checkout", "-q", "--detach", self.from_commit)
        channel = self.project / ".ai-comm"
        channel.mkdir()
        git(channel, "init", "-q")
        git(channel, "commit", "-q", "--allow-empty", "-m", "channel root")
        self.channel_id = git(channel, "rev-parse", "HEAD").strip()
        os.mkdir(self.project / ".ai-comm-local", 0o700)
        self.fake = self.base / "fake"
        (self.fake / "messages").mkdir(parents=True)
        (self.fake / "artifacts").mkdir()
        (self.fake / "state.json").write_text(json.dumps({"participants": [
            {"handle": "claude", "session": "s-claude"}, {"handle": "codex", "session": "s-codex"}]}))
        (self.project / "listener.py").write_text(LISTENER)
        staged = self.project / "staged"
        staged.mkdir()
        self.sources = {}
        for name in ("own", "peer"):
            for which, commit in (("from", self.from_commit), ("to", self.to_commit)):
                body = f"# {name} listener source for {which}\n".encode()
                (staged / f"{name}-{which}.py").write_bytes(body)
                self.sources.setdefault(name, {})[commit] = sha(body)
            shutil.copy(staged / f"{name}-from.py", self.project / f"{name}_src.py")
        os.environ["UC_FAKE_DIR"] = str(self.fake)

    def _release(self, version, controller, fail_tests):
        (self.repo / "ai_comm.py").write_text(FAKE_CLI.replace("@VERSION@", version))
        (self.repo / "PROTOCOL.md").write_text("protocol 3.0 contract\n")
        (self.repo / "scripts").mkdir(exist_ok=True)
        (self.repo / "scripts" / "check_tag.py").write_text("import sys; sys.exit(0)\n")
        (self.repo / "scripts" / "check_release_manifest.py").write_text("import sys; sys.exit(0)\n")
        (self.repo / "tests").mkdir(exist_ok=True)
        body = "self.assertTrue(False)" if fail_tests else "self.assertTrue(True)"
        (self.repo / "tests" / "test_fake.py").write_text(textwrap.dedent(f"""
            import unittest
            class T(unittest.TestCase):
                def test_ok(self):
                    {body}
            """))
        files = ["ai_comm.py", "PROTOCOL.md"]
        if controller:
            shutil.copy(ROOT / "upgrade_controller.py", self.repo / "upgrade_controller.py")
            files.append("upgrade_controller.py")
        manifest = {"format": "ai-comm-review-manifest/1", "files": [
            {"path": f, "bytes": (self.repo / f).stat().st_size, "sha256": sha((self.repo / f).read_bytes())}
            for f in files]}
        (self.repo / "RELEASE-MANIFEST.json").write_text(json.dumps(manifest))
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", f"release {version}")
        msha = sha((self.repo / "RELEASE-MANIFEST.json").read_bytes())
        git(self.repo, "tag", "-a", f"v{version}", "-m", f"ai-comm {version}; manifest sha256 {msha}")
        return git(self.repo, "rev-parse", "HEAD").strip()

    def integration(self, name, owner, **extra):
        py = sys.executable
        item = {"id": name, "owner": owner,
                "supports": {"pairs": [[self.from_commit, self.to_commit], [self.to_commit, self.from_commit]]},
                "probe": {"argv": [py, "listener.py", name, "probe"], "run_by": "owner"},
                "code_files": ["listener.py"], "source_file": f"{name}_src.py",
                "sources": self.sources[name],
                "steps": {"pause": [py, "listener.py", name, "pause"],
                          "install_source": [py, "listener.py", name, "install_source", "to"],
                          "restore_source": [py, "listener.py", name, "restore_source", "from"],
                          "rebind": [py, "listener.py", name, "rebind"],
                          "check": [py, "listener.py", name, "check"],
                          "resume": [py, "listener.py", name, "resume"]}}
        item.update(extra)
        return item

    def write_manifest(self, integrations, status="declared"):
        root = self.project / ".ai-comm-local" / "upgrade"
        uc.Project(self.project).runtime()
        manifest = {"v": 1, "schema": uc.PROJECT_SCHEMA, "status": status,
                    "project_id": {"path": str(self.project), "channel": self.channel_id},
                    "integrations": integrations}
        uc.write_private_atomic(root / "project.json", json.dumps(manifest).encode())


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="uc-")
        os.chmod(self.temp.name, 0o700)
        self.fx = Fixture(Path(self.temp.name).resolve())
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(os.environ.pop, "UC_FAIL_STEP", None)
        self.addCleanup(os.environ.pop, "UC_ADVISORY", None)
        self.addCleanup(os.environ.pop, "UC_WRITE_THEN_FAIL", None)

    def run_uc(self, *args, controller=None):
        if controller is None:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = uc.main(["--project", str(self.fx.project), *args])
            return code, json.loads(out.getvalue())
        result = subprocess.run([sys.executable, "-I", str(controller), "--project", str(self.fx.project), *args],
                                capture_output=True, env={**os.environ, **GIT_ENV})
        return result.returncode, json.loads(result.stdout)

    def consent(self, answer="yes", target="1.1.0", **kw):
        args = ["record-answer", "--answer", answer, "--target", target,
                "--target-commit", kw.get("commit", self.fx.to_commit),
                "--source", kw.get("source", "https://example.invalid/ai-comm.git"),
                "--evidence", "direct owner yes in this session"]
        code, out = self.run_uc(*args)
        self.assertEqual(code, 0, out)
        return out.get("consent")

    def prepared(self, integrations=None):
        self.fx.write_manifest(integrations if integrations is not None else [self.fx.integration("own", "claude")])
        consent = self.consent()
        code, out = self.run_uc("prepare", "--target", "1.1.0", "--consent", consent, "--as", "claude", "--session", "s-claude")
        self.assertEqual(code, 0, out)
        return consent, out

    def pin_commit(self):
        return git(self.fx.pin, "rev-parse", "HEAD").strip()

    def journal(self, txn):
        return uc.Journal(self.fx.project / ".ai-comm-local" / "upgrade" / "txn" / txn)

    # ---- consent and prompts

    def test_check_offers_prompt_without_network(self):
        self.fx.write_manifest([])
        code, out = self.run_uc("check")
        self.assertEqual(code, 0, out)
        self.assertTrue(out["prompt"]["should_ask"])
        self.assertEqual(out["prompt"]["target"], "1.1.0")
        self.assertEqual(out["prompt"]["source"], "https://example.invalid/ai-comm.git")

    def test_snooze_skip_never(self):
        self.fx.write_manifest([])
        self.run_uc("record-answer", "--answer", "not_now", "--target", "1.1.0")
        self.assertFalse(self.run_uc("check")[1]["prompt"]["should_ask"])
        state = uc.prompt_state(uc.Project(self.fx.project))
        self.assertEqual(state["versions"]["1.1.0"]["snooze_level"], 1)
        self.run_uc("record-answer", "--answer", "skip", "--target", "1.1.0")
        state = uc.prompt_state(uc.Project(self.fx.project))
        self.assertFalse(uc.should_ask(state, "1.1.0", "optional", 10 ** 12))
        self.assertTrue(uc.should_ask(state, "1.1.0", "required", 10 ** 12))
        self.assertTrue(uc.should_ask(state, "1.2.0", "optional", 10 ** 12))
        self.run_uc("record-answer", "--answer", "never", "--target", "1.1.0")
        state = uc.prompt_state(uc.Project(self.fx.project))
        self.assertFalse(uc.should_ask(state, "1.2.0", "optional", 10 ** 12))

    def test_consent_scope_source_and_target_are_bound(self):
        self.fx.write_manifest([self.fx.integration("own", "claude")])
        code, out = self.run_uc("record-answer", "--answer", "yes", "--target", "1.1.0",
                                "--target-commit", self.fx.to_commit, "--source", "https://other.invalid/x.git",
                                "--evidence", "e")
        self.assertEqual(out["code"], "consent_source_mismatch")
        fetch_only = self.consent(answer="fetch_only")
        code, out = self.run_uc("prepare", "--target", "1.1.0", "--consent", fetch_only, "--as", "claude", "--session", "s-claude")
        self.assertEqual(out["code"], "consent_scope_insufficient")
        wrong = self.consent(commit="0" * 40)
        code, out = self.run_uc("prepare", "--target", "1.1.0", "--consent", wrong, "--as", "claude", "--session", "s-claude")
        self.assertEqual(out["code"], "consent_target_mismatch")
        self.assertEqual(self.pin_commit(), self.fx.from_commit)

    def test_expired_consent_refused(self):
        self.fx.write_manifest([self.fx.integration("own", "claude")])
        consent = self.consent()
        with mock.patch.object(uc, "now", return_value=uc.now() + uc.CONSENT_TTL + 1):
            code, out = self.run_uc("prepare", "--target", "1.1.0", "--consent", consent, "--as", "claude", "--session", "s-claude")
        self.assertEqual(out["code"], "consent_expired")

    def test_consent_unconsumed_after_prepare_and_consumed_at_first_mutation(self):
        consent, out = self.prepared()
        project = uc.Project(self.fx.project)
        self.assertIsNone(uc.load_consent(project, consent)["consumed_by"])
        code, res = self.run_uc("apply", "--as", "claude", "--session", "s-claude")
        self.assertEqual(code, 0, res)
        self.assertEqual(uc.load_consent(project, consent)["consumed_by"], out["txn"])

    def test_spent_consent_cannot_start_another_transaction(self):
        consent, out = self.prepared()
        self.run_uc("apply", "--as", "claude", "--session", "s-claude")
        self.run_uc("rollback", "--as", "claude", "--session", "s-claude", "--txn", out["txn"])
        code, res = self.run_uc("prepare", "--target", "1.1.0", "--consent", consent, "--as", "claude", "--session", "s-claude")
        self.assertEqual(res["code"], "consent_from_mismatch" if self.pin_commit() != self.fx.from_commit
                         else "consent_spent")

    # ---- verification and integration gating

    def test_target_test_failure_changes_nothing(self):
        self.temp.cleanup()
        self.temp = tempfile.TemporaryDirectory(prefix="uc-")
        os.chmod(self.temp.name, 0o700)
        self.fx = Fixture(Path(self.temp.name).resolve(), fail_target_tests=True)
        self.fx.write_manifest([self.fx.integration("own", "claude")])
        consent = self.consent()
        code, out = self.run_uc("prepare", "--target", "1.1.0", "--consent", consent, "--as", "claude", "--session", "s-claude")
        self.assertEqual(out["code"], "verify_failed")
        self.assertEqual(self.pin_commit(), self.fx.from_commit)
        self.assertIsNone(uc.load_consent(uc.Project(self.fx.project), consent)["consumed_by"])

    def test_unsupported_integration_fails_before_mutation(self):
        item = self.fx.integration("own", "claude", supports={"pairs": [["1" * 40, "2" * 40]]})
        self.fx.write_manifest([item])
        consent = self.consent()
        code, out = self.run_uc("prepare", "--target", "1.1.0", "--consent", consent, "--as", "claude", "--session", "s-claude")
        self.assertEqual(out["code"], "upgrade_integration_unsupported")
        self.assertEqual(self.pin_commit(), self.fx.from_commit)

    def test_unintegrated_project_refused(self):
        self.fx.write_manifest([], status="unintegrated")
        consent = self.consent()
        code, out = self.run_uc("prepare", "--target", "1.1.0", "--consent", consent, "--as", "claude", "--session", "s-claude")
        self.assertEqual(out["code"], "project_integration_undeclared")

    def test_missing_manifest_is_error_not_empty(self):
        uc.Project(self.fx.project).runtime()
        consent = self.consent()
        code, out = self.run_uc("prepare", "--target", "1.1.0", "--consent", consent, "--as", "claude", "--session", "s-claude")
        self.assertEqual(out["code"], "runtime_missing")

    # ---- forward, identities, lock

    def test_forward_upgrade_reaches_pending_liveness_not_completed(self):
        consent, out = self.prepared()
        code, res = self.run_uc("apply", "--as", "claude", "--session", "s-claude")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["status"], "integrated_pending_liveness")
        self.assertEqual(self.pin_commit(), self.fx.to_commit)
        state = json.loads((self.fx.project / "own.state.json").read_text())
        self.assertFalse(state["paused"])
        self.assertEqual((self.fx.project / "own_src.py").read_bytes(),
                         (self.fx.project / "staged" / "own-to.py").read_bytes())
        self.assertEqual(state["bound"], sha((self.fx.pin / "ai_comm.py").read_bytes()))
        self.assertEqual(self.run_uc("status")[1]["state"], "integrated_pending_liveness")

    def test_modified_hook_code_after_prepare_stops(self):
        consent, out = self.prepared()
        with open(self.fx.project / "listener.py", "a") as stream:
            stream.write("\n# changed\n")
        code, res = self.run_uc("apply", "--as", "claude", "--session", "s-claude")
        self.assertEqual((res["code"], res.get("what")), ("upgrade_identity_changed", "integration_code"))
        self.assertEqual(self.pin_commit(), self.fx.from_commit)

    def test_moved_tag_after_prepare_stops(self):
        consent, out = self.prepared()
        git(self.fx.pin, "tag", "-f", "-a", "v1.1.0", "-m", "moved", self.fx.from_commit)
        code, res = self.run_uc("apply", "--as", "claude", "--session", "s-claude")
        self.assertEqual(res["code"], "tag_manifest_mismatch")
        self.assertEqual(self.pin_commit(), self.fx.from_commit)

    def test_simultaneous_applier_is_locked(self):
        consent, out = self.prepared()
        project = uc.Project(self.fx.project)
        with project.lock():
            code, res = self.run_uc("apply", "--as", "claude", "--session", "s-claude")
        self.assertEqual(res["code"], "upgrade_locked")

    def test_dirty_pin_refused(self):
        consent, out = self.prepared()
        with open(self.fx.pin / "PROTOCOL.md", "a") as stream:
            stream.write("x")
        code, res = self.run_uc("apply", "--as", "claude", "--session", "s-claude")
        self.assertEqual((res["code"], res.get("what")), ("upgrade_identity_changed", "pin_dirty"))

    # ---- crash and journal

    def test_crash_after_checkout_before_outcome_resolved_by_probe(self):
        consent, out = self.prepared()
        real = uc.perform_own

        def crash_after(txn, action):
            result = real(txn, action)
            if action["op"] == "checkout":
                raise KeyboardInterrupt
            return result
        with mock.patch.object(uc, "perform_own", crash_after):
            with self.assertRaises(KeyboardInterrupt):
                self.run_uc("apply", "--as", "claude", "--session", "s-claude")
        self.assertEqual(self.pin_commit(), self.fx.to_commit)
        code, res = self.run_uc("apply", "--as", "claude", "--session", "s-claude")
        self.assertEqual(res["status"], "integrated_pending_liveness", res)
        outcomes = [r for r in self.journal(out["txn"]).read()[0] if r["kind"] == "outcome"]
        self.assertTrue(any(r["data"].get("by") == "probe" for r in outcomes))

    def test_crash_after_install_source_before_outcome(self):
        consent, out = self.prepared()
        real = uc.perform_own

        def crash_after(txn, action):
            result = real(txn, action)
            if action["op"] == "install_source":
                raise KeyboardInterrupt
            return result
        with mock.patch.object(uc, "perform_own", crash_after):
            with self.assertRaises(KeyboardInterrupt):
                self.run_uc("apply", "--as", "claude", "--session", "s-claude")
        code, res = self.run_uc("apply", "--as", "claude", "--session", "s-claude")
        self.assertEqual(res["status"], "integrated_pending_liveness", res)

    def test_crash_matrix_every_action_before_and_after_side_effect(self):
        """Crash after the intent (before the side effect) and after the side effect
        (before the outcome) at every action; a rerun must finish coherently."""
        ops = ["pause", "checkout", "install_source", "rebind", "check", "resume"]
        for op in ops:
            for when in ("before", "after"):
                with self.subTest(op=op, when=when):
                    self.temp.cleanup()
                    self.temp = tempfile.TemporaryDirectory(prefix="uc-")
                    os.chmod(self.temp.name, 0o700)
                    self.fx = Fixture(Path(self.temp.name).resolve())
                    consent, out = self.prepared()
                    real = uc.perform_own

                    def crash(txn, action, op=op, when=when):
                        if action["op"] == op and when == "before":
                            raise KeyboardInterrupt
                        result = real(txn, action)
                        if action["op"] == op and when == "after":
                            raise KeyboardInterrupt
                        return result
                    with mock.patch.object(uc, "perform_own", crash):
                        with self.assertRaises(KeyboardInterrupt):
                            self.run_uc("apply", "--as", "claude", "--session", "s-claude")
                    code, res = self.run_uc("apply", "--as", "claude", "--session", "s-claude")
                    self.assertEqual(res.get("status"), "integrated_pending_liveness", res)
                    self.assertEqual(self.pin_commit(), self.fx.to_commit)
                    state = json.loads((self.fx.project / "own.state.json").read_text())
                    self.assertFalse(state["paused"])
                    self.assertEqual(state["bound"], sha((self.fx.pin / "ai_comm.py").read_bytes()))

    def test_status_never_repairs_without_lock(self):
        consent, out = self.prepared()
        journal = self.journal(out["txn"])
        with open(journal.path, "ab") as stream:
            stream.write(b'{"partial":')
        code, res = self.run_uc("status")
        self.assertEqual(res["code"], "journal_torn_tail")
        self.assertTrue(journal.path.read_bytes().endswith(b'{"partial":'))

    def test_failing_step_records_stop_and_rollback_restores(self):
        consent, out = self.prepared()
        os.environ["UC_FAIL_STEP"] = "own:rebind"
        code, res = self.run_uc("apply", "--as", "claude", "--session", "s-claude")
        self.assertEqual(res["code"], "hook_failed")
        os.environ.pop("UC_FAIL_STEP")
        code, res = self.run_uc("rollback", "--as", "claude", "--session", "s-claude")
        self.assertEqual(res["status"], "rolled_back", res)
        self.assertEqual(self.pin_commit(), self.fx.from_commit)
        self.assertEqual((self.fx.project / "own_src.py").read_bytes(),
                         (self.fx.project / "staged" / "own-from.py").read_bytes())
        state = json.loads((self.fx.project / "own.state.json").read_text())
        self.assertEqual(state["bound"], sha((self.fx.pin / "ai_comm.py").read_bytes()))
        self.assertFalse(state["paused"])

    def test_rollback_to_release_without_controller_uses_frozen_copy(self):
        consent, out = self.prepared()
        self.run_uc("apply", "--as", "claude", "--session", "s-claude")
        self.assertTrue((self.fx.pin / "upgrade_controller.py").exists())
        frozen = Path(out["invocation"][2])
        self.assertTrue(str(frozen).startswith(str(self.fx.project / ".ai-comm-local" / "upgrade" / "controller")))
        code, res = self.run_uc("rollback", "--as", "claude", "--session", "s-claude", controller=frozen)
        self.assertEqual(res["status"], "rolled_back", res)
        self.assertFalse((self.fx.pin / "upgrade_controller.py").exists())
        code, res = self.run_uc("status", controller=frozen)
        self.assertEqual(res["state"], "rolled_back")

    def test_torn_tail_preserved_and_chained(self):
        consent, out = self.prepared()
        journal = self.journal(out["txn"])
        with open(journal.path, "ab") as stream:
            stream.write(b'{"partial":')
        code, res = self.run_uc("recover", "--as", "claude", "--session", "s-claude")
        self.assertEqual(code, 0, res)
        records, torn = journal.read()
        self.assertIsNone(torn)
        self.assertEqual(records[-1]["kind"], "note")
        saved = journal.dir / records[-1]["data"]["torn_tail"]
        self.assertEqual(saved.read_bytes(), b'{"partial":')

    def test_complete_corrupt_final_line_is_corruption(self):
        consent, out = self.prepared()
        journal = self.journal(out["txn"])
        with open(journal.path, "ab") as stream:
            stream.write(b'{"v":1,"schema":"ai-comm-upgrade-journal/1","seq":99,"prev":null,"kind":"note"}\n')
        code, res = self.run_uc("recover", "--as", "claude", "--session", "s-claude")
        self.assertEqual(res["code"], "journal_corrupt")

    # ---- peers and completion

    def test_peer_phase_waits_and_validates_reports(self):
        consent, out = self.prepared([self.fx.integration("own", "claude"), self.fx.integration("peer", "codex")])
        code, res = self.run_uc("apply", "--as", "claude", "--session", "s-claude")
        self.assertEqual(res["status"], "awaiting_peer")
        self.assertEqual((res["integration"], res["phase"], res["owner"]), ("peer", "pause", "codex"))
        nonce = res["nonce"]
        self.assertEqual(self.pin_commit(), self.fx.from_commit)
        base = ["report", "--integration", "peer", "--phase", "pause"]
        self.assertEqual(self.run_uc(*base, "--nonce", "0" * 32, "--as", "codex", "--session", "s-codex")[1]["code"],
                         "stale_report")
        self.assertEqual(self.run_uc(*base, "--nonce", nonce, "--as", "claude", "--session", "s-claude")[1]["code"],
                         "not_integration_owner")
        self.assertEqual(self.run_uc(*base, "--nonce", nonce, "--as", "codex", "--session", "old")[1]["code"],
                         "session_not_current")
        code, rep = self.run_uc(*base, "--nonce", nonce, "--as", "codex", "--session", "s-codex")
        self.assertEqual(rep["status"], "reported", rep)
        self.assertEqual(self.run_uc(*base, "--nonce", nonce, "--as", "codex", "--session", "s-codex")[1]["code"],
                         "no_pending_phase")
        for _ in range(20):
            code, res = self.run_uc("continue", "--as", "claude", "--session", "s-claude")
            if res.get("status") != "awaiting_peer":
                break
            self.assertEqual(res["owner"], "codex")
            self.run_uc("report", "--integration", "peer", "--phase", res["phase"], "--nonce", res["nonce"],
                        "--as", "codex", "--session", "s-codex")
        self.assertEqual(res["status"], "integrated_pending_liveness", res)

    def test_recover_reports_peer_state_honestly(self):
        consent, out = self.prepared([self.fx.integration("own", "claude"), self.fx.integration("peer", "codex")])
        self.run_uc("apply", "--as", "claude", "--session", "s-claude")
        code, res = self.run_uc("recover", "--as", "claude", "--session", "s-claude")
        self.assertEqual(res["integrations"]["peer"], "owner_unavailable_to_probe")
        self.assertEqual(res["integrations"]["own"], "paused")
        self.assertEqual(res["package"], "from")

    def test_finalize_requires_both_decisions_bound_to_txn(self):
        consent, out = self.prepared()
        txn = out["txn"]
        code, res = self.run_uc("finalize", "--as", "claude", "--session", "s-claude",
                                "--liveness-decision", "00000001-codex",
                                "--guidance-decision", "00000002-codex", "--guidance-file", "GUIDE.md")
        self.assertEqual(res["code"], "not_ready_to_finalize")
        self.run_uc("apply", "--as", "claude", "--session", "s-claude")
        guide = self.fx.project / "GUIDE.md"
        guide.write_text("pin v1.1.0\n")

        def publish(mid, text):
            digest = sha(text.encode())
            (self.fx.fake / "artifacts" / digest).write_text(text)
            (self.fx.fake / "messages" / f"{mid}.json").write_text(json.dumps({"act": "DECIDE", "artifact": digest}))
        publish("00000001-codex", "live\nai-comm-upgrade-txn: txn-0000000000000000\n")
        publish("00000002-codex", f"guide\nai-comm-upgrade-txn: {txn}\nai-comm-upgrade-guidance-sha256: {'0' * 64}\n")
        args = ["finalize", "--as", "claude", "--session", "s-claude", "--liveness-decision", "00000001-codex", "--guidance-decision", "00000002-codex",
                "--guidance-file", "GUIDE.md"]
        self.assertEqual(self.run_uc(*args)[1]["code"], "decision_not_for_txn")
        publish("00000001-codex", f"live\nai-comm-upgrade-txn: {txn}\n")
        self.assertEqual(self.run_uc(*args)[1]["code"], "guidance_not_installed")
        publish("00000002-codex", f"guide\nai-comm-upgrade-txn: {txn}\n"
                f"ai-comm-upgrade-guidance-sha256: {sha(guide.read_bytes())}\n")
        code, res = self.run_uc(*args)
        self.assertEqual(res["status"], "completed", res)

    # ---- review 00000467 regressions (R1-R7) and closed gaps

    def _fetch_with(self, returncode, payload):
        real = subprocess.run

        def fake(argv, *a, **kw):
            if any(str(x).endswith("check_for_release.py") for x in argv):
                return subprocess.CompletedProcess(argv, returncode, json.dumps(payload).encode(), b"")
            return real(argv, *a, **kw)
        consent = self.consent(answer="fetch_only")
        with mock.patch.object(uc.subprocess, "run", fake):
            return self.run_uc("fetch", "--target", "1.1.0", "--consent", consent)

    def test_r1_fetch_success_results_and_errors(self):
        self.fx.write_manifest([])
        for code, result in ((0, "up_to_date"), (10, "update_available"), (11, "advisory_applies")):
            with self.subTest(code=code):
                rc, out = self._fetch_with(code, {"schema": "ai-comm-release-check/1", "status": "ok",
                                                  "result": result, "fetched": True})
                self.assertEqual((rc, out["status"]), (0, "fetched"), out)
        rc, out = self._fetch_with(2, {"schema": "ai-comm-release-check/1", "status": "error",
                                       "fetched": False, "fetch_error": "origin_redirect_refused"})
        self.assertEqual((rc, out["code"], out.get("helper_error")), (2, "fetch_failed", "origin_redirect_refused"))
        rc, out = self._fetch_with(0, {"schema": "other", "status": "ok", "fetched": True})
        self.assertEqual(out["code"], "fetch_failed")

    def test_r2_peer_first_action_still_validates_and_consumes_consent(self):
        consent, out = self.prepared([self.fx.integration("peer", "codex")])
        with mock.patch.object(uc, "now", return_value=uc.now() + uc.CONSENT_TTL + 1):
            code, res = self.run_uc("apply", "--as", "claude", "--session", "s-claude")
        self.assertEqual(res["code"], "consent_expired")
        self.assertFalse((self.fx.project / "peer.state.json").exists()
                         and json.loads((self.fx.project / "peer.state.json").read_text())["paused"])
        self.assertEqual(self.pin_commit(), self.fx.from_commit)

    def test_r2_peer_first_consumes_before_awaiting(self):
        consent, out = self.prepared([self.fx.integration("peer", "codex")])
        code, res = self.run_uc("apply", "--as", "claude", "--session", "s-claude")
        self.assertEqual(res["status"], "awaiting_peer")
        self.assertEqual(uc.load_consent(uc.Project(self.fx.project), consent)["consumed_by"], out["txn"])

    def test_r2_empty_integrations_and_concurrent_prepares(self):
        self.fx.write_manifest([])
        consent = self.consent()
        first = self.run_uc("prepare", "--target", "1.1.0", "--consent", consent, "--as", "claude",
                            "--session", "s-claude")[1]
        second = self.run_uc("prepare", "--target", "1.1.0", "--consent", consent, "--as", "claude",
                             "--session", "s-claude")[1]
        code, res = self.run_uc("apply", "--txn", first["txn"], "--as", "claude", "--session", "s-claude")
        self.assertEqual(res["status"], "integrated_pending_liveness", res)
        code, res = self.run_uc("apply", "--txn", second["txn"], "--as", "claude", "--session", "s-claude")
        self.assertIn(res["code"], {"consent_spent", "upgrade_identity_changed"})

    def test_r3_owner_only_peer_probe_not_run_by_preparer(self):
        self.prepared([self.fx.integration("own", "claude"), self.fx.integration("peer", "codex")])
        self.assertTrue((self.fx.project / "own.state.json").exists())
        self.assertFalse((self.fx.project / "peer.state.json").exists())

    def test_r3_applier_session_frozen_takeover_stops(self):
        consent, out = self.prepared()
        code, res = self.run_uc("apply", "--as", "claude", "--session", "other")
        self.assertEqual(res["code"], "applier_session_changed")
        code, res = self.run_uc("apply", "--as", "codex", "--session", "s-codex")
        self.assertEqual(res["code"], "not_applier")
        state = json.loads((self.fx.fake / "state.json").read_text())
        state["participants"][0]["session"] = "s-claude-takeover"
        (self.fx.fake / "state.json").write_text(json.dumps(state))
        code, res = self.run_uc("apply", "--as", "claude", "--session", "s-claude")
        self.assertEqual(res["code"], "session_not_current")
        self.assertEqual(self.pin_commit(), self.fx.from_commit)
        before = self.journal(out["txn"]).path.read_bytes()
        code, res = self.run_uc("apply", "--as", "claude", "--session", "s-claude-takeover")
        self.assertEqual(res["code"], "applier_session_changed")
        self.assertEqual(self.journal(out["txn"]).path.read_bytes(), before)
        self.assertEqual(self.pin_commit(), self.fx.from_commit)

    def _complete(self, out):
        txn = out["txn"]
        self.run_uc("apply", "--as", "claude", "--session", "s-claude")
        guide = self.fx.project / "GUIDE.md"
        guide.write_text("pin v1.1.0\n")
        for mid, text in (("00000001-codex", f"live\nai-comm-upgrade-txn: {txn}\n"),
                          ("00000002-codex", f"guide\nai-comm-upgrade-txn: {txn}\n"
                                             f"ai-comm-upgrade-guidance-sha256: {sha(guide.read_bytes())}\n")):
            digest = sha(text.encode())
            (self.fx.fake / "artifacts" / digest).write_text(text)
            (self.fx.fake / "messages" / f"{mid}.json").write_text(json.dumps({"act": "DECIDE", "artifact": digest}))
        return ["finalize", "--as", "claude", "--session", "s-claude", "--liveness-decision", "00000001-codex",
                "--guidance-decision", "00000002-codex", "--guidance-file", "GUIDE.md"]

    def test_r4_completed_upgrade_can_roll_back_once_via_frozen_controller(self):
        consent, out = self.prepared()
        args = self._complete(out)
        self.assertEqual(self.run_uc(*args)[1]["status"], "completed")
        frozen = Path(out["invocation"][2])
        code, res = self.run_uc("rollback", "--as", "claude", "--session", "s-claude", controller=frozen)
        self.assertEqual(res["status"], "rolled_back", res)
        self.assertEqual(self.pin_commit(), self.fx.from_commit)
        code, res = self.run_uc("rollback", "--as", "claude", "--session", "s-claude", controller=frozen)
        self.assertEqual(res["code"], "transaction_finished")

    def test_r5_rollback_after_interrupted_install_source(self):
        consent, out = self.prepared()
        real = uc.perform_own

        def crash_after(txn, action):
            result = real(txn, action)
            if action["op"] == "install_source":
                raise KeyboardInterrupt
            return result
        with mock.patch.object(uc, "perform_own", crash_after):
            with self.assertRaises(KeyboardInterrupt):
                self.run_uc("apply", "--as", "claude", "--session", "s-claude")
        code, res = self.run_uc("rollback", "--as", "claude", "--session", "s-claude")
        self.assertEqual(res["status"], "rolled_back", res)
        self.assertEqual(self.pin_commit(), self.fx.from_commit)
        self.assertEqual((self.fx.project / "own_src.py").read_bytes(),
                         (self.fx.project / "staged" / "own-from.py").read_bytes())

    def test_r5_peer_unresolved_intent_blocks_rollback_until_reported(self):
        consent, out = self.prepared([self.fx.integration("peer", "codex")])
        res = self.run_uc("apply", "--as", "claude", "--session", "s-claude")[1]
        code, rb = self.run_uc("rollback", "--as", "claude", "--session", "s-claude")
        self.assertEqual((rb["code"], rb["integration"], rb["nonce"]), ("peer_reconcile_required", "peer", res["nonce"]))
        self.run_uc("report", "--integration", "peer", "--phase", "pause", "--nonce", res["nonce"],
                    "--as", "codex", "--session", "s-codex")
        code, rb = self.run_uc("rollback", "--as", "claude", "--session", "s-claude")
        phases = []
        while rb.get("status") == "awaiting_peer":
            phases.append(rb["phase"])
            self.run_uc("report", "--integration", "peer", "--phase", rb["phase"], "--nonce", rb["nonce"],
                        "--as", "codex", "--session", "s-codex")
            code, rb = self.run_uc("rollback", "--as", "claude", "--session", "s-claude")
        self.assertEqual(phases, ["pause", "resume"])
        self.assertEqual(rb["status"], "rolled_back", rb)
        self.assertEqual(self.pin_commit(), self.fx.from_commit)

    def test_reverse_crash_matrix(self):
        ops = ["pause", "checkout", "restore_source", "rebind", "check", "resume"]
        for op in ops:
            for when in ("before", "after"):
                with self.subTest(op=op, when=when):
                    self.temp.cleanup()
                    self.temp = tempfile.TemporaryDirectory(prefix="uc-")
                    os.chmod(self.temp.name, 0o700)
                    self.fx = Fixture(Path(self.temp.name).resolve())
                    consent, out = self.prepared()
                    self.run_uc("apply", "--as", "claude", "--session", "s-claude")
                    real = uc.perform_own

                    def crash(txn, action, op=op, when=when):
                        if action["op"] == op and when == "before":
                            raise KeyboardInterrupt
                        result = real(txn, action)
                        if action["op"] == op and when == "after":
                            raise KeyboardInterrupt
                        return result
                    with mock.patch.object(uc, "perform_own", crash):
                        with self.assertRaises(KeyboardInterrupt):
                            self.run_uc("rollback", "--as", "claude", "--session", "s-claude")
                    code, res = self.run_uc("rollback", "--as", "claude", "--session", "s-claude")
                    self.assertEqual(res.get("status"), "rolled_back", res)
                    self.assertEqual(self.pin_commit(), self.fx.from_commit)
                    state = json.loads((self.fx.project / "own.state.json").read_text())
                    self.assertEqual((state["paused"], state["bound"]),
                                     (False, sha((self.fx.pin / "ai_comm.py").read_bytes())))

    def test_peer_report_crash_then_retry_with_same_nonce(self):
        consent, out = self.prepared([self.fx.integration("peer", "codex")])
        res = self.run_uc("apply", "--as", "claude", "--session", "s-claude")[1]
        real = uc.Journal.append

        def crash_on_outcome(journal, kind, data):
            if kind == "outcome":
                raise KeyboardInterrupt
            return real(journal, kind, data)
        with mock.patch.object(uc.Journal, "append", crash_on_outcome):
            with self.assertRaises(KeyboardInterrupt):
                self.run_uc("report", "--integration", "peer", "--phase", "pause", "--nonce", res["nonce"],
                            "--as", "codex", "--session", "s-codex")
        again = self.run_uc("apply", "--as", "claude", "--session", "s-claude")[1]
        self.assertEqual((again["status"], again["nonce"]), ("awaiting_peer", res["nonce"]))
        code, rep = self.run_uc("report", "--integration", "peer", "--phase", "pause", "--nonce", res["nonce"],
                                "--as", "codex", "--session", "s-codex")
        self.assertEqual(rep["status"], "reported", rep)

    def test_r6_finalize_rechecks_live_state(self):
        consent, out = self.prepared()
        args = self._complete(out)
        git(self.fx.pin, "checkout", "-q", "--detach", self.fx.from_commit)
        code, res = self.run_uc(*args)
        self.assertEqual((res["code"], res.get("what")), ("upgrade_identity_changed", "pin_commit"))
        git(self.fx.pin, "checkout", "-q", "--detach", self.fx.to_commit)
        subprocess.run([sys.executable, "listener.py", "own", "pause"], cwd=self.fx.project, check=True,
                       capture_output=True)
        code, res = self.run_uc(*args)
        self.assertEqual(res["code"], "postcondition_failed")
        subprocess.run([sys.executable, "listener.py", "own", "resume"], cwd=self.fx.project, check=True,
                       capture_output=True)
        self.assertEqual(self.run_uc(*args)[1]["status"], "completed")

    def test_r6_peer_attestation_required_before_completion(self):
        consent, out = self.prepared([self.fx.integration("peer", "codex")])
        res = self.run_uc("apply", "--as", "claude", "--session", "s-claude")[1]
        while res.get("status") == "awaiting_peer":
            self.run_uc("report", "--integration", "peer", "--phase", res["phase"], "--nonce", res["nonce"],
                        "--as", "codex", "--session", "s-codex")
            res = self.run_uc("continue", "--as", "claude", "--session", "s-claude")[1]
        args = self._complete(out)
        code, res = self.run_uc(*args)
        self.assertEqual(res["status"], "awaiting_peer", res)
        att = res["attestations"][0]
        code, bad = self.run_uc("report", "--integration", "peer", "--phase", "attest", "--nonce", "0" * 32,
                                "--as", "codex", "--session", "s-codex")
        self.assertEqual(bad["code"], "stale_report")
        code, rep = self.run_uc("report", "--integration", "peer", "--phase", "attest", "--nonce", att["nonce"],
                                "--as", "codex", "--session", "s-codex")
        self.assertEqual(rep["status"], "reported", rep)
        self.assertEqual(self.run_uc(*args)[1]["status"], "completed")

    def test_r7_pin_override_removed_and_channel_override_bound(self):
        consent, out = self.prepared()
        code, res = self.run_uc("--pin", str(self.fx.base / "pkg"), "status")
        self.assertEqual(res["code"], "invalid_arguments")
        other = self.fx.base / "other-channel"
        other.mkdir()
        git(other, "init", "-q")
        git(other, "commit", "-q", "--allow-empty", "-m", "other root")
        out2 = io.StringIO()
        with contextlib.redirect_stdout(out2):
            uc.main(["--project", str(self.fx.project), "--channel", str(other), "apply", "--as", "claude",
                     "--session", "s-claude"])
        result = json.loads(out2.getvalue())
        self.assertIn(result["code"], {"upgrade_identity_changed", "project_identity_mismatch"})
        self.assertEqual(self.pin_commit(), self.fx.from_commit)

    def test_required_notice_shown_once_after_never(self):
        self.fx.write_manifest([])
        self.run_uc("record-answer", "--answer", "never", "--target", "1.1.0")
        os.environ["UC_ADVISORY"] = "required"
        self.assertTrue(self.run_uc("check")[1]["prompt"]["should_ask"])
        self.assertFalse(self.run_uc("check")[1]["prompt"]["should_ask"])

    def test_prepare_emits_guidance_proposal(self):
        (self.fx.project / "GUIDE.md").write_text("intro\nTransport: ai-comm v1.0.0 pinned\n")
        item = self.fx.integration("own", "claude")
        root = self.fx.project / ".ai-comm-local" / "upgrade"
        uc.Project(self.fx.project).runtime()
        manifest = {"v": 1, "schema": uc.PROJECT_SCHEMA, "status": "declared",
                    "project_id": {"path": str(self.fx.project), "channel": self.fx.channel_id},
                    "integrations": [item], "guidance": ["GUIDE.md"]}
        uc.write_private_atomic(root / "project.json", json.dumps(manifest).encode())
        consent = self.consent()
        code, out = self.run_uc("prepare", "--target", "1.1.0", "--consent", consent, "--as", "claude",
                                "--session", "s-claude")
        text = Path(out["guidance_proposal"]).read_text()
        self.assertIn(f"ai-comm-upgrade-txn: {out['txn']}", text)
        self.assertIn("GUIDE.md:2: Transport: ai-comm v1.0.0 pinned", text)
        self.assertIn("ai-comm-upgrade-guidance-sha256:", text)

    def test_r3b_unauthorized_recover_neither_repairs_nor_probes(self):
        consent, out = self.prepared([self.fx.integration("peer", "codex")])
        journal = self.journal(out["txn"])
        with open(journal.path, "ab") as stream:
            stream.write(b'{"torn":')
        before = journal.path.read_bytes()
        for args in (["--as", "codex", "--session", "stale"], ["--as", "intruder", "--session", "s-codex"],
                     ["--as", "claude", "--session", "s-other"]):
            with self.subTest(args=args):
                code, res = self.run_uc("recover", *args)
                self.assertEqual(code, 2, res)
                self.assertEqual(journal.path.read_bytes(), before)
                self.assertFalse(list(journal.dir.glob("torn-*.bin")))
                self.assertFalse((self.fx.project / "peer.state.json").exists())
        code, res = self.run_uc("recover", "--as", "codex")
        self.assertEqual(res["code"], "invalid_arguments")
        code, res = self.run_uc("recover", "--as", "codex", "--session", "s-codex")
        self.assertEqual(res["integrations"]["peer"], "running_from", res)
        self.assertTrue(list(journal.dir.glob("torn-*.bin")))

    def test_r3b_unauthorized_report_does_not_repair(self):
        consent, out = self.prepared([self.fx.integration("peer", "codex")])
        res = self.run_uc("apply", "--as", "claude", "--session", "s-claude")[1]
        journal = self.journal(out["txn"])
        with open(journal.path, "ab") as stream:
            stream.write(b'{"torn":')
        before = journal.path.read_bytes()
        code, bad = self.run_uc("report", "--integration", "peer", "--phase", "pause", "--nonce", res["nonce"],
                                "--as", "claude", "--session", "s-claude")
        self.assertEqual(bad["code"], "not_integration_owner")
        self.assertEqual(journal.path.read_bytes(), before)

    def test_r5_hook_writes_target_then_fails_rollback_restores(self):
        consent, out = self.prepared()
        os.environ["UC_WRITE_THEN_FAIL"] = "own:install_source"
        code, res = self.run_uc("apply", "--as", "claude", "--session", "s-claude")
        self.assertEqual(res["code"], "hook_failed")
        self.assertEqual((self.fx.project / "own_src.py").read_bytes(),
                         (self.fx.project / "staged" / "own-to.py").read_bytes())
        os.environ.pop("UC_WRITE_THEN_FAIL")
        code, res = self.run_uc("rollback", "--as", "claude", "--session", "s-claude")
        self.assertEqual(res["status"], "rolled_back", res)
        self.assertEqual(self.pin_commit(), self.fx.from_commit)
        self.assertEqual((self.fx.project / "own_src.py").read_bytes(),
                         (self.fx.project / "staged" / "own-from.py").read_bytes())
        state = json.loads((self.fx.project / "own.state.json").read_text())
        self.assertEqual((state["paused"], state["bound"]), (False, sha((self.fx.pin / "ai_comm.py").read_bytes())))

    def test_changed_hook_code_blocks_recover_and_rollback_probes(self):
        consent, out = self.prepared()
        os.environ["UC_WRITE_THEN_FAIL"] = "own:install_source"
        self.run_uc("apply", "--as", "claude", "--session", "s-claude")
        os.environ.pop("UC_WRITE_THEN_FAIL")
        journal = self.journal(out["txn"])
        with open(self.fx.project / "listener.py", "a") as stream:
            stream.write("\nopen('SENTINEL', 'w').write('ran')\n")
        before = journal.path.read_bytes()
        for cmd in ("recover", "rollback"):
            with self.subTest(cmd=cmd):
                code, res = self.run_uc(cmd, "--as", "claude", "--session", "s-claude")
                self.assertEqual((res["code"], res.get("what")), ("upgrade_identity_changed", "integration_code"))
                self.assertFalse((self.fx.project / "SENTINEL").exists())
                self.assertEqual(journal.path.read_bytes(), before)

    def test_r6_finalize_rejects_inconsistent_release_metadata(self):
        consent, out = self.prepared()
        args = self._complete(out)
        with mock.patch.object(uc, "release_status",
                               return_value={"status": "index_inconsistent", "pinned_version": "1.1.0"}):
            code, res = self.run_uc(*args)
        self.assertEqual((res["code"], res.get("what")), ("postcondition_failed", "release_status"))

    def test_r6_attestation_does_not_survive_new_readiness(self):
        consent, out = self.prepared([self.fx.integration("peer", "codex")])
        res = self.run_uc("apply", "--as", "claude", "--session", "s-claude")[1]
        while res.get("status") == "awaiting_peer":
            self.run_uc("report", "--integration", "peer", "--phase", res["phase"], "--nonce", res["nonce"],
                        "--as", "codex", "--session", "s-codex")
            res = self.run_uc("continue", "--as", "claude", "--session", "s-claude")[1]
        args = self._complete(out)
        att = self.run_uc(*args)[1]["attestations"][0]
        self.run_uc("report", "--integration", "peer", "--phase", "attest", "--nonce", att["nonce"],
                    "--as", "codex", "--session", "s-codex")
        code, res = self.run_uc("continue", "--as", "claude", "--session", "s-claude")
        self.assertEqual(res["code"], "transaction_finished")
        txn = uc.Txn(uc.Project(self.fx.project), out["txn"])
        txn.journal.append("state", {"state": "integrated_pending_liveness"})
        self.assertEqual(uc.attestations(txn), {})
        code, res = self.run_uc(*args)
        self.assertEqual(res["status"], "awaiting_peer", res)

    def test_real_release_check_result_shapes_accepted(self):
        """The fetch predicate against the real helper's own _result() output."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("cfr", ROOT / "scripts" / "check_for_release.py")
        cfr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cfr)
        ok = [(cfr._result("ok", result=r, fetched=True, pinned="1.4.2", latest="1.5.0"), rc)
              for r, rc in (("up_to_date", 0), ("update_available", 10), ("advisory_applies", 11))]
        for value, rc in ok:
            self.assertTrue(uc.fetch_result_ok(rc, value), value)
        err = cfr._result("error", fetched=False, fetch_error="origin_redirect_refused", pinned="1.4.2", latest=None)
        self.assertFalse(uc.fetch_result_ok(2, err))
        self.assertFalse(uc.fetch_result_ok(0, cfr._result("ok", result="up_to_date", fetched=False,
                                                           pinned="1.4.2", latest="1.4.2")))

    # ---- paths and hooks

    def test_symlinked_project_path_refused(self):
        link = Path(self.temp.name) / "link"
        link.symlink_to(self.fx.project)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            uc.main(["--project", str(link), "status"])
        self.assertEqual(json.loads(out.getvalue())["code"], "unsafe_ancestry")

    def test_other_writable_ancestor_refused_root_owned_allowed(self):
        uc.check_ancestry(self.fx.project)
        shared = Path(self.temp.name) / "shared"
        shared.mkdir()
        os.chmod(shared, 0o777)  # other-writable, no sticky bit
        (shared / "p").mkdir()
        with self.assertRaises(uc.Stop) as ctx:
            uc.check_ancestry(shared / "p")
        self.assertEqual(ctx.exception.code, "unsafe_ancestry")

    def test_shared_local_dir_allowed_but_upgrade_dir_private(self):
        os.chmod(self.fx.project / ".ai-comm-local", 0o775)
        project = uc.Project(self.fx.project)
        project.runtime()
        self.assertEqual(os.stat(project.root).st_mode & 0o777, 0o700)
        os.chmod(self.fx.project / ".ai-comm-local", 0o777)
        with self.assertRaises(uc.Stop) as ctx:
            project.runtime()
        self.assertEqual(ctx.exception.code, "unsafe_runtime_path")

    def test_runtime_mode_enforced(self):
        project = uc.Project(self.fx.project)
        project.runtime()
        os.chmod(project.root, 0o755)
        with self.assertRaises(uc.Stop) as ctx:
            project.runtime()
        self.assertEqual(ctx.exception.code, "unsafe_runtime_mode")

    def test_hook_output_bounded_and_no_shell(self):
        with self.assertRaises(uc.Stop) as ctx:
            uc.run_json([sys.executable, "listener.py", "own", "big"], self.fx.project)
        self.assertEqual(ctx.exception.code, "hook_output_too_large")
        with self.assertRaises(uc.Stop) as ctx:
            uc.run_json(["echo hi; true"], self.fx.project)
        self.assertEqual(ctx.exception.code, "hook_unavailable")


if __name__ == "__main__":
    unittest.main()
