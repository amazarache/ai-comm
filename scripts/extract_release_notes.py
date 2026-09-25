#!/usr/bin/env python3
"""Print one exact version section from CHANGELOG.md."""
import re
import sys
from pathlib import Path


def main():
    if len(sys.argv) != 2 or not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", sys.argv[1]):
        print("usage: extract_release_notes.py X.Y.Z", file=sys.stderr)
        return 2
    changelog = (Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text(encoding="utf-8")
    lines = changelog.splitlines()
    heading = f"## {sys.argv[1]}"
    starts = [i for i, line in enumerate(lines) if line == heading]
    if len(starts) != 1:
        print("version must have exactly one changelog section", file=sys.stderr)
        return 1
    start = starts[0]
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    section = "\n".join(lines[start:end]).strip()
    if len(section) <= len(heading):
        print("changelog section is empty", file=sys.stderr)
        return 1
    print(section)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
