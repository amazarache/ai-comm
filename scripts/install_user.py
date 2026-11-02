#!/usr/bin/env python3
"""Per-user ai-comm cache (design 018c v3, sections 3, 4 and 10).

Keeps verified, immutable release checkouts under $XDG_DATA_HOME/ai-comm (default
~/.local/share/ai-comm) with `current` and `previous` links. Runs only from an already
verified package; fetches only with --fetch-permitted (the owner's direct consent).
Never touches a project pin, channel, declaration, listener or guidance. JSON output;
exit 0 on success, 2 on any stop.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import sys
import time

sys.dont_write_bytecode = True


def _shared():
    """Load the shared installer module once per process (by path: `python3 -I` does not
    put this directory on sys.path), so every installer sees the same Stop class."""
    name = "ai_comm_release_install"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / "release_install.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


ri = _shared()
uc, Stop, need = ri.uc, ri.Stop, ri.need

STATE_SCHEMA = "ai-comm-user-cache/1"
OP_SCHEMA = "ai-comm-user-op/1"
LINK_RE = re.compile(r"releases/([0-9]+\.[0-9]+\.[0-9]+)\Z")
OP_DIR_RE = re.compile(r"(install|activate|check|uninstall)-[0-9a-f]{12}\Z")
TORN_RE = re.compile(r"torn-[0-9]+\.bin\Z")
TRASH_RE = re.compile(r"trash-[0-9]+\Z")
# Operations whose unfinished journal blocks every new mutation until `recover`.
BLOCKING_OPS = {"install", "activate", "uninstall"}
RESOLVED = {"installed", "done", "superseded", "removed", "abandoned"}
TOP_LEVEL = {"releases", "staging", "journal", "state.json", "bindings.json", "index.json", "offer.json",
             "lock", "current", "previous"}
BINDING_KEYS = ("version", "tag", "tag_object", "commit", "manifest_sha256", "protocol_sha256")


# --------------------------------------------------------------------------- layout


def cache_root(env=None):
    env = os.environ if env is None else env
    base = env.get("XDG_DATA_HOME") or os.path.join(env.get("HOME", ""), ".local", "share")
    need(os.path.isabs(base), "cache_path_invalid")
    return Path(os.path.abspath(base)) / "ai-comm"


class Cache:
    def __init__(self, root):
        self.root = Path(root)

    def open(self, create):
        parent = self.root.parent
        if not create and not os.path.lexists(self.root):
            raise Stop("cache_missing")
        if create:
            os.makedirs(parent, mode=0o700, exist_ok=True)
        uc.check_ancestry(parent)
        uc.private_dir(self.root, create=create)
        if create:
            for name in ("releases", "staging", "journal"):
                uc.private_dir(self.root / name)
        return self

    @contextlib.contextmanager
    def lock(self):
        fd = os.open(self.root / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Stop("cache_locked") from exc
            yield
        finally:
            os.close(fd)

    # -- private JSON files

    def _read(self, name, default):
        data = uc.read_private(self.root / name, missing=b"")
        if not data:
            return default
        value = uc.parse_json(data, "cache_state_invalid")
        need(isinstance(value, dict) and value.get("v") == 1, "cache_state_invalid", file=name)
        return value

    def _write(self, name, value):
        uc.write_private_atomic(self.root / name, uc.canonical(value))

    def state(self):
        value = self._read("state.json", {"v": 1, "schema": STATE_SCHEMA, "releases": {}, "owned": []})
        need(value.get("schema") == STATE_SCHEMA and isinstance(value.get("releases"), dict)
             and isinstance(value.get("owned"), list), "cache_state_invalid", file="state.json")
        return value

    def write_state(self, value):
        self._write("state.json", value)

    def bindings(self):
        value = self._read("bindings.json", {"v": 1, "bindings": {}})
        need(isinstance(value.get("bindings"), dict), "cache_state_invalid", file="bindings.json")
        return value

    def write_bindings(self, value):
        self._write("bindings.json", value)

    def index(self):
        return self._read("index.json", None)

    def write_index(self, releases, refreshed_at):
        self._write("index.json", {"v": 1, "refreshed_at": refreshed_at, "releases": releases})

    # -- links

    def read_link(self, name):
        """The version a link names, or None. Anything else stops: a link must be
        `releases/<version>` pointing at a real directory recorded in state.json."""
        path = self.root / name
        if not os.path.lexists(path):
            return None
        need(path.is_symlink(), "cache_link_invalid", link=name)
        match = LINK_RE.fullmatch(os.readlink(path))
        need(match, "cache_link_invalid", link=name)
        version = match.group(1)
        target = self.root / "releases" / version
        st = os.lstat(target) if os.path.lexists(target) else None
        need(st is not None and stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode)
             and st.st_uid == os.getuid(), "cache_link_invalid", link=name)
        need(version in self.state()["releases"], "cache_link_invalid", link=name)
        return version

    def set_link(self, name, version):
        root_fd = ri.open_dir_nofollow(str(self.root))
        try:
            tmp = f".{name}-{secrets.token_hex(6)}"
            if version is None:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(name, dir_fd=root_fd)
            else:
                os.symlink(f"releases/{version}", tmp, dir_fd=root_fd)
                os.rename(tmp, name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
            os.fsync(root_fd)
        finally:
            os.close(root_fd)

    def release_dir(self, version):
        return self.root / "releases" / version

    def check_release(self, version):
        """A cached release is still the verified one: recorded, real directory owned by
        this user, and exactly the recorded commit's files (release_install.verify_tree)."""
        record = self.state()["releases"].get(version)
        need(record is not None, "release_not_installed", version=version)
        path = self.release_dir(version)
        st = os.lstat(path) if os.path.lexists(path) else None
        need(st is not None and stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode)
             and st.st_uid == os.getuid(), "release_identity_changed", version=version)
        try:
            ri.verify_tree(path, record["commit"], record["manifest_sha256"])
        except Stop as exc:
            raise Stop("release_identity_changed", version=version, what=exc.details.get("what"),
                       path=exc.details.get("path")) from exc
        return path

    def op_journal(self, op, start):
        uc.private_dir(self.root / "journal")  # a partial uninstall may have removed the empty parent
        directory = uc.private_dir(self.root / "journal" / f"{op}-{secrets.token_hex(6)}")
        journal = uc.Journal(directory)
        journal.append("start", {"schema": OP_SCHEMA, "op": op, **start})
        return journal


def binding_from_tag(tag, record):
    """A trusted binding: the fetched annotated tag agrees with the index record."""
    need(tag["commit"] == record["commit"] and tag["manifest_sha256"] == record["manifest_sha256"]
         and tag["protocol_sha256"] == record["protocol_sha256"], "index_tag_mismatch", version=tag["version"])
    return {"version": tag["version"], "tag": tag["tag"], "tag_object": tag["tag_object"],
            "commit": tag["commit"], "manifest_sha256": tag["manifest_sha256"],
            "protocol_sha256": tag["protocol_sha256"]}


def binding_agrees(binding, record):
    """A stored binding still describes exactly this index record."""
    return (isinstance(binding, dict) and isinstance(record, dict) and binding.get("tag") == record.get("tag")
            and all(binding.get(k) == record.get(k) for k in ("commit", "manifest_sha256", "protocol_sha256")))


def index_records(repo):
    raw = ri.git(repo, "show", "refs/remotes/origin/release-index:RELEASES.json").encode()
    index = uc.parse_json(raw, "release_index_invalid")
    need(isinstance(index, dict) and index.get("format") == "ai-comm-releases/1"
         and isinstance(index.get("releases"), list), "release_index_invalid")
    out = []
    for record in index["releases"]:
        if (isinstance(record, dict) and isinstance(record.get("package_version"), str)
                and uc.VERSION_RE.fullmatch(record["package_version"])
                and all(isinstance(record.get(k), str) for k in ("commit", "tag", "manifest_sha256", "protocol_sha256"))):
            out.append({k: record[k] for k in ("package_version", "commit", "tag", "manifest_sha256",
                                               "protocol_sha256", "advisory") if k in record})
    return out


def version_key(version):
    return tuple(int(part) for part in version.split("."))


# --------------------------------------------------------------------------- operations


def _switch(cache, journal, current, previous):
    """Point `previous` then `current`, each step journaled (section 10.3)."""
    journal.append("intent", {"index": 0, "action": {"op": "link", "name": "previous", "version": previous}})
    cache.set_link("previous", previous)
    journal.append("outcome", {"index": 0, "ok": True})
    journal.append("intent", {"index": 1, "action": {"op": "link", "name": "current", "version": current}})
    cache.set_link("current", current)
    journal.append("outcome", {"index": 1, "ok": True})
    journal.append("state", {"state": "done"})


def staging_identity(cache, staging):
    def check(repo, tag):
        uc.private_dir(staging, create=False)
        st = os.lstat(repo)
        need(stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode) and st.st_uid == os.getuid(),
             "unsafe_path", path="staging/repo")
        ri.clean_tree_identity(repo, tag)
    return check


def publish_release(cache, staging, version):
    root_fd = ri.open_dir_nofollow(str(cache.root))
    try:
        rel_fd = ri.open_dir_nofollow("releases", root_fd)
        stage_parent = ri.open_dir_nofollow("staging", root_fd)
        stage_fd = ri.open_dir_nofollow(staging.name, stage_parent)
        try:
            try:
                os.lstat(version, dir_fd=rel_fd)
                raise Stop("release_installed", version=version)
            except FileNotFoundError:
                pass
            src = os.lstat("repo", dir_fd=stage_fd)
            need(stat.S_ISDIR(src.st_mode) and not stat.S_ISLNK(src.st_mode) and src.st_uid == os.getuid(),
                 "unsafe_path", path="staging/repo")
            os.rename("repo", version, src_dir_fd=stage_fd, dst_dir_fd=rel_fd)
            os.fsync(rel_fd)
            os.fsync(stage_fd)
        finally:
            os.close(stage_fd)
            os.close(stage_parent)
            os.close(rel_fd)
    finally:
        os.close(root_fd)


def install(version, *, fetch_permitted, use_helper, activate=None, root=None, source=None, fetch=None,
            run_suite=ri.real_suite):
    need(uc.VERSION_RE.fullmatch(version or ""), "invalid_version")
    need(fetch_permitted, "fetch_not_permitted")
    src = source or ri.source_identity()
    cache = Cache(root or cache_root()).open(create=True)
    with cache.lock():
        _gate(cache)
        need(version not in cache.state()["releases"] and not os.path.lexists(cache.release_dir(version)),
             "release_installed", version=version)
        staging = uc.private_dir(cache.root / "staging" / f"install-{secrets.token_hex(6)}")
        journal = uc.Journal(staging)
        journal.append("start", {"schema": OP_SCHEMA, "op": "install", "version": version,
                                 "source": src["origin"], "source_kind": src["kind"]})
        repo = staging / "repo"
        try:
            ri.git(staging, "init", "--quiet", "--template=", str(repo))
            journal.append("intent", {"index": 0, "action": {"op": "fetch"}})
            (fetch or ri.real_fetch)(repo, src["origin"], version, use_helper)
            record = ri.select_record(repo, version)
            journal.append("note", {"record": record})  # lets recovery identify the staged checkout
            if src["kind"] == "archive" and src["version"] == version:
                # The bridge from a downloaded archive to the cache's Git checkout (850.1):
                # archive, tag and index must name the same manifest.
                need(src["manifest_sha256"] == record["manifest_sha256"], "archive_manifest_mismatch")
            verified = ri.verify_staging(repo, version, record, run_suite, staging_identity(cache, staging))
            binding = binding_from_tag(verified["tag"], record)
            journal.append("outcome", {"index": 0, "ok": True, "record": record,
                                       "tests_ran": verified["tests_ran"]})
            staging_identity(cache, staging)(repo, verified["tag"])
        except Stop:
            # A handled refusal before publishing leaves nothing behind. An interruption
            # (signal, crash) keeps the journal for `recover`.
            shutil.rmtree(staging, ignore_errors=True)
            raise
        old, before_previous = cache.read_link("current"), cache.read_link("previous")
        if activate is None:
            activate = old is None
        if activate:
            # Recorded before publishing, so recovery knows the switch was intended and
            # from which exact state.
            journal.append("note", {"activate": version, "previous": old, "before_previous": before_previous})
        journal.append("intent", {"index": 1, "action": {"op": "publish"}, "commit": record["commit"]})
        publish_release(cache, staging, version)
        journal.append("outcome", {"index": 1, "ok": True})
        _record_release(cache, version, verified["tag"], binding, cache.release_dir(version))
        if activate:
            _switch(cache, journal, version, old)
        journal.append("state", {"state": "installed"})
        return {"status": "installed", "version": version, "commit": record["commit"],
                "release": str(cache.release_dir(version)), "tests_ran": verified["tests_ran"],
                "current": cache.read_link("current"), "previous": cache.read_link("previous")}


def _record_release(cache, version, tag, binding, path):
    state = cache.state()
    state["releases"][version] = {"commit": tag["commit"], "tag": tag["tag"], "tag_object": tag["tag_object"],
                                  "manifest_sha256": tag["manifest_sha256"],
                                  "protocol_sha256": tag["protocol_sha256"], "cli_sha256": tag["cli_sha256"]}
    bindings = cache.bindings()
    bindings["bindings"][version] = binding
    cache.write_bindings(bindings)
    if cache.index() is None:
        cache.write_index(index_records(path), None)
    cache.write_state(state)  # state last (section 10.3)


def activate(version, *, root=None):
    need(uc.VERSION_RE.fullmatch(version or ""), "invalid_version")
    cache = Cache(root or cache_root()).open(create=False)
    with cache.lock():
        _gate(cache)
        cache.check_release(version)
        old = cache.read_link("current")
        need(old != version, "already_current", version=version)
        journal = cache.op_journal("activate", {"before": {"current": old, "previous": cache.read_link("previous")},
                                                "after": {"current": version, "previous": old}})
        _switch(cache, journal, version, old)
        return {"status": "activated", "current": version, "previous": old}


def rollback(*, root=None):
    cache = Cache(root or cache_root()).open(create=False)
    with cache.lock():
        _gate(cache)
        old, prev = cache.read_link("current"), cache.read_link("previous")
        need(prev is not None and prev != old, "nothing_to_roll_back")
        cache.check_release(prev)
        journal = cache.op_journal("activate", {"before": {"current": old, "previous": prev},
                                                "after": {"current": prev, "previous": old}})
        _switch(cache, journal, prev, old)
        return {"status": "rolled_back", "current": prev, "previous": old}


def status(*, root=None):
    cache = Cache(root or cache_root()).open(create=False)
    state, bindings, index = cache.state(), cache.bindings(), cache.index()
    return {"status": "ok", "cache": str(cache.root), "current": cache.read_link("current"),
            "previous": cache.read_link("previous"), "releases": sorted(state["releases"], key=version_key),
            "bindings": sorted(bindings["bindings"], key=version_key),
            "index_refreshed_at": index.get("refreshed_at") if index else None,
            "pending": [d.name for d in _pending(cache)],
            "unrecognized": [d.name for d in _op_dirs(cache) if d.kind == "unrecognized" or d.unexpected]}


def check(*, fetch_permitted=False, use_helper=False, root=None, source=None, fetch=None, now=time.time):
    """Offline unless --fetch-permitted: is a newer verified release known? A refresh
    fetches only the index and v* tags and builds bindings for every indexed tag."""
    need(not use_helper or fetch_permitted, "credential_helper_requires_fetch_permission")
    cache = Cache(root or cache_root()).open(create=False)
    with cache.lock():
        current = cache.read_link("current")
        refreshed = None
        if fetch_permitted:
            _gate(cache)
            src = source or ri.source_identity()
            uc.private_dir(cache.root / "staging")  # a partial uninstall may have removed the empty parent
            scratch = uc.private_dir(cache.root / "staging" / f"check-{secrets.token_hex(6)}")
            uc.Journal(scratch).append("start", {"schema": OP_SCHEMA, "op": "check"})
            try:
                repo = scratch / "repo"
                ri.git(scratch, "init", "--quiet", "--template=", str(repo))
                if fetch:
                    fetch(repo, src["origin"], use_helper)
                else:
                    ri.isolated_fetch(repo, src["origin"], use_helper, ri.release_check_module().REFSPECS)
                records = index_records(repo)
                old = cache.bindings()["bindings"]
                rebuilt = {}
                for record in records:
                    version = record["package_version"]
                    if ri.git(repo, "tag", "-l", f"v{version}").strip():
                        try:
                            rebuilt[version] = binding_from_tag(uc.tag_identity(repo, version), record)
                        except Stop:
                            pass  # the fetched tag contradicts the index: unverified
                    elif binding_agrees(old.get(version), record):
                        rebuilt[version] = old[version]  # tag not fetched now, but the new record still agrees
                # Bindings are rebuilt from this refresh only; offers re-check agreement
                # with the index too, so an interruption between the two writes is safe.
                refreshed = int(now())
                cache.write_index(records, refreshed)
                cache.write_bindings({"v": 1, "bindings": rebuilt})
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
        index = cache.index()
        bindings = cache.bindings()["bindings"]
        records = {r["package_version"]: r for r in (index or {}).get("releases", [])
                   if isinstance(r, dict) and isinstance(r.get("package_version"), str)
                   and uc.VERSION_RE.fullmatch(r["package_version"])}
        newer = [v for v in records if current is None or version_key(v) > version_key(current)]
        verified = {v for v in newer if binding_agrees(bindings.get(v), records[v])}
        verified_newer = sorted(verified, key=version_key)
        return {"status": "ok", "current": current,
                "latest_verified": verified_newer[-1] if verified_newer else current,
                "offer": bool(verified_newer),
                "unverified_newer": sorted((v for v in newer if v not in verified), key=version_key),
                "index_refreshed_at": (index or {}).get("refreshed_at"),
                "metadata": "refreshed" if refreshed else ("cached" if index else "missing")}


# --------------------------------------------------------------------------- recovery and removal


class OpDir:
    """One directory under staging/ or journal/, classified without changing it.

    kind: "op" (a valid journal this tool wrote), "empty", or "unrecognized". Whether an
    operation is pending depends only on its journal, so unexpected contents never hide
    an unfinished operation from the mutation gate. Whether its directory may be removed
    is separate: only when every entry is one its operation creates in its current
    journal state (`unexpected` is empty). `op` names the operation when the directory
    no longer carries its original name (an object moved into an uninstall's directory)."""

    def __init__(self, path, op=None):
        self.path, self.name = path, path.name
        self.kind, self.journal, self.records, self.torn, self.op = "unrecognized", None, [], None, None
        self.unexpected = []
        if op is None:
            match = OP_DIR_RE.fullmatch(self.name)
            if not match:
                return
            op = match.group(1)
        try:
            st = os.lstat(path)
        except OSError:
            return
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            return
        entries = os.listdir(path)
        if not entries:
            self.kind = "empty"
            return
        try:
            uc.private_dir(path, create=False)
            journal = uc.Journal(path)
            records, torn = journal.read()
        except Stop:
            return
        if not records or records[0]["kind"] != "start":
            return
        data = records[0]["data"]
        if data.get("schema") != OP_SCHEMA or data.get("op") != op:
            return
        self.kind, self.journal, self.records, self.torn, self.op = "op", journal, records, torn, op
        self.unexpected = sorted(e for e in entries if not _entry_expected(path, op, records, e))

    @property
    def start(self):
        return self.records[0]["data"]

    @property
    def resolved(self):
        return any(r["kind"] == "state" and r["data"].get("state") in RESOLVED for r in self.records)

    @property
    def removable(self):
        return self.kind == "op" and not self.unexpected

    @property
    def pending(self):
        return self.kind == "op" and self.op in BLOCKING_OPS and (self.torn is not None or not self.resolved)


def _states(records):
    return {r["data"].get("state") for r in records if r["kind"] == "state"}


def _published(records):
    return any(r["kind"] == "outcome" and r["data"].get("index") == 1 and r["data"].get("ok") for r in records)


def _real_dir(path):
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid()


def _entry_expected(path, op, records, entry):
    """Whether `entry` is something this operation creates, in this journal state.
    A name alone never qualifies: `repo` exists only in an unpublished install or in a
    check (whose clone never checks out files), `trash-N` only in an unfinished uninstall."""
    if entry == "journal.jsonl" or TORN_RE.fullmatch(entry):
        return True
    if entry == "repo" and op == "install":
        return _real_dir(path / entry) and not (_states(records) & RESOLVED) and not _published(records)
    if entry == "repo" and op == "check":
        return _real_dir(path / entry) and set(os.listdir(path / entry)) <= {".git"}
    if TRASH_RE.fullmatch(entry) and op == "uninstall":
        return "removed" not in _states(records)
    return False


def _op_dirs(cache):
    for parent in ("staging", "journal"):
        path = cache.root / parent
        if path.is_dir() and not path.is_symlink():
            for child in sorted(path.iterdir()):
                yield OpDir(child)


def _pending(cache):
    return [d for d in _op_dirs(cache) if d.pending]


def _gate(cache):
    """Never start a new mutation over an unfinished one."""
    pending = _pending(cache)
    need(not pending, "operation_pending", dir=pending[0].name if pending else None,
         hint="run `install.sh --user recover` first")


def _links(cache):
    try:
        return cache.read_link("current"), cache.read_link("previous")
    except Stop:
        return None


def _resume_switch(cache, journal, before, after):
    """Finish an interrupted switch only from the exact state it left behind: the
    starting links, or `previous` already moved. Any other state means something newer
    happened; the old operation is marked superseded and nothing is changed."""
    links = _links(cache)
    target = (after["current"], after["previous"])
    if links == target:
        journal.append("state", {"state": "done"})
        return "already_complete"
    if links is not None and links[0] == before["current"] and links[1] in (before["previous"], after["previous"]):
        for version in target:
            if version is not None:
                cache.check_release(version)
        _switch(cache, journal, *target)
        return "completed_switch"
    journal.append("state", {"state": "superseded", "links": list(links) if links else None})
    return "superseded"


def recover(*, root=None):
    cache = Cache(root or cache_root()).open(create=False)
    results = []
    with cache.lock():
        for item in list(_op_dirs(cache)):
            if not os.path.lexists(item.path):
                continue  # removed by an uninstall completed earlier in this loop
            if item.kind == "empty":
                os.rmdir(item.path)
                results.append({"dir": item.name, "action": "removed_empty"})
                continue
            if item.kind != "op":
                results.append({"dir": item.name, "action": "skipped_unrecognized"})
                continue
            if item.torn is not None:
                item.journal.repair_torn_tail()
                item = OpDir(item.path)
            if item.op == "check":
                if item.removable:
                    shutil.rmtree(item.path)
                    results.append({"dir": item.name, "action": "removed_check_scratch"})
                else:
                    results.append({"dir": item.name, "action": "kept_unexpected", "entries": item.unexpected})
                continue
            if item.resolved:
                continue
            if item.op == "install":
                results.append(_recover_install(cache, item))
            elif item.op == "activate":
                results.append({"dir": item.name,
                                "action": _resume_switch(cache, item.journal, item.start["before"],
                                                         item.start["after"])})
            else:
                out = _uninstall_continue(cache, item)
                results.append({"dir": item.name, "action": "completed_uninstall", "kept": out["kept"]})
    _finish_root(cache, results)
    return {"status": "recovery_report", "results": results}


def _recover_install(cache, item):
    directory, journal, records, start = item.path, item.journal, item.records, item.start
    version = start["version"]
    publish = [r for r in records if r["kind"] == "intent" and r["data"]["action"]["op"] == "publish"]
    release = cache.release_dir(version)
    if not publish:
        return _remove_unpublished(item)
    record = next(r["data"]["record"] for r in records if r["kind"] == "outcome" and r["data"]["index"] == 0)
    if os.path.lexists(release) and not release.is_symlink() and release.is_dir():
        try:
            ri.verify_tree(release, record["commit"], record["manifest_sha256"])
        except Stop:
            return {"dir": directory.name, "action": "report_only",
                    "detail": "release does not match the verified install; inspect manually"}
        if version not in cache.state()["releases"]:
            tag = uc.tag_identity(release, version)
            _record_release(cache, version, tag, binding_from_tag(tag, record), release)
        action = "completed_after_publish"
        note = [r["data"] for r in records if r["kind"] == "note" and "activate" in r["data"]]
        if note:
            switched = _resume_switch(cache, journal,
                                      {"current": note[0]["previous"], "previous": note[0].get("before_previous")},
                                      {"current": note[0]["activate"], "previous": note[0]["previous"]})
            if switched == "superseded":
                action = "completed_after_publish_switch_superseded"
        journal.append("state", {"state": "installed"})
        return {"dir": directory.name, "action": action}
    if (directory / "repo").is_dir() and not os.path.lexists(release):
        return _remove_unpublished(item)
    return {"dir": directory.name, "action": "report_only",
            "detail": "release does not match the verified install; inspect manually"}


def _remove_unpublished(item):
    """Remove an interrupted, unpublished install only when its staged checkout is
    provably the installer's own: an empty clone (only `.git`), or exactly the tree of
    the journaled record (plus bytecode an interrupted test run leaves). Otherwise the
    operation is resolved as abandoned and its directory is kept and reported."""
    repo = item.path / "repo"
    if item.unexpected:
        return _abandon(item, "unexpected entries in the operation directory")
    if os.path.lexists(repo):
        records = [r["data"]["record"] for r in item.records if r["kind"] == "note" and "record" in r["data"]]
        ok = _real_dir(repo) and set(os.listdir(repo)) <= {".git"}
        if not ok and records:
            try:
                ri.verify_tree(repo, records[0]["commit"], records[0]["manifest_sha256"], allow_bytecode=True)
                ok = True
            except Stop:
                ok = False
        if not ok:
            return _abandon(item, "staging checkout has unexpected contents")
    shutil.rmtree(item.path)
    return {"dir": item.name, "action": "removed_unpublished"}


def _abandon(item, why):
    """Nothing was published, but the staging cannot be proven to be only the
    installer's own: resolve the operation (it no longer blocks) and keep everything."""
    item.journal.append("state", {"state": "abandoned", "reason": why})
    return {"dir": item.name, "action": "kept_for_inspection", "detail": why}


def uninstall(*, confirm, root=None):
    need(confirm, "confirmation_required")
    cache = Cache(root or cache_root()).open(create=False)
    with cache.lock():
        need(not cache.state()["owned"], "owned_objects_present")  # skill links and launcher: checkpoint (b)
        pending = _pending(cache)
        others = [d for d in pending if d.op != "uninstall"]
        need(not others, "operation_pending", dir=others[0].name if others else None,
             hint="run `install.sh --user recover` first")
        if pending:
            item = pending[0]
            if item.torn is not None:
                item.journal.repair_torn_tail()
                item = OpDir(item.path)
        else:
            journal = cache.op_journal("uninstall", {})
            item = OpDir(journal.dir)
        out = _uninstall_continue(cache, item)
    _finish_root(cache, [out])
    return {"status": "uninstalled" if not out["kept"] else "partially_uninstalled", "removed": out["removed"],
            "kept": out["kept"]}


def _uninstall_continue(cache, item):
    """Idempotent: every removal first moves the object into this operation's own
    directory (journaled), so an interruption leaves only objects this tool owns there.
    Only recorded, exactly verified releases and recognized operation directories are
    removed; anything unexpected or changed is kept and reported."""
    op_dir, journal = item.path, item.journal
    kept, removed = set(), []
    counter = [len(_trash_intents(journal))]

    def to_trash(path, label, identity):
        counter[0] += 1
        name = f"trash-{counter[0]}"
        journal.append("intent", {"index": counter[0],
                                  "action": {"op": "trash", "path": label, "as": name, "identity": identity}})
        os.rename(path, op_dir / name)
        journal.append("outcome", {"index": counter[0], "ok": True})
        removed.append(label)

    for entry in sorted(os.listdir(cache.root)):
        path = cache.root / entry
        if re.fullmatch(r"\.(current|previous)-[0-9a-f]{12}", entry) and path.is_symlink() \
                and LINK_RE.fullmatch(os.readlink(path)):
            os.unlink(path)  # a temporary link from an interrupted switch
        elif entry not in TOP_LEVEL:
            kept.add(entry)
    for name in ("current", "previous"):
        path = cache.root / name
        if os.path.lexists(path):
            if path.is_symlink() and LINK_RE.fullmatch(os.readlink(path)):
                os.unlink(path)
                removed.append(name)
            else:
                kept.add(name)
    releases = cache.root / "releases"
    if releases.is_dir() and not releases.is_symlink():
        for child in sorted(releases.iterdir()):
            try:
                cache.check_release(child.name)
            except Stop:
                kept.add(f"releases/{child.name}")
                continue
            record = cache.state()["releases"][child.name]
            to_trash(child, f"releases/{child.name}",
                     {"kind": "release", "commit": record["commit"], "manifest_sha256": record["manifest_sha256"]})
            state = cache.state()
            state["releases"].pop(child.name, None)
            cache.write_state(state)
    for parent in ("staging", "journal"):
        path = cache.root / parent
        if not path.is_dir() or path.is_symlink():
            continue
        for child in sorted(path.iterdir()):
            if child == op_dir:
                continue
            other = OpDir(child)
            if other.kind == "empty":
                os.rmdir(child)
            elif other.removable and not other.pending:
                to_trash(child, f"{parent}/{child.name}", {"kind": "op", "op": other.op})
            else:
                kept.add(f"{parent}/{child.name}")
    for name in ("bindings.json", "index.json", "offer.json", "state.json"):  # state last
        path = cache.root / name
        if os.path.lexists(path):
            if path.is_file() and not path.is_symlink():
                os.unlink(path)
                removed.append(name)
            else:
                kept.add(name)
    intents = _trash_intents(journal)
    for entry in sorted(os.listdir(op_dir)):
        if not TRASH_RE.fullmatch(entry):
            continue
        action = intents.get(entry)
        if action is not None and _trash_still_owned(op_dir / entry, action["identity"]):
            shutil.rmtree(op_dir / entry)
            continue
        # Changed after it was moved here: put it back where it came from and keep it;
        # if that path is taken, keep it here, inside this operation's directory.
        original = cache.root / action["path"] if action is not None else None
        if original is not None and not os.path.lexists(original) and _real_dir(original.parent):
            journal.append("note", {"restored": entry, "to": action["path"]})
            os.rename(op_dir / entry, original)
            kept.add(action["path"])
        else:
            kept.add(f"journal/{op_dir.name}/{entry}")
        if action is not None and action["path"] in removed:
            removed.remove(action["path"])
    leftovers = [e for e in os.listdir(op_dir) if e != "journal.jsonl" and not TORN_RE.fullmatch(e)]
    journal.append("state", {"state": "removed", "kept": sorted(kept | {f"journal/{op_dir.name}/{e}" for e in leftovers})})
    if leftovers:
        # Something here is not accounted for: keep the directory and its journal, which
        # explains it. The operation is resolved and no longer blocks anything.
        kept.update(f"journal/{op_dir.name}/{e}" for e in leftovers)
    else:
        for entry in sorted(os.listdir(op_dir), key=lambda e: e == "journal.jsonl"):  # journal last
            os.unlink(op_dir / entry)
        os.rmdir(op_dir)
    for name in ("releases", "staging", "journal"):
        with contextlib.suppress(OSError):
            os.rmdir(cache.root / name)
    return {"removed": removed, "kept": sorted(kept)}


def _trash_intents(journal):
    records, _ = journal.read()
    return {r["data"]["action"]["as"]: r["data"]["action"] for r in records
            if r["kind"] == "intent" and r["data"].get("action", {}).get("op") == "trash"}


def _trash_still_owned(path, identity):
    """Revalidate a moved object against the identity journaled when it was moved."""
    if identity.get("kind") == "release":
        try:
            ri.verify_tree(path, identity["commit"], identity["manifest_sha256"])
            return True
        except Stop:
            return False
    if identity.get("kind") == "op":
        moved = OpDir(path, op=identity.get("op"))
        return moved.removable and not moved.pending
    return False


def _finish_root(cache, results):
    """After a complete uninstall, remove the lock and the empty cache directory."""
    if not any(r.get("action") == "completed_uninstall" or "removed" in r for r in results):
        return
    if set(os.listdir(cache.root)) <= {"lock"}:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(cache.root / "lock")
        with contextlib.suppress(OSError):
            os.rmdir(cache.root)


# --------------------------------------------------------------------------- CLI


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("install")
    p.add_argument("--version", required=True)
    p.add_argument("--fetch-permitted", action="store_true")
    p.add_argument("--use-git-credential-helper", action="store_true")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--activate", dest="activate", action="store_true", default=None)
    group.add_argument("--no-activate", dest="activate", action="store_false")
    p = sub.add_parser("activate")
    p.add_argument("version")
    sub.add_parser("rollback")
    sub.add_parser("status")
    p = sub.add_parser("check")
    p.add_argument("--fetch-permitted", action="store_true")
    p.add_argument("--use-git-credential-helper", action="store_true")
    sub.add_parser("recover")
    p = sub.add_parser("uninstall")
    p.add_argument("--confirm", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        print(json.dumps({"status": "stopped", "code": "invalid_arguments"}))
        return 2
    try:
        if args.command == "install":
            need(not args.use_git_credential_helper or args.fetch_permitted,
                 "credential_helper_requires_fetch_permission")
            result = install(args.version, fetch_permitted=args.fetch_permitted,
                             use_helper=args.use_git_credential_helper, activate=args.activate)
        elif args.command == "activate":
            result = activate(args.version)
        elif args.command == "rollback":
            result = rollback()
        elif args.command == "status":
            result = status()
        elif args.command == "check":
            result = check(fetch_permitted=args.fetch_permitted, use_helper=args.use_git_credential_helper)
        elif args.command == "recover":
            result = recover()
        else:
            result = uninstall(confirm=args.confirm)
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
