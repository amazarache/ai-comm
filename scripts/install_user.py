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
OP_DIR_RE = re.compile(r"(install|activate|check)-[0-9a-f]{12}\Z")
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
        """A cached release is still the verified one: recorded, real directory, exact
        commit, no tracked changes."""
        record = self.state()["releases"].get(version)
        need(record is not None, "release_not_installed", version=version)
        path = self.release_dir(version)
        st = os.lstat(path) if os.path.lexists(path) else None
        need(st is not None and stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode)
             and st.st_uid == os.getuid(), "release_identity_changed", version=version)
        identity = uc.package_identity(path)
        need(identity["commit"] == record["commit"] and not identity["dirty"]
             and identity["manifest_sha256"] == record["manifest_sha256"], "release_identity_changed",
             version=version)
        return path

    def op_journal(self, op, start):
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
        need(version not in cache.state()["releases"] and not os.path.lexists(cache.release_dir(version)),
             "release_installed", version=version)
        staging = uc.private_dir(cache.root / "staging" / f"install-{secrets.token_hex(6)}")
        journal = uc.Journal(staging)
        journal.append("start", {"schema": OP_SCHEMA, "op": "install", "version": version,
                                 "source": src["origin"], "source_kind": src["kind"]})
        repo = staging / "repo"
        ri.git(staging, "init", "--quiet", "--template=", str(repo))
        journal.append("intent", {"index": 0, "action": {"op": "fetch"}})
        (fetch or ri.real_fetch)(repo, src["origin"], version, use_helper)
        record = ri.select_record(repo, version)
        if src["kind"] == "archive" and src["version"] == version:
            # The bridge from a downloaded archive to the cache's Git checkout (850.1):
            # archive, tag and index must name the same manifest.
            need(src["manifest_sha256"] == record["manifest_sha256"], "archive_manifest_mismatch")
        verified = ri.verify_staging(repo, version, record, run_suite, staging_identity(cache, staging))
        binding = binding_from_tag(verified["tag"], record)
        journal.append("outcome", {"index": 0, "ok": True, "record": record, "tests_ran": verified["tests_ran"]})
        staging_identity(cache, staging)(repo, verified["tag"])
        old = cache.read_link("current")
        if activate is None:
            activate = old is None
        if activate:
            # Recorded before publishing, so recovery knows the switch was intended.
            journal.append("note", {"activate": version, "previous": old})
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
            "index_refreshed_at": index.get("refreshed_at") if index else None}


def check(*, fetch_permitted=False, use_helper=False, root=None, source=None, fetch=None, now=time.time):
    """Offline unless --fetch-permitted: is a newer verified release known? A refresh
    fetches only the index and v* tags and builds bindings for every indexed tag."""
    need(not use_helper or fetch_permitted, "credential_helper_requires_fetch_permission")
    cache = Cache(root or cache_root()).open(create=False)
    with cache.lock():
        current = cache.read_link("current")
        refreshed = None
        if fetch_permitted:
            src = source or ri.source_identity()
            scratch = uc.private_dir(cache.root / "staging" / f"check-{secrets.token_hex(6)}")
            try:
                repo = scratch / "repo"
                ri.git(scratch, "init", "--quiet", "--template=", str(repo))
                if fetch:
                    fetch(repo, src["origin"], use_helper)
                else:
                    ri.isolated_fetch(repo, src["origin"], use_helper, ri.release_check_module().REFSPECS)
                records = index_records(repo)
                bindings = cache.bindings()
                for record in records:
                    version = record["package_version"]
                    if not ri.git(repo, "tag", "-l", f"v{version}").strip():
                        continue
                    try:
                        tag = uc.tag_identity(repo, version)
                        bindings["bindings"][version] = binding_from_tag(tag, record)
                    except Stop:
                        bindings["bindings"].pop(version, None)  # never keep a binding the index contradicts
                cache.write_bindings(bindings)
                refreshed = int(now())
                cache.write_index(records, refreshed)
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
        index = cache.index()
        bindings = cache.bindings()["bindings"]
        listed = [r["package_version"] for r in (index or {}).get("releases", [])]
        newer = [v for v in listed if current is None or version_key(v) > version_key(current)]
        verified_newer = sorted((v for v in newer if v in bindings), key=version_key)
        return {"status": "ok", "current": current,
                "latest_verified": verified_newer[-1] if verified_newer else current,
                "offer": bool(verified_newer),
                "unverified_newer": sorted((v for v in newer if v not in bindings), key=version_key),
                "index_refreshed_at": (index or {}).get("refreshed_at"),
                "metadata": "refreshed" if refreshed else ("cached" if index else "missing")}


# --------------------------------------------------------------------------- recovery and removal


def _journal_dirs(cache):
    for parent in (cache.root / "staging", cache.root / "journal"):
        if not parent.is_dir() or parent.is_symlink():
            continue
        for child in sorted(parent.iterdir()):
            if OP_DIR_RE.fullmatch(child.name) and not child.is_symlink():
                yield child


def recover(*, root=None):
    cache = Cache(root or cache_root()).open(create=False)
    results = []
    with cache.lock():
        for directory in _journal_dirs(cache):
            try:
                uc.private_dir(directory, create=False)
                journal = uc.Journal(directory)
                records, torn = journal.read()
            except Stop:
                results.append({"dir": directory.name, "action": "skipped_unrecognized"})
                continue
            if not records or records[0]["kind"] != "start" or records[0]["data"].get("schema") != OP_SCHEMA:
                results.append({"dir": directory.name, "action": "skipped_unrecognized"})
                continue
            if torn is not None:
                journal.repair_torn_tail()
                records, _ = journal.read()
            states = [r["data"].get("state") for r in records if r["kind"] == "state"]
            start = records[0]["data"]
            if "installed" in states or ("done" in states and start["op"] != "install"):
                continue
            if start["op"] == "check":
                shutil.rmtree(directory)
                results.append({"dir": directory.name, "action": "removed_check_scratch"})
                continue
            if start["op"] == "install":
                results.append(_recover_install(cache, directory, journal, records, start))
            else:
                after = start["after"]
                if cache.read_link("current") == after["current"] and cache.read_link("previous") == after["previous"]:
                    journal.append("state", {"state": "done"})
                    results.append({"dir": directory.name, "action": "already_complete"})
                    continue
                for version in (after["current"], after["previous"]):
                    if version is not None:
                        cache.check_release(version)
                _switch(cache, journal, after["current"], after["previous"])
                results.append({"dir": directory.name, "action": "completed_switch"})
    return {"status": "recovery_report", "results": results}


def _recover_install(cache, directory, journal, records, start):
    version = start["version"]
    publish = [r for r in records if r["kind"] == "intent" and r["data"]["action"]["op"] == "publish"]
    release = cache.release_dir(version)
    if not publish:
        shutil.rmtree(directory)
        return {"dir": directory.name, "action": "removed_unpublished"}
    record = next(r["data"]["record"] for r in records if r["kind"] == "outcome" and r["data"]["index"] == 0)
    if os.path.lexists(release) and not release.is_symlink() and release.is_dir():
        identity = uc.package_identity(release)
        if identity["commit"] == record["commit"] and not identity["dirty"]:
            if version not in cache.state()["releases"]:
                tag = uc.tag_identity(release, version)
                _record_release(cache, version, tag, binding_from_tag(tag, record), release)
            note = [r["data"] for r in records if r["kind"] == "note" and "activate" in r["data"]]
            if note and "done" not in [r["data"].get("state") for r in records if r["kind"] == "state"]:
                _switch(cache, journal, note[0]["activate"], note[0]["previous"])
            journal.append("state", {"state": "installed"})
            return {"dir": directory.name, "action": "completed_after_publish"}
    if (directory / "repo").is_dir() and not os.path.lexists(release):
        shutil.rmtree(directory)
        return {"dir": directory.name, "action": "removed_unpublished"}
    return {"dir": directory.name, "action": "report_only",
            "detail": "release does not match the verified install; inspect manually"}


def uninstall(*, confirm, root=None):
    need(confirm, "confirmation_required")
    cache = Cache(root or cache_root()).open(create=False)
    kept, removed = [], []
    with cache.lock():
        state = cache.state()
        need(not state["owned"], "owned_objects_present")  # skill links and launcher: checkpoint (b)
        for entry in sorted(os.listdir(cache.root)):
            if entry not in TOP_LEVEL:
                kept.append(entry)
        releases = cache.root / "releases"
        if releases.is_dir() and not releases.is_symlink():
            for child in sorted(releases.iterdir()):
                try:
                    cache.check_release(child.name)
                except Stop:
                    kept.append(f"releases/{child.name}")
                    continue
                shutil.rmtree(child)
                removed.append(f"releases/{child.name}")
        for name in ("current", "previous"):
            path = cache.root / name
            if os.path.lexists(path):
                if path.is_symlink() and LINK_RE.fullmatch(os.readlink(path)):
                    os.unlink(path)
                    removed.append(name)
                else:
                    kept.append(name)
        for name in ("staging", "journal"):
            path = cache.root / name
            if not path.is_dir() or path.is_symlink():
                continue
            for child in sorted(path.iterdir()):
                if OP_DIR_RE.fullmatch(child.name) and child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    kept.append(f"{name}/{child.name}")
        for name in ("state.json", "bindings.json", "index.json", "offer.json"):
            path = cache.root / name
            if path.is_file() and not path.is_symlink():
                os.unlink(path)
                removed.append(name)
        for name in ("releases", "staging", "journal"):
            with contextlib.suppress(OSError):
                os.rmdir(cache.root / name)
    if not kept:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(cache.root / "lock")
        with contextlib.suppress(OSError):
            os.rmdir(cache.root)
    return {"status": "uninstalled" if not kept else "partially_uninstalled", "removed": removed,
            "kept": sorted(set(kept))}


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
