"""Tests for scripts/install_user.py (018c checkpoint a). Every test uses a temporary
cache directory; the real home is never read or written. The isolated fetch runs
against a local fixture repository standing in for RELEASE_ORIGIN."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import install_user as iu  # noqa: E402
import test_install_project as tip  # noqa: E402


class UserCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory(prefix="iu-src-")
        cls.source, cls.releases = tip.build_source(Path(cls.shared.name).resolve())

    @classmethod
    def tearDownClass(cls):
        cls.shared.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="iu-")
        self.addCleanup(self.temp.cleanup)
        os.chmod(self.temp.name, 0o700)
        self.data = Path(self.temp.name).resolve() / "data"
        self.root = self.data / "ai-comm"

    def src(self, kind="git", version="1.5.3", manifest="0" * 64):
        return {"kind": kind, "origin": str(self.source), "version": version, "manifest_sha256": manifest}

    def install(self, version="1.5.1", suite=lambda repo: 7, **kw):
        return iu.install(version, fetch_permitted=kw.pop("fetch_permitted", True), use_helper=False,
                          root=self.root, source=kw.pop("source", None) or self.src(), run_suite=suite, **kw)

    def stop(self, fn, *a, **kw):
        with self.assertRaises(iu.Stop) as ctx:
            fn(*a, **kw)
        return ctx.exception

    def link(self, name):
        path = self.root / name
        return os.readlink(path) if os.path.lexists(path) else None

    def test_install_publishes_verified_release_and_activates_first(self):
        out = self.install()
        self.assertEqual((out["status"], out["current"], out["previous"]), ("installed", "1.5.1", None))
        release = self.root / "releases" / "1.5.1"
        self.assertEqual(tip.git(release, "rev-parse", "HEAD").strip(), self.releases["1.5.1"]["commit"])
        self.assertEqual(self.link("current"), "releases/1.5.1")
        self.assertEqual(os.stat(self.root).st_mode & 0o777, 0o700)
        for name in ("state.json", "bindings.json", "index.json"):
            self.assertEqual(os.stat(self.root / name).st_mode & 0o777, 0o600, name)
        bindings = json.loads((self.root / "bindings.json").read_text())["bindings"]
        self.assertEqual(list(bindings), ["1.5.1"], "install binds only its own fetched tag")
        self.assertEqual(bindings["1.5.1"]["commit"], self.releases["1.5.1"]["commit"])
        self.assertEqual(len(bindings["1.5.1"]["tag_object"]), 40)

    def test_fetch_permission_required_and_nothing_created(self):
        self.assertEqual(self.stop(self.install, fetch_permitted=False).code, "fetch_not_permitted")
        self.assertFalse(self.root.exists())

    def test_suite_failure_publishes_nothing(self):
        def failing(repo):
            raise iu.Stop("verify_failed", check="tests")
        self.assertEqual(self.stop(self.install, suite=failing).code, "verify_failed")
        self.assertFalse((self.root / "releases" / "1.5.1").exists())
        self.assertIsNone(self.link("current"))
        self.assertEqual(json.loads((self.root / "state.json").read_text())["releases"]
                         if (self.root / "state.json").exists() else {}, {})

    def test_same_version_not_reinstalled(self):
        self.install()
        self.assertEqual(self.stop(self.install).code, "release_installed")

    def test_second_install_does_not_activate_then_activate_and_rollback(self):
        self.install("1.5.1")
        out = self.install("1.5.0")
        self.assertEqual((out["current"], out["previous"]), ("1.5.1", None))
        out = iu.activate("1.5.0", root=self.root)
        self.assertEqual((out["current"], out["previous"]), ("1.5.0", "1.5.1"))
        self.assertEqual((self.link("current"), self.link("previous")), ("releases/1.5.0", "releases/1.5.1"))
        out = iu.rollback(root=self.root)
        self.assertEqual((out["current"], out["previous"]), ("1.5.1", "1.5.0"))
        self.assertEqual(self.stop(iu.activate, "1.5.1", root=self.root).code, "already_current")
        self.assertEqual(self.stop(iu.activate, "1.9.9", root=self.root).code, "release_not_installed")

    def test_tampered_release_cannot_be_activated(self):
        self.install("1.5.1")
        self.install("1.5.0")
        (self.root / "releases" / "1.5.0" / "ai_comm.py").write_text("tampered\n")
        self.assertEqual(self.stop(iu.activate, "1.5.0", root=self.root).code, "release_identity_changed")
        self.assertEqual(self.link("current"), "releases/1.5.1")

    def test_link_outside_releases_stops(self):
        self.install()
        os.unlink(self.root / "current")
        os.symlink(str(self.data), self.root / "current")
        self.assertEqual(self.stop(iu.status, root=self.root).code, "cache_link_invalid")

    def test_archive_bridge_requires_matching_manifest(self):
        bad = self.src("archive", "1.5.1", "f" * 64)
        self.assertEqual(self.stop(self.install, source=bad).code, "archive_manifest_mismatch")
        self.assertFalse((self.root / "releases" / "1.5.1").exists())
        good = self.src("archive", "1.5.1", self.releases["1.5.1"]["manifest_sha256"])
        self.assertEqual(self.install(source=good)["status"], "installed")

    def test_check_offline_then_consented_refresh_builds_bindings(self):
        self.install("1.5.0")
        out = iu.check(root=self.root)
        self.assertEqual((out["current"], out["offer"], out["unverified_newer"], out["metadata"]),
                         ("1.5.0", False, ["1.5.1"], "cached"))
        out = iu.check(fetch_permitted=True, root=self.root, source=self.src(), now=lambda: 1234)
        self.assertEqual((out["offer"], out["latest_verified"], out["metadata"], out["index_refreshed_at"]),
                         (True, "1.5.1", "refreshed", 1234))
        bindings = json.loads((self.root / "bindings.json").read_text())["bindings"]
        self.assertEqual(sorted(bindings), ["1.5.0", "1.5.1"])
        self.assertEqual([p.name for p in (self.root / "staging").iterdir() if p.name.startswith("check-")], [],
                         "check scratch removed")
        self.assertEqual(self.stop(iu.check, use_helper=True, root=self.root).code,
                         "credential_helper_requires_fetch_permission")

    def test_recover_removes_unpublished_staging(self):
        def interrupted(repo):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.install(suite=interrupted)
        out = iu.recover(root=self.root)
        self.assertEqual([r["action"] for r in out["results"]], ["removed_unpublished"])
        self.assertEqual(list((self.root / "staging").iterdir()), [])
        self.assertFalse((self.root / "releases" / "1.5.1").exists())

    def test_recover_completes_after_publish_and_activation_intent(self):
        real = iu._record_release
        with mock.patch.object(iu, "_record_release", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.install()
        self.assertTrue((self.root / "releases" / "1.5.1").is_dir())
        self.assertIsNone(self.link("current"))
        self.assertIs(iu._record_release, real)
        out = iu.recover(root=self.root)
        self.assertEqual([r["action"] for r in out["results"]], ["completed_after_publish"])
        self.assertEqual(self.link("current"), "releases/1.5.1")
        self.assertIn("1.5.1", json.loads((self.root / "state.json").read_text())["releases"])
        self.assertEqual(iu.recover(root=self.root)["results"], [], "recovery is idempotent")

    def test_recover_completes_interrupted_switch(self):
        self.install("1.5.1")
        self.install("1.5.0")
        real = iu.Cache.set_link

        def flaky(cache, name, version):
            if name == "current":
                raise KeyboardInterrupt
            return real(cache, name, version)
        with mock.patch.object(iu.Cache, "set_link", flaky):
            with self.assertRaises(KeyboardInterrupt):
                iu.activate("1.5.0", root=self.root)
        self.assertEqual((self.link("current"), self.link("previous")), ("releases/1.5.1", "releases/1.5.1"))
        out = iu.recover(root=self.root)
        self.assertEqual([r["action"] for r in out["results"]], ["completed_switch"])
        self.assertEqual((self.link("current"), self.link("previous")), ("releases/1.5.0", "releases/1.5.1"))

    def test_uninstall_removes_only_inventoried_objects(self):
        self.assertEqual(self.stop(iu.uninstall, confirm=False, root=self.root).code, "confirmation_required")
        self.install()
        out = iu.uninstall(confirm=True, root=self.root)
        self.assertEqual(out["status"], "uninstalled")
        self.assertFalse(self.root.exists())

    def test_uninstall_keeps_unexpected_and_changed_content(self):
        self.install("1.5.1")
        self.install("1.5.0")
        (self.root / "notes.txt").write_text("mine\n")
        (self.root / "releases" / "1.5.0" / "ai_comm.py").write_text("changed\n")
        out = iu.uninstall(confirm=True, root=self.root)
        self.assertEqual(out["status"], "partially_uninstalled")
        self.assertEqual(out["kept"], ["notes.txt", "releases/1.5.0"])
        self.assertTrue((self.root / "notes.txt").exists())
        self.assertTrue((self.root / "releases" / "1.5.0").exists())
        self.assertFalse((self.root / "releases" / "1.5.1").exists())

    def test_concurrent_mutation_is_refused(self):
        self.install()
        fd = os.open(self.root / "lock", os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.assertEqual(self.stop(iu.activate, "1.5.1", root=self.root).code, "cache_locked")

    def test_cache_root_from_environment(self):
        self.assertEqual(iu.cache_root({"XDG_DATA_HOME": "/x/y"}), Path("/x/y/ai-comm"))
        self.assertEqual(iu.cache_root({"HOME": "/srv/u"}), Path("/srv/u/.local/share/ai-comm"))
        with self.assertRaises(iu.Stop) as ctx:
            iu.cache_root({"XDG_DATA_HOME": "relative"})
        self.assertEqual(ctx.exception.code, "cache_path_invalid")

    def test_install_sh_dispatches_user_commands(self):
        env = {**os.environ, "XDG_DATA_HOME": str(self.data), "HOME": str(self.temp.name)}
        done = subprocess.run(["sh", str(ROOT / "install.sh"), "--user", "status"], capture_output=True,
                              env=env, timeout=60, check=False)
        self.assertEqual(done.returncode, 2)
        self.assertEqual(json.loads(done.stdout)["code"], "cache_missing")
        self.assertFalse(self.root.exists())


if __name__ == "__main__":
    unittest.main()
