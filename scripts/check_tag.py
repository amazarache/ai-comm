#!/usr/bin/env python3
"""Check that a release tag is annotated and binds the checked manifest."""
import hashlib
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def git(*args):
    result = subprocess.run(["git", "-C", str(ROOT), *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=30)
    if result.returncode:
        raise ValueError("Git reference check failed")
    return result.stdout


def main():
    if len(sys.argv) != 2 or not re.fullmatch(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", sys.argv[1]):
        print("usage: check_tag.py vX.Y.Z", file=sys.stderr)
        return 2
    tag = sys.argv[1]
    source = (ROOT / "ai_comm.py").read_text(encoding="utf-8")
    version_match = re.search(r'^VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"$', source, re.MULTILINE)
    if not version_match or tag != "v" + version_match.group(1):
        print("tag and package version differ", file=sys.stderr)
        return 1
    try:
        if git("cat-file", "-t", f"refs/tags/{tag}").decode().strip() != "tag":
            raise ValueError("tag is not annotated")
        target = git("rev-parse", f"refs/tags/{tag}^{{}}").decode().strip()
        head = git("rev-parse", "HEAD").decode().strip()
        if target != head:
            raise ValueError("tag does not point to checked commit")
        manifest_digest = hashlib.sha256((ROOT / "RELEASE-MANIFEST.json").read_bytes()).hexdigest()
        tag_object = git("cat-file", "tag", f"refs/tags/{tag}").decode("utf-8", "replace")
        pattern = r"(?i)\bmanifest(?:[- ]sha256)?\s*[:=]?\s*" + re.escape(manifest_digest) + r"(?:\b|$)"
        if not re.search(pattern, tag_object):
            raise ValueError("tag message does not bind the release manifest")
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError) as exc:
        print(f"tag check failed: {exc}", file=sys.stderr)
        return 1
    print(f"tag {tag} matches HEAD and manifest {manifest_digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
