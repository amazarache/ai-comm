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
OP_DIR_RE = re.compile(r"(install|activate|check|uninstall|register|unregister|launcher)-[0-9a-f]{12}\Z")
TORN_RE = re.compile(r"torn-[0-9]+\.bin\Z")
TRASH_RE = re.compile(r"trash-[0-9]+\Z")
# Operations whose unfinished journal blocks every new mutation until `recover`.
BLOCKING_OPS = {"install", "activate", "uninstall", "register", "unregister", "launcher"}
RESOLVED = {"installed", "done", "superseded", "removed", "abandoned"}
TOP_LEVEL = {"releases", "staging", "journal", "state.json", "bindings.json", "index.json", "offer.json",
             "refresh.json", "lock", "current", "previous"}
OFFER_SCHEMA = "ai-comm-user-offer/1"
CLAIM_TTL = 24 * 3600           # an older notifier claim is abandoned (section 8.3)
STALE_AFTER = 7 * 24 * 3600     # cached release metadata older than this is stale
FUTURE_SKEW = 300               # a refresh time this far in the future is stale (clock skew)
SESSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
MAX_SKILL_FILE = 64 * 1024
MAX_DIR_ENTRIES = 10000
ENTRY_SKILL = ("entry", "ai-comm", "SKILL.md")
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

    def refresh(self):
        return self._read("refresh.json", None)

    def write_refresh(self, attempted_at, ok, code=None):
        self._write("refresh.json", {"v": 1, "attempted_at": attempted_at, "ok": ok, "code": code})

    def offer(self):
        value = self._read("offer.json", {"v": 1, "schema": OFFER_SCHEMA, "versions": {}, "never": False,
                                          "claim": None})
        need(value.get("schema") == OFFER_SCHEMA and isinstance(value.get("versions"), dict),
             "cache_state_invalid", file="offer.json")
        return value

    def write_offer(self, value):
        self._write("offer.json", value)

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
        _require_origin(cache, src["origin"])
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
            try:
                _require_owned_support(cache, version, release=repo)
            except Stop:
                shutil.rmtree(staging, ignore_errors=True)  # nothing published yet
                raise
            # Recorded before publishing, so recovery knows the switch was intended and
            # from which exact state.
            journal.append("note", {"activate": version, "previous": old, "before_previous": before_previous})
        journal.append("intent", {"index": 1, "action": {"op": "publish"}, "commit": record["commit"]})
        publish_release(cache, staging, version)
        journal.append("outcome", {"index": 1, "ok": True})
        _record_release(cache, version, verified["tag"], binding, cache.release_dir(version), src["origin"])
        if activate:
            _switch(cache, journal, version, old)
        journal.append("state", {"state": "installed"})
        return {"status": "installed", "version": version, "commit": record["commit"],
                "release": str(cache.release_dir(version)), "tests_ran": verified["tests_ran"],
                "current": cache.read_link("current"), "previous": cache.read_link("previous")}


def _require_origin(cache, origin):
    """The cache follows one release origin, recorded at its first install; a
    different one stops (section 8.3)."""
    recorded = cache.state().get("origin")
    need(recorded is None or recorded == origin, "origin_changed", recorded=recorded, origin=origin)


def _record_release(cache, version, tag, binding, path, origin):
    state = cache.state()
    state.setdefault("origin", origin)
    state["releases"][version] = {"commit": tag["commit"], "tag": tag["tag"], "tag_object": tag["tag_object"],
                                  "manifest_sha256": tag["manifest_sha256"],
                                  "protocol_sha256": tag["protocol_sha256"], "cli_sha256": tag["cli_sha256"]}
    bindings = cache.bindings()
    bindings["bindings"][version] = binding
    cache.write_bindings(bindings)
    if cache.index() is None:
        # The installer fetched this index from the origin just now.
        cache.write_index(index_records(path), int(time.time()))
    cache.write_state(state)  # state last (section 10.3)


def _require_owned_support(cache, version, release=None):
    """Registered skill links and the launcher reach `current`; never switch it to a
    release that lacks what they point at (releases before the entry skill existed)."""
    owned = {o.get("kind") for o in cache.state()["owned"] if isinstance(o, dict)}
    release = release or cache.release_dir(version)
    if "skill_link" in owned:
        need((release / Path(*ENTRY_SKILL)).is_file(), "release_has_no_entry_skill", version=version,
             hint="unregister the skill first, or choose a release that provides it")
    if "launcher" in owned:
        need((release / "scripts" / "project_cli.py").is_file(), "release_has_no_launcher", version=version,
             hint="remove the launcher first, or choose a release that provides it")


def activate(version, *, root=None):
    need(uc.VERSION_RE.fullmatch(version or ""), "invalid_version")
    cache = Cache(root or cache_root()).open(create=False)
    with cache.lock():
        _gate(cache)
        cache.check_release(version)
        old = cache.read_link("current")
        need(old != version, "already_current", version=version)
        _require_owned_support(cache, version)
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
        _require_owned_support(cache, prev)
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
            "metadata": _freshness(cache, int(time.time())), "origin": state.get("origin"),
            "owned": [{"kind": o.get("kind"), "path": o.get("path")} for o in state["owned"]],
            "pending": [d.name for d in _pending(cache)],
            "unrecognized": [d.name for d in _op_dirs(cache) if d.kind == "unrecognized" or d.unexpected]}


def check(*, fetch_permitted=False, use_helper=False, root=None, source=None, fetch=None, now=time.time):
    """Offline unless --fetch-permitted: is a newer verified release known? A refresh
    fetches only the index and v* tags and builds bindings for every indexed tag."""
    need(not use_helper or fetch_permitted, "credential_helper_requires_fetch_permission")
    cache = Cache(root or cache_root()).open(create=False)
    with cache.lock():
        refreshed = False
        if fetch_permitted:
            _gate(cache)
            src = source or ri.source_identity()
            _require_origin(cache, src["origin"])
            _refresh(cache, src, use_helper, fetch, int(now()))
            refreshed = True
        return _evaluate(cache, int(now()), refreshed)


def _refresh(cache, src, use_helper, fetch, at):
    uc.private_dir(cache.root / "staging")  # a partial uninstall may have removed the empty parent
    scratch = uc.private_dir(cache.root / "staging" / f"check-{secrets.token_hex(6)}")
    uc.Journal(scratch).append("start", {"schema": OP_SCHEMA, "op": "check"})
    try:
        repo = scratch / "repo"
        ri.git(scratch, "init", "--quiet", "--template=", str(repo))
        try:
            if fetch:
                fetch(repo, src["origin"], use_helper)
            else:
                ri.isolated_fetch(repo, src["origin"], use_helper, ri.release_check_module().REFSPECS)
            records = index_records(repo)
        except Stop as exc:
            cache.write_refresh(at, False, exc.code)  # later offline checks report the failure
            raise
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
        cache.write_index(records, at)
        cache.write_bindings({"v": 1, "bindings": rebuilt})
        cache.write_refresh(at, True)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _freshness(cache, at):
    """How far cached release metadata can be trusted to be current: `cached`
    (refreshed within STALE_AFTER), `stale` (older, never dated, or dated in the
    future), `failed` (the last refresh attempt failed) or `missing`."""
    index = cache.index()
    if index is None:
        return "missing"
    refreshed = index.get("refreshed_at")
    if not isinstance(refreshed, int) or isinstance(refreshed, bool):
        return "stale"
    attempt = cache.refresh()
    if attempt and attempt.get("ok") is False and isinstance(attempt.get("attempted_at"), int) \
            and attempt["attempted_at"] >= refreshed:
        return "failed"
    if refreshed > at + FUTURE_SKEW or at - refreshed > STALE_AFTER:
        return "stale"
    return "cached"


def _evaluate(cache, at, refreshed=False):
    current = cache.read_link("current")
    index = cache.index()
    bindings = cache.bindings()["bindings"]
    records = {r["package_version"]: r for r in (index or {}).get("releases", [])
               if isinstance(r, dict) and isinstance(r.get("package_version"), str)
               and uc.VERSION_RE.fullmatch(r["package_version"])}
    newer = [v for v in records if current is None or version_key(v) > version_key(current)]
    verified = {v for v in newer if binding_agrees(bindings.get(v), records[v])}
    verified_newer = sorted(verified, key=version_key)
    unverified = sorted((v for v in newer if v not in verified), key=version_key)
    metadata = "refreshed" if refreshed else _freshness(cache, at)
    if verified_newer:
        verdict = "update_available"
    elif metadata in ("refreshed", "cached") and not unverified and current is not None:
        verdict = "up_to_date"
    else:
        verdict = "unknown"  # never claim to be current on missing, stale or failed metadata
    return {"status": "ok", "current": current,
            "latest_verified": verified_newer[-1] if verified_newer else current,
            "offer": bool(verified_newer), "unverified_newer": unverified,
            "index_refreshed_at": (index or {}).get("refreshed_at"), "metadata": metadata,
            "verdict": verdict,
            "advisory": records[verified_newer[-1]].get("advisory", "optional") if verified_newer else None}


# --------------------------------------------------------------------------- owned objects outside the cache
#
# Skill links and the launcher live in directories the user names. state.json
# records each one with its exact link target or content digest; only an object that
# still matches is ever removed (section 10.2). Containing directories are never
# created or removed, and links are never followed recursively.


def _user_dir(path, code):
    """A directory the user named: absolute, existing, a real directory, safe ancestors."""
    need(path and os.path.isabs(path), code, path=path)
    directory = Path(os.path.abspath(path))
    try:
        st = os.lstat(directory)
    except OSError as exc:
        raise Stop(code, path=str(directory)) from exc
    need(stat.S_ISDIR(st.st_mode), code, path=str(directory))
    uc.check_ancestry(directory)
    return directory


UNSUPPORTED_NAME = "<unsupported front matter>"  # never a valid skill name; treated as a conflict


def _yaml_scalar(text):
    """The value of a one-line YAML scalar, or UNSUPPORTED_NAME for anything this
    bounded parser does not handle exactly (block, flow, tagged, anchored or aliased
    values, unknown escapes, text after a closing quote other than a comment)."""
    text = text.strip()
    if not text or text.startswith("#"):
        return None
    if text[0] in "|>[{&*!%@`":
        return UNSUPPORTED_NAME
    if text[0] == "'":
        out, i = [], 1
        while i < len(text):
            if text[i] == "'":
                if text[i + 1:i + 2] == "'":
                    out.append("'")
                    i += 2
                    continue
                rest = text[i + 1:]
                return "".join(out) if not rest.strip() or re.match(r"\s+#", rest) else UNSUPPORTED_NAME
            out.append(text[i])
            i += 1
        return UNSUPPORTED_NAME
    if text[0] == '"':
        out, i = [], 1
        while i < len(text):
            char = text[i]
            if char == "\\":
                nxt = text[i + 1:i + 2]
                if nxt not in ('"', "\\"):
                    return UNSUPPORTED_NAME
                out.append(nxt)
                i += 2
                continue
            if char == '"':
                rest = text[i + 1:]
                return "".join(out) if not rest.strip() or re.match(r"\s+#", rest) else UNSUPPORTED_NAME
            out.append(char)
            i += 1
        return UNSUPPORTED_NAME
    value = re.split(r"\s+#", text, maxsplit=1)[0].strip()  # a comment needs whitespace before `#`
    if re.search(r":(\s|\Z)", value):
        return UNSUPPORTED_NAME  # `a: b` is not a plain scalar
    return value or None


ROOT_KEY = re.compile(r"""(?:"([^"\\]*)"|'([^']*)'|([A-Za-z0-9_][A-Za-z0-9_.-]*))[ ]*:(?:[ ]+(.*)|)\Z""")


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def frontmatter_name(data):
    """The root mapping's `name` in a SKILL.md YAML front matter block: the name hosts
    would see. None when there is no front matter or the root mapping has no name;
    UNSUPPORTED_NAME whenever the block cannot be read exactly (registration then
    treats the entry as a conflict, so an unread name can never become a duplicate).

    The root mapping's indentation is that of its first content line (it need not
    be column zero). Only lines at exactly that indentation are root keys; deeper
    lines belong to the key before them (a nested mapping, or a continued value).
    Tabs in indentation, a line indented less than the root, a root line that is
    not a simple `key:` entry, a `name` whose value continues on further lines, or
    more than one root `name`, are all unsupported."""
    lines = data.decode("utf-8", "replace").splitlines()
    if not lines or lines[0].rstrip() != "---":
        # Not plain front matter. If a marker follows a byte-order mark, blank, comment
        # or directive lines, or the opener carries more text, a YAML reader might
        # still find a name there: unsupported rather than "no name".
        for line in lines:
            text = line.lstrip("\ufeff")
            if text.startswith("---"):
                return UNSUPPORTED_NAME
            if text.strip() and not text.lstrip().startswith(("#", "%")):
                break
        return None
    body = []
    for line in lines[1:]:
        # Only a marker in column zero ends the block; an indented `---` or `...` is
        # content of a scalar. A column-zero marker followed by more text is unsupported.
        if re.match(r"(?:---|\.\.\.)(?:\s|\Z)", line):
            if line.rstrip() not in ("---", "..."):
                return UNSUPPORTED_NAME
            break
        body.append(line)
    else:
        return None if _root_name(body) is None else UNSUPPORTED_NAME  # unterminated block
    return _root_name(body)


def _spans_lines(value):
    """A root value that opens a quoted scalar or a flow collection without closing
    it on its own line: its content continues on lines this reader does not track."""
    value = value.strip()
    if value[:1] in ("'", '"'):
        return _yaml_scalar(value) == UNSUPPORTED_NAME and value.count(value[0]) < 2
    if value[:1] in ("[", "{"):
        return value.count("[") + value.count("{") != value.count("]") + value.count("}")
    return False


def _root_name(body):
    content = [line for line in body if line.strip() and not line.lstrip().startswith("#")]
    if not content:
        return None
    if any("\t" in line[:len(line) - len(line.lstrip())] for line in content):
        return UNSUPPORTED_NAME
    root = _indent(content[0])
    found = []
    for number, line in enumerate(content):
        indent = _indent(line)
        if indent < root:
            return UNSUPPORTED_NAME
        if indent > root:
            continue  # part of the previous root key's value
        match = ROOT_KEY.match(line[root:])
        if not match or _spans_lines(match.group(4) or ""):
            return UNSUPPORTED_NAME
        key = next(group for group in match.groups()[:3] if group is not None)
        if key != "name":
            continue
        continued = number + 1 < len(content) and _indent(content[number + 1]) > root
        found.append(UNSUPPORTED_NAME if continued else _yaml_scalar(match.group(4) or ""))
    if len(found) > 1:
        return UNSUPPORTED_NAME
    return found[0] if found else None


def _read_bounded(path, own=False):
    """A small regular file's bytes, never through a final link and never blocking;
    None if it is anything else (or, with `own`, not this user's)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_SKILL_FILE or (own and st.st_uid != os.getuid()):
            return None
        return os.read(fd, MAX_SKILL_FILE)
    finally:
        os.close(fd)


def _read_skill_file(skill_dir):
    return _read_bounded(Path(skill_dir) / "SKILL.md")


def effective_names(directory):
    """[(entry path, effective skill name or None)] for every immediate child of
    `directory`: a directory's own SKILL.md, or for a link the SKILL.md of the
    directory it names (one level only: a link to a link is not followed). Bounded,
    inert reads; nothing found there is executed."""
    out = []
    names = sorted(os.listdir(directory))
    need(len(names) <= MAX_DIR_ENTRIES, "skills_dir_too_large", path=str(directory))
    for name in names:
        entry = Path(directory) / name
        try:
            st = os.lstat(entry)
        except OSError:
            continue
        skill_dir = None
        if stat.S_ISLNK(st.st_mode):
            target = os.readlink(entry)
            resolved = Path(os.path.normpath(target if os.path.isabs(target) else Path(directory) / target))
            try:
                if stat.S_ISDIR(os.lstat(resolved).st_mode):
                    skill_dir = resolved
            except OSError:
                pass
        elif stat.S_ISDIR(st.st_mode):
            skill_dir = entry
        else:
            continue
        data = _read_skill_file(skill_dir) if skill_dir is not None else None
        out.append((entry, frontmatter_name(data) if data else None))
    return out


def _owned(state, kind):
    return [o for o in state["owned"] if isinstance(o, dict) and o.get("kind") == kind]


def _is_owned_link(path, target):
    try:
        return stat.S_ISLNK(os.lstat(path).st_mode) and os.readlink(path) == target
    except OSError:
        return False


def _file_digest(path):
    """sha256 of a regular file owned by this user (never through a link), or None."""
    data = _read_bounded(path, own=True)
    return None if data is None else uc.sha256_bytes(data)


def _still_owned(obj):
    path = Path(obj["path"])
    if obj["kind"] == "skill_link":
        return _is_owned_link(path, obj["target"])
    if obj["kind"] == "launcher":
        return _file_digest(path) == obj["sha256"]
    return False


def _remove_owned(journal, obj, index):
    """Remove one recorded object only if it is still exactly what was recorded.
    Returns removed, absent or kept (changed, replaced or behind unsafe ancestors)."""
    path = Path(obj["path"])
    try:
        uc.check_ancestry(path.parent)
    except Stop:
        return "kept"
    if not os.path.lexists(path):
        return "absent"
    if not _still_owned(obj):
        return "kept"
    journal.append("intent", {"index": index, "action": {"op": "unlink", "path": str(path), "object": obj}})
    os.unlink(path)
    uc.fsync_dir(path.parent)
    journal.append("outcome", {"index": index, "ok": True})
    return "removed"


def _drop_owned(cache, obj):
    state = cache.state()
    state["owned"] = [o for o in state["owned"] if o != obj]
    cache.write_state(state)


def _add_owned(cache, obj):
    state = cache.state()
    if obj not in state["owned"]:
        state["owned"].append(obj)
        cache.write_state(state)


def entry_target(cache):
    return str(cache.root / "current" / Path(*ENTRY_SKILL).parent)


def register(skills_dir, *, root=None):
    """Link DIR/ai-comm to the cache's entry skill, if no other entry in DIR or in any
    registered directory already has the effective name `ai-comm` (section 8.2)."""
    cache = Cache(root or cache_root()).open(create=False)
    with cache.lock():
        _gate(cache)
        directory = _user_dir(skills_dir, "skills_dir_invalid")
        current = cache.read_link("current")
        need(current is not None, "no_current_release")
        cache.check_release(current)
        need((cache.release_dir(current) / Path(*ENTRY_SKILL)).is_file(), "release_has_no_entry_skill",
             version=current)
        target = entry_target(cache)
        path = directory / "ai-comm"
        state = cache.state()
        links = _owned(state, "skill_link")
        ours = {o["path"] for o in links if _is_owned_link(o["path"], o["target"])}
        if str(path) in ours:
            return _registered(cache, "already_registered")
        conflicts = set()
        for scanned in sorted({str(directory)} | {str(Path(o["path"]).parent) for o in links}):
            if not os.path.isdir(scanned):
                continue
            for entry, name in effective_names(scanned):
                if name in ("ai-comm", UNSUPPORTED_NAME) and str(entry) not in ours:
                    conflicts.add(str(entry))
        if os.path.lexists(path):
            conflicts.add(str(path))
        need(not conflicts, "skill_name_conflict", entries=sorted(conflicts),
             hint="nothing was changed; the listed entries are named ai-comm, occupy DIR/ai-comm, or have "
                  "front matter this check cannot read exactly; resolve or rename them, or use another directory")
        obj = {"kind": "skill_link", "path": str(path), "target": target}
        journal = cache.op_journal("register", {"object": obj})
        journal.append("intent", {"index": 0, "action": {"op": "symlink", "object": obj}})
        os.symlink(target, path)  # creates only: an entry that appeared meanwhile is never replaced
        uc.fsync_dir(directory)
        journal.append("outcome", {"index": 0, "ok": True})
        _add_owned(cache, obj)
        journal.append("state", {"state": "done"})
        return _registered(cache, "registered")


def _registered(cache, status):
    paths = sorted(o["path"] for o in _owned(cache.state(), "skill_link"))
    out = {"status": status, "registered": paths}
    if len(paths) > 1:
        out["warning"] = ("ai-comm is registered in more than one directory; a host that scans several of them "
                          "would show duplicate entries. Confirm in each host that exactly one ai-comm skill "
                          "appears, and unregister the others if not.")
    return out


def unregister(skills_dir=None, *, root=None):
    cache = Cache(root or cache_root()).open(create=False)
    with cache.lock():
        _gate(cache)
        links = _owned(cache.state(), "skill_link")
        if skills_dir is not None:
            directory = str(Path(os.path.abspath(skills_dir)))
            links = [o for o in links if str(Path(o["path"]).parent) == directory]
        need(links, "not_registered")
        journal = cache.op_journal("unregister", {"objects": links})
        return _remove_recorded(cache, journal, links)


def _remove_recorded(cache, journal, objects):
    removed, kept, absent = [], [], []
    for index, obj in enumerate(objects):
        result = _remove_owned(journal, obj, index)
        {"removed": removed, "kept": kept, "absent": absent}[result].append(obj["path"])
        _drop_owned(cache, obj)  # a changed object is no longer ours: kept, and no longer recorded
    journal.append("state", {"state": "done"})
    return {"status": "removed" if not kept else "partially_removed", "removed": removed, "kept": kept,
            "absent": absent}


def launcher_script(cache_root_path):
    """A POSIX sh launcher. It resolves `current` once, accepts only a direct
    `releases/X.Y.Z` child, and runs that release's project_cli.py, which verifies
    the project's pin before running anything from it."""
    quoted = "'" + str(cache_root_path).replace("'", "'\"'\"'") + "'"
    return (
        "#!/bin/sh\n"
        "# ai-comm launcher written by `install.sh --user launcher`: it runs the current project's own\n"
        "# verified pin. Remove it with `install.sh --user launcher --remove` or `uninstall`.\n"
        f"cache={quoted}\n"
        'link=$(readlink "$cache/current") || {\n'
        "  printf '%s\\n' '{\"status\": \"stopped\", \"code\": \"cache_current_missing\"}' >&2; exit 2; }\n"
        'case $link in\n'
        "  releases/*/*|*..*) link= ;;\n"
        "  releases/[0-9]*.[0-9]*.[0-9]*) ;;\n"
        "  *) link= ;;\n"
        "esac\n"
        '[ -n "$link" ] || {\n'
        "  printf '%s\\n' '{\"status\": \"stopped\", \"code\": \"cache_link_invalid\"}' >&2; exit 2; }\n"
        'exec python3 -I "$cache/$link/scripts/project_cli.py" "$@"\n'
    ).encode()


def install_launcher(bin_dir, *, root=None):
    cache = Cache(root or cache_root()).open(create=False)
    with cache.lock():
        _gate(cache)
        directory = _user_dir(bin_dir, "bin_dir_invalid")
        current = cache.read_link("current")
        need(current is not None, "no_current_release")
        need((cache.release_dir(current) / "scripts" / "project_cli.py").is_file(), "release_has_no_launcher",
             version=current)
        path = directory / "ai-comm"
        need(not os.path.lexists(path), "launcher_path_taken", path=str(path))
        data = launcher_script(cache.root)
        obj = {"kind": "launcher", "path": str(path), "sha256": uc.sha256_bytes(data)}
        tmp = directory / f".ai-comm-{secrets.token_hex(6)}.tmp"
        journal = cache.op_journal("launcher", {"action": "create", "object": obj, "tmp": str(tmp)})
        journal.append("intent", {"index": 0, "action": {"op": "write", "object": obj, "tmp": str(tmp)}})
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
        try:
            os.fchmod(fd, 0o755)
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(tmp, path)  # creates only: an entry that appeared meanwhile is never replaced
        except FileExistsError as exc:
            os.unlink(tmp)
            journal.append("state", {"state": "superseded"})
            raise Stop("launcher_path_taken", path=str(path)) from exc
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
        uc.fsync_dir(directory)
        journal.append("outcome", {"index": 0, "ok": True})
        _add_owned(cache, obj)
        journal.append("state", {"state": "done"})
        return {"status": "launcher_installed", "path": str(path)}


def remove_launcher(bin_dir=None, *, root=None):
    cache = Cache(root or cache_root()).open(create=False)
    with cache.lock():
        _gate(cache)
        objects = _owned(cache.state(), "launcher")
        if bin_dir is not None:
            directory = str(Path(os.path.abspath(bin_dir)))
            objects = [o for o in objects if str(Path(o["path"]).parent) == directory]
        need(objects, "no_launcher")
        journal = cache.op_journal("launcher", {"action": "remove", "objects": objects})
        return _remove_recorded(cache, journal, objects)


def _recover_owned(cache, item):
    """Finish an interrupted register, unregister or launcher operation by probing
    the object it names; never guesses (section 10.3)."""
    start = item.start
    if item.op == "unregister" or start.get("action") == "remove":
        objects = [o for o in start.get("objects", []) if o in cache.state()["owned"]]
        _remove_recorded(cache, item.journal, objects)
        return "completed_removal"
    obj = start["object"]
    if item.op == "launcher":
        tmp = Path(start["tmp"])
        expected = launcher_script(cache.root)
        if os.path.lexists(tmp) and uc.sha256_bytes(expected) == obj["sha256"]:
            data = _read_bounded(tmp, own=True)
            if data is not None and expected.startswith(data):
                os.unlink(tmp)  # this operation's own, possibly partial, temporary file
    if os.path.lexists(obj["path"]):
        if _still_owned(obj):
            _add_owned(cache, obj)
            item.journal.append("state", {"state": "done"})
            return "completed"
        _drop_owned(cache, obj)  # recorded before the interruption, but no longer ours
        item.journal.append("state", {"state": "superseded"})
        return "not_ours"  # something else is there now: kept, not recorded
    _drop_owned(cache, obj)
    item.journal.append("state", {"state": "done"})
    return "nothing_created"


# --------------------------------------------------------------------------- the cache's own update offers


def _offer_entry_suppressed(state, version, advisory, at):
    entry = state["versions"].get(version, {})
    if entry.get("accepted"):
        return "answered"
    if not uc.should_ask(state, version, advisory, at):
        return "never" if state.get("never") else ("skipped" if entry.get("skip") else "snoozed")
    return None


def offer_status(*, root=None, now=time.time):
    """Read-only: the pending cache offer, if any, and who is its notifier."""
    cache = Cache(root or cache_root()).open(create=False)
    at = int(now())
    evaluation = _evaluate(cache, at)
    state = cache.offer()
    out = {"status": "ok", "evaluation": evaluation, "claim": state.get("claim"), "never": state.get("never")}
    if evaluation["offer"]:
        out["suppressed"] = _offer_entry_suppressed(state, evaluation["latest_verified"],
                                                    evaluation["advisory"], at)
    return out


def _claim_valid(claim, version, at):
    return (isinstance(claim, dict) and claim.get("version") == version and isinstance(claim.get("at"), int)
            and 0 <= at - claim["at"] < CLAIM_TTL)


def offer_claim(session, *, root=None, now=time.time):
    """Make one session the notifier of the pending offer, so concurrent and
    sequential sessions do not repeat it. A claim older than 24 hours, or for an
    older offered version, is abandoned and may be re-claimed."""
    need(SESSION_RE.fullmatch(session or ""), "invalid_session")
    cache = Cache(root or cache_root()).open(create=False)
    with cache.lock():
        at = int(now())
        evaluation = _evaluate(cache, at)
        if not evaluation["offer"]:
            return {"status": "no_offer", "verdict": evaluation["verdict"], "metadata": evaluation["metadata"]}
        version = evaluation["latest_verified"]
        state = cache.offer()
        why = _offer_entry_suppressed(state, version, evaluation["advisory"], at)
        if why:
            return {"status": "suppressed", "version": version, "reason": why}
        claim = state.get("claim")
        if _claim_valid(claim, version, at):
            if claim.get("session") == session:
                return {"status": "claimed", "version": version, "answers": list(uc.ANSWERS)}
            return {"status": "claimed_by_other", "version": version}
        state["claim"] = {"session": session, "version": version, "at": at}
        cache.write_offer(state)
        return {"status": "claimed", "version": version, "current": evaluation["current"],
                "answers": list(uc.ANSWERS)}


def offer_answer(session, version, answer, *, root=None, now=time.time):
    """Record the owner's direct answer, relayed by the notifier session. `fetch_only`
    permits `install --version V --fetch-permitted --no-activate`; `yes` also permits
    `activate V`. Neither is consent for anything else; no project pin changes."""
    need(SESSION_RE.fullmatch(session or ""), "invalid_session")
    need(uc.VERSION_RE.fullmatch(version or ""), "invalid_version")
    need(answer in uc.ANSWERS, "invalid_answer")
    cache = Cache(root or cache_root()).open(create=False)
    with cache.lock():
        at = int(now())
        state = cache.offer()
        claim = state.get("claim")
        need(_claim_valid(claim, version, at) and claim.get("session") == session, "offer_not_claimed")
        evaluation = _evaluate(cache, at)
        need(evaluation["offer"] and evaluation["latest_verified"] == version, "offer_changed")
        entry = state["versions"].setdefault(version, {})
        result = {"status": "answered", "version": version, "answer": answer, "permits": []}
        if answer == "not_now":
            level = min(entry.get("snooze_level", 0), len(uc.SNOOZE_STEPS) - 1)
            entry["snooze_until"] = at + uc.SNOOZE_STEPS[level]
            entry["snooze_level"] = level + 1
            result["snooze_until"] = entry["snooze_until"]
        elif answer == "skip":
            entry["skip"] = True
        elif answer == "never":
            state["never"] = True
        else:
            entry["accepted"] = answer
            result["permits"] = [f"install --version {version} --fetch-permitted --no-activate"]
            if answer == "yes":
                result["permits"].append(f"activate {version}")
        entry["answered_at"] = at
        state["claim"] = None
        cache.write_offer(state)
        return result


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
            elif item.op in ("register", "unregister", "launcher"):
                results.append({"dir": item.name, "action": _recover_owned(cache, item)})
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
            _record_release(cache, version, tag, binding_from_tag(tag, record), release, start["source"])
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

    # Owned objects outside the cache first, each only if still exactly as recorded.
    for number, obj in enumerate(list(cache.state()["owned"])):
        result = _remove_owned(journal, obj, f"owned-{number}") if obj.get("kind") in (
            "skill_link", "launcher") else "kept"
        if result == "removed":
            removed.append(obj["path"])
        elif result == "kept":
            kept.add(obj.get("path", "?"))
        _drop_owned(cache, obj)
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
    # Every state file the layout defines (TOP_LEVEL), state.json last.
    for name in sorted(n for n in TOP_LEVEL if n.endswith(".json") and n != "state.json") + ["state.json"]:
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
    p = sub.add_parser("register", help="link SKILLS_DIR/ai-comm to the cache's entry skill")
    p.add_argument("--skills-dir", required=True)
    p = sub.add_parser("unregister")
    p.add_argument("--skills-dir")
    p = sub.add_parser("launcher", help="write or remove the optional `ai-comm` command")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--bin-dir")
    group.add_argument("--remove", action="store_true")
    p.add_argument("--remove-from", dest="remove_from", help="with --remove: only the launcher in this directory")
    p = sub.add_parser("offer", help="the cache's own update offer")
    offer = p.add_subparsers(dest="offer_command", required=True)
    offer.add_parser("status")
    q = offer.add_parser("claim")
    q.add_argument("--session", required=True)
    q = offer.add_parser("answer")
    q.add_argument("--session", required=True)
    q.add_argument("--version", required=True)
    q.add_argument("--answer", required=True, choices=uc.ANSWERS)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code == 0:
            return 0  # --help
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
        elif args.command == "register":
            result = register(args.skills_dir)
        elif args.command == "unregister":
            result = unregister(args.skills_dir)
        elif args.command == "launcher":
            result = remove_launcher(args.remove_from) if args.remove else install_launcher(args.bin_dir)
        elif args.command == "offer":
            if args.offer_command == "status":
                result = offer_status()
            elif args.offer_command == "claim":
                result = offer_claim(args.session)
            else:
                result = offer_answer(args.session, args.version, args.answer)
        else:
            result = uninstall(confirm=args.confirm)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Stop as exc:
        safe = {k: v for k, v in exc.details.items() if isinstance(v, (str, int, bool)) or v is None
                or (isinstance(v, list) and all(isinstance(i, str) for i in v))}
        print(json.dumps({"status": "stopped", "code": exc.code, **safe}, sort_keys=True))
        return 2
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"status": "stopped", "code": "operation_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
