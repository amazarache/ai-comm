#!/usr/bin/env python3
"""Guided project setup (design rev 7): plan, apply, status and abandon.

Runs from a per-user cache release (`install.sh --user setup ...`). `plan` prints one
canonical plan envelope and its SHA-256 and writes nothing: no file, directory or
lock, in the project or the cache. `status` reports the project's setup stages,
also without writing. The channel is later created by the verified pin's own
`ai_comm.py init`, never by this code. JSON output; exit 0 on success, 2 on a stop.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import ctypes.util
import errno
import fcntl
import importlib.util
import json
import os
import shutil
import subprocess
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
    need(ri.unfinished_setup(project) is None, "setup_in_progress")
    channel, note = check_paths(project, channel, note)

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


# --------------------------------------------------------------------------- apply

STAGES = ("pin", "gitignore", "channel", "join", "note", "declaration")
PIN_TIMEOUT = 120


def installer_module():
    return _load("ai_comm_install_project", RELEASE / "scripts" / "install_project.py")


def declare_module():
    return _load("ai_comm_declare_project", RELEASE / "scripts" / "declare_project.py")


def read_plan(plan_file, digest):
    """The approved envelope: exact canonical bytes whose SHA-256 is `digest`."""
    need(isinstance(digest, str) and uc.HEX.fullmatch(digest), "invalid_sha256")
    with open(plan_file, "rb") as stream:
        data = stream.read(1 << 20)
    need(uc.sha256_bytes(data) == digest, "plan_digest_mismatch")
    envelope = uc.parse_json(data, "plan_invalid")
    need(isinstance(envelope, dict) and envelope.get("schema") == PLAN_SCHEMA
         and uc.canonical(envelope) == data, "plan_invalid")
    return envelope


def rename_noreplace(src, dst):
    """Move a directory only if nothing exists at dst, an empty directory included.
    There is no fallback to a replacing rename (design rev 7, 6 step 4)."""
    libc = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
    if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        result = libc.renameat2(-100, os.fsencode(src), -100, os.fsencode(dst), 1)  # AT_FDCWD, RENAME_NOREPLACE
    elif sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        result = libc.renamex_np(os.fsencode(src), os.fsencode(dst), 0x4)  # RENAME_EXCL
    else:
        raise Stop("noreplace_unsupported")
    if result != 0:
        code = ctypes.get_errno()
        if code in (errno.EEXIST, errno.ENOTEMPTY):
            raise Stop("channel_exists")
        if code in (errno.ENOSYS, errno.EINVAL):
            raise Stop("noreplace_unsupported")
        raise OSError(code, os.strerror(code))


def run_pin(pin, channel, *args):
    """The verified pin's own CLI, as argv without a shell."""
    try:
        done = subprocess.run([sys.executable, "-I", str(pin / "ai_comm.py"), "--channel", str(channel), *args],
                              capture_output=True, stdin=subprocess.DEVNULL, env=uc.clean_env(),
                              timeout=PIN_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Stop("pin_command_failed", command=args[0]) from exc
    need(done.returncode == 0, "pin_command_failed", command=args[0])
    return uc.parse_json(done.stdout[:65536], "pin_command_failed")


def channel_facts(path):
    """Task, reviewers, policy and root commit of a channel repository, read with Git."""
    need(uc.git(path, "rev-parse", "--is-bare-repository").strip() == "true", "channel_invalid")
    roots = uc.git(path, "rev-list", "--max-parents=0", "HEAD").split()
    need(len(roots) == 1 and uc.COMMIT.fullmatch(roots[0]), "channel_invalid")
    config = uc.parse_json(uc.git(path, "show", "HEAD:channel.json").encode(), "channel_invalid")
    policy = config.get("policy") or {}
    return {"root": roots[0], "task": config.get("task"), "reviewers": config.get("reviewers"),
            "require_confidence": policy.get("require_confidence"), "require_sources": policy.get("require_sources")}


def expected_channel(env):
    return {"task": env["task"], "reviewers": env["reviewers"],
            "require_confidence": env["evidence"]["require_confidence"],
            "require_sources": env["evidence"]["require_sources"]}


def json_block(value):
    """Canonical JSON for the note's data block; backquotes escaped so text stays data."""
    return uc.canonical(value).decode("utf-8").replace("`", "\\u0060")


def render_note(env, root, digest):
    roles, target = env["roles"], env["target"]
    data = {"release": target["version"], "commit": target["commit"], "tag_object": target["tag_object"],
            "manifest_sha256": target.get("manifest_sha256"), "channel": env["channel"], "channel_root": root,
            "task": env["task"], "reviewers": env["reviewers"], "evidence": env["evidence"],
            "roles": [{k: r[k] for k in ("role", "handle", "host_label", "duties", "notifier")} for r in roles],
            "release_checks": env["release_checks"], "wake": "none approved", "nudge": "none approved",
            "plan_sha256": digest}
    lines = [
        "# ai-comm activation note",
        "",
        "Written once by guided setup. It records the owner-approved plan as data; it grants nothing.",
        "Before using the ai-comm channel, read `.tools/ai-comm/skills/ai-comm/SKILL.md`.",
        "",
        f"- Release: {target['version']} (commit {target['commit']})",
        f"- Channel: `{env['channel']}` (reviewers fixed at creation; FACT and CORRECT require confidence and sources)",
        "- Wake and nudge: none approved",
        "- Release checks: " + (f"allowed for the notifier `{env['release_checks']['notifier']}` while its session "
                                "is active; nothing is checked while no session runs"
                                if env["release_checks"]["allowed"] else "not allowed"),
        "",
        "| Role | Handle | Host label | Notifier |",
        "| --- | --- | --- | --- |",
    ]
    for r in roles:
        lines.append(f"| {r['role']} | `{r['handle']}` | `{r['host_label']}` | {'yes' if r['notifier'] else 'no'} |")
    lines += [
        "",
        "Task and working duties are owner-provided text, recorded only in the data block below.",
        "",
        f"To start B, the owner says in the other assistant, in this directory: "
        f"ai-comm start B as {roles[1]['handle']} (setup note {env['note']})",
        "",
        "```json",
        json_block(data),
        "```",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def declaration_bytes(project, root, note):
    return uc.canonical({"v": 1, "schema": uc.PROJECT_SCHEMA, "status": "declared",
                         "project_id": {"path": str(project), "channel": root},
                         "guidance": [note], "integrations": []})


class Setup:
    """One approved envelope applied to one project, journalled for resume."""

    def __init__(self, env, digest, cache, installer):
        self.env, self.digest, self.cache = env, digest, cache
        self.installer = installer
        self.project = uc.check_ancestry(env["project"])
        self.pin = self.project / ".tools" / "ai-comm"
        self.channel = self.project.joinpath(*PurePosixPath(env["channel"]).parts)
        self.note = self.project.joinpath(*PurePosixPath(env["note"]).parts)
        self.dir = self.project / ".ai-comm-local" / "setup"
        self.staging = self.dir / "txn"
        self.done, self.intents, self.txn = {}, {}, None

    # -- journal

    def journal(self):
        return uc.Journal(self.dir)

    def load(self):
        records, _ = self.journal().read()
        start = records[0]["data"]
        need(start.get("schema") == uc.SETUP_SCHEMA, "setup_journal_invalid")
        need(start.get("plan_sha256") == self.digest, "setup_plan_mismatch")
        states = [r["data"].get("state") for r in records if r["kind"] == "state"]
        need(not any(state in uc.SETUP_TERMINAL for state in states), "setup_finished")
        self.txn = start["txn"]
        for r in records:
            if r["kind"] == "intent":
                self.intents[r["data"]["stage"]] = r["data"]
            elif r["kind"] == "outcome":
                self.done[r["data"]["stage"]] = r["data"]

    def intent(self, stage, **data):
        self.journal().append("intent", {"stage": stage, **data})
        self.intents[stage] = {"stage": stage, **data}

    def outcome(self, stage, **data):
        self.journal().append("outcome", {"stage": stage, **data})
        self.done[stage] = {"stage": stage, **data}

    # -- checks

    def pin_matches(self):
        report = vp.check_project(self.project, self.cache)
        target = self.env["target"]
        return (report["status"] == "verified" and report["commit"] == target["commit"]
                and report["tag_object"] == target["tag_object"])

    def gitignore_digest(self):
        path = self.project / ".gitignore"
        need(not path.is_symlink(), "unsafe_path", path=".gitignore")
        return ri.file_digest(path.read_bytes() if path.exists() else None)

    def check_baseline(self):
        """Everything not yet done must still be as planned; drift is never adopted."""
        env, gi = self.env, self.env["baseline"]["gitignore"]
        need(self.env["executing"] == {"path": str(RELEASE), **executing_identity()}, "executing_release_changed")
        if "pin" not in self.done:
            if env["profile"] == "fresh":
                report = vp.check_project(self.project, self.cache)
                if report["status"] != "absent" and "pin" not in self.intents:
                    raise Stop("pin_exists")
                if "pin" not in self.intents:
                    need(not os.path.lexists(self.project / ".ai-comm-local" / "upgrade"), "upgrade_state_exists")
                need(not os.path.lexists(self.project / ".ai-comm-local" / "wake"), "wake_installation_exists")
            else:
                need(self.pin_matches(), "pin_changed")
            if "pin" not in self.intents:
                need(self.gitignore_digest() == gi["g0"], "gitignore_changed")
        elif "gitignore" not in self.done:
            need(self.gitignore_digest() in (gi["g1"], gi["g2"]), "gitignore_changed")
        if "channel" not in self.done and "channel" not in self.intents:
            need(not os.path.lexists(self.channel), "channel_exists")
        if "note" not in self.done and "note" not in self.intents:
            need(not os.path.lexists(self.note), "note_exists")

    # -- stages

    def stage_pin(self):
        target = self.env["target"]
        if self.env["profile"] == "reuse":
            need(self.pin_matches(), "pin_changed")
            return self.outcome("pin", commit=target["commit"])
        self.intent("pin", version=target["version"])
        ip = installer_module()
        if any(self.project.joinpath(".ai-comm-local").glob("install-*")):
            ip.recover(self.project)  # finishes a published install or removes unpublished staging
        if not os.path.lexists(self.pin):
            gi = self.env["baseline"]["gitignore"]
            self.installer(self.project, target["version"], fetch_permitted=True, use_helper=False,
                           expect={k: target[k] for k in ("tag_object", "commit", "manifest_sha256")},
                           gitignore_guard={"before": gi["g0"], "after": gi["g1"]}, setup_txn=self.txn)
        need(self.pin_matches(), "pin_changed")
        self.outcome("pin", commit=target["commit"])

    def stage_gitignore(self):
        gi = self.env["baseline"]["gitignore"]
        current = self.gitignore_digest()
        if current != gi["g2"]:
            need(current == gi["g1"], "gitignore_changed")
            path = self.project / ".gitignore"
            existing = path.read_bytes() if path.exists() else None
            data, _ = ri.gitignore_after(existing, (*ri.INSTALLER_GITIGNORE_LINES, f"/{self.env['channel']}/"))
            need(ri.file_digest(data) == gi["g2"], "gitignore_guard_mismatch")
            tmp = self.project / f".gitignore.ai-comm-{secrets.token_hex(4)}"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if existing is not None:
                os.chmod(tmp, os.stat(path).st_mode & 0o777)
            need(self.gitignore_digest() == gi["g1"], "gitignore_changed")
            os.replace(tmp, path)
            uc.fsync_dir(self.project)
        self.outcome("gitignore", digest=gi["g2"])

    def stage_channel(self):
        want = expected_channel(self.env)
        recorded = self.intents.get("channel", {}).get("root")
        if os.path.lexists(self.channel):
            need(recorded is not None, "channel_exists")
            try:
                facts = channel_facts(self.channel)
            except Stop as exc:
                raise Stop("channel_foreign") from exc
            need(facts["root"] == recorded and {k: facts[k] for k in want} == want, "channel_foreign")
            return self.outcome("channel", root=recorded)
        staged = self.staging / "channel"
        if os.path.lexists(staged):
            shutil.rmtree(staged)  # this transaction's own interrupted init
        uc.private_dir(self.staging)
        self.intent("channel", root=None)
        run_pin(self.pin, staged, "init", "--task", self.env["task"], "--reviewers", *self.env["reviewers"],
                "--require-confidence", *want["require_confidence"],
                "--require-sources", *want["require_sources"])
        facts = channel_facts(staged)
        need({k: facts[k] for k in want} == want, "channel_invalid")
        self.intent("channel", root=facts["root"])
        need(device_of(self.channel.parent) == device_of(self.project), "channel_other_filesystem")
        rename_noreplace(staged, self.channel)
        uc.fsync_dir(self.channel.parent)
        self.outcome("channel", root=facts["root"])

    def stage_join(self):
        a = self.env["roles"][0]
        run_pin(self.pin, self.channel, "join", "--as", a["handle"], "--platform", a["host_label"],
                "--session", self.env["a_reference"]["value"])
        self.outcome("join", handle=a["handle"])

    def stage_note(self):
        data = render_note(self.env, self.done["channel"]["root"], self.digest)
        digest = uc.sha256_bytes(data)
        if os.path.lexists(self.note):
            need(self.intents.get("note", {}).get("sha256") == digest and not self.note.is_symlink()
                 and uc.sha256_bytes(self.note.read_bytes()) == digest, "note_exists")
            return self.outcome("note", sha256=digest)
        self.intent("note", sha256=digest)
        fd = os.open(self.note, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        uc.fsync_dir(self.note.parent)
        self.outcome("note", sha256=digest)

    def stage_declaration(self):
        if self.env["profile"] != "fresh":
            return self.outcome("declaration", status="as found")
        data = declaration_bytes(self.project, self.done["channel"]["root"], self.env["note"])
        digest = uc.sha256_bytes(data)
        live = self.project / ".ai-comm-local" / "upgrade" / "project.json"
        if live.is_file() and not live.is_symlink() and uc.sha256_bytes(live.read_bytes()) == digest:
            return self.outcome("declaration", sha256=digest)
        self.intent("declaration", sha256=digest)
        candidate = uc.private_dir(self.staging) / "declaration.json"
        uc.write_private_atomic(candidate, data)
        previous = uc.sha256_bytes(ri.unintegrated_declaration(self.project))
        declare_module().install(self.project, candidate=str(candidate), sha256=digest, previous=previous,
                                 cache=self.cache, channel=str(self.channel), setup_txn=self.txn)
        self.outcome("declaration", sha256=digest)

    def run(self):
        for stage in STAGES:
            if stage not in self.done:
                getattr(self, "stage_" + stage)()
        self.journal().append("state", {"state": "completed"})
        if os.path.lexists(self.staging):
            shutil.rmtree(self.staging)
        return handoff(self.env)


@contextlib.contextmanager
def setup_lock(directory):
    fd = os.open(directory / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Stop("setup_locked") from exc
        yield
    finally:
        os.close(fd)


def default_installer(project, version, **kw):
    return installer_module().install(project, version, **kw)


def apply(plan_file, digest, *, session, cache_dir=None, installer=None):
    """Apply one approved envelope, or resume it with the same file and digest."""
    env = read_plan(plan_file, digest)
    check_text(session, "approving session reference", MAX_REFERENCE)
    cache = vp.cache_for_release(cache_dir)
    setup = Setup(env, digest, cache, installer or default_installer)
    existing = uc.unfinished_setup(setup.project)
    if existing is None:
        setup.check_baseline()  # preflight: nothing is written if this fails
    local = setup.project / ".ai-comm-local"
    if not os.path.lexists(local):
        os.mkdir(local, 0o700)
    need(not local.is_symlink() and uc._mode_ok_ancestor(os.lstat(local)), "unsafe_path", path=".ai-comm-local")
    uc.private_dir(setup.dir)
    with setup_lock(setup.dir):
        if os.path.lexists(setup.dir / "journal.jsonl"):
            setup.load()
        else:
            setup.check_baseline()  # again under the lock
            setup.txn = secrets.token_hex(32)
            setup.journal().append("start", {"schema": uc.SETUP_SCHEMA, "txn": setup.txn, "plan_sha256": digest,
                                             "plan": env, "approved_by": session, "approved_at": int(time.time())})
        setup.check_baseline()
        result = setup.run()
    return {"status": "set_up", "txn": setup.txn, **result}


def abandon(project_dir):
    """The owner's explicit way out of an unfinished setup. Removes only setup's own
    staging; everything already published stays and is named."""
    project = uc.check_ancestry(project_dir)
    directory = project / ".ai-comm-local" / "setup"
    current = uc.unfinished_setup(project)
    need(current is not None, "nothing_to_abandon")
    need(current != "unrecognized", "setup_journal_invalid")
    with setup_lock(directory):
        records, _ = uc.Journal(directory).read()
        env = records[0]["data"]["plan"]
        uc.Journal(directory).append("state", {"state": "abandoned"})
        if os.path.lexists(directory / "txn"):
            shutil.rmtree(directory / "txn")
    remaining = [name for name, path in (("pin", project / ".tools" / "ai-comm"),
                                         ("channel", project.joinpath(*PurePosixPath(env["channel"]).parts)),
                                         ("note", project.joinpath(*PurePosixPath(env["note"]).parts)),
                                         ("declaration", project / ".ai-comm-local" / "upgrade" / "project.json"))
                 if os.path.lexists(path)]
    return {"status": "abandoned", "txn": current, "kept": remaining + [".gitignore lines, if added"]}


def handoff(env):
    roles = env["roles"]
    a, b = roles[0], roles[1]
    target = env["target"]
    lines = [
        f"ai-comm {target['version']} is set up in {env['project']} for the task in {env['note']}.",
        f"{a['handle']}: this session, joined. {b['handle']}: not started.",
        "In the other assistant, in this directory, say:",
        f"  ai-comm start B as {b['handle']} (setup note {env['note']})",
    ]
    if b["notifier"] and env["release_checks"]["allowed"]:
        lines.append(f"  and add: and check {env['release_checks']['origin']} for new releases while active")
    if env["b_fallback"]:
        lines.append(f"  if that assistant has no session identifier, add: reference {env['b_fallback']['value']}")
    lines += [
        "Skill invocation differs by host: a slash command (/ai-comm), a $ mention ($ai-comm), or plain words.",
        "No skill discovery in that assistant: first tell it to read .tools/ai-comm/skills/ai-comm/SKILL.md.",
        "Guided upgrades: " + ("ready, declared with no integrations." if env["profile"] == "fresh"
                               else "as found; the first guided upgrade may need a separate declaration step."),
    ]
    if env["release_checks"]["allowed"]:
        lines.append(f"Release checks: the notifier {env['release_checks']['notifier']} may check "
                     f"{env['release_checks']['origin']} at start and about daily while active. Nothing is checked "
                     "while no session runs. Revoke by telling a session. Nothing upgrades without your yes.")
    lines.append("No automatic wake. If this session has ended when B joins, say \"continue\" here.")
    return {"handoff": lines}


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
    stages = {}
    directory = project / ".ai-comm-local" / "setup"
    if ri.unfinished_setup(project) not in (None, "unrecognized"):
        records, _ = uc.Journal(directory).read()
        for r in records:
            if r["kind"] in ("intent", "outcome"):
                stages[r["data"]["stage"]] = "done" if r["kind"] == "outcome" else "started"
    return {"status": "ok", "project": str(project), "pin": report["status"], "pin_version": report.get("version"),
            "setup": ri.unfinished_setup(project) and "unfinished" or "none", "stages": stages,
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
    p = sub.add_parser("apply")
    p.add_argument("--plan", required=True)
    p.add_argument("--sha256", required=True)
    p.add_argument("--session", required=True, help="the approving session's reference")
    p = sub.add_parser("abandon")
    p.add_argument("--project", required=True)
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
        elif args.command == "apply":
            result = apply(args.plan, args.sha256, session=args.session, cache_dir=args.cache)
        elif args.command == "abandon":
            result = abandon(args.project)
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
