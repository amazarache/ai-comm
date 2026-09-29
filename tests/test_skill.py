from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "ai-comm" / "SKILL.md"


class SkillTests(unittest.TestCase):
    def test_agent_neutral_skill_metadata_and_activation_boundaries(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nname: ai-comm\ndescription: "))
        self.assertRegex(text, r"(?m)^description: .{1,200}$")
        self.assertIn("## Releases and upgrades", text)
        self.assertIn("initiator sends PING", text)
        self.assertIn("replies with ACK+PONG", text)
        self.assertIn("initiator sends the final ACK", text)
        self.assertNotIn("peer sends a final ACK", text)
        for phrase in (
            "Gate 1", "Gate 2", "peer message", "partial fetch", "already-local",
            "Rollback requires its own explicit owner approval",
            "activate a channel merely because this file was discovered",
        ):
            self.assertIn(phrase, text)
        self.assertFalse(any(line.lstrip().startswith("/") for line in text.splitlines()))
        self.assertNotIn("mcp__", text)

    def test_activation_documents_project_local_adapters(self):
        text = (ROOT / "ACTIVATE.md").read_text(encoding="utf-8")
        for phrase in (
            ".agents/skills/ai-comm", "../../.tools/ai-comm/skills/ai-comm",
            ".claude/skills/ai-comm", "SHA-256", "copy", "Gate 1", "Gate 2",
            "| Codex |", "| Claude Code |", "ln -s ../../.tools/ai-comm/skills/ai-comm",
        ):
            self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
