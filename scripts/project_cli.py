#!/usr/bin/env python3
"""Run the current project's own ai-comm pin, only if it is verified (design 018c v3, section 9).

The optional `ai-comm` launcher resolves the per-user cache's `current` link once and
runs this file from that release, so verifier, metadata and launch all come from one
release even if `current` changes meanwhile. This program checks that it is itself an
unmodified recorded release of that cache, finds the project from the working
directory, verifies its pin with this release's trusted code, and only then replaces
itself with the pin's `ai_comm.py`, passing every argument through. It never operates a
channel itself. On any refusal it prints the JSON report and exits 2.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
RELEASE = Path(__file__).resolve().parents[1]


def _load(name, path):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def self_check(ri):
    """Check this whole release against the cache's record before loading anything
    else from it, so a changed verifier never runs. Only this file and the two
    modules this check needs (release_install.py, upgrade_controller.py) run first;
    writers under the same OS account remain inside the stated trust boundary."""
    state = ri.uc.parse_json(ri.uc.read_private(RELEASE.parent.parent / "state.json"), "cache_state_invalid")
    record = state.get("releases", {}).get(RELEASE.name) if isinstance(state, dict) else None
    ri.need(isinstance(record, dict) and isinstance(record.get("commit"), str), "launcher_release_changed",
            what="unrecorded")
    try:
        ri.verify_tree(RELEASE, record["commit"], record.get("manifest_sha256"))
    except ri.Stop as exc:
        raise ri.Stop("launcher_release_changed", what=exc.details.get("what"),
                      path=exc.details.get("path")) from exc


def refuse(report):
    print(json.dumps(report, sort_keys=True), file=sys.stderr)
    return 2


def main(argv=None, *, execv=os.execv, cwd=None):
    argv = sys.argv[1:] if argv is None else argv
    if RELEASE.parent.name != "releases":
        return refuse({"status": "stopped", "code": "not_a_cache_release", "release": str(RELEASE)})
    ri = _load("ai_comm_release_install", RELEASE / "scripts" / "release_install.py")
    try:
        self_check(ri)
        vp = _load("ai_comm_verify_pin", RELEASE / "scripts" / "verify_pin.py")
        cache = vp.cache_for_release()
        report = vp.check_project(vp.find_project(cwd or os.getcwd()), cache)
    except ri.Stop as exc:
        safe = {k: v for k, v in exc.details.items() if isinstance(v, (str, int, bool)) or v is None}
        return refuse({"status": "stopped", "code": exc.code, **safe})
    if report["status"] != "verified":
        report["hint"] = ("the project's pin was not run; ask the owner how to proceed (inspect it, or reinstall "
                          "into a new project directory after review)")
        return refuse(report)
    pin_cli = str(Path(report["pin"]) / "ai_comm.py")
    execv(sys.executable, [sys.executable, "-I", pin_cli, *argv])
    return 0  # only reached with a test double for execv


if __name__ == "__main__":
    raise SystemExit(main())
