"""The package speaks to every agent host the same way.

No package file may name a particular agent host or vendor: instructions are
written by capability, so any agent can follow them. This file is the only place
such names appear, as the data this regression check looks for. The list is
finite, so passing it is regression coverage, not proof that the text suits every
host. The file list comes from RELEASE-MANIFEST.json, so the check also runs in
an extracted release archive without Git metadata.
"""
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve().relative_to(ROOT).as_posix()

# Whole-word product, host and vendor names. Generic English words that are also
# product names (for example "cursor", as in notification cursor) are not listed.
NAMES = (
    "aider", "amazon q", "antigravity", "anthropic", "chatgpt", "claude", "cline",
    "codex", "copilot", "devin", "gemini", "openai", "opencode", "roo code",
    "windsurf",
)
PATTERN = re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(n) for n in NAMES) + r")(?![a-z0-9])",
                     re.IGNORECASE)


def package_files():
    manifest = json.loads((ROOT / "RELEASE-MANIFEST.json").read_text(encoding="utf-8"))
    return sorted({entry["path"] for entry in manifest["files"]} | {"RELEASE-MANIFEST.json"})


class NeutralityTests(unittest.TestCase):
    def test_no_package_file_names_an_agent_host(self):
        files = package_files()
        self.assertIn(SELF, files, "the neutrality check must itself be a released file")
        findings = []
        for name in files:
            if name == SELF:
                continue
            text = (ROOT / name).read_text(encoding="utf-8", errors="replace")
            for number, line in enumerate(text.splitlines(), 1):
                for match in PATTERN.finditer(line):
                    findings.append(f"{name}:{number}: {match.group(0)}")
        self.assertEqual([], findings)

    def test_pattern_matches_names_but_not_ordinary_words(self):
        for text in ("Claude Code", "use codex here", "OpenCode", "GEMINI", "(copilot)"):
            with self.subTest(text=text):
                self.assertIsNotNone(PATTERN.search(text))
        for text in ("notification cursor", "codexes-free", "encode", "recursor", "agent-a"):
            with self.subTest(text=text):
                self.assertIsNone(PATTERN.search(text))


if __name__ == "__main__":
    unittest.main()
