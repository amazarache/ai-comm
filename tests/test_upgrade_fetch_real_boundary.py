"""1.11.1 Part 2 against the genuine v1.10.0 release (plan r2, REVIEW 102 point 4).

The pin is the real v1.10.0 tree at its annotated tag, laid out as the 1.10.0 project
installer publishes it: no other refs and no `remote.origin.url`. Its released bytes and
its manifest-bound origin are unchanged. The target is the genuine v1.11.0 tag. The
disclosed transport fixture: a local bare repository holds the two real tags and a release
index built from them, and a `git` wrapper first on PATH rewrites the manifest-bound origin
URL to it, in fetch and ls-remote only. Then:

1. the pin's own 1.10.0 controller records the owner's answer and its own `fetch` stops
   with `origin_missing` (the defect);
2. this tree's cache-side `upgrade-fetch` publishes the tag and index refs;
3. the pin's own 1.10.0 `prepare` runs for real, including its full verification of the
   target (check_tag, manifest check and the target's full suite).

Skipped when either tag is not available locally, and inside an inner verification suite.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
GUARD = "AI_COMM_UPGRADE_REAL_BOUNDARY_INNER"
OLD, NEW = "1.10.0", "1.11.0"
ENV = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid", "HOME": "/nonexistent"}


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=ENV).stdout.decode()


def have_tags():
    try:
        return all(subprocess.run(["git", "-C", str(ROOT), "cat-file", "-t", f"refs/tags/v{v}"], capture_output=True,
                                  env=ENV).stdout.strip() == b"tag" for v in (OLD, NEW))
    except OSError:
        return False


@unittest.skipIf(os.environ.get(GUARD) == "1", "inside an inner verification suite")
@unittest.skipUnless(have_tags(), f"real v{OLD} and v{NEW} tags are not both available locally")
class GenuineOldPinUpgradeFetchTests(unittest.TestCase):
    def setUp(self):
        import test_upgrade_fetch as tuf
        self.tuf, self.uf, self.uc, self.iu = tuf, tuf.uf, tuf.uc, tuf.iu
        temp = tempfile.TemporaryDirectory(prefix="ufr-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        os.chmod(self.base, 0o700)
        # The origin both genuine releases name, read from their own bytes.
        self.url = re.search(r'^RELEASE_ORIGIN = "([^"]+)"$', git(ROOT, "show", f"v{OLD}:ai_comm.py"), re.M).group(1)
        self.assertEqual(self.url, re.search(r'^RELEASE_ORIGIN = "([^"]+)"$',
                                             git(ROOT, "show", f"v{NEW}:ai_comm.py"), re.M).group(1))
        self.bare = self.base / "remote.git"
        self.make_origin()
        redirect = tuf.git_redirect(self.base / "bin", self.url, self.bare)
        redirect.start()
        self.addCleanup(redirect.stop)
        self.project = self.base / "project"
        (self.project / ".tools").mkdir(parents=True)
        self.pin = self.project / ".tools" / "ai-comm"
        subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(ROOT), str(self.pin)], check=True, env=ENV)
        git(self.pin, "checkout", "-q", "--detach", f"v{OLD}^{{commit}}")
        for ref in git(self.pin, "for-each-ref", "--format=%(refname)").split():
            if ref != f"refs/tags/v{OLD}":
                git(self.pin, "update-ref", "-d", ref)
        git(self.pin, "remote", "remove", "origin")
        self.cache_dir = self.base / "data" / "ai-comm"
        cache = self.iu.Cache(self.cache_dir).open(create=True)
        bindings = {}
        for version in (OLD, NEW):
            tag = self.uc.tag_identity(ROOT, version)
            record = {k: tag[k] for k in ("commit", "manifest_sha256", "protocol_sha256")}
            bindings[version] = self.iu.binding_from_tag(tag, record)
        cache.write_bindings({"v": 1, "bindings": bindings})
        state = cache.state()
        state["origin"] = self.url
        cache.write_state(state)
        self.channel = self.project / ".ai-comm"
        self.cli("init", "--task", "real upgrade-fetch", "--reviewers", "agent-a", "agent-b")
        self.cli("join", "--as", "agent-a", "--platform", "assistant", "--session", "s-a")
        root = self.uc.Project(self.project).channel_id()
        self.uc.Project(self.project).runtime()
        self.uc.write_private_atomic(self.project / ".ai-comm-local" / "upgrade" / "project.json", self.uc.canonical(
            {"v": 1, "schema": self.uc.PROJECT_SCHEMA, "status": "declared",
             "project_id": {"path": str(self.project), "channel": root}, "integrations": []}))

    def make_origin(self):
        """A bare repository with the two genuine tags and an index built from them."""
        subprocess.run(["git", "init", "-q", "--bare", str(self.bare)], check=True, env=ENV)
        for version in (OLD, NEW):
            subprocess.run(["git", "-C", str(ROOT), "push", "-q", str(self.bare),
                            f"refs/tags/v{version}:refs/tags/v{version}"], check=True, capture_output=True, env=ENV)
        work = self.base / "index-work"
        git(self.base, "init", "-q", "--initial-branch=release-index", str(work))
        releases = []
        for version in (OLD, NEW):
            tag = self.uc.tag_identity(ROOT, version)
            releases.append({"advisory": "optional", "reason": "No advisory recorded for this release.",
                             "summary": "s", "package_version": version, "tag": f"v{version}",
                             "commit": tag["commit"], "manifest_sha256": tag["manifest_sha256"],
                             "protocol_sha256": tag["protocol_sha256"]})
        (work / "RELEASES.json").write_text(json.dumps({"format": "ai-comm-releases/1", "releases": releases},
                                                       sort_keys=True, separators=(",", ":")) + "\n")
        git(work, "add", "RELEASES.json")
        git(work, "commit", "-q", "-m", "index")
        git(work, "push", "-q", str(self.bare), "release-index:refs/heads/release-index")

    def cli(self, *args):
        done = subprocess.run([sys.executable, "-I", str(self.pin / "ai_comm.py"), "--channel", str(self.channel),
                               *args], capture_output=True, env=self.uc.clean_env(), timeout=120, check=False)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return json.loads(done.stdout)

    def controller(self, *args, timeout=300):
        done = subprocess.run([sys.executable, "-I", str(self.pin / "ai_comm.py"), "upgrade", "--project",
                               str(self.project), *args], capture_output=True,
                              env={**self.uc.clean_env(), GUARD: "1"}, timeout=timeout, check=False)
        return done.returncode, json.loads(done.stdout)

    def test_genuine_old_pin_cache_fetch_then_its_own_real_prepare(self):
        pin_bytes = git(self.pin, "rev-parse", "HEAD")
        config = (self.pin / ".git" / "config").read_bytes()
        target = self.uc.tag_identity(ROOT, NEW)
        code, answer = self.controller("record-answer", "--answer", "yes", "--target", NEW, "--target-commit",
                                       target["commit"], "--source", self.url, "--evidence", "owner yes in a test")
        self.assertEqual(code, 0, answer)
        code, out = self.controller("fetch", "--target", NEW, "--consent", answer["consent"])
        self.assertEqual((code, out.get("helper_error")), (2, "origin_missing"))
        out = self.uf.upgrade_fetch(self.project, NEW, answer["consent"], cache_dir=self.cache_dir)
        self.assertEqual((out["status"], out["tag_object"]), ("fetched", target["tag_object"]))
        self.assertEqual(git(self.pin, "rev-parse", "HEAD"), pin_bytes)
        self.assertEqual((self.pin / ".git" / "config").read_bytes(), config)
        code, prep = self.controller("prepare", "--target", NEW, "--consent", answer["consent"], "--as", "agent-a",
                                     "--session", "s-a", timeout=3600)
        self.assertEqual((code, prep.get("status")), (0, "prepared"), prep)
        self.assertGreater(prep["verification"]["tests_ran"], 0)


if __name__ == "__main__":
    unittest.main()
