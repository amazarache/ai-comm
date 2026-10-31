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
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCHEMA = "ai-comm-install/1"
GITIGNORE_LINES = (".tools/", ".ai-comm-local/")
GUARD = "AI_COMM_UPGRADE_REAL_BOUNDARY_INNER"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


uc = _load("ai_comm_upgrade_controller_for_install", ROOT / "upgrade_controller.py")
Stop, need = uc.Stop, uc.need


def git(cwd, *args, timeout=300):
    return uc.git(cwd, *args, timeout=timeout)


def run_checked(argv, cwd, code, timeout=600, env=None):
    try:
        done = subprocess.run(argv, cwd=str(cwd), capture_output=True, timeout=timeout,
                              env=env or uc.clean_env(), check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Stop(code) from exc
    need(done.returncode == 0, code)
    return done


def trusted_source():
    """The running package must itself be a verified, unmodified release."""
    try:
        identity = uc.package_identity(ROOT)
    except Stop as exc:
        raise Stop("installer_package_unverified") from exc
    need(not identity["dirty"] and identity["origin"], "installer_package_unverified")
    run_checked([sys.executable, "-I", str(ROOT / "scripts" / "check_release_manifest.py")], ROOT,
                "installer_package_unverified", timeout=120)
    return identity["origin"]


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


def select_record(repo, version):
    raw = git(repo, "show", "refs/remotes/origin/release-index:RELEASES.json").encode()
    index = uc.parse_json(raw, "release_index_invalid")
    need(isinstance(index, dict) and index.get("format") == "ai-comm-releases/1"
         and isinstance(index.get("releases"), list), "release_index_invalid")
    matches = [r for r in index["releases"] if isinstance(r, dict) and r.get("package_version") == version]
    need(len(matches) == 1, "release_not_in_index", version=version)
    record = matches[0]
    for key in ("commit", "tag", "manifest_sha256", "protocol_sha256"):
        need(isinstance(record.get(key), str), "release_index_invalid")
    need(record["tag"] == "v" + version, "release_index_invalid")
    return record


def verify_staging(repo, version, record, run_suite):
    tag = uc.tag_identity(repo, version)
    need(tag["commit"] == record["commit"], "index_tag_mismatch", what="commit")
    need(tag["manifest_sha256"] == record["manifest_sha256"], "index_tag_mismatch", what="manifest")
    need(tag["protocol_sha256"] == record["protocol_sha256"], "index_tag_mismatch", what="protocol")
    git(repo, "checkout", "--quiet", "--detach", tag["commit"])
    for argv in (["scripts/check_tag.py", tag["tag"]], ["scripts/check_release_manifest.py"]):
        run_checked([sys.executable, "-I", *argv], repo, "verify_failed", timeout=300)
    tests = run_suite(repo)
    status = uc.release_status(repo)
    awareness = "complete"
    if status.get("status") == "index_tags_missing":
        awareness = "partial: newer releases are listed but not verified locally; run the guided check to learn about them"
    else:
        need(status.get("status") == "ok", "verify_failed", check="release_status")
    need(status.get("pinned_version") == version, "verify_failed", check="release_status")
    return {"tag": tag, "tests_ran": tests, "release_awareness": awareness}


def real_suite(repo):
    env = uc.clean_env() | {GUARD: "1"}
    done = run_checked([sys.executable, "-m", "unittest", "discover", "-s", "tests"], repo, "verify_failed",
                       timeout=uc.TEST_TIMEOUT, env=env)
    summary = re.findall(rb"^Ran ([0-9]+) tests? in", done.stderr, re.MULTILINE)
    need(summary, "verify_failed", check="tests")
    return int(summary[-1])


def real_fetch(repo, origin, version, use_helper):
    cfr = _load("ai_comm_install_release_check", ROOT / "scripts" / "check_for_release.py")
    cfr._load_package(ROOT)
    helpers = cfr._read_credential_helpers() if use_helper else []
    refspecs = (f"refs/tags/v{version}:refs/tags/v{version}",
                "refs/heads/release-index:refs/remotes/origin/release-index")
    try:
        cfr._fetch_isolated(repo, origin, helpers, refspecs=refspecs)
    except cfr.CheckError as exc:
        raise Stop("fetch_failed", helper_error=exc.code) from exc


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
    upgrade = uc.private_dir(project / ".ai-comm-local" / "upgrade")
    for name in ("txn", "consents", "controller"):
        uc.private_dir(upgrade / name)
    manifest = upgrade / "project.json"
    if not manifest.exists():
        uc.write_private_atomic(manifest, uc.canonical({
            "v": 1, "schema": uc.PROJECT_SCHEMA, "status": "unintegrated",
            "project_id": {"path": str(project), "channel": None}, "integrations": []}))
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
        journal.append("start", {"version": version, "project": str(project), "source": source})
        repo = staging / "repo"
        git(staging, "init", "--quiet", "--template=", str(repo))
        journal.append("intent", {"index": 0, "action": {"op": "fetch"}})
        (fetch or real_fetch)(repo, source, version, use_helper)
        record = select_record(repo, version)
        verified = verify_staging(repo, version, record, run_suite)
        journal.append("outcome", {"index": 0, "ok": True, "record": record,
                                   "tests_ran": verified["tests_ran"]})
        tools = project / ".tools"
        if not tools.exists():
            os.mkdir(tools, 0o755)
        need(not os.path.lexists(tools / "ai-comm"), "pin_exists")
        journal.append("intent", {"index": 1, "action": {"op": "publish"}, "commit": record["commit"]})
        os.rename(repo, tools / "ai-comm")
        uc.fsync_dir(tools)
        journal.append("outcome", {"index": 1, "ok": True})
        added = finish(project, staging, journal, record)
        return {"status": "installed", "version": version, "commit": record["commit"],
                "pin": str(tools / "ai-comm"), "tests_ran": verified["tests_ran"],
                "release_awareness": verified["release_awareness"], "gitignore_added": added,
                "project_manifest": "unintegrated",
                "next": "Follow ACTIVATE.md to create or join a channel; review integrations and mark "
                        ".ai-comm-local/upgrade/project.json declared before any guided upgrade."}


def recover(project_dir):
    project = uc.check_ancestry(project_dir)
    local = project / ".ai-comm-local"
    need(local.is_dir() and not local.is_symlink(), "nothing_to_recover")
    results = []
    with install_lock(local):
        for staging in sorted(local.glob("install-*")):
            uc.private_dir(staging, create=False)
            journal = uc.Journal(staging)
            journal.repair_torn_tail()
            records, _ = journal.read()
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
            if pin.is_dir() and not pin.is_symlink():
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
