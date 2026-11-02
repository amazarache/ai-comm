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

    def assert_no_project_identity(self, text):
        self.assertNotRegex(text, r"(?i)\b(?:sol-a|ha-live)\b")
        self.assertNotRegex(text, r"(?i)\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b")
        # The literal /tmp warning is required by the reviewed wake guidance.
        # Reject user-home notation and other concrete absolute paths, including
        # subpaths beneath the temporary directory; relative package paths pass.
        self.assertNotRegex(text, r"~/|(?<![\w.])/(?!tmp(?=[`\s]|$))[\w.-]")
        self.assertNotRegex(text, r"\b[A-Za-z]:[\\/]")
        self.assertNotRegex(text, r"--as\s+(?!<HANDLE>)\S+")

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
            "Use the pinned ai-comm package for channel work",
            "Do not modify or fork it",
        )
        for phrase in required:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, normalized)
        self.assert_no_project_identity(text)
        self.assertFalse(any(line.lstrip().startswith("/") for line in text.splitlines()))

    def test_start_status_stop_and_release_awareness_instructions(self):
        skill = " ".join(SKILL.read_text(encoding="utf-8").split())
        for phrase in (
            "## Start, status, and stop",
            "Start A.",
            "Start B.",
            "If an approved listener is available, start it first",
            "A listener is optional",
            "work in active-polling mode",
            "the host's own identifier if it has one, otherwise the one the owner assigned to this session; never invent one",
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
            "use only the approved listener named in the project activation note",
            "This is not permission to build a helper",
            "wait --timeout 30 --new-since N",
            "during an active turn, read with ordinary bounded `wait --timeout N`",
            "Never read with `--new-since`",
            "a detached shell process wakes nobody",
            "If the host has a documented way for another process to queue a message into one existing session",
            "Check the ai-comm inbox from message N.",
            "owner approved that exact current target session",
            "owner writes or directly confirms that entry",
            "never create, edit, or carry forward nudge approval on its own authority",
            "an entry alone is not owner consent",
            "a takeover never inherits it",
            "runtime receipt `ai-comm-runtime/<HANDLE>.json` within the channel",
            "integer `read` field, never `seen`",
            "If it cannot safely read and validate that receipt, skip and log the nudge",
            "Do not follow a symlink, change a receipt, or acknowledge on the target's behalf",
            "read cursor already covers N",
            "log each nudge",
            "no message content, instructions, or approvals",
            "durable, project-local, git-ignored",
            "never only in `/tmp`",
            "do not build watchers, wakers, services, or channel tooling",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, skill)
        self.assert_no_project_identity(skill)
        disclosure = skill.split("Then ask the owner exactly:", 1)[0]
        self.assertTrue(disclosure.endswith(
            "Erase removes the rejected credential, which can be valid after a transient 401. "))

        activate = " ".join((ROOT / "ACTIVATE.md").read_text(encoding="utf-8").split())
        for phrase in (
            "## Start a two-session exchange",
            "invoke the `ai-comm` skill with `start A <subject>`",
            "invoke it with `start B`",
            "The session never uses `--new-since` to read",
            "a detached shell process wakes nobody",
            "Exactly one role is the notifier",
            "never authorize another session to fetch or re-pin",
            "does not wake an idle model",
            "Package 1.4.2 session flow",
            "do not modify it, fork it, design changes to ai-comm",
            "build channel tooling unless the owner explicitly asks",
            "evidence-backed FACTs or ASKs",
            "owner approved the exact current target session",
            "owner writes or directly confirms that entry",
            "never create, edit, or carry forward nudge approval on its own authority",
            "an entry alone is not owner consent",
            "revocable and is not inherited by a takeover",
            "approved listener named in the project activation note",
            "This is not permission to build a helper",
            "permission is recorded in the project activation note",
            "runtime receipt `ai-comm-runtime/<HANDLE>.json` within the channel",
            "integer `read` field, never `seen`",
            "If it cannot safely read and validate that receipt, skip and log the nudge",
            "read cursor already covers N",
            "target reads `inbox` before acting",
            "durable, project-local, git-ignored path",
            "never only in `/tmp`",
            "**Session references.**",
            "the owner assigns one per session",
            "it is never reused for another session or handle",
            "A session never invents an identifier and never presents it as the host's own",
            "a takeover never inherits it",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, activate)
        self.assertNotIn("recorded here", activate)
        disclosure = activate.split("Then ask this exact additional question:", 1)[0]
        self.assertTrue(disclosure.endswith(
            "Erase removes the rejected credential, which can be valid after a transient 401. "))

        readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
        self.assertIn("## How any agent uses ai-comm", readme)
        self.assertIn("invoke the `ai-comm` skill with `start A <subject>`", readme)
        self.assertIn("read `.tools/ai-comm/skills/ai-comm/SKILL.md` and follow it", readme)
        self.assertIn("ai-comm itself does not wake idle sessions; separately approved host listeners or nudge helpers must remain available", readme)

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

    def install_block(self):
        activate = (ROOT / "ACTIVATE.md").read_text(encoding="utf-8")
        block = activate.split("From the project root", 1)[1].split("```sh", 1)[1].split("```", 1)[0]
        self.assertIn("SKILLS_DIR=<skills-dir>", block)
        return block

    def run_block(self, block, project, skills_dir):
        script = block.replace("SKILLS_DIR=<skills-dir>", "SKILLS_DIR=" + skills_dir)
        return subprocess.run(["/bin/sh", "-eu", "-c", script], cwd=project,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)

    def test_copy_fallback_hash_command_uses_manifest_entry(self):
        activate = (ROOT / "ACTIVATE.md").read_text(encoding="utf-8")
        command = re.search(r"python3 -c '([^']+)' \"\$SKILLS_DIR/ai-comm/SKILL\.md\"", activate)
        self.assertIsNotNone(command)
        pinned = self.project / ".tools" / "ai-comm"
        pinned.mkdir(parents=True)
        shutil.copy2(ROOT / "RELEASE-MANIFEST.json", pinned / "RELEASE-MANIFEST.json")
        shutil.copytree(ROOT / "skills" / "ai-comm", pinned / "skills" / "ai-comm")
        destination = self.project / "host-skills" / "ai-comm" / "SKILL.md"
        block = activate.split("If symbolic links are unavailable", 1)[1].split("```sh", 1)[1].split("```", 1)[0]
        copy_commands = [line for line in block.splitlines()
                         if line.startswith(("mkdir -p ", "cp -R .tools/ai-comm/skills/ai-comm"))]
        self.assertEqual(2, len(copy_commands))
        copied = subprocess.run(["/bin/sh", "-eu", "-c", "SKILLS_DIR=host-skills\n" + "\n".join(copy_commands)],
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

    def test_activation_skill_install_is_generic_and_stays_inside_the_project_pin(self):
        activate = " ".join((ROOT / "ACTIVATE.md").read_text(encoding="utf-8").split())
        for phrase in (
            "`<skills-dir>/ai-comm`",
            "check the host's own documentation",
            "Every link must resolve inside this project's own pinned `.tools/ai-comm` checkout",
            "discovers exactly one skill named `ai-comm`",
            "Before using the ai-comm channel, read `.tools/ai-comm/skills/ai-comm/SKILL.md`.",
            'cp -R .tools/ai-comm/skills/ai-comm "$SKILLS_DIR/ai-comm"',
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, activate)
        self.assertIn("| `1.4.0` | `3.0` (unchanged)",
                      (ROOT / "README.md").read_text(encoding="utf-8"))
        self.assertIn("| `1.4.2` | `3.0` (unchanged)",
                      (ROOT / "README.md").read_text(encoding="utf-8"))

    def test_skill_link_resolves_to_the_pin_from_any_skills_directory(self):
        block = self.install_block()
        pinned = self.project / ".tools" / "ai-comm" / "skills" / "ai-comm"
        pinned.mkdir(parents=True)
        for skills_dir, expected in ((".agents/skills", "../../.tools/ai-comm/skills/ai-comm"),
                                     ("host-skills", "../.tools/ai-comm/skills/ai-comm"),
                                     ("a/b/skills", "../../../.tools/ai-comm/skills/ai-comm")):
            with self.subTest(skills_dir=skills_dir):
                link = self.project / skills_dir / "ai-comm"
                for _ in range(2):
                    result = self.run_block(block, self.project, skills_dir)
                    self.assertEqual(0, result.returncode, result.stderr.decode("utf-8", "replace"))
                    self.assertTrue(link.is_symlink())
                    self.assertEqual(expected, os.readlink(link))
                    self.assertEqual(pinned.resolve(), link.resolve())
                self.assertIn(b"skill path already exists", result.stderr)
        self.assertFalse((pinned / "ai-comm").exists())

    def test_skill_link_preserves_existing_paths(self):
        block = self.install_block()
        for kind in ("directory", "dangling"):
            with self.subTest(kind=kind):
                project = self.project / kind
                (project / ".tools" / "ai-comm" / "skills" / "ai-comm").mkdir(parents=True)
                parent = project / "host-skills"
                parent.mkdir(parents=True)
                link = parent / "ai-comm"
                if kind == "directory":
                    link.mkdir()
                    (link / "marker").write_text("preserve me", encoding="utf-8")
                else:
                    link.symlink_to("keep-this-target")
                result = self.run_block(block, project, "host-skills")
                self.assertEqual(0, result.returncode, result.stderr.decode("utf-8", "replace"))
                self.assertIn(b"skill path already exists", result.stderr)
                if kind == "directory":
                    self.assertFalse(link.is_symlink())
                    self.assertEqual("preserve me", (link / "marker").read_text())
                else:
                    self.assertEqual("keep-this-target", os.readlink(link))

if __name__ == "__main__":
    unittest.main()
