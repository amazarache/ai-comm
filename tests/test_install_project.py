"""Tests for scripts/install_project.py (013b). The real isolated fetch runs against a
local fixture repository standing in for RELEASE_ORIGIN; no network is used."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
GUARD = "AI_COMM_UPGRADE_REAL_BOUNDARY_INNER"
ENV = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid", "HOME": "/nonexistent"}

sys.path.insert(0, str(ROOT / "scripts"))
import install_project as ip  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=ENV).stdout.decode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def build_source(base):
    """A release repository with v1.5.0 (this tree) and v1.5.1, plus a release index."""
    src = base / "origin"
    src.mkdir()
    git(src, "init", "-q", "--initial-branch=main")
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True,
                             check=True).stdout.decode().split("\0")
    for name in [t for t in tracked if t]:
        dest = src / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, dest)
    releases = []

    def set_version(version):
        cli = src / "ai_comm.py"
        cli.write_text(re.sub(r'^VERSION = "[^"]+"$', f'VERSION = "{version}"', cli.read_text(), flags=re.M))
        manifest = json.loads((src / "RELEASE-MANIFEST.json").read_text())
        for entry in manifest["files"]:
            data = (src / entry["path"]).read_bytes()
            entry["bytes"], entry["sha256"] = len(data), sha(data)
        (src / "RELEASE-MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
        git(src, "add", "-A")
        git(src, "commit", "-q", "--allow-empty", "-m", version)

    set_version("1.5.0")

    def tag(version):
        manifest = sha((src / "RELEASE-MANIFEST.json").read_bytes())
        git(src, "tag", "-a", f"v{version}", "-m", f"ai-comm {version} (protocol 3.0); manifest sha256 {manifest}")
        releases.append({"advisory": "optional", "reason": "No advisory recorded for this release.",
                         "summary": "s", "package_version": version, "tag": f"v{version}",
                         "commit": git(src, "rev-parse", "HEAD").strip(), "manifest_sha256": manifest,
                         "protocol_sha256": sha((src / "PROTOCOL.md").read_bytes())})
    tag("1.5.0")
    set_version("1.5.1")
    tag("1.5.1")
    git(src, "switch", "-q", "--orphan", "release-index")
    for child in src.iterdir():
        if child.name != ".git":
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    (src / "RELEASES.json").write_text(json.dumps({"format": "ai-comm-releases/1", "releases": releases},
                                                  sort_keys=True, separators=(",", ":")) + "\n")
    git(src, "add", "RELEASES.json")
    git(src, "commit", "-q", "-m", "index")
    git(src, "switch", "-q", "main")
    return src, {r["package_version"]: r for r in releases}


class InstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory(prefix="ip-src-")
        cls.source, cls.releases = build_source(Path(cls.shared.name).resolve())

    @classmethod
    def tearDownClass(cls):
        cls.shared.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ip-")
        self.addCleanup(self.temp.cleanup)
        os.chmod(self.temp.name, 0o700)
        self.project = Path(self.temp.name).resolve() / "project"
        self.project.mkdir()

    def install(self, version="1.5.1", suite=lambda repo: 7, **kw):
        return ip.install(self.project, version, fetch_permitted=kw.pop("fetch_permitted", True), use_helper=False,
                          origin=str(self.source), run_suite=suite, **kw)

    def stop(self, fn, *a, **kw):
        with self.assertRaises(ip.Stop) as ctx:
            fn(*a, **kw)
        return ctx.exception

    def test_install_latest_publishes_verified_pin(self):
        (self.project / ".gitignore").write_text("node_modules/\n# keep\n")
        out = self.install()
        pin = self.project / ".tools" / "ai-comm"
        self.assertEqual(out["status"], "installed")
        self.assertEqual(git(pin, "rev-parse", "HEAD").strip(), self.releases["1.5.1"]["commit"])
        self.assertEqual(out["release_awareness"], "complete")
        self.assertEqual(git(pin, "tag", "-l").split(), ["v1.5.1"])
        self.assertEqual((self.project / ".gitignore").read_text(), "node_modules/\n# keep\n.tools/\n.ai-comm-local/\n")
        upgrade = self.project / ".ai-comm-local" / "upgrade"
        self.assertEqual(os.stat(upgrade).st_mode & 0o777, 0o700)
        manifest = json.loads((upgrade / "project.json").read_text())
        self.assertEqual(manifest["status"], "unintegrated")
        self.assertEqual(os.stat(upgrade / "project.json").st_mode & 0o777, 0o600)
        self.assertFalse((self.project / ".ai-comm").exists())

    def test_older_version_reports_partial_awareness(self):
        out = self.install("1.5.0")
        self.assertTrue(out["release_awareness"].startswith("partial"))
        self.assertEqual(git(self.project / ".tools" / "ai-comm", "tag", "-l").split(), ["v1.5.0"])

    def test_gitignore_already_complete_untouched(self):
        body = b".ai-comm-local/\n.tools\nother"
        (self.project / ".gitignore").write_bytes(body)
        out = self.install()
        self.assertEqual(out["gitignore_added"], [])
        self.assertEqual((self.project / ".gitignore").read_bytes(), body)

    def test_requires_fetch_permission(self):
        self.assertEqual(self.stop(self.install, fetch_permitted=False).code, "fetch_not_permitted")
        self.assertFalse((self.project / ".ai-comm-local").exists())

    def test_unknown_version_refused(self):
        self.assertEqual(self.stop(self.install, "1.9.9").code, "fetch_failed")
        self.assertFalse((self.project / ".tools").exists())

    def test_suite_failure_leaves_project_untouched(self):
        def failing(repo):
            raise ip.Stop("verify_failed", check="tests")
        (self.project / ".gitignore").write_text("x\n")
        self.assertEqual(self.stop(self.install, suite=failing).code, "verify_failed")
        self.assertFalse((self.project / ".tools").exists())
        self.assertEqual((self.project / ".gitignore").read_text(), "x\n")
        self.assertFalse((self.project / ".ai-comm-local" / "upgrade").exists())

    def test_network_failure_leaves_project_untouched(self):
        def broken(repo, origin, version, helper):
            raise ip.Stop("fetch_failed", helper_error="network_unavailable")
        self.assertEqual(self.stop(self.install, fetch=broken).code, "fetch_failed")
        self.assertFalse((self.project / ".tools").exists())

    def test_refuses_existing_symlink_and_nested_targets(self):
        (self.project / ".tools" / "ai-comm").mkdir(parents=True)
        self.assertEqual(self.stop(self.install).code, "pin_exists")
        shutil.rmtree(self.project / ".tools")
        real = Path(self.temp.name) / "elsewhere"
        real.mkdir()
        (self.project / ".tools").symlink_to(real)
        self.assertEqual(self.stop(self.install).code, "unsafe_path")
        (self.project / ".tools").unlink()
        (self.project / ".gitignore").symlink_to(real / "gi")
        self.assertEqual(self.stop(self.install).code, "unsafe_path")
        (self.project / ".gitignore").unlink()
        nested = self.project / ".tools" / "inner"
        nested.mkdir(parents=True)
        self.assertEqual(self.stop(ip.install, nested, "1.5.1", fetch_permitted=True, use_helper=False,
                                   origin=str(self.source), run_suite=lambda r: 1).code, "nested_in_tools")
        link = Path(self.temp.name) / "link"
        link.symlink_to(self.project)
        self.assertEqual(self.stop(ip.install, link, "1.5.1", fetch_permitted=True, use_helper=False,
                                   origin=str(self.source), run_suite=lambda r: 1).code, "unsafe_ancestry")

    def test_existing_upgrade_state_refused(self):
        os.makedirs(self.project / ".ai-comm-local" / "upgrade", 0o700)
        self.assertEqual(self.stop(self.install).code, "upgrade_state_exists")

    def test_crash_before_publish_recover_removes_staging(self):
        with mock.patch.object(ip.os, "rename", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.install()
        stagings = list((self.project / ".ai-comm-local").glob("install-*"))
        self.assertEqual(len(stagings), 1)
        out = ip.recover(self.project)
        self.assertEqual(out["results"][0]["action"], "removed_unpublished")
        self.assertFalse(stagings[0].exists())
        self.assertFalse((self.project / ".tools" / "ai-comm").exists())
        self.assertEqual(self.install()["status"], "installed")

    def test_crash_before_fetch_finish_recover_removes_unpublished(self):
        def crash(repo, origin, version, helper):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.install(fetch=crash)
        out = ip.recover(self.project)
        self.assertEqual(out["results"][0]["action"], "removed_unpublished")
        self.assertFalse(list((self.project / ".ai-comm-local").glob("install-*")))
        self.assertFalse((self.project / ".tools" / "ai-comm").exists())

    def test_crash_after_publish_recover_completes(self):
        real_finish = ip.finish
        with mock.patch.object(ip, "finish", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.install()
        self.assertTrue((self.project / ".tools" / "ai-comm").is_dir())
        self.assertFalse((self.project / ".ai-comm-local" / "upgrade").exists())
        out = ip.recover(self.project)
        self.assertEqual(out["results"][0]["action"], "completed_after_publish")
        self.assertEqual(json.loads((self.project / ".ai-comm-local" / "upgrade" / "project.json").read_text())["status"],
                         "unintegrated")
        self.assertIn(".tools/", (self.project / ".gitignore").read_text())
        self.assertEqual(ip.recover(self.project)["results"], [])
        self.assertIs(ip.finish, real_finish)

    def test_install_lock(self):
        local = self.project / ".ai-comm-local"
        os.mkdir(local, 0o700)
        with ip.install_lock(local):
            self.assertEqual(self.stop(self.install).code, "install_locked")

    def test_cli_requires_permission_and_version(self):
        for argv, code in ((["--project", str(self.project), "--version", "1.5.1"], "fetch_not_permitted"),
                           (["--project", str(self.project), "--fetch-permitted"], "invalid_version"),
                           (["--project", str(self.project), "--version", "1.5.1", "--use-git-credential-helper"],
                            "credential_helper_requires_fetch_permission")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = ip.main(argv)
            self.assertEqual((rc, json.loads(out.getvalue())["code"]), (2, code))

    def test_i1_recover_never_touches_unrecognized_directories(self):
        local = self.project / ".ai-comm-local"
        os.mkdir(local, 0o700)
        foreign = local / "install-0123456789ab"
        os.mkdir(foreign, 0o700)
        (foreign / "KEEP.txt").write_text("keep")
        other = local / "install-abcdefabcdef"
        os.mkdir(other, 0o700)
        ip.uc.Journal(other).append("start", {"schema": ip.INSTALL_SCHEMA, "project": "/elsewhere", "version": "1.5.1"})
        out = ip.recover(self.project)
        self.assertEqual(sorted(r["action"] for r in out["results"]), ["skipped_unrecognized", "skipped_unrecognized"])
        self.assertEqual((foreign / "KEEP.txt").read_text(), "keep")
        self.assertTrue(other.exists())

    def test_i2_staged_copy_modified_during_tests_is_not_published(self):
        def tamper(repo):
            with open(repo / "ai_comm.py", "a") as stream:
                stream.write("\n# changed during verification\n")
            return 7
        err = self.stop(self.install, suite=tamper)
        self.assertEqual((err.code, err.details.get("what")), ("staged_identity_changed", "worktree"))
        self.assertFalse((self.project / ".tools" / "ai-comm").exists())

    def test_i2_untracked_debris_cleaned_before_identity_check(self):
        def add(repo):
            (repo / "scripts" / "extra.py").write_text("print('x')\n")
            return 7
        out = self.install(suite=add)  # untracked/ignored debris is removed, then the exact tree is required
        self.assertFalse((self.project / ".tools" / "ai-comm" / "scripts" / "extra.py").exists())
        self.assertEqual(out["status"], "installed")

    def test_i3_tools_swapped_for_symlink_during_tests(self):
        elsewhere = Path(self.temp.name) / "elsewhere"
        elsewhere.mkdir()

        def swap(repo):
            (self.project / ".tools").symlink_to(elsewhere)
            return 7
        err = self.stop(self.install, suite=swap)
        self.assertEqual(err.code, "unsafe_path")
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertFalse((self.project / ".ai-comm-local" / "upgrade").exists())

    def test_archive_trust_source_verified_and_tamper_refused(self):
        archive = Path(self.temp.name) / "archive"
        tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True,
                                 check=True).stdout.decode().split("\0")
        for name in [t for t in tracked if t]:
            dest = archive / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, dest)
        origin = re.findall(r'^RELEASE_ORIGIN = "([^"]+)"$', (archive / "ai_comm.py").read_text(), re.M)[0]
        self.assertEqual(ip.trusted_source(archive), origin)
        with open(archive / "ai_comm.py", "a") as stream:
            stream.write("\n# tampered\n")
        self.assertEqual(self.stop(ip.trusted_source, archive).code, "installer_package_unverified")

    def test_i4_global_git_hooks_never_run(self):
        hooks = Path(self.temp.name) / "hooks"
        hooks.mkdir()
        sentinel = Path(self.temp.name) / "HOOK_RAN"
        for name in ("post-checkout", "post-merge", "reference-transaction"):
            hook = hooks / name
            hook.write_text(f"#!/bin/sh\ntouch '{sentinel}'\n")
            hook.chmod(0o755)
        cfg = Path(self.temp.name) / "gitconfig"
        cfg.write_text(f"[core]\n\thooksPath = {hooks}\n")
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(cfg)}):
            out = self.install()
        self.assertEqual(out["status"], "installed")
        self.assertFalse(sentinel.exists())

    def test_index_record_disagreeing_with_tag_is_refused(self):
        bad = Path(self.temp.name) / "bad-origin"
        subprocess.run(["git", "clone", "-q", "--mirror", str(self.source), str(bad / ".git")], check=True, env=ENV)
        git(bad, "config", "--bool", "core.bare", "false")
        git(bad, "checkout", "-q", "release-index")
        index = json.loads((bad / "RELEASES.json").read_text())
        for entry in index["releases"]:
            if entry["package_version"] == "1.5.1":
                entry["manifest_sha256"] = "0" * 64
        (bad / "RELEASES.json").write_text(json.dumps(index, sort_keys=True, separators=(",", ":")) + "\n")
        git(bad, "commit", "-qam", "tampered index")
        err = self.stop(ip.install, self.project, "1.5.1", fetch_permitted=True, use_helper=False,
                        origin=str(bad), run_suite=lambda r: 7)
        self.assertEqual((err.code, err.details.get("what")), ("index_tag_mismatch", "manifest"))
        self.assertFalse((self.project / ".tools" / "ai-comm").exists())

    def test_i1_foreign_torn_journal_untouched(self):
        local = self.project / ".ai-comm-local"
        os.mkdir(local, 0o700)
        other = local / "install-abcdefabcdef"
        os.mkdir(other, 0o700)
        ip.uc.Journal(other).append("start", {"schema": ip.INSTALL_SCHEMA, "project": "/elsewhere", "version": "1.5.1"})
        with open(other / "journal.jsonl", "ab") as stream:
            stream.write(b'{"torn":')
        before = (other / "journal.jsonl").read_bytes()
        out = ip.recover(self.project)
        self.assertEqual(out["results"][0]["action"], "skipped_unrecognized")
        self.assertEqual((other / "journal.jsonl").read_bytes(), before)
        self.assertFalse(list(other.glob("torn-*.bin")))

    def test_i3_staging_repo_swapped_for_symlink_during_tests(self):
        outside = Path(self.temp.name) / "outside"

        def swap(repo):
            shutil.copytree(repo, outside, symlinks=True)
            (outside / "KEEP.txt").write_text("keep")
            shutil.rmtree(repo)
            repo.symlink_to(outside)
            return 7
        err = self.stop(self.install, suite=swap)
        self.assertEqual(err.code, "unsafe_path")
        self.assertEqual((outside / "KEEP.txt").read_text(), "keep")
        self.assertFalse(os.path.lexists(self.project / ".tools" / "ai-comm"))

    def test_i3_recover_refuses_symlinked_tools_after_publish(self):
        with mock.patch.object(ip, "finish", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.install()
        moved = Path(self.temp.name) / "moved-tools"
        shutil.move(str(self.project / ".tools"), str(moved))
        (self.project / ".tools").symlink_to(moved)
        err = self.stop(ip.recover, self.project)
        self.assertEqual(err.code, "unsafe_path")
        self.assertFalse((self.project / ".ai-comm-local" / "upgrade").exists())

    @unittest.skipIf(os.environ.get(GUARD) == "1", "inside a target verification")
    def test_real_suite_install_end_to_end(self):
        out = self.install(suite=ip.real_suite)
        self.assertGreater(out["tests_ran"], 100)
        pin = self.project / ".tools" / "ai-comm"
        status = json.loads(subprocess.run([sys.executable, "-I", str(pin / "ai_comm.py"), "release-status"],
                                           capture_output=True, env=ENV).stdout)
        self.assertEqual((status["status"], status["pinned_version"]), ("ok", "1.5.1"))


if __name__ == "__main__":
    unittest.main()
