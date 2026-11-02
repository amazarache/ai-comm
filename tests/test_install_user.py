"""Tests for scripts/install_user.py (018c checkpoint a). Every test uses a temporary
cache directory; the real home is never read or written. The isolated fetch runs
against a local fixture repository standing in for RELEASE_ORIGIN."""
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
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


    # -- review 926 regressions

    def interrupt_activate(self, version):
        real = iu.Cache.set_link

        def flaky(cache, name, target):
            if name == "current":
                raise KeyboardInterrupt
            return real(cache, name, target)
        with mock.patch.object(iu.Cache, "set_link", flaky):
            with self.assertRaises(KeyboardInterrupt):
                iu.activate(version, root=self.root)

    def test_uninstall_keeps_untracked_release_file_and_unrecognized_staging(self):
        self.install()
        note = self.root / "releases" / "1.5.1" / "personal-note.txt"
        note.write_text("must be preserved\n")
        foreign = self.root / "staging" / "install-deadbeef0000"
        foreign.mkdir(mode=0o700)
        (foreign / "personal.txt").write_text("not an installer journal\n")
        out = iu.uninstall(confirm=True, root=self.root)
        self.assertEqual(out["status"], "partially_uninstalled")
        self.assertEqual(out["kept"], ["releases/1.5.1", "staging/install-deadbeef0000"])
        self.assertTrue(note.exists() and (foreign / "personal.txt").exists())

    def test_new_mutations_refused_while_an_operation_is_unfinished(self):
        self.install("1.5.1")
        self.install("1.5.0")
        self.interrupt_activate("1.5.0")
        for call in (lambda: iu.activate("1.5.0", root=self.root), lambda: iu.rollback(root=self.root),
                     lambda: self.install("1.4.2"), lambda: iu.uninstall(confirm=True, root=self.root),
                     lambda: iu.check(fetch_permitted=True, root=self.root, source=self.src())):
            self.assertEqual(self.stop(call).code, "operation_pending")
        self.assertEqual(len(iu.status(root=self.root)["pending"]), 1)
        self.assertEqual([r["action"] for r in iu.recover(root=self.root)["results"]], ["completed_switch"])
        self.assertEqual(iu.status(root=self.root)["pending"], [])
        self.assertEqual(iu.rollback(root=self.root)["current"], "1.5.1")

    def test_recover_never_replays_an_old_switch_over_newer_state(self):
        self.install("1.5.1")
        self.install("1.5.0")
        self.interrupt_activate("1.5.0")
        cache = iu.Cache(self.root)
        cache.set_link("previous", None)
        cache.set_link("current", "1.5.0")  # a state the interrupted operation never produces
        out = iu.recover(root=self.root)
        self.assertEqual([r["action"] for r in out["results"]], ["superseded"])
        self.assertEqual((self.link("current"), self.link("previous")), ("releases/1.5.0", None))
        self.assertEqual(iu.status(root=self.root)["pending"], [])

    def test_hidden_or_extra_changes_in_a_cached_release_are_detected(self):
        self.install("1.5.1")
        release = self.root / "releases" / "1.5.0"
        cases = {
            "content": lambda: ((tip.git(release, "update-index", "--assume-unchanged", "ai_comm.py"),
                                 (release / "ai_comm.py").write_text("raise RuntimeError('modified')\n"))),
            "unexpected": lambda: (release / "extra.py").write_text("x = 1\n"),
            "not_regular": lambda: ((release / "ai_comm.py").unlink(),
                                    os.symlink("PROTOCOL.md", release / "ai_comm.py")),
            "mode": lambda: os.chmod(release / "PROTOCOL.md", 0o755),
            "missing": lambda: (release / "PROTOCOL.md").unlink(),
        }
        for what, damage in cases.items():
            with self.subTest(what=what):
                self.install("1.5.0")
                damage()
                err = self.stop(iu.activate, "1.5.0", root=self.root)
                self.assertEqual((err.code, err.details.get("what")), ("release_identity_changed", what))
                self.assertEqual(self.link("current"), "releases/1.5.1")
                self.assertIn("releases/1.5.0", iu.uninstall(confirm=True, root=self.root)["kept"])
                shutil.rmtree(self.root)
                self.install("1.5.1")

    def test_verify_tree_does_not_trust_a_rewritten_object_store(self):
        self.install()
        release = self.root / "releases" / "1.5.1"
        record = json.loads((self.root / "state.json").read_text())["releases"]["1.5.1"]
        self.assertEqual(iu.ri.verify_tree(release, record["commit"], record["manifest_sha256"])["commit"],
                         record["commit"])
        err = self.stop(iu.ri.verify_tree, release, "0" * 40, record["manifest_sha256"])
        self.assertEqual(err.details.get("what"), "object")
        err = self.stop(iu.ri.verify_tree, release, record["commit"], "f" * 64)
        self.assertEqual(err.details.get("what"), "manifest")

    def test_interrupted_uninstall_is_finished_by_recover(self):
        self.install("1.5.1")
        self.install("1.5.0")
        real, calls = iu.shutil.rmtree, []

        def flaky(path, *a, **kw):
            real(path, *a, **kw)
            calls.append(path)
            if len(calls) == 1:
                raise KeyboardInterrupt
        with mock.patch.object(iu.shutil, "rmtree", flaky):
            with self.assertRaises(KeyboardInterrupt):
                iu.uninstall(confirm=True, root=self.root)
        report = iu.status(root=self.root)
        self.assertEqual((report["current"], len(report["pending"])), (None, 1))
        self.assertEqual(self.stop(iu.activate, "1.5.0", root=self.root).code, "operation_pending")
        out = iu.recover(root=self.root)
        self.assertEqual([r["action"] for r in out["results"]], ["completed_uninstall"])
        self.assertFalse(self.root.exists())

    def test_interrupted_uninstall_resumed_by_uninstall(self):
        self.install()
        with mock.patch.object(iu.Cache, "write_state", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                iu.uninstall(confirm=True, root=self.root)
        self.assertFalse((self.root / "releases" / "1.5.1").exists(), "moved into the operation's own directory")
        self.assertEqual(iu.uninstall(confirm=True, root=self.root)["status"], "uninstalled")
        self.assertFalse(self.root.exists())

    def test_refused_install_leaves_nothing_that_blocks_a_retry(self):
        def failing(repo):
            raise iu.Stop("verify_failed", check="tests")
        self.stop(self.install, suite=failing)
        self.assertEqual(list((self.root / "staging").iterdir()), [])
        self.assertEqual(self.install()["status"], "installed")

    def test_recover_removes_empty_operation_directory(self):
        self.install()
        (self.root / "journal" / "activate-0123456789ab").mkdir(mode=0o700)
        out = iu.recover(root=self.root)
        self.assertEqual([r["action"] for r in out["results"]], ["removed_empty"])


    # -- review 927 regressions

    def own_source(self):
        """A private fixture origin this test may change."""
        base = Path(self.temp.name).resolve() / "own"
        base.mkdir()
        source, _ = tip.build_source(base)
        return {"kind": "git", "origin": str(source), "version": "1.5.3", "manifest_sha256": "0" * 64}, source

    @staticmethod
    def edit_index(source, change):
        tip.git(source, "switch", "-q", "release-index")
        index = json.loads((source / "RELEASES.json").read_text())
        change(index["releases"])
        (source / "RELEASES.json").write_text(json.dumps(index, sort_keys=True, separators=(",", ":")) + "\n")
        tip.git(source, "commit", "-q", "-am", "edit")
        tip.git(source, "switch", "-q", "main")

    def test_refresh_drops_a_binding_the_new_index_contradicts(self):
        src, origin = self.own_source()
        self.install("1.5.0", source=src)
        self.assertTrue(iu.check(fetch_permitted=True, root=self.root, source=src)["offer"])
        tip.git(origin, "tag", "-d", "v1.5.1")

        def change(releases):
            next(r for r in releases if r["package_version"] == "1.5.1")["manifest_sha256"] = "e" * 64
        self.edit_index(origin, change)
        out = iu.check(fetch_permitted=True, root=self.root, source=src)
        self.assertEqual((out["offer"], out["latest_verified"], out["unverified_newer"]), (False, "1.5.0", ["1.5.1"]))
        self.assertNotIn("1.5.1", json.loads((self.root / "bindings.json").read_text())["bindings"])

    def test_refresh_keeps_an_agreeing_binding_when_the_tag_is_not_fetched(self):
        src, origin = self.own_source()
        self.install("1.5.0", source=src)
        iu.check(fetch_permitted=True, root=self.root, source=src)
        tip.git(origin, "tag", "-d", "v1.5.1")
        out = iu.check(fetch_permitted=True, root=self.root, source=src)
        self.assertEqual((out["offer"], out["latest_verified"]), (True, "1.5.1"))

    def test_conflicting_tag_is_never_offered(self):
        src, origin = self.own_source()
        self.install("1.5.0", source=src)

        def change(releases):
            next(r for r in releases if r["package_version"] == "1.5.1")["commit"] = "a" * 40
        self.edit_index(origin, change)
        out = iu.check(fetch_permitted=True, root=self.root, source=src)
        self.assertEqual((out["offer"], out["unverified_newer"]), (False, ["1.5.1"]))

    def test_failed_refresh_changes_nothing_and_offers_nothing_new(self):
        src, origin = self.own_source()
        self.install("1.5.0", source=src)
        before = {n: (self.root / n).read_bytes() for n in ("bindings.json", "index.json")}
        def unreachable(repo, origin, use_helper):
            raise iu.Stop("fetch_failed", helper_error="network")
        self.assertEqual(self.stop(iu.check, fetch_permitted=True, root=self.root, source=src,
                                   fetch=unreachable).code, "fetch_failed")
        self.assertEqual({n: (self.root / n).read_bytes() for n in before}, before)
        out = iu.check(root=self.root)
        self.assertEqual((out["offer"], out["metadata"], out["verdict"]), (False, "failed", "unknown"))

    def test_index_flags_and_symlinked_directories_cannot_hide_changes(self):
        self.install("1.5.1")
        release = self.root / "releases" / "1.5.0"
        cases = {
            "skip-worktree": ("content", lambda: (tip.git(release, "update-index", "--skip-worktree", "ai_comm.py"),
                                                   (release / "ai_comm.py").write_text("x = 1\n"))),
            "working manifest": ("content", lambda: (release / "RELEASE-MANIFEST.json").write_text("{}\n")),
            "symlinked directory": ("not_regular", lambda: (shutil.move(str(release / "scripts"),
                                                                       str(self.data / "moved-scripts")),
                                                           os.symlink(self.data / "moved-scripts",
                                                                      release / "scripts"))),
        }
        for label, (what, damage) in cases.items():
            with self.subTest(case=label):
                self.install("1.5.0")
                damage()
                err = self.stop(iu.activate, "1.5.0", root=self.root)
                self.assertEqual((err.code, err.details.get("what")), ("release_identity_changed", what))
                self.assertEqual((self.link("current"), self.link("previous")), ("releases/1.5.1", None))
                shutil.rmtree(self.root)
                shutil.rmtree(self.data / "moved-scripts", ignore_errors=True)
                self.install("1.5.1")

    def interrupt_at_each_journal_record(self, setup, operation, finish):
        """Interrupt `operation` before its n-th journal record, for every n, then check
        that `finish` completes it from that boundary."""
        real = uc_append = iu.uc.Journal.append
        n = 1
        while True:
            setup()
            calls = [0]

            def flaky(journal, kind, data, n=n):
                calls[0] += 1
                if calls[0] == n:
                    raise KeyboardInterrupt
                return uc_append(journal, kind, data)
            with self.subTest(boundary=n):
                with mock.patch.object(iu.uc.Journal, "append", flaky):
                    try:
                        operation()
                        interrupted = False
                    except KeyboardInterrupt:
                        interrupted = True
                self.assertIs(iu.uc.Journal.append, real)
                finish(interrupted)
            if not interrupted:
                return n
            n += 1

    def test_activate_interrupted_at_every_boundary_recovers_to_one_consistent_state(self):
        def setup():
            shutil.rmtree(self.root, ignore_errors=True)
            self.install("1.5.1")
            self.install("1.5.0")

        def finish(interrupted):
            iu.recover(root=self.root)
            self.assertEqual(iu.status(root=self.root)["pending"], [])
            links = (self.link("current"), self.link("previous"))
            self.assertIn(links, [("releases/1.5.1", None), ("releases/1.5.0", "releases/1.5.1")])
        self.assertGreater(self.interrupt_at_each_journal_record(
            setup, lambda: iu.activate("1.5.0", root=self.root), finish), 4)

    def test_uninstall_interrupted_at_every_boundary_is_completed(self):
        def setup():
            shutil.rmtree(self.root, ignore_errors=True)
            self.install("1.5.1")
            self.install("1.5.0")

        def finish(interrupted):
            if interrupted:
                self.assertNotIn("cache_link_invalid", json.dumps(iu.status(root=self.root)))
                iu.recover(root=self.root)
            if self.root.exists():
                # Interrupted before the start record: the uninstall never began.
                report = iu.status(root=self.root)
                self.assertEqual((report["current"], report["pending"]), ("1.5.1", []))
                self.assertEqual(iu.uninstall(confirm=True, root=self.root)["status"], "uninstalled")
            self.assertFalse(self.root.exists())
        self.assertGreater(self.interrupt_at_each_journal_record(
            setup, lambda: iu.uninstall(confirm=True, root=self.root), finish), 4)

    def test_user_file_added_after_an_interrupted_uninstall_is_kept(self):
        self.install()
        with mock.patch.object(iu.Cache, "write_state", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                iu.uninstall(confirm=True, root=self.root)
        (self.root / "added-later.txt").write_text("mine\n")
        out = iu.recover(root=self.root)
        self.assertEqual(out["results"][0]["kept"], ["added-later.txt"])
        self.assertEqual(sorted(os.listdir(self.root)), ["added-later.txt", "lock"])

    def test_sentinel_inside_a_recognized_operation_directory_is_kept(self):
        self.install()
        done = next((self.root / "staging").iterdir())
        (done / "sentinel.txt").write_text("mine\n")
        out = iu.uninstall(confirm=True, root=self.root)
        self.assertEqual(out["kept"], [f"staging/{done.name}"])
        self.assertTrue((done / "sentinel.txt").exists())


    # -- review 931 (049) regressions: removal needs journal state plus identity

    def assert_stable(self, kept):
        """Repeated uninstall and recover keep the same objects and never fail."""
        for _ in range(2):
            self.assertEqual(iu.recover(root=self.root)["status"], "recovery_report")
            self.assertEqual(iu.status(root=self.root)["pending"], [])
            self.assertEqual(iu.uninstall(confirm=True, root=self.root)["kept"], kept)

    def test_new_repo_in_a_completed_install_directory_is_kept(self):
        self.install()
        done = next((self.root / "staging").iterdir())
        (done / "repo").mkdir()
        (done / "repo" / "personal.txt").write_text("mine\n")
        out = iu.uninstall(confirm=True, root=self.root)
        self.assertEqual((out["status"], out["kept"]), ("partially_uninstalled", [f"staging/{done.name}"]))
        self.assert_stable([f"staging/{done.name}"])
        self.assertTrue((done / "repo" / "personal.txt").exists())

    def interrupted_uninstall(self):
        with mock.patch.object(iu.Cache, "write_state", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                iu.uninstall(confirm=True, root=self.root)
        return next((self.root / "journal").glob("uninstall-*"))

    def test_changed_trash_is_restored_to_its_original_path(self):
        self.install()
        op_dir = self.interrupted_uninstall()
        (op_dir / "trash-1" / "personal.txt").write_text("mine\n")
        out = iu.recover(root=self.root)
        self.assertEqual(out["results"][0]["kept"], ["releases/1.5.1"])
        self.assertTrue((self.root / "releases" / "1.5.1" / "personal.txt").exists())
        self.assert_stable(["releases/1.5.1"])
        self.assertTrue((self.root / "releases" / "1.5.1" / "personal.txt").exists())

    def test_changed_trash_whose_original_path_is_taken_stays_in_the_operation_directory(self):
        self.install()
        op_dir = self.interrupted_uninstall()
        (op_dir / "trash-1" / "personal.txt").write_text("mine\n")
        (self.root / "releases" / "1.5.1").mkdir()
        out = iu.recover(root=self.root)
        self.assertEqual(out["results"][0]["kept"], [f"journal/{op_dir.name}/trash-1", "releases/1.5.1"])
        self.assertTrue((op_dir / "trash-1" / "personal.txt").exists())
        self.assertEqual(iu.status(root=self.root)["pending"], [], "resolved; nothing is blocked")
        self.assert_stable([f"journal/{op_dir.name}", "releases/1.5.1"])
        self.assertTrue((op_dir / "trash-1" / "personal.txt").exists())

    def test_changed_moved_operation_directory_is_restored(self):
        self.install()
        staging_name = next((self.root / "staging").iterdir()).name
        real, calls = iu.shutil.rmtree, []

        def flaky(path, *a, **kw):
            calls.append(path)
            if len(calls) == 1:
                raise KeyboardInterrupt  # before deleting any moved object
            return real(path, *a, **kw)
        with mock.patch.object(iu.shutil, "rmtree", flaky):
            with self.assertRaises(KeyboardInterrupt):
                iu.uninstall(confirm=True, root=self.root)
        op_dir = next((self.root / "journal").glob("uninstall-*"))
        intents = iu._trash_intents(iu.uc.Journal(op_dir))
        moved = next(n for n, a in intents.items() if a["identity"]["kind"] == "op")
        (op_dir / moved / "sentinel.txt").write_text("mine\n")
        out = iu.recover(root=self.root)
        self.assertEqual(out["results"][0]["kept"], [f"staging/{staging_name}"])
        self.assertTrue((self.root / "staging" / staging_name / "sentinel.txt").exists())

    def test_unexpected_entries_never_hide_an_unfinished_operation(self):
        self.install("1.5.1")
        self.install("1.5.0")
        self.interrupt_activate("1.5.0")
        op_dir = next((self.root / "journal").glob("activate-*"))
        (op_dir / "sentinel.txt").write_text("mine\n")
        self.assertEqual(self.stop(iu.rollback, root=self.root).code, "operation_pending")
        self.assertEqual([r["action"] for r in iu.recover(root=self.root)["results"]], ["completed_switch"])
        self.assertIn(f"journal/{op_dir.name}", iu.uninstall(confirm=True, root=self.root)["kept"])
        self.assertTrue((op_dir / "sentinel.txt").exists())

    def test_sentinel_in_an_unfinished_uninstall_directory_is_kept_after_completion(self):
        self.install()
        op_dir = self.interrupted_uninstall()
        (op_dir / "sentinel.txt").write_text("mine\n")
        self.assertEqual(self.stop(self.install, version="1.5.0").code, "operation_pending")
        out = iu.recover(root=self.root)
        self.assertEqual(out["results"][0]["kept"], [f"journal/{op_dir.name}/sentinel.txt"])
        self.assertTrue((op_dir / "sentinel.txt").exists())
        self.assertFalse((self.root / "releases" / "1.5.1").exists())
        self.assert_stable([f"journal/{op_dir.name}"])

    def interrupted_staging(self, debris):
        def suite(repo):
            debris(repo)
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.install(suite=suite)
        return next((self.root / "staging").glob("install-*"))

    def test_interrupted_staging_with_only_test_bytecode_is_removed(self):
        def debris(repo):
            (repo / "scripts" / "__pycache__").mkdir()
            (repo / "scripts" / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"\x00")
        self.interrupted_staging(debris)
        self.assertEqual([r["action"] for r in iu.recover(root=self.root)["results"]], ["removed_unpublished"])
        self.assertEqual(list((self.root / "staging").iterdir()), [])

    def test_interrupted_staging_with_user_files_is_kept_and_no_longer_blocks(self):
        staging = self.interrupted_staging(lambda repo: (repo / "personal.txt").write_text("mine\n"))
        self.assertEqual(self.stop(self.install).code, "operation_pending")
        self.assertEqual([r["action"] for r in iu.recover(root=self.root)["results"]], ["kept_for_inspection"])
        self.assertTrue((staging / "repo" / "personal.txt").exists())
        self.assertEqual(iu.recover(root=self.root)["results"], [], "resolved: nothing to do again")
        self.assertEqual(self.install()["status"], "installed")
        self.assertIn(f"staging/{staging.name}", iu.uninstall(confirm=True, root=self.root)["kept"])
        self.assertTrue((staging / "repo" / "personal.txt").exists())

    def test_commands_work_after_a_partial_uninstall(self):
        self.install()
        (self.root / "notes.txt").write_text("mine\n")
        self.assertEqual(iu.uninstall(confirm=True, root=self.root)["kept"], ["notes.txt"])
        self.assertEqual(iu.check(fetch_permitted=True, root=self.root, source=self.src())["metadata"], "refreshed")
        self.assertEqual(self.install()["status"], "installed")
        self.assertEqual(iu.uninstall(confirm=True, root=self.root)["kept"], ["notes.txt"])

    def test_check_scratch_with_extra_files_is_kept(self):
        self.install()

        def fetch(repo, origin, use_helper):
            (repo / "personal.txt").write_text("mine\n")
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            with mock.patch.object(iu.shutil, "rmtree"):  # the scratch is normally removed on the way out
                iu.check(fetch_permitted=True, root=self.root, source=self.src(), fetch=fetch)
        scratch = next((self.root / "staging").glob("check-*"))
        self.assertEqual([r["action"] for r in iu.recover(root=self.root)["results"]], ["kept_unexpected"])
        self.assertIn(scratch.name, iu.status(root=self.root)["unrecognized"])
        self.assertIn(f"staging/{scratch.name}", iu.uninstall(confirm=True, root=self.root)["kept"])
        self.assertTrue((scratch / "repo" / "personal.txt").exists())


class BootstrapExtractionTests(unittest.TestCase):
    """The extraction command in docs/INSTALL.md, run exactly as documented."""

    @classmethod
    def setUpClass(cls):
        text = (ROOT / "docs" / "INSTALL.md").read_text()
        start = text.index("python3 -I -c '") + len("python3 -I -c '")
        cls.code = text[start:text.index("' \"ai-comm-X.tar.gz\" pkg", start)]

    def run_doc(self, members):
        with tempfile.TemporaryDirectory(prefix="iu-tar-") as temp:
            archive, dest = Path(temp) / "a.tar.gz", Path(temp) / "pkg"
            dest.mkdir()
            with tarfile.open(archive, "w:gz") as tar:
                for info, data in members:
                    tar.addfile(info, io.BytesIO(data) if data is not None else None)
            done = subprocess.run([sys.executable, "-I", "-c", self.code, str(archive), str(dest)],
                                  capture_output=True, timeout=60, check=False)
            return done.returncode, sorted(p.name for p in dest.rglob("*"))

    @staticmethod
    def member(name, kind=tarfile.REGTYPE, data=b"x", link=""):
        info = tarfile.TarInfo(name)
        info.type, info.linkname = kind, link
        info.size = len(data) if kind == tarfile.REGTYPE else 0
        return info, data if kind == tarfile.REGTYPE else None

    def test_release_shaped_archive_extracts(self):
        code, files = self.run_doc([self.member("pkg", tarfile.DIRTYPE), self.member("pkg/a.py")])
        self.assertEqual((code, files), (0, ["a.py", "pkg"]))

    def test_unexpected_members_refuse_the_whole_archive(self):
        bad = {"absolute": self.member("/absolute.txt"), "dotdot": self.member("pkg/../../x"),
               "symlink": self.member("pkg/s", tarfile.SYMTYPE, link="a.py"),
               "hardlink": self.member("pkg/h", tarfile.LNKTYPE, link="pkg/a.py"),
               "device": self.member("pkg/d", tarfile.CHRTYPE), "fifo": self.member("pkg/f", tarfile.FIFOTYPE)}
        for what, item in bad.items():
            with self.subTest(what=what):
                code, files = self.run_doc([self.member("pkg/a.py"), item])
                self.assertNotEqual(code, 0)
                self.assertEqual(files, [], "nothing is written before the check")
        code, files = self.run_doc([self.member("pkg/a.py"), self.member("pkg/a.py", data=b"y")])
        self.assertEqual((code != 0, files), (True, []))

if __name__ == "__main__":
    unittest.main()
