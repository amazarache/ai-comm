"""Every release documents itself: the running VERSION has a CHANGELOG section that
the release workflow can extract, and a row in the README's compatibility table.
Reads files only, so it also runs in an extracted release archive."""
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
GUARD = "AI_COMM_UPGRADE_REAL_BOUNDARY_INNER"


def version():
    text = (ROOT / "ai_comm.py").read_text(encoding="utf-8")
    return re.findall(r'^VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"$', text, re.MULTILINE)[0]


def key(value):
    return tuple(int(part) for part in value.split("."))


class ReleaseDocumentTests(unittest.TestCase):
    def test_current_version_has_release_notes_and_a_compatibility_row(self):
        current = version()
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertEqual(changelog.count(f"\n## {current}\n"), 1)
        done = subprocess.run([sys.executable, "-I", str(ROOT / "scripts" / "extract_release_notes.py"), current],
                              capture_output=True, timeout=60, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"## {current}", done.stdout.decode())
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"| `{current}` | `3.0`", readme)

    @unittest.skipIf(os.environ.get(GUARD) == "1", "fixtures inside a target verification use older versions")
    def test_changelog_lists_releases_newest_first_starting_with_this_one(self):
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        sections = re.findall(r"^## ([0-9]+\.[0-9]+\.[0-9]+)$", changelog, re.MULTILINE)
        self.assertEqual(sections[0], version())
        self.assertEqual(sections, sorted(sections, key=key, reverse=True))


if __name__ == "__main__":
    unittest.main()
