#!/usr/bin/env python3
"""Guided, owner-consented ai-comm package upgrades for one project (design 013 v3).

This file is deliberately standalone (stdlib only) and never imports the pinned
ai_comm.py. `prepare` copies the verified target release's copy of this file to
.ai-comm-local/upgrade/controller/<sha256>/ so that recovery keeps working after
the pin changes, including a rollback to a release without upgrade commands.

Nothing here asks the owner a question, fetches without a recorded consent for
the exact target, or applies anything automatically. All output is JSON.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import fcntl
import grp
import pwd
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True

CONTROLLER_VERSION = "1"
JOURNAL_SCHEMA = "ai-comm-upgrade-journal/1"
PROJECT_SCHEMA = "ai-comm-upgrade-project/1"
CONSENT_SCHEMA = "ai-comm-upgrade-consent/1"
PROMPT_SCHEMA = "ai-comm-upgrade-prompt/1"
MAX_JSON = 1 << 20
MAX_HOOK_OUTPUT = 64 * 1024
HOOK_TIMEOUT = 600
TEST_TIMEOUT = 3600
CONSENT_TTL = 24 * 3600
SNOOZE_STEPS = (24 * 3600, 48 * 3600, 7 * 24 * 3600)
HEX = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
VERSION_RE = re.compile(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\Z")
HANDLE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
TXN_RE = re.compile(r"txn-[0-9a-f]{16}\Z")
CONSENT_RE = re.compile(r"consent-[0-9a-f]{16}\Z")
INTEGRATION_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
MESSAGE_ID = re.compile(r"[0-9]{8}-[a-z][a-z0-9_-]{0,31}\Z")
ANSWERS = ("yes", "fetch_only", "not_now", "skip", "never")
PHASES = ("pause", "install_source", "rebind", "check", "resume", "restore_source")
REPORT_PHASES = PHASES + ("attest",)
ATTEST_MAX_AGE = 3600
RELEASE_CHECK_SCHEMA = "ai-comm-release-check/1"
GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS",
                "GIT_CONFIG_COUNT", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES")


class Stop(Exception):
    """A deliberate, coded stop. The message is a stable code, never raw data."""

    def __init__(self, code, **details):
        super().__init__(code)
        self.code = code
        self.details = details


def need(condition, code, **details):
    if not condition:
        raise Stop(code, **details)


def now():
    return int(_dt.datetime.now(_dt.timezone.utc).timestamp())


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def parse_json(data, code="invalid_json"):
    need(isinstance(data, (bytes, bytearray)) and len(data) <= MAX_JSON, code)
    try:
        value = json.loads(bytes(data).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise Stop(code) from exc
    return value


# --------------------------------------------------------------------------- paths


def _mode_ok_ancestor(st):
    if stat.S_ISLNK(st.st_mode):
        return False
    if not stat.S_ISDIR(st.st_mode):
        return False
    if st.st_uid not in (0, os.getuid()):
        return False
    if st.st_mode & stat.S_ISVTX:
        return True
    if st.st_mode & stat.S_IWOTH:
        return False
    if st.st_mode & stat.S_IWGRP and not _private_group(st.st_gid):
        return False
    return True


def _private_group(gid):
    """The user's own primary group with no other listed members (user-private-group scheme)."""
    if gid != os.getgid():
        return False
    try:
        user = pwd.getpwuid(os.getuid()).pw_name
        members = set(grp.getgrgid(gid).gr_mem)
    except KeyError:
        return False
    return members <= {user}


def check_ancestry(directory):
    """Ancestors may be root- or user-owned; never symlinks or other-writable without sticky."""
    path = Path(os.path.abspath(directory))
    need(path.is_absolute(), "unsafe_ancestry")
    current = Path(path.anchor)
    parts = path.parts[1:]
    try:
        need(_mode_ok_ancestor(os.lstat(current)), "unsafe_ancestry")
        for part in parts:
            current = current / part
            st = os.lstat(current)
            need(_mode_ok_ancestor(st), "unsafe_ancestry")
    except FileNotFoundError as exc:
        raise Stop("project_missing") from exc
    return path


def private_dir(path, create=True):
    """A project-owned runtime directory: user-owned, 0700, not a symlink."""
    path = Path(path)
    if create:
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
    try:
        st = os.lstat(path)
    except FileNotFoundError as exc:
        raise Stop("runtime_missing") from exc
    need(stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode), "unsafe_runtime_path")
    need(st.st_uid == os.getuid(), "unsafe_runtime_owner")
    need(stat.S_IMODE(st.st_mode) & 0o077 == 0, "unsafe_runtime_mode")
    return path


def read_private(path, limit=MAX_JSON, missing=None):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        if missing is not None:
            return missing
        raise Stop("runtime_missing")
    except OSError as exc:
        raise Stop("unsafe_runtime_path") from exc
    with os.fdopen(fd, "rb") as stream:
        st = os.fstat(stream.fileno())
        need(stat.S_ISREG(st.st_mode), "unsafe_runtime_path")
        need(st.st_uid == os.getuid(), "unsafe_runtime_owner")
        need(stat.S_IMODE(st.st_mode) & 0o077 == 0, "unsafe_runtime_mode")
        data = stream.read(limit + 1)
    need(len(data) <= limit, "runtime_file_too_large")
    return data


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_private_atomic(path, data):
    path = Path(path)
    fd, temp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink():
            raise Stop("unsafe_runtime_path")
        os.replace(temp, path)
        fsync_dir(path.parent)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)
        raise


def create_private_exclusive(path, data):
    """Create a new private file; fails if it exists (used for single-use markers)."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError as exc:
        raise Stop("already_exists") from exc
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    fsync_dir(Path(path).parent)


# --------------------------------------------------------------------------- project


class Project:
    def __init__(self, directory, channel=None):
        # The pin location is fixed by the accepted layout (R7): an override could
        # redirect an approved transaction to another project's checkout.
        self.dir = check_ancestry(directory)
        self.pin = self.dir / ".tools" / "ai-comm"
        self.channel = check_ancestry(channel) if channel else self.dir / ".ai-comm"
        self.local = self.dir / ".ai-comm-local"
        self.root = self.local / "upgrade"

    def runtime(self, create=True):
        # .ai-comm-local is shared project scratch space: it must pass the ancestor
        # rule (§2); only upgrade/ and below must be private (0700/0600).
        if create and not os.path.lexists(self.local):
            os.mkdir(self.local, 0o700)
        try:
            need(_mode_ok_ancestor(os.lstat(self.local)), "unsafe_runtime_path")
        except FileNotFoundError as exc:
            raise Stop("runtime_missing") from exc
        private_dir(self.root, create)
        for name in ("txn", "consents", "controller"):
            private_dir(self.root / name, create)
        return self.root

    @contextlib.contextmanager
    def lock(self):
        self.runtime()
        fd = os.open(self.root / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Stop("upgrade_locked") from exc
            yield
        finally:
            os.close(fd)

    def channel_id(self):
        out = git(self.channel, "rev-list", "--max-parents=0", "HEAD")
        roots = out.split()
        need(len(roots) == 1 and COMMIT.fullmatch(roots[0]), "channel_identity_unavailable")
        return roots[0]

    def project_id(self):
        """Identity bound into consent and transactions: canonical locations, not just commits."""
        pin = check_ancestry(self.pin)
        channel = check_ancestry(self.channel)
        return {"path": str(self.dir), "channel": self.channel_id(),
                "pin_path": os.path.realpath(pin), "channel_path": os.path.realpath(channel)}


def clean_env():
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV_DROP}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["LC_ALL"] = "C"
    return env


def git(directory, *args, ok=(0,), timeout=120):
    try:
        result = subprocess.run(["git", "-C", str(directory), *args], capture_output=True,
                                env=clean_env(), timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Stop("git_unavailable") from exc
    need(result.returncode in ok, "git_failed")
    return result.stdout.decode("utf-8", "replace")


def run_json(argv, cwd, timeout=HOOK_TIMEOUT, limit=MAX_HOOK_OUTPUT, env=None):
    """Run an argv (no shell) and parse bounded JSON stdout; never leak raw output."""
    need(isinstance(argv, list) and argv and all(isinstance(a, str) and a and "\x00" not in a
                                                 for a in argv), "invalid_argv")
    try:
        result = subprocess.run(argv, cwd=str(cwd), capture_output=True, timeout=timeout,
                                env=env or clean_env(), check=False, shell=False)
    except subprocess.TimeoutExpired as exc:
        raise Stop("hook_timeout") from exc
    except OSError as exc:
        raise Stop("hook_unavailable") from exc
    need(len(result.stdout) <= limit, "hook_output_too_large")
    if result.returncode != 0:
        raise Stop("hook_failed", exit=result.returncode)
    value = parse_json(result.stdout or b"{}", "hook_output_invalid")
    need(isinstance(value, dict), "hook_output_invalid")
    return value


# --------------------------------------------------------------------------- package identity


def package_identity(pin):
    """Identity of a pinned checkout, read without importing it."""
    need(Path(pin, ".git").exists(), "pin_not_git_checkout")
    head = git(pin, "rev-parse", "HEAD").strip()
    need(COMMIT.fullmatch(head), "pin_identity_invalid")
    dirty = git(pin, "status", "--porcelain", "--untracked-files=no").strip() != ""
    manifest = git(pin, "show", "HEAD:RELEASE-MANIFEST.json").encode()
    protocol = git(pin, "show", "HEAD:PROTOCOL.md").encode()
    cli = git(pin, "show", "HEAD:ai_comm.py").encode()
    version = re.findall(r'^VERSION = "([^"\r\n]+)"$', cli.decode("utf-8", "replace"), re.MULTILINE)
    origin = re.findall(r'^RELEASE_ORIGIN = "([^"\r\n]+)"$', cli.decode("utf-8", "replace"), re.MULTILINE)
    need(len(version) == 1 and VERSION_RE.fullmatch(version[0]), "pin_identity_invalid")
    return {"commit": head, "dirty": dirty, "version": version[0],
            "origin": origin[0] if len(origin) == 1 else None,
            "manifest_sha256": sha256_bytes(manifest), "protocol_sha256": sha256_bytes(protocol),
            "cli_sha256": sha256_bytes(cli)}


def tag_identity(pin, version):
    need(VERSION_RE.fullmatch(version), "invalid_target")
    tag = "v" + version
    obj = git(pin, "rev-parse", "--verify", "-q", f"refs/tags/{tag}", ok=(0, 1)).strip()
    need(COMMIT.fullmatch(obj), "upgrade_fetch_required", target=version)
    kind = git(pin, "cat-file", "-t", obj).strip()
    need(kind == "tag", "tag_not_annotated")
    commit = git(pin, "rev-parse", f"{obj}^{{commit}}").strip()
    need(COMMIT.fullmatch(commit), "tag_identity_invalid")
    manifest = git(pin, "show", f"{commit}:RELEASE-MANIFEST.json").encode()
    protocol = git(pin, "show", f"{commit}:PROTOCOL.md").encode()
    cli = git(pin, "show", f"{commit}:ai_comm.py").encode()
    message = git(pin, "cat-file", "tag", obj)
    need(sha256_bytes(manifest) in message, "tag_manifest_mismatch")
    return {"version": version, "tag": tag, "tag_object": obj, "commit": commit,
            "manifest_sha256": sha256_bytes(manifest), "protocol_sha256": sha256_bytes(protocol),
            "cli_sha256": sha256_bytes(cli)}


def release_status(pin):
    script = Path(pin) / "ai_comm.py"
    try:
        result = subprocess.run([sys.executable, "-I", str(script), "release-status"],
                                capture_output=True, timeout=120, env=clean_env(), check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Stop("release_status_unavailable") from exc
    value = parse_json(result.stdout, "release_status_invalid")
    need(isinstance(value, dict), "release_status_invalid")
    return value


def upgrade_plan(pin, target=None):
    argv = [sys.executable, "-I", str(Path(pin) / "ai_comm.py"), "upgrade-plan"]
    if target:
        argv += ["--to", target]
    try:
        result = subprocess.run(argv, capture_output=True, timeout=120, env=clean_env(), check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Stop("upgrade_plan_unavailable") from exc
    value = parse_json(result.stdout, "upgrade_plan_invalid")
    need(isinstance(value, dict), "upgrade_plan_invalid")
    return value


# --------------------------------------------------------------------------- journal


class Journal:
    """Hash-chained JSON Lines with fsync; see design §3."""

    def __init__(self, directory):
        self.dir = Path(directory)
        self.path = self.dir / "journal.jsonl"

    def _raw(self):
        return read_private(self.path, limit=64 * MAX_JSON, missing=b"")

    def read(self):
        """Return (records, torn_tail_bytes or None). Complete bad lines are corruption."""
        data = self._raw()
        records = []
        prev = None
        torn = None
        lines = data.split(b"\n")
        complete, tail = lines[:-1], lines[-1]
        if tail:
            torn = tail
        for index, line in enumerate(complete):
            try:
                record = json.loads(line.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise Stop("journal_corrupt", line=index + 1) from exc
            need(isinstance(record, dict) and record.get("v") == 1 and record.get("schema") == JOURNAL_SCHEMA
                 and record.get("seq") == index + 1 and record.get("prev") == prev
                 and record.get("kind") in {"start", "intent", "outcome", "note", "plan", "state"},
                 "journal_corrupt", line=index + 1)
            records.append(record)
            prev = sha256_bytes(line)
        return records, torn

    def repair_torn_tail(self):
        """Preserve an unterminated tail, truncate to the last complete line, chain a note."""
        records, torn = self.read()
        if torn is None:
            return records
        data = self._raw()
        keep = data[: len(data) - len(torn)]
        seq = len(records) + 1
        create_private_exclusive(self.dir / f"torn-{seq}.bin", torn)
        fd = os.open(self.path, os.O_WRONLY | os.O_NOFOLLOW)
        try:
            os.ftruncate(fd, len(keep))
            os.fsync(fd)
        finally:
            os.close(fd)
        self.append("note", {"torn_tail": f"torn-{seq}.bin", "sha256": sha256_bytes(torn)})
        records, torn = self.read()
        need(torn is None, "journal_corrupt")
        return records

    def append(self, kind, data):
        records, torn = self.read()
        need(torn is None, "journal_torn_tail")
        prev = None
        if records:
            raw = self._raw().split(b"\n")[len(records) - 1]
            prev = sha256_bytes(raw)
        record = {"v": 1, "schema": JOURNAL_SCHEMA, "seq": len(records) + 1, "prev": prev,
                  "kind": kind, "ts": now(), "data": data}
        line = json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        new = not self.path.exists()
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)
        if new:
            fsync_dir(self.dir)
        return record


# --------------------------------------------------------------------------- project manifest


def load_project_manifest(project):
    data = read_private(project.root / "project.json", missing=None)
    value = parse_json(data, "project_manifest_invalid")
    need(isinstance(value, dict) and value.get("v") == 1 and value.get("schema") == PROJECT_SCHEMA,
         "project_manifest_invalid")
    need(value.get("status") in {"declared", "unintegrated"}, "project_manifest_invalid")
    need(value.get("status") == "declared", "project_integration_undeclared")
    pid = value.get("project_id")
    live = project.project_id()
    need(pid == {"path": live["path"], "channel": live["channel"]}, "project_identity_mismatch")
    guidance = value.get("guidance", [])
    need(isinstance(guidance, list) and all(isinstance(g, str) and g for g in guidance), "project_manifest_invalid")
    integrations = value.get("integrations")
    need(isinstance(integrations, list), "project_manifest_invalid")
    seen = set()
    for item in integrations:
        need(isinstance(item, dict) and INTEGRATION_ID.fullmatch(str(item.get("id", ""))), "project_manifest_invalid")
        need(item["id"] not in seen, "project_manifest_invalid")
        seen.add(item["id"])
        need(HANDLE.fullmatch(str(item.get("owner", ""))), "project_manifest_invalid")
        supports = item.get("supports")
        need(isinstance(supports, dict), "project_manifest_invalid")
        probe = item.get("probe")
        need(isinstance(probe, dict) and isinstance(probe.get("argv"), list)
             and probe.get("run_by") in {"owner", "any"}, "project_manifest_invalid")
        steps = item.get("steps")
        need(isinstance(steps, dict) and set(steps) <= set(PHASES)
             and {"pause", "rebind", "check", "resume"} <= set(steps), "project_manifest_invalid")
        for argv in [probe["argv"], *steps.values()]:
            need(isinstance(argv, list) and argv and all(isinstance(a, str) and a for a in argv),
                 "project_manifest_invalid")
        sources = item.get("sources", {})
        need(isinstance(sources, dict), "project_manifest_invalid")
        for key, digest in sources.items():
            need(COMMIT.fullmatch(key) and HEX.fullmatch(str(digest)), "project_manifest_invalid")
        files = item.get("code_files")
        need(isinstance(files, list) and files and all(isinstance(f, str) and f for f in files),
             "project_manifest_invalid")
        source_file = item.get("source_file")
        need(source_file is None or (isinstance(source_file, str) and source_file and source_file not in files),
             "project_manifest_invalid")
        need((source_file is None) == (not sources), "project_manifest_invalid")
    return value


def integration_supports(item, from_commit, to_commit):
    supports = item["supports"]
    if supports.get("any_cli") is True:
        return True
    pairs = supports.get("pairs")
    if not isinstance(pairs, list):
        return False
    return [from_commit, to_commit] in pairs


def code_hashes(project, item):
    """Hash every file the integration runs as code (not just argv[0])."""
    hashes = {}
    for name in item["code_files"]:
        path = (project.dir / name).resolve()
        need(str(path).startswith(str(project.dir) + os.sep), "integration_path_escapes")
        try:
            hashes[name] = sha256_bytes(path.read_bytes())
        except OSError as exc:
            raise Stop("integration_code_missing", integration=item["id"]) from exc
    return hashes


def source_hash(project, item):
    """Hash of the listener source that install_source/restore_source replaces, or None."""
    name = item.get("source_file")
    if name is None:
        return None
    path = (project.dir / name).resolve()
    need(str(path).startswith(str(project.dir) + os.sep), "integration_path_escapes")
    try:
        return sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise Stop("integration_code_missing", integration=item["id"]) from exc


def probe(project, item):
    value = run_json(item["probe"]["argv"], project.dir, timeout=120)
    result = {}
    for key in ("paused", "running", "pending"):
        if key in value:
            need(isinstance(value[key], bool), "probe_invalid")
            result[key] = value[key]
    for key in ("source_sha256", "bound_cli_sha256"):
        if key in value and value[key] is not None:
            need(HEX.fullmatch(str(value[key])), "probe_invalid")
            result[key] = value[key]
    return result


# --------------------------------------------------------------------------- consent and prompts


def prompt_state(project):
    data = read_private(project.root / "prompt.json", missing=b"")
    if not data:
        return {"v": 1, "schema": PROMPT_SCHEMA, "versions": {}, "never": False}
    value = parse_json(data, "prompt_state_invalid")
    need(isinstance(value, dict) and value.get("schema") == PROMPT_SCHEMA, "prompt_state_invalid")
    return value


def should_ask(state, version, advisory, at):
    if state.get("never"):
        return advisory == "required" and not state["versions"].get(version, {}).get("required_shown")
    entry = state["versions"].get(version, {})
    if entry.get("skip"):
        return advisory == "required" and not entry.get("required_shown")
    until = entry.get("snooze_until", 0)
    return at >= until


def record_answer(project, args):
    with project.lock():
        need(args.answer in ANSWERS, "invalid_answer")
        need(VERSION_RE.fullmatch(args.target or ""), "invalid_target")
        state = prompt_state(project)
        entry = state["versions"].setdefault(args.target, {})
        at = now()
        result = {"answer": args.answer, "target": args.target}
        if args.answer == "not_now":
            level = min(entry.get("snooze_level", 0), len(SNOOZE_STEPS) - 1)
            entry["snooze_until"] = at + SNOOZE_STEPS[level]
            entry["snooze_level"] = level + 1
            result["snooze_until"] = entry["snooze_until"]
        elif args.answer == "skip":
            entry["skip"] = True
        elif args.answer == "never":
            state["never"] = True
        else:
            pin = package_identity(project.pin)
            need(not pin["dirty"], "pin_dirty")
            need(args.source and args.source == pin["origin"], "consent_source_mismatch")
            need(COMMIT.fullmatch(args.target_commit or ""), "invalid_target")
            need(args.evidence and len(args.evidence) <= 500, "consent_evidence_required")
            consent_id = "consent-" + secrets.token_hex(8)
            consent = {"v": 1, "schema": CONSENT_SCHEMA, "id": consent_id,
                       "scope": "fetch_only" if args.answer == "fetch_only" else "upgrade",
                       "project": project.project_id(), "source": args.source,
                       "from_commit": pin["commit"], "target": args.target,
                       "target_commit": args.target_commit,
                       "target_tag_object": args.target_tag_object,
                       "credential_helper": bool(args.credential_helper),
                       "created": at, "expires": at + CONSENT_TTL, "evidence": args.evidence}
            if consent["target_tag_object"] is not None:
                need(COMMIT.fullmatch(consent["target_tag_object"]), "invalid_target")
            create_private_exclusive(project.root / "consents" / f"{consent_id}.json", canonical(consent))
            result["consent"] = consent_id
        write_private_atomic(project.root / "prompt.json", canonical(state))
        return result


def load_consent(project, consent_id):
    need(CONSENT_RE.fullmatch(consent_id or ""), "consent_missing")
    consent = parse_json(read_private(project.root / "consents" / f"{consent_id}.json"), "consent_invalid")
    need(isinstance(consent, dict) and consent.get("schema") == CONSENT_SCHEMA, "consent_invalid")
    consumed = read_private(project.root / "consents" / f"{consent_id}.consumed", missing=b"")
    consent["consumed_by"] = parse_json(consumed)["txn"] if consumed else None
    return consent


def check_consent(project, consent, *, scope, target, pin, tag=None, txn=None):
    need(consent["project"] == project.project_id(), "consent_project_mismatch")
    need(consent["target"] == target, "consent_target_mismatch")
    need(consent["from_commit"] == pin["commit"], "consent_from_mismatch")
    need(consent["source"] == pin["origin"], "consent_source_mismatch")
    if scope == "upgrade":
        need(consent["scope"] == "upgrade", "consent_scope_insufficient")
    if tag is not None:
        need(consent["target_commit"] == tag["commit"], "consent_target_mismatch")
        if consent["target_tag_object"] is not None:
            need(consent["target_tag_object"] == tag["tag_object"], "consent_target_mismatch")
    if consent["consumed_by"] is not None:
        need(txn is not None and consent["consumed_by"] == txn, "consent_spent")
    else:
        need(now() < consent["expires"], "consent_expired")


def consume_consent(project, consent, txn):
    try:
        create_private_exclusive(project.root / "consents" / f"{consent['id']}.consumed",
                                 canonical({"txn": txn, "at": now()}))
    except Stop as exc:
        raise Stop("consent_spent") from exc


# --------------------------------------------------------------------------- commands: check / fetch


def cmd_check(project, args):
    with project.lock():
        return _check(project)


def _check(project):
    pin = package_identity(project.pin)
    status = release_status(project.pin)
    plan = upgrade_plan(project.pin) if status.get("status") == "ok" else {}
    state = prompt_state(project)
    version = plan.get("to") if "error" not in plan else None
    result = {"status": "ok", "pinned": pin["version"], "pin_commit": pin["commit"],
              "release_status": status.get("status"), "latest": status.get("latest_version"),
              "prompt": None}
    if status.get("status") == "index_tags_missing":
        result["prompt"] = {"should_ask": False, "blocked": "release_tags_missing_gate1_required"}
    if version and plan.get("channel_action") == "unchanged":
        advisory = (plan.get("applicable_advisory") or {}).get("advisory", "none")
        ask = should_ask(state, version, advisory, now())
        entry = state["versions"].get(version, {})
        if ask and advisory == "required" and (state.get("never") or entry.get("skip")):
            # A required notice overrides skip/never once per version; remember it was shown.
            state["versions"].setdefault(version, {})["required_shown"] = True
            write_private_atomic(project.root / "prompt.json", canonical(state))
        result["prompt"] = {
            "should_ask": ask,
            "target": version, "target_commit": plan.get("commit"), "target_tag": plan.get("tag"),
            "source": pin["origin"], "project": str(project.dir),
            "advisory": advisory, "owner_notice": plan.get("owner_notice"),
            "answers": list(ANSWERS)}
    elif version:
        result["prompt"] = {"should_ask": False, "target": version,
                            "blocked": "protocol_migration_required"}
    return result


def fetch_result_ok(returncode, value):
    """check_for_release.py contract: 0 up_to_date, 10 update_available, 11 advisory_applies."""
    return (returncode in (0, 10, 11) and isinstance(value, dict)
            and value.get("schema") == RELEASE_CHECK_SCHEMA and value.get("status") == "ok"
            and value.get("fetched") is True)


def cmd_fetch(project, args):
    with project.lock():
        pin = package_identity(project.pin)
        consent = load_consent(project, args.consent)
        check_consent(project, consent, scope="fetch_only", target=args.target, pin=pin)
        argv = [sys.executable, "-I", str(project.pin / "scripts" / "check_for_release.py"),
                "--fetch-permitted"]
        if consent["credential_helper"]:
            argv.append("--use-git-credential-helper")
        try:
            result = subprocess.run(argv, capture_output=True, timeout=600, env=clean_env(), check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Stop("fetch_failed") from exc
        value = parse_json(result.stdout, "fetch_failed")
        need(isinstance(value, dict), "fetch_failed")
        helper_error = value.get("fetch_error")
        need(fetch_result_ok(result.returncode, value), "fetch_failed",
             helper_error=helper_error if isinstance(helper_error, str) and len(helper_error) <= 64 else None)
        tag = tag_identity(project.pin, args.target)
        return {"status": "fetched", "target": args.target, "tag_object": tag["tag_object"],
                "commit": tag["commit"]}


# --------------------------------------------------------------------------- prepare / verify


def verify_target(project, tag, workdir):
    """Full verification in a private clone of the target; never touches the live pin."""
    clone = Path(workdir) / "verify"
    git(project.pin, "rev-parse", "--git-dir")
    try:
        result = subprocess.run(["git", "clone", "--quiet", "--no-checkout", "--no-hardlinks",
                                 str(project.pin), str(clone)], capture_output=True,
                                env=clean_env(), timeout=600, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Stop("verify_clone_failed") from exc
    need(result.returncode == 0, "verify_clone_failed")
    git(clone, "fetch", "--quiet", "--no-tags", str(project.pin),
        f"refs/tags/{tag['tag']}:refs/tags/{tag['tag']}")
    git(clone, "checkout", "--quiet", "--detach", tag["commit"])
    need(git(clone, "rev-parse", f"refs/tags/{tag['tag']}").strip() == tag["tag_object"], "verify_identity_changed")
    checks = {}
    for name, argv in (("check_tag", ["scripts/check_tag.py", tag["tag"]]),
                       ("check_release_manifest", ["scripts/check_release_manifest.py"])):
        try:
            done = subprocess.run([sys.executable, "-I", *argv], cwd=clone, capture_output=True,
                                  env=clean_env(), timeout=300, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Stop("verify_failed", check=name) from exc
        checks[name] = done.returncode == 0
        need(done.returncode == 0, "verify_failed", check=name)
    try:
        done = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=clone,
                              capture_output=True, env=clean_env(), timeout=TEST_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Stop("verify_failed", check="tests") from exc
    summary = re.findall(rb"^Ran ([0-9]+) tests? in", done.stderr, re.MULTILINE)
    checks["tests"] = done.returncode == 0
    need(done.returncode == 0 and summary, "verify_failed", check="tests")
    return {"commit": tag["commit"], "tag_object": tag["tag_object"], "tests_ran": int(summary[-1]),
            "checks": checks, "at": now()}


def cmd_prepare(project, args):
    with project.lock():
        need(HANDLE.fullmatch(args.as_handle or ""), "invalid_handle")
        verify_session(project, args.as_handle, args.session)
        pin = package_identity(project.pin)
        need(not pin["dirty"], "pin_dirty")
        consent = load_consent(project, args.consent)
        tag = tag_identity(project.pin, args.target)
        check_consent(project, consent, scope="upgrade", target=args.target, pin=pin, tag=tag)
        need(consent["consumed_by"] is None, "consent_spent")
        need(tag["protocol_sha256"] == pin["protocol_sha256"], "protocol_migration_required")
        manifest = load_project_manifest(project)
        integrations = []
        for item in manifest["integrations"]:
            need(integration_supports(item, pin["commit"], tag["commit"]), "upgrade_integration_unsupported",
                 integration=item["id"])
            for commit in (pin["commit"], tag["commit"]):
                if "install_source" in item["steps"] or "restore_source" in item["steps"]:
                    need(commit in item.get("sources", {}), "upgrade_integration_unsupported",
                         integration=item["id"])
            # Owner-only probes are never run by another agent (R3).
            state = probe(project, item) if (item["owner"] == args.as_handle
                                             or item["probe"]["run_by"] == "any") else None
            integrations.append({"id": item["id"], "owner": item["owner"],
                                 "run_by": item["probe"]["run_by"], "code": code_hashes(project, item),
                                 "sources": item.get("sources", {}), "initial_probe": state,
                                 "initial_source": source_hash(project, item)})
            if item.get("sources"):
                need(source_hash(project, item) == item["sources"][pin["commit"]],
                     "upgrade_identity_changed", what="integration_source", integration=item["id"])
        controller_src = git(project.pin, "show", f"{tag['commit']}:upgrade_controller.py").encode()
        manifest_raw = git(project.pin, "show", f"{tag['commit']}:RELEASE-MANIFEST.json").encode()
        entries = [e for e in parse_json(manifest_raw).get("files", []) if e.get("path") == "upgrade_controller.py"]
        need(len(entries) == 1 and entries[0].get("sha256") == sha256_bytes(controller_src),
             "controller_manifest_mismatch")
        controller_sha = sha256_bytes(controller_src)
        controller_dir = private_dir(project.root / "controller" / controller_sha)
        controller_path = controller_dir / "upgrade_controller.py"
        if controller_path.exists():
            need(sha256_bytes(read_private(controller_path)) == controller_sha, "controller_tampered")
        else:
            write_private_atomic(controller_path, controller_src)
        txn = "txn-" + secrets.token_hex(8)
        tdir = private_dir(project.root / "txn" / txn)
        verification = verify_target(project, tag, tdir)
        shutil.rmtree(tdir / "verify", ignore_errors=True)
        frozen = {"txn": txn, "project": project.project_id(), "consent": consent["id"],
                  "from": {k: pin[k] for k in ("commit", "version", "manifest_sha256", "protocol_sha256", "cli_sha256")},
                  "to": tag, "verification": verification,
                  "controller_sha256": controller_sha,
                  "project_manifest_sha256": sha256_bytes(read_private(project.root / "project.json")),
                  "integrations": integrations,
                  "applier": args.as_handle, "applier_session": args.session,
                  "invocation": [sys.executable, "-I", str(controller_path), "--project", str(project.dir)]}
        if project.channel != project.dir / ".ai-comm":
            frozen["invocation"] += ["--channel", str(project.channel)]
        journal = Journal(tdir)
        journal.append("start", frozen)
        journal.append("state", {"state": "prepared"})
        proposal = write_guidance_proposal(project, manifest, frozen, tdir)
        write_private_atomic(project.root / "current", txn.encode())
        return {"status": "prepared", "txn": txn, "verification": verification,
                "invocation": frozen["invocation"], "consent": "unconsumed",
                "guidance_proposal": str(proposal)}


def write_guidance_proposal(project, manifest, frozen, tdir):
    """Emit a reviewable guidance-change proposal for this transaction (design §6).

    It lists every line of each declared guidance file that names the from-version
    or from-commit, and carries the marker lines `finalize` checks. It does not edit
    any guidance file; the guidance owner edits, publishes and DECIDEs it.
    """
    old = frozen["from"]
    needles = [f"v{old['version']}", old["version"], old["commit"], old["commit"][:7]]
    lines = [f"# ai-comm guidance change for {frozen['txn']}", "",
             f"ai-comm-upgrade-txn: {frozen['txn']}",
             f"Replace v{old['version']} ({old['commit']}) with "
             f"v{frozen['to']['version']} ({frozen['to']['commit']}).", "",
             "Lines naming the old pin:"]
    for name in manifest.get("guidance", []):
        path = (project.dir / name).resolve()
        need(str(path).startswith(str(project.dir) + os.sep), "guidance_path_escapes")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise Stop("guidance_unreadable") from exc
        for number, line in enumerate(text.splitlines(), 1):
            if any(n in line for n in needles):
                lines.append(f"- {name}:{number}: {line.strip()[:200]}")
    lines += ["", "After editing, add the installed file's hash before publishing:",
              "ai-comm-upgrade-guidance-sha256: <sha256 of the edited guidance file>"]
    out = tdir / "guidance-proposal.md"
    write_private_atomic(out, ("\n".join(lines) + "\n").encode())
    return out


# --------------------------------------------------------------------------- transaction engine


def forward_plan(frozen):
    actions = []
    ids = [i["id"] for i in frozen["integrations"]]
    for integ in ids:
        actions.append({"op": "pause", "integration": integ})
    actions.append({"op": "checkout", "commit": frozen["to"]["commit"]})
    for integ in frozen["integrations"]:
        if frozen["to"]["commit"] in integ["sources"]:
            actions.append({"op": "install_source", "integration": integ["id"],
                            "expect_source": integ["sources"][frozen["to"]["commit"]]})
    for op in ("rebind", "check", "resume"):
        for integ in ids:
            actions.append({"op": op, "integration": integ})
    return actions


def rollback_plan(frozen, package_switched):
    actions = []
    ids = [i["id"] for i in frozen["integrations"]]
    for integ in ids:
        actions.append({"op": "pause", "integration": integ})
    if package_switched:
        actions.append({"op": "checkout", "commit": frozen["from"]["commit"]})
        for integ in frozen["integrations"]:
            if frozen["from"]["commit"] in integ["sources"]:
                actions.append({"op": "restore_source", "integration": integ["id"],
                                "expect_source": integ["sources"][frozen["from"]["commit"]]})
        for integ in ids:
            actions.append({"op": "rebind", "integration": integ})
        for integ in ids:
            actions.append({"op": "check", "integration": integ})
    for integ in ids:
        actions.append({"op": "resume", "integration": integ})
    return actions


class Txn:
    def __init__(self, project, txn):
        """Open a transaction without writing anything.

        The frozen start record is read from the complete, chain-verified prefix,
        so the actor can be validated before any repair (R3b). A torn tail is
        repaired only by `repair()`, after validation, under the lock.
        """
        need(TXN_RE.fullmatch(txn or ""), "txn_missing")
        self.project = project
        self.id = txn
        self.dir = private_dir(project.root / "txn" / txn, create=False)
        self.journal = Journal(self.dir)
        records, self.torn = self.journal.read()
        need(records and records[0]["kind"] == "start", "journal_corrupt")
        self.frozen = records[0]["data"]
        need(self.frozen.get("txn") == txn, "journal_corrupt")
        self.manifest = None

    def repair(self):
        """Preserve and chain a torn tail; call only after the actor was validated."""
        self.journal.repair_torn_tail()
        self.torn = None

    def records(self):
        records, torn = self.journal.read()
        need(torn is None, "journal_torn_tail")
        return records

    def state(self):
        last = None
        for record in self.records():
            if record["kind"] == "state":
                last = record["data"]["state"]
        return last

    def current_plan(self):
        plans = [r for r in self.records() if r["kind"] == "plan"]
        return plans[-1] if plans else None

    def progress(self, plan_seq):
        """Map action index -> last intent/outcome after the active plan record."""
        done = {}
        for record in self.records():
            if (record["seq"] <= plan_seq or record["kind"] not in {"intent", "outcome"}
                    or "index" not in record["data"]):
                continue
            idx = record["data"]["index"]
            done.setdefault(idx, {})[record["kind"]] = record["data"]
        return done

    def integration(self, integ_id):
        for item in self.frozen["integrations"]:
            if item["id"] == integ_id:
                return item
        raise Stop("journal_corrupt")

    def manifest_item(self, integ_id):
        if self.manifest is None:
            self.manifest = load_project_manifest(self.project)
        for item in self.manifest["integrations"]:
            if item["id"] == integ_id:
                return item
        raise Stop("upgrade_identity_changed", integration=integ_id)

    def check_identities(self, phase_commit, pending=None):
        """Compare live values with those expected for the current phase (design §3).

        `pending` is an action whose intent was journaled without an outcome: its
        own after-state is also acceptable until a probe resolves it.
        """
        need(self.project.project_id() == self.frozen["project"], "upgrade_identity_changed", what="project")
        need(sha256_bytes(read_private(self.project.root / "project.json"))
             == self.frozen["project_manifest_sha256"], "upgrade_identity_changed", what="project_manifest")
        tag = tag_identity(self.project.pin, self.frozen["to"]["version"])
        need(tag == self.frozen["to"], "upgrade_identity_changed", what="target_tag")
        live = package_identity(self.project.pin)
        need(not live["dirty"], "upgrade_identity_changed", what="pin_dirty")
        allowed_commits = {phase_commit}
        if pending is not None and pending["op"] == "checkout":
            allowed_commits.add(pending["commit"])
        need(live["commit"] in allowed_commits, "upgrade_identity_changed", what="pin_commit")
        for item in self.frozen["integrations"]:
            manifest_item = self.manifest_item(item["id"])
            need(code_hashes(self.project, manifest_item) == item["code"],
                 "upgrade_identity_changed", what="integration_code", integration=item["id"])
            allowed_sources = {self.expected_source(item["id"])}
            if (pending is not None and pending.get("integration") == item["id"]
                    and pending["op"] in ("install_source", "restore_source")):
                allowed_sources.add(pending["expect_source"])
            need(source_hash(self.project, manifest_item) in allowed_sources,
                 "upgrade_identity_changed", what="integration_source", integration=item["id"])

    def check_static(self):
        """Frozen project, manifest and hook-code identities, checked before any probe runs.

        The package commit is not checked here (recovery may find either side).
        Each listener source may be its expected phase value or, for an unfinished
        or failed install/restore in the active plan, that step's after-state.
        """
        need(self.project.project_id() == self.frozen["project"], "upgrade_identity_changed", what="project")
        need(sha256_bytes(read_private(self.project.root / "project.json"))
             == self.frozen["project_manifest_sha256"], "upgrade_identity_changed", what="project_manifest")
        plan = self.current_plan()
        progress = self.progress(plan["seq"]) if plan else {}
        for item in self.frozen["integrations"]:
            manifest_item = self.manifest_item(item["id"])
            need(code_hashes(self.project, manifest_item) == item["code"],
                 "upgrade_identity_changed", what="integration_code", integration=item["id"])
            allowed = {self.expected_source(item["id"])}
            if plan:
                for index, action in enumerate(plan["data"]["actions"]):
                    entry = progress.get(index, {})
                    done = (entry.get("outcome") or {}).get("ok")
                    if (not done and entry.get("intent") is not None and action.get("integration") == item["id"]
                            and action["op"] in ("install_source", "restore_source")):
                        allowed.add(action["expect_source"])
            need(source_hash(self.project, manifest_item) in allowed,
                 "upgrade_identity_changed", what="integration_source", integration=item["id"])

    def expected_source(self, integ_id):
        """Phase-specific source hash: the last confirmed install/restore, else the initial one."""
        expected = self.integration(integ_id)["initial_source"]
        plan_actions = {}
        for record in self.records():
            if record["kind"] == "plan":
                plan_actions = record["data"]["actions"]
            elif record["kind"] == "outcome" and record["data"].get("ok") and "index" in record["data"]:
                action = plan_actions[record["data"]["index"]]
                if (action.get("integration") == integ_id
                        and action["op"] in ("install_source", "restore_source")):
                    expected = action["expect_source"]
        return expected

    def expected_commit_before(self, plan, index):
        """Package commit expected before running action `index` of `plan`."""
        commit = plan["data"]["start_commit"]
        for action in plan["data"]["actions"][:index]:
            if action["op"] == "checkout":
                commit = action["commit"]
        return commit


def action_satisfied(txn, action, observed):
    op = action["op"]
    if op == "checkout":
        live = package_identity(txn.project.pin)
        return live["commit"] == action["commit"] and not live["dirty"]
    if observed is None:
        return False
    if op == "pause":
        return observed.get("paused") is True and observed.get("pending") is False
    if op in ("install_source", "restore_source"):
        item = txn.manifest_item(action["integration"])
        return (observed.get("source_sha256") == action["expect_source"]
                and source_hash(txn.project, item) == action["expect_source"])
    if op == "rebind":
        return observed.get("bound_cli_sha256") == package_identity(txn.project.pin)["cli_sha256"]
    if op == "resume":
        return observed.get("paused") is False and observed.get("running") is True
    return False


def perform_own(txn, action):
    op = action["op"]
    if op == "checkout":
        git(txn.project.pin, "checkout", "--quiet", "--detach", action["commit"])
        return {}
    item = txn.manifest_item(action["integration"])
    argv = item["steps"].get(op)
    need(argv is not None, "upgrade_integration_unsupported", integration=item["id"], phase=op)
    run_json(argv, txn.project.dir)
    return probe(txn.project, item)


def advance(txn, applier):
    """Run actions until done, a peer phase is pending, or a stop. Returns status dict."""
    plan = txn.current_plan()
    need(plan is not None, "journal_corrupt")
    actions = plan["data"]["actions"]
    progress = txn.progress(plan["seq"])
    for index, action in enumerate(actions):
        entry = progress.get(index, {})
        outcome = entry.get("outcome")
        if outcome and outcome.get("ok"):
            continue
        intent = entry.get("intent")
        txn.check_identities(txn.expected_commit_before(plan, index),
                             pending=action if intent is not None else None)
        integ = txn.integration(action["integration"]) if "integration" in action else None
        owner = integ["owner"] if integ else applier
        if intent is not None and outcome is None:
            # Unknown outcome: resolve only by probe.
            observed = None
            if action["op"] != "checkout" and integ and (owner == applier or integ["run_by"] == "any"):
                observed = probe(txn.project, txn.manifest_item(integ["id"]))
            if action["op"] == "checkout" or observed is not None:
                if action_satisfied(txn, action, observed):
                    txn.journal.append("outcome", {"index": index, "ok": True, "by": "probe",
                                                   "observed": observed})
                    continue
            if owner != applier:
                return {"status": "awaiting_peer", "integration": integ["id"], "phase": action["op"],
                        "owner": owner, "nonce": intent.get("nonce"), "index": index}
        if plan["data"]["direction"] == "forward":
            ensure_consent(txn)
        if owner != applier:
            if intent is None:
                nonce = secrets.token_hex(16)
                txn.journal.append("intent", {"index": index, "action": action, "owner": owner,
                                              "nonce": nonce})
            else:
                nonce = intent.get("nonce")
            txn.journal.append("state", {"state": f"awaiting_peer:{integ['id']}:{action['op']}"})
            return {"status": "awaiting_peer", "integration": integ["id"], "phase": action["op"],
                    "owner": owner, "nonce": nonce, "index": index}
        txn.journal.append("intent", {"index": index, "action": action, "owner": owner})
        try:
            observed = perform_own(txn, action)
        except Stop as exc:
            txn.journal.append("outcome", {"index": index, "ok": False, "code": exc.code})
            txn.journal.append("state", {"state": f"stopped:{exc.code}"})
            raise
        ok = True if action["op"] == "check" else action_satisfied(txn, action, observed)
        txn.journal.append("outcome", {"index": index, "ok": ok, "observed": observed})
        if not ok:
            txn.journal.append("state", {"state": "stopped:postcondition_failed"})
            raise Stop("postcondition_failed", index=index, op=action["op"])
        if action["op"] == "checkout":
            txn.journal.append("state", {"state": "package_switched"})
    final = "integrated_pending_liveness" if plan["data"]["direction"] == "forward" else "rolled_back"
    if final == "integrated_pending_liveness":
        live = package_identity(txn.project.pin)
        need(live["commit"] == txn.frozen["to"]["commit"], "postcondition_failed")
        require_release_status(txn.project.pin, txn.frozen["to"]["version"])
    txn.journal.append("state", {"state": final})
    return {"status": final, "txn": txn.id}


def ensure_consent(txn):
    """Validate and atomically consume the transaction's consent before any live action (R2).

    Bound to the transaction, not to an action index or owner order; a resumed
    transaction reuses only its own consumed consent.
    """
    consent = load_consent(txn.project, txn.frozen["consent"])
    if consent["consumed_by"] is None:
        pin = package_identity(txn.project.pin)
        need(pin["commit"] == txn.frozen["from"]["commit"], "consent_from_mismatch")
        check_consent(txn.project, consent, scope="upgrade", target=txn.frozen["to"]["version"],
                      pin=pin, tag=txn.frozen["to"])
        consume_consent(txn.project, consent, txn.id)
        consent = load_consent(txn.project, txn.frozen["consent"])
    need(consent["consumed_by"] == txn.id, "consent_spent")


def require_applier(project, txn, args):
    """The applier handle and the session frozen at prepare, still current (R3a).

    Checked before any journal write or effect. A same-handle takeover is not a
    transfer of this transaction's approval: it stops with
    `applier_session_changed`, and the transaction and pin stay unchanged.
    """
    need(args.as_handle == txn.frozen["applier"], "not_applier")
    need(args.session == txn.frozen["applier_session"], "applier_session_changed")
    verify_session(project, args.as_handle, args.session)


def require_participant(project, txn, args):
    """The applier (frozen session) or a declared integration owner (current session)."""
    if args.as_handle == txn.frozen["applier"]:
        require_applier(project, txn, args)
        return
    need(any(i["owner"] == args.as_handle for i in txn.frozen["integrations"]), "not_participant")
    need(args.session, "session_required")
    verify_session(project, args.as_handle, args.session)


def require_release_status(pin, version):
    """One status policy for apply and finalize: verified metadata naming the target as pinned.

    `index_tags_missing` is accepted only because newer unrelated tags may be absent
    locally; missing, invalid or inconsistent metadata is refused (R6.1).
    """
    status = release_status(pin)
    need(status.get("status") in {"ok", "index_tags_missing"}
         and status.get("pinned_version") == version, "postcondition_failed", what="release_status")


def open_txn(project, txn_id):
    if txn_id is None:
        txn_id = read_private(project.root / "current", missing=b"").decode() or None
    return Txn(project, txn_id)


def cmd_apply(project, args):
    with project.lock():
        txn = open_txn(project, args.txn)
        require_applier(project, txn, args)
        txn.repair()
        state = txn.state()
        need(state not in {"completed", "rolled_back", "integrated_pending_liveness", "awaiting_attestation"},
             "transaction_finished")
        if txn.current_plan() is None:
            txn.journal.append("plan", {"direction": "forward", "start_commit": txn.frozen["from"]["commit"],
                                        "actions": forward_plan(txn.frozen)})
        return advance(txn, args.as_handle)


def cmd_continue(project, args):
    return cmd_apply(project, args)


def cmd_rollback(project, args):
    with project.lock():
        txn = open_txn(project, args.txn)
        require_applier(project, txn, args)
        txn.repair()
        state = txn.state()
        need(state != "rolled_back", "transaction_finished")
        if state == "completed":
            # Only the project's latest transaction, still in its target state (R4).
            current = read_private(project.root / "current", missing=b"").decode()
            need(current == txn.id, "transaction_stale")
            txn.check_identities(txn.frozen["to"]["commit"])
        plan = txn.current_plan()
        if plan is None or plan["data"]["direction"] == "forward":
            if plan is not None:
                reconcile_outstanding(txn, plan, args.as_handle)
            live = package_identity(txn.project.pin)
            switched = live["commit"] != txn.frozen["from"]["commit"]
            txn.journal.append("state", {"state": "rolling_back"})
            txn.journal.append("plan", {"direction": "rollback", "start_commit": live["commit"],
                                        "actions": rollback_plan(txn.frozen, switched)})
        return advance(txn, args.as_handle)


def reconcile_outstanding(txn, plan, applier):
    """Resolve every intent without an outcome by probe before changing direction (R5)."""
    txn.check_static()
    progress = txn.progress(plan["seq"])
    for index, action in enumerate(plan["data"]["actions"]):
        entry = progress.get(index, {})
        outcome = entry.get("outcome")
        # Unfinished or failed steps may still have taken effect (a hook can write
        # and then exit non-zero): resolve both by probe, never by exit code (R5).
        if entry.get("intent") is None or (outcome is not None and outcome.get("ok")):
            continue
        if action["op"] == "checkout":
            ok = action_satisfied(txn, action, None)
            txn.journal.append("outcome", {"index": index, "ok": ok, "by": "probe", "observed": {}})
            continue
        integ = txn.integration(action["integration"])
        if integ["owner"] != applier and integ["run_by"] != "any":
            raise Stop("peer_reconcile_required", integration=integ["id"], phase=action["op"],
                       owner=integ["owner"], nonce=entry["intent"].get("nonce"))
        observed = probe(txn.project, txn.manifest_item(integ["id"]))
        ok = action["op"] != "check" and action_satisfied(txn, action, observed)
        txn.journal.append("outcome", {"index": index, "ok": ok, "by": "probe", "observed": observed})


def cmd_report(project, args):
    """A peer runs its own phase in its own session and records the outcome with the nonce."""
    with project.lock():
        txn = open_txn(project, args.txn)
        need(HANDLE.fullmatch(args.as_handle or ""), "invalid_handle")
        verify_session(project, args.as_handle, args.session)
        need(any(i["id"] == args.integration and i["owner"] == args.as_handle
                 for i in txn.frozen["integrations"]), "not_integration_owner")
        txn.repair()
        if args.phase == "attest":
            return report_attestation(project, txn, args)
        plan = txn.current_plan()
        need(plan is not None, "no_pending_phase")
        if plan["data"]["direction"] == "forward":
            need(load_consent(project, txn.frozen["consent"])["consumed_by"] == txn.id, "consent_not_consumed")
        progress = txn.progress(plan["seq"])
        for index, action in enumerate(plan["data"]["actions"]):
            entry = progress.get(index, {})
            if entry.get("outcome", {}).get("ok"):
                continue
            intent = entry.get("intent")
            need(intent is not None and intent.get("nonce"), "no_pending_phase")
            need(intent["nonce"] == args.nonce, "stale_report")
            need(action.get("integration") == args.integration and action["op"] == args.phase, "stale_report")
            integ = txn.integration(args.integration)
            need(integ["owner"] == args.as_handle, "not_integration_owner")
            txn.check_identities(txn.expected_commit_before(plan, index), pending=action)
            item = txn.manifest_item(args.integration)
            observed = probe(project, item)
            if not action_satisfied(txn, action, observed) and action["op"] != "check":
                run_json(item["steps"][action["op"]], project.dir)
                observed = probe(project, item)
            elif action["op"] == "check":
                run_json(item["steps"]["check"], project.dir)
            ok = True if action["op"] == "check" else action_satisfied(txn, action, observed)
            txn.journal.append("outcome", {"index": index, "ok": ok, "nonce": args.nonce,
                                           "by": args.as_handle, "session": args.session,
                                           "observed": observed})
            need(ok, "postcondition_failed", index=index, op=action["op"])
            return {"status": "reported", "txn": txn.id, "integration": args.integration,
                    "phase": args.phase, "observed": observed}
        raise Stop("no_pending_phase")


def channel_cli(project, *args):
    argv = [sys.executable, "-I", str(project.pin / "ai_comm.py"), "--channel", str(project.channel), *args]
    try:
        result = subprocess.run(argv, capture_output=True, timeout=120, env=clean_env(), check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Stop("channel_unavailable") from exc
    need(result.returncode == 0, "channel_unavailable")
    return result.stdout


def verify_session(project, handle, session):
    state = parse_json(channel_cli(project, "state"), "channel_unavailable")
    participants = [p for p in state.get("participants", []) if p.get("handle") == handle]
    need(len(participants) == 1 and participants[0].get("session") == session, "session_not_current")


def decision_binds(project, decision_id, txn_id):
    need(MESSAGE_ID.fullmatch(decision_id or ""), "decision_invalid")
    message = parse_json(channel_cli(project, "show", decision_id), "decision_invalid")
    message = message.get("message", message)
    need(message.get("act") == "DECIDE" and HEX.fullmatch(str(message.get("artifact", ""))), "decision_invalid")
    artifact = channel_cli(project, "show-artifact", message["artifact"])
    need(f"ai-comm-upgrade-txn: {txn_id}".encode() in artifact, "decision_not_for_txn")
    return artifact


def integration_final_ok(txn, item_id, observed):
    frozen = txn.integration(item_id)
    expected_source = frozen["sources"].get(txn.frozen["to"]["commit"])
    return (observed.get("paused") is False and observed.get("running") is True
            and observed.get("bound_cli_sha256") == txn.frozen["to"]["cli_sha256"]
            and (expected_source is None or observed.get("source_sha256") == expected_source))


def attestations(txn):
    """Latest peer attestation intents/outcomes since the transaction became ready."""
    out = {}
    for record in txn.records():
        if record["kind"] == "state" and record["data"]["state"] == "integrated_pending_liveness":
            out = {}  # a new readiness epoch invalidates earlier evidence
            continue
        if "attest" in record.get("data", {}):
            out.setdefault(record["data"]["attest"], {})[record["kind"]] = dict(record["data"], ts=record["ts"])
    return out


def report_attestation(project, txn, args):
    need(txn.state() == "awaiting_attestation", "no_pending_phase")
    entry = attestations(txn).get(args.integration, {})
    intent = entry.get("intent")
    need(intent is not None, "no_pending_phase")
    need(intent["nonce"] == args.nonce, "stale_report")
    need(txn.integration(args.integration)["owner"] == args.as_handle, "not_integration_owner")
    txn.check_identities(txn.frozen["to"]["commit"])
    observed = probe(project, txn.manifest_item(args.integration))
    ok = integration_final_ok(txn, args.integration, observed)
    txn.journal.append("outcome", {"attest": args.integration, "ok": ok, "nonce": args.nonce,
                                   "by": args.as_handle, "session": args.session, "observed": observed})
    need(ok, "postcondition_failed", integration=args.integration, phase="attest")
    return {"status": "reported", "txn": txn.id, "integration": args.integration, "phase": "attest"}


def cmd_finalize(project, args):
    with project.lock():
        txn = open_txn(project, args.txn)
        require_applier(project, txn, args)
        txn.repair()
        need(txn.state() in {"integrated_pending_liveness", "awaiting_attestation"}, "not_ready_to_finalize")
        # Re-verify the live state under the lock (R6): a decision is not a substitute.
        txn.check_identities(txn.frozen["to"]["commit"])
        require_release_status(project.pin, txn.frozen["to"]["version"])
        pending = []
        current = attestations(txn)
        for item in txn.frozen["integrations"]:
            if item["owner"] == args.as_handle or item["run_by"] == "any":
                observed = probe(project, txn.manifest_item(item["id"]))
                need(integration_final_ok(txn, item["id"], observed), "postcondition_failed",
                     integration=item["id"])
                continue
            outcome = current.get(item["id"], {}).get("outcome")
            fresh = outcome is not None and outcome.get("ok") and now() - outcome["ts"] <= ATTEST_MAX_AGE
            if not fresh:
                intent = current.get(item["id"], {}).get("intent")
                if intent is None or outcome is not None:
                    nonce = secrets.token_hex(16)
                    txn.journal.append("intent", {"attest": item["id"], "nonce": nonce, "owner": item["owner"]})
                else:
                    nonce = intent["nonce"]
                pending.append({"integration": item["id"], "owner": item["owner"], "phase": "attest",
                                "nonce": nonce})
        if pending:
            if txn.state() != "awaiting_attestation":
                txn.journal.append("state", {"state": "awaiting_attestation"})
            return {"status": "awaiting_peer", "txn": txn.id, "attestations": pending}
        decision_binds(project, args.liveness_decision, txn.id)
        guidance = decision_binds(project, args.guidance_decision, txn.id)
        match = re.search(rb"ai-comm-upgrade-guidance-sha256: ([0-9a-f]{64})", guidance)
        need(match, "guidance_hash_missing")
        path = (project.dir / args.guidance_file).resolve()
        need(str(path).startswith(str(project.dir) + os.sep), "guidance_path_escapes")
        need(sha256_bytes(path.read_bytes()) == match.group(1).decode(), "guidance_not_installed")
        txn.journal.append("note", {"liveness_decision": args.liveness_decision,
                                    "guidance_decision": args.guidance_decision})
        txn.journal.append("state", {"state": "completed"})
        return {"status": "completed", "txn": txn.id}


def cmd_status(project, args):
    project.runtime()
    current = read_private(project.root / "current", missing=b"").decode() or None
    out = {"status": "ok", "current_txn": current, "pin": package_identity(project.pin)}
    if current:
        txn = Txn(project, current)
        need(txn.torn is None, "journal_torn_tail")
        out["state"] = txn.state()
        out["invocation"] = txn.frozen["invocation"]
        out["from"] = txn.frozen["from"]["version"]
        out["to"] = txn.frozen["to"]["version"]
    return out


def cmd_recover(project, args):
    """Validate the actor, repair a torn tail, then report observed state honestly.

    Owner-only probes run only for the validated owner's own integrations.
    """
    with project.lock():
        txn = open_txn(project, args.txn)
        require_participant(project, txn, args)
        txn.repair()
        txn.check_static()
        observed = {}
        for item in txn.frozen["integrations"]:
            if item["owner"] == args.as_handle or item["run_by"] == "any":
                try:
                    state = probe(project, txn.manifest_item(item["id"]))
                    if state.get("paused") is True:
                        observed[item["id"]] = "paused"
                    elif state.get("bound_cli_sha256") == txn.frozen["to"]["cli_sha256"]:
                        observed[item["id"]] = "running_target"
                    elif state.get("bound_cli_sha256") == txn.frozen["from"]["cli_sha256"]:
                        observed[item["id"]] = "running_from"
                    else:
                        observed[item["id"]] = "unknown"
                except Stop:
                    observed[item["id"]] = "unknown"
            else:
                observed[item["id"]] = "owner_unavailable_to_probe"
        live = package_identity(project.pin)
        package = ("target" if live["commit"] == txn.frozen["to"]["commit"] else
                   "from" if live["commit"] == txn.frozen["from"]["commit"] else "unknown")
        return {"status": "recovery_report", "txn": txn.id, "state": txn.state(), "package": package,
                "pin_dirty": live["dirty"], "integrations": observed,
                "next": "run apply/continue to finish forward, or rollback; peers must run report"}


# --------------------------------------------------------------------------- cli


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", required=True)
    parser.add_argument("--channel")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    p = sub.add_parser("record-answer")
    p.add_argument("--answer", required=True, choices=ANSWERS)
    p.add_argument("--target", required=True)
    p.add_argument("--target-commit")
    p.add_argument("--target-tag-object")
    p.add_argument("--source")
    p.add_argument("--credential-helper", action="store_true")
    p.add_argument("--evidence")
    p = sub.add_parser("fetch")
    p.add_argument("--target", required=True)
    p.add_argument("--consent", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--target", required=True)
    p.add_argument("--consent", required=True)
    p.add_argument("--as", dest="as_handle", required=True)
    p.add_argument("--session", required=True)
    for name in ("apply", "continue", "rollback", "recover"):
        p = sub.add_parser(name)
        p.add_argument("--txn")
        p.add_argument("--as", dest="as_handle", required=True)
        p.add_argument("--session", required=True)
    p = sub.add_parser("report")
    p.add_argument("--txn")
    p.add_argument("--integration", required=True)
    p.add_argument("--phase", required=True, choices=REPORT_PHASES)
    p.add_argument("--nonce", required=True)
    p.add_argument("--as", dest="as_handle", required=True)
    p.add_argument("--session", required=True)
    p = sub.add_parser("finalize")
    p.add_argument("--txn")
    p.add_argument("--as", dest="as_handle", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--liveness-decision", required=True)
    p.add_argument("--guidance-decision", required=True)
    p.add_argument("--guidance-file", required=True)
    sub.add_parser("status")
    return parser


COMMANDS = {"check": cmd_check, "record-answer": record_answer, "fetch": cmd_fetch,
            "prepare": cmd_prepare, "apply": cmd_apply, "continue": cmd_continue,
            "rollback": cmd_rollback, "recover": cmd_recover, "report": cmd_report,
            "finalize": cmd_finalize, "status": cmd_status}


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
    except SystemExit:
        print(json.dumps({"status": "stopped", "code": "invalid_arguments"}))
        return 2
    try:
        project = Project(args.project, args.channel)
        result = COMMANDS[args.command](project, args)
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
