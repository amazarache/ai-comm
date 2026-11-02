#!/usr/bin/env python3
"""Shared, layout-independent steps of the ai-comm installers (design 018c v3, section 4).

Used by install_project.py (a project's pin) and install_user.py (the per-user cache).
Nothing here chooses where a release is published; callers do that.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
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


def source_identity(root=ROOT):
    """The running package must itself be a verified, unmodified release: a clean Git
    checkout, or an extracted release archive (which the owner checked against the
    release's SHA256SUMS) whose every manifest entry and protocol digest verify.
    RELEASE_ORIGIN is read only from the manifest-verified ai_comm.py."""
    run_checked([sys.executable, "-I", str(root / "scripts" / "check_release_manifest.py")], root,
                "installer_package_unverified", timeout=120)
    manifest_bytes = (root / "RELEASE-MANIFEST.json").read_bytes()
    cli = (root / "ai_comm.py").read_bytes()
    version = re.findall(r'^VERSION = "([^"\r\n]+)"$', cli.decode("utf-8", "replace"), re.MULTILINE)
    need(len(version) == 1, "installer_package_unverified")
    if (root / ".git").exists():
        try:
            identity = uc.package_identity(root)
        except Stop as exc:
            raise Stop("installer_package_unverified") from exc
        need(not identity["dirty"] and identity["origin"], "installer_package_unverified")
        return {"kind": "git", "origin": identity["origin"], "version": version[0],
                "manifest_sha256": uc.sha256_bytes(manifest_bytes)}
    manifest = uc.parse_json(manifest_bytes, "installer_package_unverified")
    entries = [e for e in manifest.get("files", []) if isinstance(e, dict) and e.get("path") == "ai_comm.py"]
    need(len(entries) == 1 and entries[0].get("sha256") == uc.sha256_bytes(cli), "installer_package_unverified")
    origin = re.findall(r'^RELEASE_ORIGIN = "([^"\r\n]+)"$', cli.decode("utf-8", "replace"), re.MULTILINE)
    need(len(origin) == 1, "installer_package_unverified")
    return {"kind": "archive", "origin": origin[0], "version": version[0],
            "manifest_sha256": uc.sha256_bytes(manifest_bytes)}


def trusted_source(root=ROOT):
    return source_identity(root)["origin"]


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


def verify_staging(repo, version, record, run_suite, staged_identity):
    """Verify a freshly fetched staging repository against its index record.
    `staged_identity(repo, tag)` re-checks the caller's layout and the exact tag tree."""
    tag = uc.tag_identity(repo, version)
    need(tag["commit"] == record["commit"], "index_tag_mismatch", what="commit")
    need(tag["manifest_sha256"] == record["manifest_sha256"], "index_tag_mismatch", what="manifest")
    need(tag["protocol_sha256"] == record["protocol_sha256"], "index_tag_mismatch", what="protocol")
    git(repo, "checkout", "--quiet", "--detach", tag["commit"])
    for argv in (["scripts/check_tag.py", tag["tag"]], ["scripts/check_release_manifest.py"]):
        run_checked([sys.executable, "-I", *argv], repo, "verify_failed", timeout=300)
    tests = run_suite(repo)
    staged_identity(repo, tag)
    status = uc.release_status(repo)
    awareness = "complete"
    if status.get("status") == "index_tags_missing":
        awareness = "partial: newer releases are listed but not verified locally; run the guided check to learn about them"
    else:
        need(status.get("status") == "ok", "verify_failed", check="release_status")
    need(status.get("pinned_version") == version, "verify_failed", check="release_status")
    return {"tag": tag, "tests_ran": tests, "release_awareness": awareness}


def clean_tree_identity(repo, tag):
    """Remove build debris, then require the exact tag commit with no tracked,
    untracked or ignored changes. Only for repositories this installer created."""
    git(repo, "clean", "-fdxq")
    need(git(repo, "rev-parse", "HEAD").strip() == tag["commit"], "staged_identity_changed", what="commit")
    need(git(repo, "status", "--porcelain", "--untracked-files=all", "--ignored").strip() == "",
         "staged_identity_changed", what="worktree")
    need(git(repo, "rev-parse", f"refs/tags/{tag['tag']}").strip() == tag["tag_object"],
         "staged_identity_changed", what="tag")


def real_suite(repo):
    env = uc.clean_env() | {GUARD: "1"}
    done = run_checked([sys.executable, "-m", "unittest", "discover", "-s", "tests"], repo, "verify_failed",
                       timeout=uc.TEST_TIMEOUT, env=env)
    summary = re.findall(rb"^Ran ([0-9]+) tests? in", done.stderr, re.MULTILINE)
    need(summary, "verify_failed", check="tests")
    return int(summary[-1])


def release_check_module():
    cfr = _load("ai_comm_install_release_check", ROOT / "scripts" / "check_for_release.py")
    cfr._load_package(ROOT)
    return cfr


def isolated_fetch(repo, origin, use_helper, refspecs):
    """The existing bounded, redirect-refusing fetch of check_for_release.py, limited
    to the given exact refspecs."""
    cfr = release_check_module()
    helpers = cfr._read_credential_helpers() if use_helper else []
    try:
        cfr._fetch_isolated(repo, origin, helpers, refspecs=refspecs)
    except cfr.CheckError as exc:
        raise Stop("fetch_failed", helper_error=exc.code) from exc


def release_refspecs(version):
    return (f"refs/tags/v{version}:refs/tags/v{version}",
            "refs/heads/release-index:refs/remotes/origin/release-index")


def real_fetch(repo, origin, version, use_helper):
    isolated_fetch(repo, origin, use_helper, release_refspecs(version))


def open_dir_nofollow(name, dir_fd=None):
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError as exc:
        raise Stop("unsafe_path", path=str(name)) from exc
