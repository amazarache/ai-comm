#!/usr/bin/env python3
"""Guided project setup (design rev 7): plan and status, read-only.

Runs from a per-user cache release (`install.sh --user setup ...`). `plan` prints one
canonical plan envelope and its SHA-256 and writes nothing: no file, directory or
lock, in the project or the cache. `status` reports the project's setup stages,
also without writing. The channel is later created by the verified pin's own
`ai_comm.py init`, never by this code. JSON output; exit 0 on success, 2 on a stop.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import sys
import time

sys.dont_write_bytecode = True
RELEASE = Path(__file__).resolve().parents[1]

PLAN_SCHEMA = "ai-comm-setup-plan/1"
MIN_SUPPORTED = "1.10.0"   # oldest pin whose init/join/state/validate and controller setup relies on
DEFAULT_CHANNEL = ".ai-comm"
DEFAULT_NOTE = "AI-COMM-ACTIVATION.md"
DEFAULT_HANDLES = ("agent-a", "agent-b")
DEFAULT_DUTIES = ("implements", "reviews")
DEFAULT_B_LABEL = "assistant"
EVIDENCE_POLICY = {"require_confidence": ["FACT", "CORRECT"], "require_sources": ["FACT", "CORRECT"]}
NO_INTEGRATIONS = "this project has no existing ai-comm listeners or integrations"
COMPONENT = re.compile(r"[A-Za-z0-9._-]+\Z")
RESERVED = (".git", ".tools", ".ai-comm-local")
MAX_DUTY = 200
MAX_TASK = 2000
MAX_REFERENCE = 160

CHANGE_RULES = {
    "handles": "Permanent in this channel. A takeover replaces the session behind a handle, never its name; "
               "prefer stable, neutral names because duties can change and handles cannot.",
    "task": "Fixed at channel creation.",
    "reviewers": "Fixed at channel creation; every listed reviewer must accept.",
    "host_labels": "Chosen before joining; kept as history.",
    "channel": "Location chosen now; a later move is coordinated manual maintenance, not a supported one-step "
               "operation.",
    "note": "Can be changed later; update what refers to it.",
    "notifier": "Can be changed later by an owner-directed edit of the note, recorded as a FACT.",
    "duties": "Can be changed later by an owner-directed edit of the note; duties grant nothing.",
    "release_checks": "Revocable at any time by telling a session.",
}


def _load(name, path):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


vp = _load("ai_comm_verify_pin", RELEASE / "scripts" / "verify_pin.py")
iu, ri, uc, Stop, need = vp.iu, vp.ri, vp.uc, vp.Stop, vp.need


def core():
    """The executing release's own ai_comm module, for its text and handle rules."""
    return ri.release_check_module().ac


def version_key(version):
    return tuple(int(part) for part in version.split("."))


# --------------------------------------------------------------------------- input rules


def check_relative(value, field):
    """Project-relative path of plain components: no escape, no option-like name."""
    need(isinstance(value, str) and value and len(value) <= 255, "path_invalid", field=field)
    parts = PurePosixPath(value).parts
    need(not value.startswith("/") and parts and all(COMPONENT.fullmatch(p) for p in parts)
         and all(p not in (".", "..") and not p.startswith("-") for p in parts)
         and "/".join(parts) == value.rstrip("/"), "path_invalid", field=field)
    return "/".join(parts)


def overlaps(a, b):
    pa, pb = PurePosixPath(a).parts, PurePosixPath(b).parts
    n = min(len(pa), len(pb))
    return pa[:n] == pb[:n]


def device_of(path):
    """Filesystem identity of a directory; a separate function so tests can inject devices."""
    return os.stat(path).st_dev


def check_paths(project, channel, note):
    channel = check_relative(channel, "channel")
    note = check_relative(note, "note")
    need(not overlaps(channel, note), "path_overlap", field="note")
    for path, field in ((channel, "channel"), (note, "note")):
        for reserved in RESERVED:
            need(not overlaps(path, reserved), "path_reserved", field=field, reserved=reserved)
        parent = project.joinpath(*PurePosixPath(path).parts[:-1])
        cursor = project
        for part in PurePosixPath(path).parts[:-1]:
            cursor = cursor / part
            st = os.lstat(cursor) if os.path.lexists(cursor) else None
            need(st is not None and stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode),
                 "parent_missing", field=field)
        uc.check_ancestry(parent)
        target = project.joinpath(*PurePosixPath(path).parts)
        need(not os.path.lexists(target), "channel_exists" if field == "channel" else "note_exists", field=field)
    channel_parent = project.joinpath(*PurePosixPath(channel).parts[:-1])
    need(device_of(channel_parent) == device_of(project), "channel_other_filesystem")
    return channel, note


def check_text(value, field, maximum, multiline=False):
    ac = core()
    try:
        ac.text_field(value, field, maximum, multiline)
    except ac.ProtocolError as exc:
        raise Stop("text_invalid", field=field) from exc
    return value


def check_handle(value, field):
    ac = core()
    need(isinstance(value, str) and ac.HANDLE.fullmatch(value), "handle_invalid", field=field)
    return value


def check_roles(handles, reviewers, notifier, labels, duties):
    a, b = (check_handle(h, "handles") for h in handles)
    need(a != b, "handle_invalid", field="handles")
    need(2 <= len(reviewers) <= 16 and len(set(reviewers)) == len(reviewers), "reviewers_invalid")
    for handle in reviewers:
        check_handle(handle, "reviewers")
    need(a in reviewers and b in reviewers, "reviewers_invalid", field="roles")
    need(notifier in (a, b), "notifier_invalid")
    for label in labels:
        check_handle(label, "host_label")
    for duty in duties:
        if duty is not None:
            check_text(duty, "duty", MAX_DUTY)


# --------------------------------------------------------------------------- release selection


def select_target(cache, version=None, now=time.time):
    """The fresh profile's frozen target: the highest version of the complete, strictly
    validated, freshly checked index with a trusted binding (design rev 7, section 4)."""
    index = cache.index()
    need(index is not None and index.get("validation") == "strict"
         and isinstance(index.get("versions"), list) and index["versions"], "release_check_required")
    metadata = iu._freshness(cache, int(now()))
    need(metadata == "cached", "release_check_required", metadata=metadata)
    records = {r["package_version"]: r for r in index["releases"]}
    need(set(records) == set(index["versions"]), "release_check_required", metadata="inconsistent")
    newest = max(index["versions"], key=version_key)
    chosen = version or newest
    need(chosen in records, "release_not_in_index", version=chosen)
    binding = cache.bindings()["bindings"].get(chosen)
    need(iu.binding_agrees(binding, records[chosen]), "release_unbound", version=chosen)
    need(version_key(chosen) >= version_key(MIN_SUPPORTED), "release_unsupported", version=chosen)
    origin = cache.state().get("origin")
    need(isinstance(origin, str) and origin, "release_check_required", metadata="origin")
    return {"origin": origin, "version": chosen, "tag_object": binding["tag_object"],
            "commit": binding["commit"], "manifest_sha256": binding["manifest_sha256"],
            "protocol_sha256": binding["protocol_sha256"], "checked_at": index["refreshed_at"],
            "newest_indexed": newest, "index_sha256": index.get("index_sha256")}


def reuse_target(report):
    need(report["status"] == "verified", "pin_unverified", status=report["status"])
    need(report.get("class") == "controller"
         and version_key(report["version"]) >= version_key(MIN_SUPPORTED), "release_unsupported",
         version=report.get("version"))
    return {"version": report["version"], "commit": report["commit"], "tag_object": report["tag_object"]}


# --------------------------------------------------------------------------- discovery


def skill_candidates(project, cache):
    """`name: ai-comm` entries in skill directories registered with the cache and in the
    project's own `.<dir>/skills` directories. Reported only; host precedence is unknown."""
    seen = []
    places = [Path(o["path"]) for o in cache.state()["owned"] if o.get("kind") == "skill_link"]
    for hidden in sorted(project.glob(".*/skills/*")):
        places.append(hidden)
    for place in places:
        skill = place / "SKILL.md"
        try:
            head = skill.read_text(encoding="utf-8", errors="replace")[:2048]
        except OSError:
            continue
        if re.search(r"^name:\s*ai-comm\s*$", head, re.MULTILINE):
            seen.append(str(place))
    return {"candidates": seen, "duplicate": len(seen) > 1}


# --------------------------------------------------------------------------- the plan


def executing_identity():
    """The release running setup. Cache releases are Git checkouts; an extracted archive
    (for example a test run from a release tarball) is named by its version only."""
    try:
        identity = uc.package_identity(RELEASE)
    except Stop as exc:
        if exc.code != "pin_not_git_checkout":
            raise
        return {"version": core().VERSION, "commit": None}
    return {"version": identity["version"], "commit": identity["commit"]}


def ignore_states(project, channel, profile):
    path = project / ".gitignore"
    need(not path.is_symlink(), "unsafe_path", path=".gitignore")
    g0 = path.read_bytes() if path.exists() else None
    if profile == "fresh":
        g1, added = ri.gitignore_after(g0, ri.INSTALLER_GITIGNORE_LINES)
        g1 = g1 if added else g0
    else:
        g1 = g0
    g2, added = ri.gitignore_after(g1, (*ri.INSTALLER_GITIGNORE_LINES, f"/{channel}/"))
    g2 = g2 if added else g1
    return {"g0": ri.file_digest(g0), "g1": ri.file_digest(g1), "g2": ri.file_digest(g2)}


def reference(value, origin):
    if value is None:
        return {"value": "ref-" + secrets.token_hex(8), "origin": "proposed owner-assigned reference, not a host identifier"}
    check_text(value, "session reference", MAX_REFERENCE)
    need(origin in ("host", "owner"), "reference_origin_invalid")
    return {"value": value, "origin": "host identifier" if origin == "host" else "owner-assigned reference"}


def plan(project_dir, *, task, channel=None, note=None, handles=None, reviewers=None, notifier=None,
         duties=None, a_label=None, a_session=None, a_session_origin="host", b_label=None,
         b_fallback=False, b_reference=None, version=None, release_checks=True, cache_dir=None, now=time.time):
    """Compute the plan envelope. Reads the project and the cache; writes nothing."""
    chosen = {k for k, v in (("channel", channel), ("note", note), ("handles", handles),
                             ("reviewers", reviewers), ("notifier", notifier), ("duties", duties),
                             ("b_label", b_label), ("version", version)) if v is not None}
    channel = DEFAULT_CHANNEL if channel is None else channel
    note = DEFAULT_NOTE if note is None else note
    handles = tuple(handles or DEFAULT_HANDLES)
    need(len(handles) == 2, "handle_invalid", field="handles")
    reviewers = list(reviewers or handles)
    notifier = notifier or handles[0]
    duties = tuple(duties) if duties is not None else DEFAULT_DUTIES
    need(len(duties) == 2, "duty_invalid")
    b_label = b_label or DEFAULT_B_LABEL
    need(a_label is not None, "host_label_required", field="a_label")
    check_text(task, "task", MAX_TASK, True)
    check_roles(handles, reviewers, notifier, (a_label, b_label), duties)
    if version is not None:
        need(uc.VERSION_RE.fullmatch(version), "invalid_version")

    project = uc.check_ancestry(project_dir)
    need(project.is_dir(), "project_missing")
    for part in project.parts:
        need(part != ".tools", "nested_in_tools")
    channel, note = check_paths(project, channel, note)
    unfinished = ri.unfinished_setup(project)
    need(unfinished is None, "setup_in_progress")

    cache = vp.cache_for_release(cache_dir)
    report = vp.check_project(project, cache)
    if report["status"] == "absent":
        profile = "fresh"
        need(not os.path.lexists(project / ".ai-comm-local" / "upgrade"), "upgrade_state_exists")
        need(not os.path.lexists(project / ".ai-comm-local" / "wake"), "wake_installation_exists")
        target = select_target(cache, version, now)
        network = {"origin": target["origin"], "fetch": [f"refs/tags/v{target['version']}", "release-index"]}
        declaration = {"status": "declared", "integrations": [], "guidance": [note], "condition": NO_INTEGRATIONS}
    else:
        profile = "reuse"
        need(version is None, "version_not_applicable")
        target = reuse_target(report)
        network = None
        declaration = None

    release = executing_identity()
    origin = cache.state().get("origin")
    envelope = {
        "schema": PLAN_SCHEMA,
        "executing": {"path": str(RELEASE), "version": release["version"], "commit": release["commit"]},
        "project": str(project),
        "profile": profile,
        "target": target,
        "network": network,
        "declaration": declaration,
        "channel": channel,
        "note": note,
        "task": task,
        "reviewers": reviewers,
        "evidence": EVIDENCE_POLICY,
        "roles": [{"role": "A", "handle": handles[0], "host_label": a_label, "duties": duties[0],
                   "notifier": notifier == handles[0]},
                  {"role": "B", "handle": handles[1], "host_label": b_label, "duties": duties[1],
                   "notifier": notifier == handles[1]}],
        "release_checks": {"allowed": bool(release_checks), "origin": origin, "notifier": notifier},
        "a_reference": reference(a_session, a_session_origin),
        "b_fallback": (reference(b_reference, "owner") if b_reference else reference(None, None))
        if (b_fallback or b_reference) else None,
        "baseline": {"gitignore": ignore_states(project, channel, profile), "channel": "absent", "note": "absent"},
        "origins": {k: ("your choice" if k in chosen else "suggested")
                    for k in ("channel", "note", "handles", "reviewers", "notifier", "duties", "b_label", "version")},
    }
    data = uc.canonical(envelope)
    return {"status": "planned", "plan": envelope, "sha256": uc.sha256_bytes(data),
            "display": display(envelope), "discovery": skill_candidates(project, cache)}


def display(envelope):
    """Plain lines for the owner: every value with its origin and change rule."""
    o, roles = envelope["origins"], envelope["roles"]
    target = envelope["target"]
    lines = [f"profile: {envelope['profile']}"]
    if envelope["profile"] == "fresh":
        lines.append(f"release: {target['version']} ({o['version']}), newest indexed {target['newest_indexed']}, "
                     f"commit {target['commit']}, fetched from {target['origin']} and tested before use")
    else:
        lines.append(f"release: existing verified pin {target['version']}, unchanged; nothing is fetched or installed")
    lines += [
        f"task: {envelope['task']!r} ({CHANGE_RULES['task']})",
        f"channel: {envelope['channel']} ({o['channel']}; {CHANGE_RULES['channel']})",
        f"note: {envelope['note']} ({o['note']}; {CHANGE_RULES['note']})",
        f"handles: A {roles[0]['handle']}, B {roles[1]['handle']} ({o['handles']}; {CHANGE_RULES['handles']})",
        f"reviewers: {' '.join(envelope['reviewers'])} ({o['reviewers']}; {CHANGE_RULES['reviewers']})",
        f"host labels: A {roles[0]['host_label']}, B {roles[1]['host_label']} ({CHANGE_RULES['host_labels']})",
        f"notifier: {envelope['release_checks']['notifier']} ({o['notifier']}; {CHANGE_RULES['notifier']})",
        f"duties: A {roles[0]['duties']}, B {roles[1]['duties']} ({o['duties']}; {CHANGE_RULES['duties']})",
        f"A's session reference: {envelope['a_reference']['value']} ({envelope['a_reference']['origin']})",
        "release checks: " + ("allowed for the notifier session while it is active"
                              if envelope["release_checks"]["allowed"] else "not allowed")
        + f" ({CHANGE_RULES['release_checks']})",
        "evidence policy: FACT and CORRECT require confidence and sources (fixed at channel creation)",
        "nothing else may edit this project while setup applies",
    ]
    for role in envelope["reviewers"]:
        if role not in (roles[0]["handle"], roles[1]["handle"]):
            lines.append(f"reviewer {role}: must join and accept themselves; setup binds nobody but A")
    if envelope["b_fallback"]:
        lines.append(f"B's fallback reference: {envelope['b_fallback']['value']} ({envelope['b_fallback']['origin']})")
    if envelope["declaration"]:
        lines.append(f"guided upgrades: declared with no integrations ({envelope['declaration']['condition']})")
    return lines


# --------------------------------------------------------------------------- status


def status(project_dir, *, cache_dir=None):
    """What exists in the project now, without writing anything."""
    project = uc.check_ancestry(project_dir)
    cache = vp.cache_for_release(cache_dir)
    report = vp.check_project(project, cache)
    declaration = project / ".ai-comm-local" / "upgrade" / "project.json"
    found = "absent"
    if declaration.is_file() and not declaration.is_symlink():
        try:
            found = json.loads(declaration.read_text(encoding="utf-8")).get("status", "unknown")
        except (OSError, ValueError):
            found = "unreadable"
    return {"status": "ok", "project": str(project), "pin": report["status"], "pin_version": report.get("version"),
            "setup": ri.unfinished_setup(project) and "unfinished" or "none",
            "declaration": found,
            "release_checks": "nothing is checked while no session runs"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache", help="per-user cache (default: the cache this release belongs to)")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--project", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--channel")
    p.add_argument("--note")
    p.add_argument("--handles", nargs=2)
    p.add_argument("--reviewers", nargs="+")
    p.add_argument("--notifier")
    p.add_argument("--duties", nargs=2)
    p.add_argument("--a-label", required=True)
    p.add_argument("--a-session")
    p.add_argument("--a-session-origin", choices=("host", "owner"), default="host")
    p.add_argument("--b-label")
    p.add_argument("--b-fallback", action="store_true")
    p.add_argument("--b-reference")
    p.add_argument("--version")
    p.add_argument("--no-release-checks", action="store_true")
    p = sub.add_parser("status")
    p.add_argument("--project", required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        print(json.dumps({"status": "stopped", "code": "invalid_arguments"}))
        return 2
    try:
        if args.command == "plan":
            result = plan(args.project, task=args.task, channel=args.channel, note=args.note, handles=args.handles,
                          reviewers=args.reviewers, notifier=args.notifier, duties=args.duties,
                          a_label=args.a_label, a_session=args.a_session, a_session_origin=args.a_session_origin,
                          b_label=args.b_label, b_fallback=args.b_fallback, b_reference=args.b_reference,
                          version=args.version, release_checks=not args.no_release_checks, cache_dir=args.cache)
        else:
            result = status(args.project, cache_dir=args.cache)
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
