import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "ai-comm" / "SKILL.md"


class SkillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ai-comm-skill-")
        self.project = Path(self.temp.name)
        self.channel = self.project / ".ai-comm"
        self.env = os.environ.copy()
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args, input_data=None):
        return subprocess.run([sys.executable, str(ROOT / "ai_comm.py"),
                               "--channel", str(self.channel), *args],
                              cwd=ROOT, input=input_data, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=self.env, timeout=30,
                              check=False)

    def successful(self, *args, input_data=None):
        result = self.run_cli(*args, input_data=input_data)
        self.assertEqual(0, result.returncode,
                         result.stderr.decode("utf-8", "replace"))
        return json.loads(result.stdout.decode("utf-8"))

    def test_resume_checks_cached_release_status(self):
        text = SKILL.read_text(encoding="utf-8")
        resume = text.split("## Resume and routine work", 1)[1].split("## Coordination rules", 1)[0]
        self.assertIn("python3 ai_comm.py release-status", resume)

    def test_agent_neutral_skill_covers_the_reviewed_workflow(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nname: ai-comm\ndescription: "))
        frontmatter = text.split("---", 2)[1].strip().splitlines()
        self.assertEqual(["name: ai-comm", next(line for line in frontmatter
                                                 if line.startswith("description: "))],
                         frontmatter)
        self.assertRegex(frontmatter[1], r"^description: .{1,200}$")
        normalized = " ".join(text.split())
        required = (
            "## When to use this skill", "activation note first", "state --as",
            "inbox --as", "ack --as", "wait --as", "--timeout 30",
            "FACT and CORRECT", "confidence and sources", "No source, no fact",
            "append-only ledger message in one commit", "never hand-commit to a channel",
            "Check each command's exit status", "Never edit a published message",
            "write to a frozen channel", "process polled recently",
            "only the PING exchange tests that a model responded",
            "Candidate:", "PROPOSE the exact commit and manifest", "20 full-suite",
            "200 focused", "independent adversarial review", "ACCEPTs the same manifest",
            "DECIDEs that exact candidate", "fast-forward `main`",
            "annotated release tag", "release tarball", "fresh clone",
            "exact `RELEASES.json`", "Push the index only after that decision",
            "python3 ai_comm.py release-status", "python3 ai_comm.py upgrade-plan",
            "scripts/check_for_release.py --fetch-permitted", "updates refs only",
            "including a partial fetch, stop", "updates refs only",
            "upgrade-plan.repin_steps", "Gate 2 yes",
            "Stop at the first failed step", "older pin (1.3.0 or earlier)", "omit",
            "same-origin tag fetch", "state.local_package", "Restart any watcher",
            "Rollback", "repeat the same tag, manifest, protocol, test, state",
            "FACT citing the owner's yes", "a shared pin moved", "own pin asks its own owner",
            "migration plan", "project-local installation", "owner's explicit permission",
        )
        for phrase in required:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, normalized)
        self.assertNotIn("Codex", text)
        self.assertNotIn("Claude", text)
        self.assertFalse(any(line.lstrip().startswith("/") for line in text.splitlines()))

    def test_start_status_stop_and_release_awareness_instructions(self):
        skill = " ".join(SKILL.read_text(encoding="utf-8").split())
        for phrase in (
            "## Start, status, and stop",
            "Start A.",
            "Start B.",
            "Start this host's approved listener first",
            "bounded wait calls of at most 30 seconds",
            "PING / PONG / final ACK",
            "three substantive proposal/critique cycles",
            "notification form of bounded `wait`",
            "Do not call `inbox` or `ack` just to count unread messages",
            "Stop. Stop only a listener this session started",
            "unverified peer claim",
            "Only the configured notifier",
            "topic `upgrade-prompt`",
            "never authorizes a fetch or re-pin",
            "recorded no or later",
            "at least daily while the session is already running",
            "Nothing here wakes an idle model",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, skill)
        self.assertNotIn("Codex", skill)
        self.assertNotIn("Claude", skill)

        activate = " ".join((ROOT / "ACTIVATE.md").read_text(encoding="utf-8").split())
        for phrase in (
            "## Start a two-session exchange",
            "`$ai-comm start A <subject>`",
            "`$ai-comm start B`",
            "`/comm start A <subject>`",
            "`/ai-comm start A <subject>`",
            "Exactly one role is the notifier",
            "never authorize another session to fetch or re-pin",
            "does not wake an idle model",
            "Package 1.4.2 session flow",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, activate)

        readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
        self.assertIn("$ai-comm start A <subject>", readme)
        self.assertIn("/comm start A <subject>", readme)
        self.assertIn("Nothing wakes a session after its host leaves it idle", readme)

    def test_skill_channel_commands_run_against_a_scratch_channel(self):
        init = self.run_cli("init", "--task", "Test the installed skill commands",
                            "--reviewers", "alpha", "beta")
        self.assertEqual(0, init.returncode, init.stderr.decode("utf-8", "replace"))
        for handle, platform, session in (("alpha", "test", "skill-alpha"),
                                          ("beta", "test", "skill-beta")):
            joined = self.run_cli("join", "--as", handle, "--platform", platform,
                                  "--session", session)
            self.assertEqual(0, joined.returncode, joined.stderr.decode("utf-8", "replace"))

        self.successful("state", "--as", "alpha", "--session", "skill-alpha")
        draft = {"act": "ASK", "to": ["beta"], "topic": "skill-test",
                 "summary": "Notification for the bounded wait test.",
                 "questions": [{"op": "open", "id": "skill-wait",
                                "text": "Did the scratch channel receive this?"}]}
        sent = self.successful("send", "--as", "alpha", "--session", "skill-alpha",
                               "--key", "skill-test-ask", "--draft", "-",
                               input_data=json.dumps(draft).encode())
        self.assertFalse(sent["duplicate"])
        first_page = self.successful("inbox", "--as", "beta", "--session", "skill-beta")
        self.assertEqual(1, len(first_page["messages"]))
        self.assertGreaterEqual(first_page["through"], 1)
        wait = self.successful("wait", "--as", "beta", "--session", "skill-beta",
                               "--timeout", "30")
        self.assertGreaterEqual(wait["through"], first_page["through"])
        page = self.successful("inbox", "--as", "beta", "--session", "skill-beta")
        ack = self.successful("ack", "--as", "beta", "--session", "skill-beta",
                              "--through", str(page["through"]))
        self.assertEqual(page["through"], ack["read_through"])
        artifact_file = self.project / "review.txt"
        artifact_file.write_text("scratch evidence\n", encoding="utf-8")
        artifact = self.successful("artifact", "--as", "alpha", "--session", "skill-alpha",
                                   "--file", str(artifact_file))
        self.assertEqual(hashlib.sha256(artifact_file.read_bytes()).hexdigest(), artifact["artifact"])
        self.successful("validate")

    def test_copy_fallback_hash_command_uses_manifest_entry(self):
        activate = (ROOT / "ACTIVATE.md").read_text(encoding="utf-8")
        command = re.search(r"python3 -c '([^']+)' <adapter>/ai-comm/SKILL\.md", activate)
        self.assertIsNotNone(command)
        pinned = self.project / ".tools" / "ai-comm"
        pinned.mkdir(parents=True)
        shutil.copy2(ROOT / "RELEASE-MANIFEST.json", pinned / "RELEASE-MANIFEST.json")
        shutil.copytree(ROOT / "skills" / "ai-comm", pinned / "skills" / "ai-comm")
        destination = self.project / ".agents" / "skills" / "ai-comm" / "SKILL.md"
        activate = (ROOT / "ACTIVATE.md").read_text(encoding="utf-8")
        block = activate.split("If symlinks are unavailable", 1)[1].split("```sh", 1)[1].split("```", 1)[0]
        copy_commands = [line.replace("<adapter>", ".agents/skills") for line in block.splitlines()
                         if line.startswith(("mkdir -p <adapter>", "cp -R .tools/ai-comm/skills/ai-comm"))]
        copied = subprocess.run(["/bin/sh", "-eu", "-c", "\n".join(copy_commands)],
                                cwd=self.project, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(0, copied.returncode, copied.stderr.decode("utf-8", "replace"))
        args = [sys.executable, "-c", command.group(1), str(destination)]
        good = subprocess.run(args, cwd=self.project, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(0, good.returncode, good.stderr.decode("utf-8", "replace"))
        destination.write_text("stale skill\n", encoding="utf-8")
        stale = subprocess.run(args, cwd=self.project, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=30)
        self.assertNotEqual(0, stale.returncode)
        manifest = json.loads((ROOT / "RELEASE-MANIFEST.json").read_text(encoding="utf-8"))
        record = next(row for row in manifest["files"]
                      if row["path"] == "skills/ai-comm/SKILL.md")
        self.assertEqual(hashlib.sha256(SKILL.read_bytes()).hexdigest(), record["sha256"])

    def test_activation_adapters_stay_inside_the_project_pin(self):
        activate = " ".join((ROOT / "ACTIVATE.md").read_text(encoding="utf-8").split())
        for phrase in (
            "| Codex |", "| Claude Code |", ".agents/skills/ai-comm",
            ".claude/skills/ai-comm", "../../.tools/ai-comm/skills/ai-comm",
            "each adapter must resolve inside this project's own pinned",
            "Before using the ai-comm channel, read `.tools/ai-comm/skills/ai-comm/SKILL.md`.",
            "mkdir -p <adapter>",
            "cp -R .tools/ai-comm/skills/ai-comm <adapter>/ai-comm",
            "ln -sn ../../.tools/ai-comm/skills/ai-comm .agents/skills/ai-comm",
            "ln -sn ../../.tools/ai-comm/skills/ai-comm .claude/skills/ai-comm",
            "Codex CLI", "0.156.1", "Claude Code", "2.1.284",
        ):
            self.assertIn(phrase, activate)
        self.assertIn("| `1.4.0` | `3.0` (unchanged)",
                      (ROOT / "README.md").read_text(encoding="utf-8"))
        self.assertIn("| `1.4.2` | `3.0` (unchanged)",
                      (ROOT / "README.md").read_text(encoding="utf-8"))

    def test_activation_symlink_commands_fail_safely_when_repeated(self):
        activate = (ROOT / "ACTIVATE.md").read_text(encoding="utf-8")
        block = activate.split("From the project root", 1)[1].split("```sh", 1)[1].split("```", 1)[0]
        pinned = self.project / ".tools" / "ai-comm" / "skills" / "ai-comm"
        pinned.mkdir(parents=True)
        first = subprocess.run(["/bin/sh", "-eu", "-c", block], cwd=self.project,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(0, first.returncode, first.stderr.decode("utf-8", "replace"))
        codex = self.project / ".agents" / "skills" / "ai-comm"
        claude = self.project / ".claude" / "skills" / "ai-comm"
        expected = "../../.tools/ai-comm/skills/ai-comm"
        self.assertTrue(codex.is_symlink())
        self.assertTrue(claude.is_symlink())
        self.assertEqual(expected, os.readlink(codex))
        self.assertEqual(expected, os.readlink(claude))
        second = subprocess.run(["/bin/sh", "-eu", "-c", block], cwd=self.project,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertNotEqual(0, second.returncode)
        self.assertFalse((pinned / "ai-comm").exists())
        self.assertEqual(expected, os.readlink(codex))
        self.assertEqual(expected, os.readlink(claude))


if __name__ == "__main__":
    unittest.main()
