#!/usr/bin/env python3
"""Render the per-release bootstrap `install-ai-comm.sh` from `scripts/bootstrap.sh.in`.

    render_bootstrap.py VERSION [--out FILE]      write the bootstrap (default: stdout)
    render_bootstrap.py VERSION --digest          print only its sha256
    render_bootstrap.py --check                   compare docs/INSTALL.md's digest table with a fresh render
                                                  for this tree's own VERSION; exit 1 on any difference

The output is deterministic: a fixed substitution of the version and the production
constants, LF line endings, no timestamp. The published bootstrap is always rendered with
the production constants below; `render()` accepts other values only so that tests can
build a separate, never-published copy (for example a loopback HTTPS fixture and small time
limits). There is no runtime override in the rendered script.
"""
import argparse
import hashlib
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "scripts" / "bootstrap.sh.in"
INSTALL_DOC = ROOT / "docs" / "INSTALL.md"
BASE = "https://github.com/amazarache/ai-comm/releases/download/v{version}"
PRODUCTION = {"TOTAL_SECONDS": 300, "SOCKET_SECONDS": 20, "MAX_HOPS": 5, "SIZE_LIMIT": 64 * 1024 * 1024}
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
TABLE_RE = re.compile(r"^\| `v([0-9]+\.[0-9]+\.[0-9]+)` \| `install-ai-comm\.sh` \| `([0-9a-f]{64})` \|$", re.MULTILINE)
PLACEHOLDER = re.compile(r"@[A-Z_]+@")


def render(version, base=None, **limits):
    """Return the bootstrap bytes for VERSION. Only tests pass `base` or limits."""
    if not VERSION_RE.fullmatch(version):
        raise ValueError("invalid version")
    values = dict(PRODUCTION)
    for key, value in limits.items():
        if key not in values or type(value) is not int or value <= 0:
            raise ValueError("invalid limit " + key)
        values[key] = value
    url = base if base is not None else BASE.format(version=version)
    if not url.startswith("https://") or "'" in url:
        raise ValueError("the base URL must be HTTPS")
    text = TEMPLATE.read_text(encoding="utf-8").replace("\r\n", "\n")
    text = text.replace("@VERSION@", version).replace("@BASE@", url)
    for key, value in values.items():
        text = text.replace(f"@{key}@", str(value))
    leftover = PLACEHOLDER.search(text)
    if leftover:
        raise ValueError("unrendered placeholder " + leftover.group(0))
    return text.encode("utf-8")


def tree_version():
    text = (ROOT / "ai_comm.py").read_text(encoding="utf-8")
    return re.findall(r'^VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"$', text, re.MULTILINE)[0]


def check():
    version = tree_version()
    rows = TABLE_RE.findall(INSTALL_DOC.read_text(encoding="utf-8"))
    expected = hashlib.sha256(render(version)).hexdigest()
    if rows != [(version, expected)]:
        print(f"docs/INSTALL.md digest table {rows} does not match v{version} {expected}", file=sys.stderr)
        return 1
    print(f"bootstrap v{version} sha256 {expected} matches docs/INSTALL.md")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("version", nargs="?")
    p.add_argument("--out")
    p.add_argument("--digest", action="store_true")
    p.add_argument("--check", action="store_true")
    args = p.parse_args(argv)
    if args.check:
        return check()
    if not args.version:
        p.error("VERSION is required")
    data = render(args.version)
    if args.digest:
        print(hashlib.sha256(data).hexdigest())
    elif args.out:
        Path(args.out).write_bytes(data)
    else:
        sys.stdout.buffer.write(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
