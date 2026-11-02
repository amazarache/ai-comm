"""Tests for scripts/declare_project.py (018c section 7). Temporary projects,
channels and caches only."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import declare_project as dp  # noqa: E402
import test_install_project as tip  # noqa: E402
import test_verify_pin as tvp  # noqa: E402

uc, iu = dp.uc, dp.iu


class DeclareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory(prefix="dp-src-")
        cls.origin, _ = tip.build_source(Path(cls.shared.name).resolve())
        cls.binding = tvp.binding(cls.origin, "1.5.1")

    @classmethod
    def tearDownClass(cls):
        cls.shared.cleanup()

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dp-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        os.chmod(self.base, 0o700)
        self.project = self.base / "project"
        self.project.mkdir()
        tvp.make_pin(self.project, self.origin, "1.5.1")
        self.cache = iu.Cache(self.base / "cache").open(create=True)
        self.cache.write_bindings({"v": 1, "bindings": {"1.5.1": self.binding}})
        self.sentinel = self.base / "integration-ran"

    def channel(self):
        channel = self.project / ".ai-comm"
        tip.git(self.project, "init", "-q", str(channel))
        tip.git(channel, "commit", "-q", "--allow-empty", "-m", "root")
        return tip.git(channel, "rev-list", "--max-parents=0", "HEAD").strip()

    def declaration(self, channel, **change):
        run = ["sh", "-c", f"echo ran > '{self.sentinel}'"]
        value = {"v": 1, "schema": uc.PROJECT_SCHEMA, "status": "declared",
                 "project_id": {"path": str(self.project), "channel": channel}, "guidance": [],
                 "integrations": [{"id": "listener", "owner": "alpha", "supports": {},
                                   "probe": {"argv": run, "run_by": "owner"},
                                   "steps": {s: run for s in ("pause", "rebind", "check", "resume")},
                                   "code_files": ["tools/listener.py"]}]}
        value.update(change)
        path = self.base / "candidate.json"
        data = json.dumps(value, sort_keys=True, indent=1).encode() + b"\n"
        path.write_bytes(data)
        return path, uc.sha256_bytes(data)

    def live(self):
        path = self.project / ".ai-comm-local" / "upgrade" / "project.json"
        return path.read_bytes() if path.exists() else None

    def stop(self, fn, *a, **kw):
        with self.assertRaises(dp.Stop) as ctx:
            fn(*a, **kw)
        return ctx.exception

    def install(self, path, digest, previous="absent"):
        return dp.install(self.project, candidate=path, sha256=digest, previous=previous, cache=self.cache)

    # -- init

    def test_init_writes_the_installers_unintegrated_bytes_and_never_overwrites(self):
        out = dp.init(self.project, cache=self.cache)
        self.assertEqual(self.live(), dp.ri.unintegrated_declaration(self.project))
        self.assertEqual(out["sha256"], uc.sha256_bytes(self.live()))
        path = self.project / ".ai-comm-local" / "upgrade" / "project.json"
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(path.parent).st_mode & 0o777, 0o700)
        self.assertEqual(self.stop(dp.init, self.project, cache=self.cache).code, "declaration_exists")

    def test_unverified_pin_is_refused(self):
        (self.project / ".tools" / "ai-comm" / "ai_comm.py").write_text("x\n")
        err = self.stop(dp.init, self.project, cache=self.cache)
        self.assertEqual((err.code, err.details["status"]), ("pin_not_verified", "modified"))
        self.assertIsNone(self.live())

    # -- install

    def test_successful_install_is_exactly_the_reviewed_bytes_and_runs_nothing(self):
        channel = self.channel()
        dp.init(self.project, cache=self.cache)
        previous = uc.sha256_bytes(self.live())
        path, digest = self.declaration(channel)
        out = self.install(path, digest, previous)
        self.assertEqual((out["status"], self.live()), ("declared", path.read_bytes()))
        self.assertFalse(self.sentinel.exists())
        self.assertEqual(uc.load_project_manifest(uc.Project(self.project))["status"], "declared")

    def test_failures_leave_the_previous_declaration_byte_identical(self):
        channel = self.channel()
        dp.init(self.project, cache=self.cache)
        before = self.live()
        previous = uc.sha256_bytes(before)
        good, digest = self.declaration(channel)
        cases = {
            "invalid candidate": (lambda: self.install(*self.declaration(channel, integrations="x"), previous),
                                  "project_manifest_invalid"),
            "other project": (lambda: self.install(*self.declaration("0" * 40), previous),
                              "project_identity_mismatch"),
            "digest mismatch": (lambda: self.install(good, "f" * 64, previous), "candidate_digest_mismatch"),
            "stale previous": (lambda: self.install(*self.declaration(channel), "absent"), "declaration_changed"),
        }
        for label, (call, code) in cases.items():
            with self.subTest(case=label):
                self.assertEqual(self.stop(call).code, code)
                self.assertEqual(self.live(), before)
        self.assertFalse(self.sentinel.exists())
        self.assertEqual(digest, uc.sha256_bytes(good.read_bytes()))

    def test_hash_race_between_validation_and_replacement(self):
        channel = self.channel()
        dp.init(self.project, cache=self.cache)
        previous = uc.sha256_bytes(self.live())
        path, digest = self.declaration(channel)
        live_path = self.project / ".ai-comm-local" / "upgrade" / "project.json"
        real = uc.validate_project_manifest

        def racing(data, identity):
            result = real(data, identity)
            uc.write_private_atomic(live_path, b'{"someone": "else"}\n')  # a concurrent edit
            return result
        with mock.patch.object(uc, "validate_project_manifest", racing):
            self.assertEqual(self.stop(self.install, path, digest, previous).code, "declaration_changed")
        self.assertEqual(self.live(), b'{"someone": "else"}\n')

    def test_guided_transaction_in_flight_is_refused(self):
        channel = self.channel()
        dp.init(self.project, cache=self.cache)
        before = self.live()
        upgrade = uc.Project(self.project)
        txn = "txn-0123456789abcdef"
        journal = uc.Journal(uc.private_dir(upgrade.root / "txn" / txn))
        journal.append("start", {"txn": txn})
        journal.append("state", {"state": "applying"})
        uc.write_private_atomic(upgrade.root / "current", txn.encode())
        path, digest = self.declaration(channel)
        err = self.stop(self.install, path, digest, uc.sha256_bytes(before))
        self.assertEqual((err.code, err.details["state"]), ("transaction_in_flight", "applying"))
        self.assertEqual(self.live(), before)
        journal.append("state", {"state": "completed"})
        self.assertEqual(self.install(path, digest, uc.sha256_bytes(before))["status"], "declared")

    def test_interrupted_write_keeps_the_previous_declaration(self):
        channel = self.channel()
        dp.init(self.project, cache=self.cache)
        before = self.live()
        path, digest = self.declaration(channel)
        with mock.patch.object(uc.os, "replace", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.install(path, digest, uc.sha256_bytes(before))
        self.assertEqual(self.live(), before)
        upgrade = self.project / ".ai-comm-local" / "upgrade"
        self.assertEqual([p.name for p in upgrade.iterdir() if p.name.startswith(".tmp-")], [])

    def test_absent_previous_and_missing_channel(self):
        path, digest = self.declaration("0" * 40)
        err = self.stop(self.install, path, digest)
        self.assertIn(err.code, {"channel_identity_unavailable", "git_failed", "project_missing"})
        channel = self.channel()
        path, digest = self.declaration(channel)
        self.assertEqual(self.install(path, digest, "absent")["status"], "declared")

    def test_cli(self):
        import subprocess
        done = subprocess.run([sys.executable, "-I", str(ROOT / "scripts" / "declare_project.py"), "--cache",
                               str(self.cache.root), "init", "--project", str(self.project)],
                              capture_output=True, timeout=120, check=False)
        self.assertEqual((done.returncode, json.loads(done.stdout)["status"]), (0, "initialized"))
        done = subprocess.run([sys.executable, "-I", str(ROOT / "scripts" / "declare_project.py"), "--cache",
                               str(self.cache.root), "init", "--project", str(self.project)],
                              capture_output=True, timeout=120, check=False)
        self.assertEqual((done.returncode, json.loads(done.stdout)["code"]), (2, "declaration_exists"))


if __name__ == "__main__":
    unittest.main()
