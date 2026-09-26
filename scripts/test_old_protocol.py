#!/usr/bin/env python3
"""Prove released CLIs fail closed, with no write, on a protocol-3 channel."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
RELEASES = ("v0.1.0", "v0.1.3", "v0.2.0")


def run(args, *, cwd=None, data=None, check=True):
    result = subprocess.run(args, cwd=cwd, input=data, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=30)
    if check and result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {args!r}\n"
                           f"{result.stderr.decode(errors='replace')}")
    return result


def main():
    with tempfile.TemporaryDirectory(prefix="ai-comm-old-tool-") as temporary:
        base = Path(temporary)
        channel = base / "channel"
        sys.path.insert(0, str(ROOT))
        import ai_comm as current
        current.Channel.init(channel, "Old tool regression fixture", ["alpha", "beta"])
        ledger = current.Channel(channel)
        ledger.join("alpha", "codex", "alpha-session")
        ledger.join("beta", "claude", "beta-session")
        initial_head = ledger.snapshot().head
        draft = json.dumps({"act": "PING", "to": ["beta"], "topic": "compat",
                            "summary": "Must not be published."}).encode()

        for tag in RELEASES:
            source = run(["git", "-C", str(ROOT), "show", f"{tag}:ai_comm.py"], check=False)
            protocol = run(["git", "-C", str(ROOT), "show", f"{tag}:PROTOCOL.md"], check=False)
            if source.returncode or protocol.returncode:
                raise RuntimeError(f"required historical tag is missing: {tag}")
            package = base / tag
            package.mkdir()
            script = package / "ai_comm.py"
            script.write_bytes(source.stdout)
            (package / "PROTOCOL.md").write_bytes(protocol.stdout)
            commands = [
                ["state"],
                ["inbox", "--as", "alpha", "--session", "alpha-session"],
                ["wait", "--as", "alpha", "--session", "alpha-session", "--timeout", "0"],
                ["render"],
                ["log"],
            ]
            if tag == "v0.2.0":
                commands += [["validate"], ["registry", "sessions"],
                             ["send", "--as", "alpha", "--session", "alpha-session",
                              "--key", "old-dry-run", "--draft", "-", "--dry-run"]]
            for command in commands:
                before = run(["git", "--git-dir", str(channel), "rev-parse", "HEAD"]).stdout
                result = run([sys.executable, str(script), "--channel", str(channel), *command],
                             cwd=base, data=draft if "--draft" in command else None, check=False)
                if result.returncode != 2:
                    raise AssertionError(f"{tag} {command!r}: expected exit 2, got "
                                         f"{result.returncode}; stdout={result.stdout!r}; stderr={result.stderr!r}")
                try:
                    error = json.loads(result.stderr)
                except (ValueError, UnicodeError) as exc:
                    raise AssertionError(f"{tag} {command!r}: error was not structured JSON") from exc
                if "package contract does not match channel" not in error.get("error", ""):
                    raise AssertionError(f"{tag} {command!r}: unexpected error {error!r}")
                after = run(["git", "--git-dir", str(channel), "rev-parse", "HEAD"]).stdout
                if before != after or after.decode().strip() != initial_head:
                    raise AssertionError(f"{tag} {command!r}: old tool changed channel HEAD")
            print(f"{tag}: {len(commands)} commands rejected without writes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
