#!/usr/bin/env python3
"""Shared, layout-independent steps of the ai-comm installers (design 018c v3, section 4).

Used by install_project.py (a project's pin) and install_user.py (the per-user cache).
Nothing here chooses where a release is published; callers do that.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import re
import stat
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


def unintegrated_declaration(project):
    """The bytes both installers write for a project with no declared integrations."""
    return uc.canonical({"v": 1, "schema": uc.PROJECT_SCHEMA, "status": "unintegrated",
                         "project_id": {"path": str(project), "channel": None}, "integrations": []})


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


MAX_TREE_FILE = 16 * 1024 * 1024
MAX_TREE_ENTRIES = 20000
_COMMIT_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def _inert_git_env():
    env = uc.clean_env()
    env.update({"GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1", "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_1": "core.fsmonitor", "GIT_CONFIG_VALUE_1": "false"})
    return env


def _git_object(git_dir, kind, oid, code):
    """Raw bytes of one object, read without a work tree, index or replacement refs,
    then re-hashed here: the object store itself is not trusted."""
    try:
        done = subprocess.run(["git", "--git-dir", str(git_dir), "--no-replace-objects", "cat-file", kind, oid],
                              capture_output=True, env=_inert_git_env(), timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Stop(code, what="object") from exc
    need(done.returncode == 0, code, what="object")
    data = done.stdout
    algo = hashlib.sha1 if len(oid) == 40 else hashlib.sha256
    need(algo(kind.encode() + b" %d\x00" % len(data) + data).hexdigest() == oid, code, what="object_hash")
    return data


def _tree_entries(git_dir, tree_oid, prefix, out, code):
    data = _git_object(git_dir, "tree", tree_oid, code)
    raw_len = len(tree_oid) // 2
    pos = 0
    while pos < len(data):
        space = data.index(b" ", pos)
        nul = data.index(b"\x00", space)
        mode = data[pos:space].decode("ascii")
        name = data[space + 1:nul].decode("utf-8", "strict")
        oid = data[nul + 1:nul + 1 + raw_len].hex()
        need(len(oid) == len(tree_oid), code, what="tree")
        need(name not in ("", ".", "..", ".git") and "/" not in name, code, what="tree_name")
        pos = nul + 1 + raw_len
        path = f"{prefix}{name}"
        if mode == "40000":
            _tree_entries(git_dir, oid, path + "/", out, code)
        else:
            need(mode in ("100644", "100755"), code, what="tree_mode", path=path)
            out[path] = (oid, mode == "100755")
            need(len(out) <= MAX_TREE_ENTRIES, code, what="tree_size")


def commit_tree(git_dir, commit, code):
    """{path: (blob id, executable)} for every file of `commit`, read from `git_dir`
    with every commit and tree object re-hashed (see `git_object`)."""
    need(_COMMIT_OID.fullmatch(commit or ""), code, what="commit")
    body = _git_object(git_dir, "commit", commit, code)
    header = body.split(b"\n\n", 1)[0].split(b"\n")
    need(header and header[0].startswith(b"tree "), code, what="commit")
    tree = header[0][5:].decode("ascii", "replace")
    need(_COMMIT_OID.fullmatch(tree) and len(tree) == len(commit), code, what="commit")
    expected = {}
    try:
        _tree_entries(git_dir, tree, "", expected, code)
    except (ValueError, UnicodeError) as exc:
        raise Stop(code, what="tree") from exc
    return expected


def verify_tree(repo, commit, manifest_sha256, code="release_identity_changed", allow_bytecode=False):
    """The checkout at `repo` is exactly `commit`: every working file and nothing else.

    Trust comes only from `commit` (a recorded, previously verified id). The commit and
    tree objects are re-hashed; HEAD, the index, `git status` and per-file flags such as
    assume-unchanged are never consulted. Working files are opened by safe relative path
    (no symlinks, regular files, bounded, non-blocking) and their blob hashes compared.
    `allow_bytecode` (only for an installer's own interrupted staging checkout) also
    accepts untracked `__pycache__` directories holding nothing but regular `.pyc` files,
    which an interrupted test run leaves behind."""
    repo = Path(repo)
    git_st = os.lstat(repo / ".git") if os.path.lexists(repo / ".git") else None
    need(git_st is not None and stat.S_ISDIR(git_st.st_mode), code, what="git_dir")
    expected = commit_tree(repo / ".git", commit, code)
    need("RELEASE-MANIFEST.json" in expected, code, what="manifest")
    manifest = git_object(repo / ".git", "blob", expected["RELEASE-MANIFEST.json"][0], code)
    need(uc.sha256_bytes(manifest) == manifest_sha256, code, what="manifest")
    algo = hashlib.sha1 if len(commit) == 40 else hashlib.sha256
    seen = set()
    root_fd = open_dir_nofollow(str(repo))
    try:
        for dirpath, dirnames, filenames, dir_fd in os.fwalk(".", dir_fd=root_fd, follow_symlinks=False):
            rel = "" if dirpath == "." else dirpath[2:] + "/"
            if rel == "":
                need(".git" in dirnames, code, what="git_dir")
                dirnames.remove(".git")
            for name in list(dirnames):
                st = os.lstat(name, dir_fd=dir_fd)
                need(stat.S_ISDIR(st.st_mode), code, what="not_regular", path=rel + name)
                if any(p.startswith(rel + name + "/") for p in expected):
                    continue
                need(allow_bytecode and name == "__pycache__" and _only_bytecode(name, dir_fd), code,
                     what="unexpected", path=rel + name)
                dirnames.remove(name)
            for name in filenames:
                path = rel + name
                need(path in expected, code, what="unexpected", path=path)
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
                except OSError as exc:
                    raise Stop(code, what="not_regular", path=path) from exc
                try:
                    st = os.fstat(fd)
                    need(stat.S_ISREG(st.st_mode) and st.st_size <= MAX_TREE_FILE, code, what="not_regular",
                         path=path)
                    chunks, size = [], 0
                    while True:
                        chunk = os.read(fd, 1 << 20)
                        if not chunk:
                            break
                        size += len(chunk)
                        need(size <= MAX_TREE_FILE, code, what="not_regular", path=path)
                        chunks.append(chunk)
                finally:
                    os.close(fd)
                data = b"".join(chunks)
                oid, executable = expected[path]
                need(algo(b"blob %d\x00" % len(data) + data).hexdigest() == oid, code, what="content", path=path)
                need(bool(st.st_mode & 0o100) == executable, code, what="mode", path=path)
                seen.add(path)
    finally:
        os.close(root_fd)
    missing = sorted(set(expected) - seen)
    need(not missing, code, what="missing", path=missing[0] if missing else None)
    return {"commit": commit, "files": len(seen)}


# Public names for the other trusted checkers (verify_pin.py).
git_object = _git_object
inert_git_env = _inert_git_env


def _only_bytecode(name, parent_fd):
    fd = open_dir_nofollow(name, parent_fd)
    try:
        for entry in os.listdir(fd):
            st = os.lstat(entry, dir_fd=fd)
            if not (stat.S_ISREG(st.st_mode) and entry.endswith(".pyc")):
                return False
        return True
    finally:
        os.close(fd)


def open_dir_nofollow(name, dir_fd=None):
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError as exc:
        raise Stop("unsafe_path", path=str(name)) from exc
