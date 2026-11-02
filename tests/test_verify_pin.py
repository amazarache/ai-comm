"""Tests for scripts/verify_pin.py (018c section 6). Everything runs in temporary
directories against fixture repositories in the real release format; the real
home, cache and project pins are never read."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import verify_pin as vp  # noqa: E402
import test_install_project as tip  # noqa: E402

iu, uc = vp.iu, vp.uc


def binding(repo, version, label=None):
    tag = uc.tag_identity(repo, version)
    return {"version": label or version, "tag": f"v{label or version}", "tag_object": tag["tag_object"],
            "commit": tag["commit"], "manifest_sha256": tag["manifest_sha256"],
            "protocol_sha256": tag["protocol_sha256"]}


def make_pin(project, origin, version):
    pin = project / ".tools" / "ai-comm"
    pin.parent.mkdir(parents=True)
    tip.git(project, "clone", "-q", "--no-hardlinks", str(origin), str(pin))
    tip.git(pin, "checkout", "-q", "--detach", f"v{version}")
    return pin


def have_tags(*versions):
    if not (ROOT / ".git").exists():
        return False
    done = subprocess.run(["git", "-C", str(ROOT), "tag", "-l"], capture_output=True, check=False)
    tags = set(done.stdout.decode().split())
    return all(f"v{v}" in tags for v in versions)


class VerifyPinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory(prefix="vp-src-")
        cls.origin, cls.releases = tip.build_source(Path(cls.shared.name).resolve())
        cls.bindings = {"1.5.1": binding(cls.origin, "1.5.1"), "1.5.0": binding(cls.origin, "1.5.0")}

    @classmethod
    def tearDownClass(cls):
        cls.shared.cleanup()

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="vp-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        os.chmod(self.base, 0o700)
        self.project = self.base / "project"
        self.project.mkdir()

    def pin(self, version="1.5.1"):
        return make_pin(self.project, self.origin, version)

    def check(self, bindings=None):
        return vp.verify(self.project, self.bindings if bindings is None else bindings)

    def assert_modified(self, what, path=None):
        report = self.check()
        self.assertEqual((report["status"], report.get("what")), ("modified", what), report)
        if path:
            self.assertEqual(report.get("path"), path)

    # -- results

    def test_verified_pin_reports_version_commit_and_class(self):
        self.pin()
        report = self.check()
        self.assertEqual((report["status"], report["version"], report["class"]),
                         ("verified", "1.5.1", "controller-check-workaround"))
        self.assertEqual(report["commit"], self.releases["1.5.1"]["commit"])
        self.assertEqual(report["tag_object"], self.bindings["1.5.1"]["tag_object"])

    def test_absent_pin(self):
        self.assertEqual(self.check()["status"], "absent")

    def test_missing_binding_is_unknown_never_verified(self):
        self.pin()
        report = self.check({"1.5.0": self.bindings["1.5.0"]})
        self.assertEqual((report["status"], report["reason"], report["version"]),
                         ("unknown", "no_trusted_binding", None))

    def test_self_consistent_unrecognized_release_is_unknown(self):
        pin = self.pin()
        (pin / "ai_comm.py").write_text("print('mine')\n")
        tip.git(pin, "add", "-A")
        tip.git(pin, "commit", "-q", "-m", "local")
        self.assertEqual(self.check()["reason"], "no_trusted_binding")

    def test_each_compatibility_class(self):
        cases = {"1.6.0": "controller", "1.5.2": "controller", "1.5.1": "controller-check-workaround",
                 "1.5.0": "controller-check-workaround", "1.4.2": "legacy-manual", "1.4.0": "legacy-manual",
                 "1.3.0": "unsupported", "0.2.0": "unsupported"}
        for version, cls in cases.items():
            with self.subTest(version=version):
                self.assertEqual(vp.compat_class(version), cls)
        self.pin()
        for label in ("1.6.0", "1.4.2", "1.3.0"):
            with self.subTest(label=label):
                relabelled = {label: binding(self.origin, "1.5.1", label)}
                report = self.check(relabelled)
                self.assertEqual((report["status"], report["class"]), ("verified", vp.compat_class(label)))

    # -- nothing from the pin runs

    def test_modified_pin_code_never_runs(self):
        pin = self.pin()
        sentinel = self.base / "sentinel"
        payload = f"open({str(sentinel)!r}, 'w').write('ran')\n"
        for name in ("scripts/check_release_manifest.py", "ai_comm.py", "upgrade_controller.py"):
            (pin / name).write_text(payload)
        report = self.check()
        self.assertEqual(report["status"], "modified")
        self.assertFalse(sentinel.exists())

    def test_repository_fsmonitor_helper_never_runs(self):
        pin = self.pin()
        sentinel = self.base / "fsmonitor-ran"
        helper = self.base / "helper.sh"
        helper.write_text(f"#!/bin/sh\necho ran > '{sentinel}'\n")
        helper.chmod(0o700)
        tip.git(pin, "config", "core.fsmonitor", str(helper))
        # Control: an ordinary work-tree command does run the helper here.
        subprocess.run(["git", "-C", str(pin), "status", "--porcelain"], capture_output=True, check=False,
                       env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")})
        control = sentinel.exists()
        if control:
            sentinel.unlink()
        self.assertEqual(self.check()["status"], "verified")
        self.assertFalse(sentinel.exists())
        if not control:
            self.skipTest("this Git did not run the fsmonitor helper even for `status`; inertness shown only")

    # -- changes Git can hide

    def test_assume_unchanged_and_skip_worktree_cannot_hide_changes(self):
        for flag in ("--assume-unchanged", "--skip-worktree"):
            with self.subTest(flag=flag):
                pin = self.pin()
                tip.git(pin, "update-index", flag, "ai_comm.py")
                (pin / "ai_comm.py").write_text("print('changed')\n")
                self.assertEqual(subprocess.run(["git", "-C", str(pin), "status", "--porcelain",
                                                 "--untracked-files=no"], capture_output=True,
                                                check=False).stdout, b"", "Git itself reports clean")
                self.assert_modified("content", "ai_comm.py")
                shutil.rmtree(self.project / ".tools")

    def test_altered_working_manifest(self):
        pin = self.pin()
        manifest = json.loads((pin / "RELEASE-MANIFEST.json").read_text())
        manifest["files"] = manifest["files"][1:]
        (pin / "RELEASE-MANIFEST.json").write_text(json.dumps(manifest))
        self.assert_modified("working_manifest", "RELEASE-MANIFEST.json")

    def test_mode_change_and_missing_file(self):
        pin = self.pin()
        os.chmod(pin / "PROTOCOL.md", 0o755)
        self.assert_modified("mode", "PROTOCOL.md")
        os.chmod(pin / "PROTOCOL.md", 0o644)
        (pin / "PROTOCOL.md").unlink()
        self.assert_modified("missing", "PROTOCOL.md")

    def test_symlinked_or_escaping_paths(self):
        pin = self.pin()
        shutil.move(str(pin / "scripts"), str(self.base / "scripts-elsewhere"))
        os.symlink(self.base / "scripts-elsewhere", pin / "scripts")
        self.assertEqual(self.check()["what"], "not_regular")
        os.unlink(pin / "scripts")
        shutil.move(str(self.base / "scripts-elsewhere"), str(pin / "scripts"))
        (pin / "ai_comm.py").unlink()
        os.symlink(self.base / "elsewhere.py", pin / "ai_comm.py")
        (self.base / "elsewhere.py").write_text("x = 1\n")
        self.assert_modified("not_regular", "ai_comm.py")
        fd = os.open(pin, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        for bad in ("../outside", "/etc/passwd", "scripts//x", "./ai_comm.py", ""):
            with self.subTest(path=bad):
                with self.assertRaises(vp.Mismatch) as ctx:
                    vp.read_entry(fd, bad)
                self.assertEqual(ctx.exception.what, "unsafe_path")

    def test_tag_object_that_differs_from_the_binding(self):
        pin = self.pin()
        tip.git(pin, "tag", "-f", "-a", "v1.5.1", "-m", "another tag object", "HEAD")
        self.assert_modified("tag")

    def test_extra_tracked_file_in_a_bound_release_tree(self):
        # A release whose committed tree has a file its manifest does not list.
        origin = self.base / "odd"
        tip.git(self.base, "clone", "-q", "--no-hardlinks", str(self.origin), str(origin))
        tip.git(origin, "checkout", "-q", "--detach", "v1.5.1")
        (origin / "extra.txt").write_text("not in the manifest\n")
        tip.git(origin, "add", "extra.txt")
        tip.git(origin, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "x")
        manifest = tip.sha((origin / "RELEASE-MANIFEST.json").read_bytes())
        tip.git(origin, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "tag", "-a", "v1.5.9",
                "-m", f"ai-comm 1.5.9 (protocol 3.0); manifest sha256 {manifest}")
        make_pin(self.project, origin, "1.5.9")
        report = self.check({"1.5.9": binding(origin, "1.5.9")})
        self.assertEqual((report["status"], report["what"]), ("modified", "file_set"))

    def test_unsupported_layouts_are_unknown(self):
        pin = self.pin()
        tip.git(pin, "config", "extensions.partialclone", "origin")
        self.assertEqual(self.check()["reason"], "promisor_remote")
        tip.git(pin, "config", "--unset", "extensions.partialclone")
        (pin / ".git" / "objects" / "info" / "alternates").write_text(str(self.origin / ".git" / "objects") + "\n")
        self.assertEqual(self.check()["reason"], "alternate_object_store")
        (pin / ".git" / "objects" / "info" / "alternates").unlink()
        shutil.rmtree(pin / ".git")
        (pin / ".git").write_text("gitdir: /elsewhere\n")
        self.assertEqual(self.check()["reason"], "not_a_git_checkout")

    # -- review 940: no transport, whatever the Git version

    def promisor_pin(self):
        """A trusted pin whose configuration names a promisor remote reached through a
        helper that would write a sentinel, the upstream Git 2.43.0 reproduction."""
        pin = self.pin()
        sentinel = self.base / "helper-ran"
        helper = self.base / "helper.sh"
        helper.write_text(f"#!/bin/sh\necho ran > '{sentinel}'\nexit 1\n")
        helper.chmod(0o700)
        tip.git(pin, "config", "remote.origin.url", f"ext::{helper}")
        tip.git(pin, "config", "protocol.ext.allow", "always")
        return pin, sentinel

    def test_every_promisor_layout_fails_closed_before_object_reads(self):
        settings = (("remote.origin.promisor", "true"), ("remote.origin.partialclonefilter", "blob:none"),
                    ("extensions.partialclone", "origin"), ("core.partialclonefilter", "blob:none"))
        for key, value in settings:
            with self.subTest(key=key):
                pin, sentinel = self.promisor_pin()
                tip.git(pin, "config", key, value)
                with mock.patch.object(vp.ri, "git_object", side_effect=AssertionError("object read")):
                    report = self.check()
                self.assertEqual((report["status"], report["reason"]), ("unknown", "promisor_remote"))
                self.assertFalse(sentinel.exists())
                shutil.rmtree(self.project / ".tools")
        pin, sentinel = self.promisor_pin()
        included = self.base / "included.config"
        included.write_text("[remote \"origin\"]\n\tpromisor = true\n")
        tip.git(pin, "config", "include.path", str(included))
        self.assertEqual(self.check()["reason"], "promisor_remote", "settings from included files count")
        self.assertFalse(sentinel.exists())

    def test_review_053_reproduction_through_verify_itself(self):
        """Review 053 B1 exactly: promisor remote without extensions.partialclone, ext helper,
        the committed manifest's loose object removed, then verify(). Also with the
        layout check bypassed, so only the transport ban stands between the missing
        object and the helper."""
        pin, sentinel = self.promisor_pin()
        tip.git(pin, "config", "remote.origin.promisor", "true")
        blob = tip.git(pin, "rev-parse", "HEAD:RELEASE-MANIFEST.json").strip()
        loose = pin / ".git" / "objects" / blob[:2] / blob[2:]
        if not loose.exists():
            self.skipTest("the fixture clone packed its objects")
        loose.unlink()
        report = self.check()
        self.assertEqual((report["status"], report["reason"]), ("unknown", "promisor_remote"))
        self.assertFalse(sentinel.exists())
        with mock.patch.object(vp, "promisor_layout", return_value=None):
            env = vp.ri.inert_git_env()
            env.pop("GIT_NO_LAZY_FETCH")  # as a Git that does not implement the flag
            with mock.patch.object(vp.ri, "_inert_git_env", return_value=env), \
                    mock.patch.object(vp.ri, "inert_git_env", return_value=env):
                report = self.check()
        self.assertEqual((report["status"], report["what"]), ("modified", "object"))
        self.assertFalse(sentinel.exists(), "the missing object must not start a transport")

    def test_no_transport_even_where_the_lazy_fetch_flag_is_ignored(self):
        """Upstream Git 2.43.0 ignores GIT_NO_LAZY_FETCH. Remove the flag, as such a Git
        effectively does, and ask for a missing object in a promisor layout: the
        protocol ban alone must keep the helper from running. A control without the
        ban shows that this Git really would run it."""
        pin, sentinel = self.promisor_pin()
        tip.git(pin, "config", "remote.origin.promisor", "true")
        missing = "1" * 40

        def cat_file(env):
            return subprocess.run(["git", "--git-dir", str(pin / ".git"), "--no-replace-objects", "cat-file",
                                   "blob", missing], env=env, capture_output=True, timeout=60, check=False)
        upstream_like = vp.ri.inert_git_env()
        upstream_like.pop("GIT_NO_LAZY_FETCH")
        control = dict(upstream_like)
        control.pop("GIT_ALLOW_PROTOCOL")
        control["GIT_CONFIG_COUNT"] = "2"  # drop protocol.allow=never
        cat_file(control)
        control_ran = sentinel.exists()
        if control_ran:
            sentinel.unlink()
        done = cat_file(upstream_like)
        self.assertNotEqual(done.returncode, 0)
        self.assertFalse(sentinel.exists(), "no transport or helper may start")
        if not control_ran:
            self.skipTest("this Git did not attempt a lazy fetch even without the guards; ban shown inert only")

    def test_verifier_inside_the_checked_pin_stops(self):
        pin = self.pin()
        with mock.patch.object(vp, "RELEASE", pin.resolve()):
            with self.assertRaises(vp.Stop) as ctx:
                self.check()
        self.assertEqual(ctx.exception.code, "verifier_inside_pin")

    # -- legacy pins and offers

    def test_legacy_pin_reports_its_own_index_as_untrusted(self):
        pin = self.pin()
        legacy = vp.legacy_status(pin, "1.4.2")
        self.assertEqual((legacy["pin_index"], legacy["pin_index_newer"]), ("read", ["1.5.0", "1.5.1"]))
        tip.git(pin, "update-ref", "-d", "refs/remotes/origin/release-index")
        self.assertEqual(vp.legacy_status(pin, "1.4.2"), {"pin_index": "absent"})

    def test_newer_verified_uses_only_agreeing_bindings(self):
        cache = iu.Cache(self.base / "cache").open(create=True)
        records = [{k: r[k] for k in ("package_version", "commit", "tag", "manifest_sha256", "protocol_sha256")}
                   for r in self.releases.values()]
        cache.write_index(records, 1)
        cache.write_bindings({"v": 1, "bindings": self.bindings})
        self.assertEqual(vp.newer_verified(cache, "1.5.0"), ["1.5.1"])
        bad = dict(self.bindings["1.5.1"], manifest_sha256="e" * 64)
        cache.write_bindings({"v": 1, "bindings": {"1.5.1": bad}})
        self.assertEqual(vp.newer_verified(cache, "1.5.0"), [])

    # -- the command

    def run_cli(self, cache, *args, script=ROOT / "scripts" / "verify_pin.py"):
        done = subprocess.run([sys.executable, "-I", str(script), "--project", str(self.project), *args]
                              + ([] if cache is None else ["--cache", str(cache)]),
                              capture_output=True, timeout=120, check=False)
        return done.returncode, json.loads(done.stdout)

    def test_cli_exit_codes(self):
        cache = iu.Cache(self.base / "cache").open(create=True)
        cache.write_bindings({"v": 1, "bindings": self.bindings})
        self.assertEqual(self.run_cli(cache.root)[0:1], (1,))
        self.pin()
        code, report = self.run_cli(cache.root)
        self.assertEqual((code, report["status"]), (0, "verified"))
        (self.project / ".tools" / "ai-comm" / "ai_comm.py").write_text("x\n")
        self.assertEqual(self.run_cli(cache.root)[0], 1)
        self.assertEqual(self.run_cli(self.base / "no-cache")[1]["code"], "cache_missing")


@unittest.skipUnless(have_tags("1.4.2", "1.5.1", "1.5.3"), "real release tags need the package's Git history")
class RealReleaseFixtureTests(unittest.TestCase):
    """The package's own published tags, checked out as pins."""

    def test_real_releases_verify_in_their_classes(self):
        expected = {"1.4.2": "legacy-manual", "1.5.1": "controller-check-workaround", "1.5.3": "controller"}
        with tempfile.TemporaryDirectory(prefix="vp-real-") as temp:
            base = Path(temp).resolve()
            os.chmod(base, 0o700)
            for version, cls in expected.items():
                with self.subTest(version=version):
                    project = base / version
                    project.mkdir()
                    make_pin(project, ROOT, version)
                    report = vp.verify(project, {version: binding(ROOT, version)})
                    self.assertEqual((report["status"], report["class"]), ("verified", cls), report)
                    (project / ".tools" / "ai-comm" / "PROTOCOL.md").write_text("changed\n")
                    self.assertEqual(vp.verify(project, {version: binding(ROOT, version)})["what"], "content")


if __name__ == "__main__":
    unittest.main()
