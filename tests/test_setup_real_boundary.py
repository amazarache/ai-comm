"""Guided setup over the genuine v1.10.0 release (T13, plan r2 section 3a; no network, no fakes of the pin).

The reused pin is the real v1.10.0 tree at its annotated tag, bound in a temporary cache
from that tag. Setup (this tree's cache code) plans and applies the reuse profile with a
non-default channel; then the 1.10.0 CLI itself joins B and runs the PING/PONG/ACK
handshake, `state` and `validate`, and this tree's wake installer plans an exit listener
for that channel. The pin's bytes and the project's upgrade bookkeeping must be exactly
as found. Skipped when the v1.10.0 tag is not available locally, and inside an inner
verification suite (the propagated guard), so nested suites stay bounded.
"""
import contextlib
import io
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
GUARD = "AI_COMM_UPGRADE_REAL_BOUNDARY_INNER"
OLD = "1.10.0"
ENV = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid", "HOME": "/nonexistent"}


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=ENV).stdout.decode()


def have_old_tag():
    try:
        return subprocess.run(["git", "-C", str(ROOT), "cat-file", "-t", f"refs/tags/v{OLD}"], capture_output=True,
                              env=ENV).stdout.strip() == b"tag"
    except OSError:
        return False


def tree(root):
    return {p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() and not p.is_symlink() else None)
            for p in sorted(root.rglob("*"))}


@unittest.skipIf(os.environ.get(GUARD) == "1", "inside an inner verification suite")
@unittest.skipUnless(have_old_tag(), f"real v{OLD} tag is not available locally")
class RealReuseSetupTests(unittest.TestCase):
    def setUp(self):
        import setup_project as sp
        self.sp, self.uc, self.iu = sp, sp.uc, sp.iu
        temp = tempfile.TemporaryDirectory(prefix="sr-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        os.chmod(self.base, 0o700)
        self.project = self.base / "project"
        (self.project / ".tools").mkdir(parents=True)
        self.pin = self.project / ".tools" / "ai-comm"
        subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(ROOT), str(self.pin)], check=True, env=ENV)
        git(self.pin, "checkout", "-q", "--detach", f"v{OLD}^{{commit}}")
        for ref in git(self.pin, "for-each-ref", "--format=%(refname)").split():
            if ref != f"refs/tags/v{OLD}":
                git(self.pin, "update-ref", "-d", ref)
        git(self.pin, "remote", "remove", "origin")
        data = self.base / "data"
        self.cache_dir = data / "ai-comm"
        cache = self.iu.Cache(self.cache_dir).open(create=True)
        tag = self.uc.tag_identity(self.pin, OLD)
        record = {k: tag[k] for k in ("commit", "manifest_sha256", "protocol_sha256")}
        cache.write_bindings({"v": 1, "bindings": {OLD: self.iu.binding_from_tag(tag, record)}})
        env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(data)})
        env.start()
        self.addCleanup(env.stop)
        self.channel = self.project / "collab" / "live-channel"
        (self.project / "collab").mkdir()

    def cli(self, *args):
        done = subprocess.run([sys.executable, "-I", str(self.pin / "ai_comm.py"), "--channel", str(self.channel),
                               *args], capture_output=True, env=self.uc.clean_env(), timeout=120, check=False)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return json.loads(done.stdout)

    def send(self, handle, session, key, draft):
        path = self.base / f"{key}.json"
        path.write_text(json.dumps(draft))
        return self.cli("send", "--as", handle, "--session", session, "--key", key, "--draft", str(path))["message"]

    def setup(self, declaration=None):
        if declaration is not None:
            upgrade = self.uc.private_dir(self.uc.private_dir(self.project / ".ai-comm-local") / "upgrade")
            self.uc.write_private_atomic(upgrade / "project.json", declaration)
        before_pin = tree(self.pin)
        before_local = tree(self.project / ".ai-comm-local") if (self.project / ".ai-comm-local").exists() else {}
        out = self.sp.plan(self.project, task="real reuse check", channel="collab/live-channel",
                           a_label="assistant", a_session="a-real-1", cache_dir=self.cache_dir)
        env = out["plan"]
        self.assertEqual((env["profile"], env["target"]["version"]), ("reuse", OLD))
        path = self.base / "plan.json"
        path.write_bytes(self.uc.canonical(env))
        self.sp.apply(path, out["sha256"], session="a-real-1", cache_dir=self.cache_dir)
        self.assertEqual(tree(self.pin), before_pin, "the reused pin is byte-identical")
        after_local = tree(self.project / ".ai-comm-local")
        self.assertEqual({k: v for k, v in after_local.items() if not k.startswith("setup")},
                         {k: v for k, v in before_local.items() if not k.startswith("setup")},
                         "upgrade bookkeeping exactly as found")
        return env

    def test_reuse_over_the_genuine_old_release_supports_todays_commands(self):
        self.setup()
        self.assertFalse((self.project / ".ai-comm-local" / "upgrade").exists())
        self.cli("join", "--as", "agent-b", "--platform", "assistant", "--session", "b-real-1")
        ping = self.send("agent-a", "a-real-1", "ping", {"act": "PING", "to": ["agent-b"], "topic": "handshake",
                                                          "summary": "ping"})
        pong = self.send("agent-b", "b-real-1", "pong", {"act": "PONG", "to": ["agent-a"], "topic": "handshake",
                                                          "summary": "pong", "reply_to": ping["id"]})
        ack = self.send("agent-a", "a-real-1", "ack", {"act": "ACK", "to": ["agent-b"], "topic": "handshake",
                                                        "summary": "ack", "reply_to": pong["id"]})
        self.assertEqual((pong["echo"], ack["echo"]), (ping["nonce"], pong["nonce"]))
        state = self.cli("state", "--as", "agent-a", "--session", "a-real-1")
        self.assertEqual(sorted(p["handle"] for p in state["participants"]), ["agent-a", "agent-b"])
        self.assertEqual({p["tool_version"] for p in state["participants"]}, {OLD})
        self.assertTrue(self.cli("validate")["valid"])
        wake = self.sp._load("ai_comm_wake_installer_rb", ROOT / "scripts" / "wake.py")
        out = io.StringIO()
        code = wake.main(["plan", "--project", str(self.project), "--handle", "agent-a", "--kind", "exit",
                          "--session=a-real-1", "--generation", "1", "--channel", str(self.channel)],
                         host=wake.Host({"HOME": str(self.base / "home")}), out=out)
        result = json.loads(out.getvalue())
        self.assertEqual(code, 0, result)
        self.assertNotIn("code", result)

    def test_reuse_preserves_an_unintegrated_declaration_as_found(self):
        unintegrated = self.sp.ri.unintegrated_declaration(self.project)
        self.setup(declaration=unintegrated)
        live = self.project / ".ai-comm-local" / "upgrade" / "project.json"
        self.assertEqual(live.read_bytes(), unintegrated)
        self.assertEqual(self.sp.status(self.project, cache_dir=self.cache_dir)["declaration"], "unintegrated")

    def test_reuse_preserves_a_declared_declaration_as_found(self):
        declared = self.uc.canonical({"v": 1, "schema": self.uc.PROJECT_SCHEMA, "status": "declared",
                                      "project_id": {"path": str(self.project), "channel": "0" * 40},
                                      "guidance": [], "integrations": []})
        self.setup(declaration=declared)
        self.assertEqual((self.project / ".ai-comm-local" / "upgrade" / "project.json").read_bytes(), declared)


if __name__ == "__main__":
    unittest.main()
