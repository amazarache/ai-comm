#!/usr/bin/env python3
"""Validate release-manifest paths, sizes, hashes, and the protocol digest."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
HEX = re.compile(r"[0-9a-f]{64}\Z")


def fail(message):
    print(f"manifest check failed: {message}", file=sys.stderr)
    return 1


def main():
    path = ROOT / "RELEASE-MANIFEST.json"
    try:
        raw = path.read_bytes()
        manifest = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return fail("RELEASE-MANIFEST.json is not readable UTF-8 JSON")
    if manifest.get("format") != "ai-comm-review-manifest/1" or not isinstance(manifest.get("files"), list):
        return fail("unsupported manifest format")
    seen = set()
    for entry in manifest["files"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "bytes", "sha256"}:
            return fail("invalid file entry")
        name = entry["path"]
        if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
            return fail("invalid relative path")
        relative = PurePosixPath(name)
        if (relative.is_absolute() or name != relative.as_posix() or
                any(part in {"", ".", ".."} for part in relative.parts)):
            return fail("path escapes package")
        if name in seen or name == "RELEASE-MANIFEST.json":
            return fail("duplicate path or self-reference")
        seen.add(name)
        target = ROOT.joinpath(*relative.parts)
        try:
            cursor = ROOT
            for part in relative.parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    return fail("manifest path traverses a symlink")
            resolved = target.resolve(strict=True)
            resolved.relative_to(ROOT.resolve())
            if target.is_symlink() or not target.is_file():
                return fail("manifest entry is not a regular file")
            content = target.read_bytes()
        except (OSError, ValueError):
            return fail("manifest path is missing or escapes package")
        if type(entry["bytes"]) is not int or entry["bytes"] != len(content):
            return fail("byte count mismatch")
        if not isinstance(entry["sha256"], str) or not HEX.fullmatch(entry["sha256"]):
            return fail("invalid file digest")
        if hashlib.sha256(content).hexdigest() != entry["sha256"]:
            return fail("file digest mismatch")
    # Only the package's own checkout is inspected; an extracted archive has no Git metadata,
    # and Git must not search a parent repository in its place.
    checkout = False
    if (ROOT / ".git").exists():
        top = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--show-toplevel"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30).stdout
        top_path = top.removesuffix(b"\n").decode()
        checkout = bool(top_path) and Path(top_path).resolve() == ROOT.resolve()
    if checkout:
        try:
            tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], check=True,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30).stdout
            tracked_paths = {name.decode("utf-8") for name in tracked.split(b"\0") if name}
        except (OSError, UnicodeError, subprocess.SubprocessError):
            return fail("cannot inspect tracked package paths")
        if tracked_paths - {"RELEASE-MANIFEST.json"} != seen:
            return fail("manifest paths do not match the complete tracked package file set")
    protocol_digest = manifest.get("protocol_sha256")
    if not isinstance(protocol_digest, str) or not HEX.fullmatch(protocol_digest):
        return fail("invalid protocol digest")
    protocol = (ROOT / "PROTOCOL.md").read_bytes()
    if hashlib.sha256(protocol).hexdigest() != protocol_digest:
        return fail("protocol digest mismatch")
    scope = "" if checkout else " (no Git checkout: tracked-file set not checked)"
    print(f"manifest ok: {len(seen)} files; protocol {protocol_digest}{scope}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
