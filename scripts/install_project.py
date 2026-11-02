#!/usr/bin/env python3
"""Install a verified ai-comm pin into a new project (design 013 v3, section 8).

Runs only from an already verified ai-comm package. Fetches exactly one release
tag and the release index from that package's manifest-bound RELEASE_ORIGIN, and
only with --fetch-permitted (the owner's direct consent). Verifies the selected
index record, the annotated tag, manifest, protocol and full suite in a private
staging repository, then publishes it atomically as DIR/.tools/ai-comm. It never
initializes or joins a channel. JSON output; exit 0 on success, 2 on any stop.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCHEMA = "ai-comm-install/1"
GITIGNORE_LINES = (".tools/", ".ai-comm-local/")


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
uc, Stop, need, GUARD = ri.uc, ri.Stop, ri.need, ri.GUARD
_load, git, run_checked, trusted_source = ri._load, ri.git, ri.run_checked, ri.trusted_source
select_record, real_suite, real_fetch, open_dir_nofollow = ri.select_record, ri.real_suite, ri.real_fetch, ri.open_dir_nofollow


def check_project(project_dir):
    project = uc.check_ancestry(project_dir)
    need(project.is_dir(), "project_missing")
    for part in project.parts:
        need(part != ".tools", "nested_in_tools")
    need(not os.path.lexists(project / ".tools" / "ai-comm"), "pin_exists")
    need(not os.path.lexists(project / ".ai-comm-local" / "upgrade"), "upgrade_state_exists")
    for name in (".tools", ".ai-comm-local", ".gitignore"):
        path = project / name
        if os.path.lexists(path):
            need(not path.is_symlink(), "unsafe_path", path=name)
    if (project / ".tools").exists():
        uc.check_ancestry(project / ".tools")
    return project


def local_dir(project):
    local = project / ".ai-comm-local"
    if not os.path.lexists(local):
        os.mkdir(local, 0o700)
    need(uc._mode_ok_ancestor(os.lstat(local)), "unsafe_path", path=".ai-comm-local")
    return local


@contextlib.contextmanager
def install_lock(local):
    fd = os.open(local / "install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Stop("install_locked") from exc
        yield
    finally:
        os.close(fd)




def verify_staging(repo, version, record, run_suite):
    return ri.verify_staging(repo, version, record, run_suite, require_staged_identity)


def check_staged_repo(repo):
    """The staging repository and every component up to the project are real,
    user-owned directories, not symlinks (checked before cleanup and publish)."""
    staging = repo.parent
    local = staging.parent
    project = local.parent
    uc.check_ancestry(project)
    need(not local.is_symlink() and uc._mode_ok_ancestor(os.lstat(local)), "unsafe_path", path=".ai-comm-local")
    uc.private_dir(staging, create=False)
    st = os.lstat(repo)
    need(stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode) and st.st_uid == os.getuid(),
         "unsafe_path", path="staging/repo")


def require_staged_identity(repo, tag):
    """After code ran in the staging repository: remove build debris, then require the
    exact tag commit with no tracked, untracked or ignored changes (I2)."""
    check_staged_repo(repo)
    ri.clean_tree_identity(repo, tag)








def publish(project, staging):
    """Atomically move staging/repo to project/.tools/ai-comm using directory file
    descriptors opened without following symlinks, so a path swapped during
    verification cannot redirect the publish (I3)."""
    uc.check_ancestry(project)
    proj_fd = open_dir_nofollow(str(project))
    try:
        local_fd = open_dir_nofollow(".ai-comm-local", proj_fd)
        stage_fd = open_dir_nofollow(staging.name, local_fd)
        try:
            os.mkdir(".tools", 0o755, dir_fd=proj_fd)
        except FileExistsError:
            pass
        tools_fd = open_dir_nofollow(".tools", proj_fd)
        try:
            st = os.fstat(tools_fd)
            need(st.st_uid == os.getuid() and not st.st_mode & 0o022, "unsafe_path", path=".tools")
            try:
                os.lstat("ai-comm", dir_fd=tools_fd)
                raise Stop("pin_exists")
            except FileNotFoundError:
                pass
            src = os.lstat("repo", dir_fd=stage_fd)
            need(stat.S_ISDIR(src.st_mode) and not stat.S_ISLNK(src.st_mode) and src.st_uid == os.getuid(),
                 "unsafe_path", path="staging/repo")
            os.rename("repo", "ai-comm", src_dir_fd=stage_fd, dst_dir_fd=tools_fd)
            os.fsync(tools_fd)
            os.fsync(stage_fd)
        finally:
            os.close(tools_fd)
            os.close(stage_fd)
            os.close(local_fd)
    finally:
        os.close(proj_fd)


def update_gitignore(project):
    path = project / ".gitignore"
    need(not path.is_symlink(), "unsafe_path", path=".gitignore")
    existing = path.read_bytes() if path.exists() else b""
    lines = existing.decode("utf-8").splitlines()
    missing = [line for line in GITIGNORE_LINES if line not in lines and line.rstrip("/") not in lines]
    if not missing:
        return []
    prefix = existing if (not existing or existing.endswith(b"\n")) else existing + b"\n"
    data = prefix + "".join(line + "\n" for line in missing).encode()
    tmp = project / f".gitignore.ai-comm-{secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if path.exists():
        os.chmod(tmp, os.stat(path).st_mode & 0o777)
    os.replace(tmp, path)
    uc.fsync_dir(project)
    return missing


def finish(project, staging, journal, record):
    """Steps after publish: unintegrated project manifest and .gitignore (idempotent)."""
    uc.check_ancestry(project)
    local = project / ".ai-comm-local"
    need(not local.is_symlink() and uc._mode_ok_ancestor(os.lstat(local)), "unsafe_path", path=".ai-comm-local")
    upgrade = uc.private_dir(local / "upgrade")
    for name in ("txn", "consents", "controller"):
        uc.private_dir(upgrade / name)
    manifest = upgrade / "project.json"
    if not manifest.exists():
        uc.write_private_atomic(manifest, ri.unintegrated_declaration(project))
    uc.write_private_atomic(upgrade / "install.json", uc.canonical({
        "v": 1, "schema": INSTALL_SCHEMA, "version": record["package_version"], "commit": record["commit"],
        "manifest_sha256": record["manifest_sha256"], "staging": staging.name}))
    added = update_gitignore(project)
    journal.append("state", {"state": "installed", "gitignore_added": added})
    return added


def install(project_dir, version, *, fetch_permitted, use_helper, fetch=None, origin=None, run_suite=real_suite):
    need(uc.VERSION_RE.fullmatch(version or ""), "invalid_version")
    need(fetch_permitted, "fetch_not_permitted")
    source = origin or trusted_source()
    project = check_project(project_dir)
    local = local_dir(project)
    with install_lock(local):
        staging = uc.private_dir(local / f"install-{secrets.token_hex(6)}")
        journal = uc.Journal(staging)
        journal.append("start", {"schema": INSTALL_SCHEMA, "version": version, "project": str(project),
                                 "source": source})
        repo = staging / "repo"
        git(staging, "init", "--quiet", "--template=", str(repo))
        journal.append("intent", {"index": 0, "action": {"op": "fetch"}})
        (fetch or real_fetch)(repo, source, version, use_helper)
        record = select_record(repo, version)
        verified = verify_staging(repo, version, record, run_suite)
        journal.append("outcome", {"index": 0, "ok": True, "record": record,
                                   "tests_ran": verified["tests_ran"]})
        require_staged_identity(repo, verified["tag"])
        journal.append("intent", {"index": 1, "action": {"op": "publish"}, "commit": record["commit"]})
        publish(project, staging)
        journal.append("outcome", {"index": 1, "ok": True})
        tools = project / ".tools"
        added = finish(project, staging, journal, record)
        return {"status": "installed", "version": version, "commit": record["commit"],
                "pin": str(tools / "ai-comm"), "tests_ran": verified["tests_ran"],
                "release_awareness": verified["release_awareness"], "gitignore_added": added,
                "project_manifest": "unintegrated",
                "next": "Follow ACTIVATE.md to create or join a channel; review integrations and mark "
                        ".ai-comm-local/upgrade/project.json declared before any guided upgrade."}


STAGING_RE = re.compile(r"install-[0-9a-f]{12}\Z")


def recognized_journal(project, staging):
    """A staging directory is touched only if it is private, named by the installer,
    and holds a valid install journal whose start record names this project (I1)."""
    if not STAGING_RE.fullmatch(staging.name) or staging.is_symlink():
        return None
    try:
        uc.private_dir(staging, create=False)
        if not (staging / "journal.jsonl").is_file():
            return None
        journal = uc.Journal(staging)
        records, torn = journal.read()  # read-only: complete, chain-verified prefix
    except Stop:
        return None
    if not records or records[0]["kind"] != "start":
        return None
    start = records[0]["data"]
    if start.get("schema") != INSTALL_SCHEMA or start.get("project") != str(project):
        return None
    if torn is not None:
        journal.repair_torn_tail()  # only after the binding to this project is verified
        records, _ = journal.read()
    return records


def real_pin_path(project):
    """True only if .tools and .tools/ai-comm are real, user-owned directories."""
    for path in (project / ".tools", project / ".tools" / "ai-comm"):
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            if stat.S_ISLNK(st.st_mode):
                raise Stop("unsafe_path", path=str(path.relative_to(project)))
            return False
    return True


def recover(project_dir):
    project = uc.check_ancestry(project_dir)
    local = project / ".ai-comm-local"
    need(local.is_dir() and not local.is_symlink(), "nothing_to_recover")
    results = []
    with install_lock(local):
        for staging in sorted(local.glob("install-*")):
            records = recognized_journal(project, staging)
            if records is None:
                results.append({"staging": staging.name, "action": "skipped_unrecognized"})
                continue
            journal = uc.Journal(staging)
            states = [r["data"].get("state") for r in records if r["kind"] == "state"]
            if "installed" in states:
                continue
            publish = [r for r in records if r["kind"] == "intent" and r["data"]["action"]["op"] == "publish"]
            if not publish:
                shutil.rmtree(staging)
                results.append({"staging": staging.name, "action": "removed_unpublished"})
                continue
            pin = project / ".tools" / "ai-comm"
            record = next(r["data"]["record"] for r in records if r["kind"] == "outcome" and r["data"]["index"] == 0)
            if real_pin_path(project):
                identity = uc.package_identity(pin)
                if identity["commit"] == record["commit"] and not identity["dirty"]:
                    if not [r for r in records if r["kind"] == "outcome" and r["data"]["index"] == 1]:
                        journal.append("outcome", {"index": 1, "ok": True, "by": "probe"})
                    finish(project, staging, journal, record)
                    results.append({"staging": staging.name, "action": "completed_after_publish"})
                    continue
            if (staging / "repo").is_dir() and not os.path.lexists(pin):
                # The rename is atomic: the repository is still staged, so nothing was published.
                shutil.rmtree(staging)
                results.append({"staging": staging.name, "action": "removed_unpublished"})
                continue
            results.append({"staging": staging.name, "action": "report_only",
                            "detail": "pin does not match the verified install; inspect manually"})
    return {"status": "recovery_report", "results": results}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", required=True)
    parser.add_argument("--version")
    parser.add_argument("--fetch-permitted", action="store_true",
                        help="the owner directly approved fetching this release from RELEASE_ORIGIN")
    parser.add_argument("--use-git-credential-helper", action="store_true")
    parser.add_argument("--recover", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        print(json.dumps({"status": "stopped", "code": "invalid_arguments"}))
        return 2
    try:
        if args.recover:
            result = recover(args.project)
        else:
            need(args.version, "invalid_version")
            need(not args.use_git_credential_helper or args.fetch_permitted, "credential_helper_requires_fetch_permission")
            result = install(args.project, args.version, fetch_permitted=args.fetch_permitted,
                             use_helper=args.use_git_credential_helper)
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
