#!/usr/bin/env python3
"""Small dependency-free scan for common committed credential formats."""
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".py", ".md", ".json", ".yml", ".yaml", ".sh", ".txt", ".toml"}
PATTERNS = [
    re.compile(r"gh[pousr]_" + r"[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_" + r"[A-Za-z0-9_]{50,}"),
    re.compile(r"AKIA" + r"[0-9A-Z]{16}"),
    re.compile(r"-" * 5 + r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY" + r"-" * 5),
    re.compile(r"(?i)(?:api[_-]?key|access[_-]?token|password)\s*[:=]\s*[\"'][A-Za-z0-9/+=_-]{24,}[\"']"),
]


def main():
    findings = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.is_symlink() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if any(part in {".git", "__pycache__", ".venv"} for part in path.relative_to(ROOT).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            if any(pattern.search(line) for pattern in PATTERNS):
                findings.append(f"{path.relative_to(ROOT)}:{line_number}")
    if findings:
        print("possible credential material found at: " + ", ".join(findings), file=sys.stderr)
        return 1
    print("credential leak scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
