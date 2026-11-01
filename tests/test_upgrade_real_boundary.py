"""End-to-end controller run against the real CLI, scripts and suite (no network, no fakes).

Builds a throwaway release repository from the real v1.4.2 tag plus the current
tree as v1.5.0, a real channel, and runs check, record-answer, prepare (real
check_tag, check_release_manifest and full suite), apply and rollback. Skipped
when v1.4.2 is not available locally (shallow CI checkout) and inside its own
target verification, so it never recurses.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
GUARD = "AI_COMM_UPGRADE_REAL_BOUNDARY_INNER"
ENV = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid", "HOME": "/nonexistent", GUARD: "1"}


def package_files(root):
    """Files of a release package, from its manifest (works without Git metadata)."""
    manifest = json.loads((root / "RELEASE-MANIFEST.json").read_text())
    return [entry["path"] for entry in manifest["files"]] + ["RELEASE-MANIFEST.json"]


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=ENV).stdout.decode()


def have_base():
    try:
        return subprocess.run(["git", "-C", str(ROOT), "cat-file", "-t", "refs/tags/v1.4.2"], capture_output=True,
                              env=ENV).stdout.strip() == b"tag"
    except OSError:
        return False


@unittest.skipIf(os.environ.get(GUARD) == "1", "inside the controller's own target verification")
@unittest.skipUnless(have_base(), "real v1.4.2 tag is not available locally")
class RealBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="uc-real-")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        os.chmod(base, 0o700)
        pkg = base / "pkg"
        subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(ROOT), str(pkg)], check=True, env=ENV)
        git(pkg, "checkout", "-q", "--detach", "v1.4.2^{commit}")
        self.from_commit = git(pkg, "rev-parse", "HEAD").strip()
        for name in package_files(ROOT):
            dest = pkg / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, dest)
        git(pkg, "add", "-A")
        git(pkg, "commit", "-q", "-m", "candidate 1.5.0")
        manifest = hashlib.sha256((pkg / "RELEASE-MANIFEST.json").read_bytes()).hexdigest()
        git(pkg, "tag", "-a", "v1.5.0", "-m", f"ai-comm 1.5.0 (protocol 3.0); manifest sha256 {manifest}")
        self.to_commit = git(pkg, "rev-parse", "HEAD").strip()
        protocol = hashlib.sha256((pkg / "PROTOCOL.md").read_bytes()).hexdigest()
        old_manifest = hashlib.sha256(git(pkg, "show", "v1.4.2:RELEASE-MANIFEST.json").encode()).hexdigest()
        entry = {"advisory": "optional", "reason": "No advisory recorded for this release.", "summary": "s",
                 "protocol_sha256": protocol}
        index = {"format": "ai-comm-releases/1", "releases": [
            dict(entry, package_version="1.4.2", commit=self.from_commit, tag="v1.4.2", manifest_sha256=old_manifest),
            dict(entry, package_version="1.5.0", commit=self.to_commit, tag="v1.5.0", manifest_sha256=manifest)]}
        git(pkg, "switch", "-q", "--orphan", "release-index")
        for child in pkg.iterdir():
            if child.name != ".git":
                shutil.rmtree(child) if child.is_dir() else child.unlink()
        (pkg / "RELEASES.json").write_text(json.dumps(index, sort_keys=True, separators=(",", ":")) + "\n")
        git(pkg, "add", "RELEASES.json")
        git(pkg, "commit", "-q", "-m", "index")
        git(pkg, "checkout", "-q", "--detach", self.from_commit)
        self.project = base / "project"
        (self.project / ".tools").mkdir(parents=True)
        self.pin = self.project / ".tools" / "ai-comm"
        subprocess.run(["git", "clone", "-q", str(pkg), str(self.pin)], check=True, env=ENV)
        git(self.pin, "checkout", "-q", "--detach", self.from_commit)
        git(self.pin, "fetch", "-q", str(pkg), "refs/heads/release-index:refs/remotes/origin/release-index")
        os.mkdir(self.project / ".ai-comm-local", 0o700)
        cli = [sys.executable, "-I", str(self.pin / "ai_comm.py"), "--channel", str(self.project / ".ai-comm")]
        subprocess.run(cli + ["init", "--task", "boundary test", "--reviewers", "claude", "codex"], check=True,
                       capture_output=True, env=ENV)
        subprocess.run(cli + ["join", "--as", "claude", "--platform", "claude", "--session", "s-claude"],
                       check=True, capture_output=True, env=ENV)
        sys.path.insert(0, str(ROOT))
        import upgrade_controller as uc
        self.uc = uc
        uc.Project(self.project).runtime()
        channel = git(self.project / ".ai-comm", "rev-list", "--max-parents=0", "HEAD").strip()
        uc.write_private_atomic(self.project / ".ai-comm-local" / "upgrade" / "project.json", json.dumps(
            {"v": 1, "schema": uc.PROJECT_SCHEMA, "status": "declared",
             "project_id": {"path": str(self.project), "channel": channel}, "integrations": []}).encode())

    def ctl(self, controller, *args):
        result = subprocess.run([sys.executable, "-I", str(controller), "--project", str(self.project), *args],
                                capture_output=True, env=ENV, timeout=1800)
        return result.returncode, json.loads(result.stdout)

    def test_real_check_prepare_apply_rollback(self):
        here = ROOT / "upgrade_controller.py"
        code, out = self.ctl(here, "check")
        self.assertEqual(code, 0, out)
        self.assertEqual((out["release_status"], out["prompt"]["target"]), ("ok", "1.5.0"), out)
        self.assertTrue(out["prompt"]["should_ask"])
        code, ans = self.ctl(here, "record-answer", "--answer", "yes", "--target", "1.5.0",
                             "--target-commit", out["prompt"]["target_commit"], "--source", out["prompt"]["source"],
                             "--evidence", "direct owner yes (test)")
        self.assertEqual(code, 0, ans)
        code, prep = self.ctl(here, "prepare", "--target", "1.5.0", "--consent", ans["consent"],
                              "--as", "claude", "--session", "s-claude")
        self.assertEqual(code, 0, prep)
        self.assertTrue(prep["verification"]["checks"]["tests"])
        self.assertGreater(prep["verification"]["tests_ran"], 100)
        frozen = Path(prep["invocation"][2])
        code, res = self.ctl(frozen, "apply", "--as", "claude", "--session", "s-claude")
        self.assertEqual(res["status"], "integrated_pending_liveness", res)
        self.assertEqual(git(self.pin, "rev-parse", "HEAD").strip(), self.to_commit)
        status = json.loads(subprocess.run([sys.executable, "-I", str(self.pin / "ai_comm.py"), "release-status"],
                                           capture_output=True, env=ENV).stdout)
        self.assertEqual((status["status"], status["pinned_version"]), ("ok", "1.5.0"))
        code, res = self.ctl(frozen, "rollback", "--as", "claude", "--session", "s-claude")
        self.assertEqual(res["status"], "rolled_back", res)
        self.assertEqual(git(self.pin, "rev-parse", "HEAD").strip(), self.from_commit)
        self.assertFalse((self.pin / "upgrade_controller.py").exists())


if __name__ == "__main__":
    unittest.main()
