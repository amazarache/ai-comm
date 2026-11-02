#!/usr/bin/env python3
"""Write a project's guided-upgrade declaration with trusted cache code (design 018c v3, section 7).

`.ai-comm-local/upgrade/project.json` tells a project's own upgrade controller which
listeners (integrations) to pause and rebind. This tool runs from a verified per-user
cache release and only for a project whose pin is verified, so it also works for pins
that predate the declaration commands. It never executes anything a declaration names.

  init     write the unintegrated declaration if there is none (never overwrites)
  install  replace the declaration with exact owner-reviewed bytes: the candidate's
           digest must be the reviewed one, it must validate against the real project
           identity, no guided transaction may be in flight, and the live file must
           still be the expected previous one (or absent)

JSON output; exit 0 on success, 2 on any stop.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import stat
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


vp = _load("ai_comm_verify_pin", RELEASE / "scripts" / "verify_pin.py")
iu, ri, uc, Stop, need = vp.iu, vp.ri, vp.uc, vp.Stop, vp.need
FINISHED = {"completed", "rolled_back"}


def _project(project_dir):
    project = uc.check_ancestry(project_dir)
    need(project.is_dir(), "project_missing")
    return project


def _require_verified(project, cache):
    report = vp.check_project(project, cache)
    need(report["status"] == "verified", "pin_not_verified", status=report["status"],
         reason=report.get("reason") or report.get("what"))
    return report


def _no_transaction_in_flight(project):
    current = uc.read_private(project.root / "current", missing=b"").decode("utf-8", "replace").strip()
    if current:
        try:
            state = uc.Txn(project, current).state()
        except Stop as exc:
            raise Stop("transaction_in_flight", txn=current, state="unreadable") from exc
        need(state in FINISHED, "transaction_in_flight", txn=current, state=state)


def _live_digest(project):
    path = project.root / "project.json"
    if not os.path.lexists(path):
        return "absent"
    return uc.sha256_bytes(uc.read_private(path))


def _read_candidate(path):
    """The candidate's exact bytes: a regular file, not a link, bounded, non-blocking."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise Stop("candidate_unreadable") from exc
    with os.fdopen(fd, "rb") as stream:
        need(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "candidate_unreadable")
        data = stream.read(uc.MAX_JSON + 1)
    need(len(data) <= uc.MAX_JSON, "candidate_too_large")
    return data


def init(project_dir, *, cache):
    project = _project(project_dir)
    _require_verified(project, cache)
    upgrade = uc.Project(project)
    with upgrade.lock():
        path = upgrade.root / "project.json"
        need(not os.path.lexists(path), "declaration_exists")
        data = ri.unintegrated_declaration(project)
        try:
            uc.create_private_exclusive(path, data)
        except Stop as exc:
            raise Stop("declaration_exists") from exc
    return {"status": "initialized", "declaration": str(path), "sha256": uc.sha256_bytes(data)}


def install(project_dir, *, candidate, sha256, previous, cache):
    need(uc.HEX.fullmatch(sha256 or ""), "invalid_sha256")
    need(previous == "absent" or uc.HEX.fullmatch(previous or ""), "invalid_previous")
    project = _project(project_dir)
    _require_verified(project, cache)
    data = _read_candidate(candidate)
    need(uc.sha256_bytes(data) == sha256, "candidate_digest_mismatch")
    upgrade = uc.Project(project)
    live = upgrade.project_id()  # the real project, computed by this release's own code
    uc.validate_project_manifest(data, {"path": live["path"], "channel": live["channel"]})
    with upgrade.lock():
        _no_transaction_in_flight(upgrade)
        current = _live_digest(upgrade)
        need(current == previous, "declaration_changed", current=current)
        uc.write_private_atomic(upgrade.root / "project.json", data)
    return {"status": "declared", "declaration": str(upgrade.root / "project.json"), "sha256": sha256,
            "previous": previous}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache", help="per-user cache (default: the cache this release belongs to)")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init")
    p.add_argument("--project", required=True)
    p = sub.add_parser("install")
    p.add_argument("--project", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--sha256", required=True)
    p.add_argument("--previous", required=True, help="sha256 of the live declaration, or `absent`")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code == 0:
            return 0  # --help
        print(json.dumps({"status": "stopped", "code": "invalid_arguments"}))
        return 2
    try:
        cache = vp.cache_for_release(args.cache)
        if args.command == "init":
            result = init(args.project, cache=cache)
        else:
            result = install(args.project, candidate=args.file, sha256=args.sha256, previous=args.previous,
                             cache=cache)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Stop as exc:
        safe = {k: v for k, v in exc.details.items() if isinstance(v, (str, int, bool)) or v is None}
        print(json.dumps({"status": "stopped", "code": exc.code, **safe}, sort_keys=True))
        return 2
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"status": "stopped", "code": "operation_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
