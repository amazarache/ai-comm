#!/usr/bin/env python3
"""Cache-side release fetch for an existing pin without a recorded origin (1.11.1).

Pins published by the project installer before 1.11.1 (and by 1.11.0 guided setup)
have no `remote.origin.url`, so their own `ai_comm.py upgrade fetch` stops with
`origin_missing`. This command, run from a verified per-user cache release and never
from pin code, does that one fetch for them under the same guided-upgrade consent and
project lock, and publishes exactly two refs in the pin:
`refs/tags/vX` and `refs/remotes/origin/release-index`. Nothing else in the pin changes:
no configuration, no working tree, no other ref. The pin's own `prepare` then proceeds.

Usage (through `install.sh --user upgrade-fetch`):
    upgrade_fetch.py --project DIR --target X.Y.Z --consent ID [--channel PATH] [--cache DIR]
"""
from __future__ import annotations

import argparse
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
RELEASE = Path(__file__).resolve().parents[1]


def _load(name, path):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


ri = _load("ai_comm_release_install", RELEASE / "scripts" / "release_install.py")
iu = _load("ai_comm_install_user", RELEASE / "scripts" / "install_user.py")
vp = _load("ai_comm_verify_pin", RELEASE / "scripts" / "verify_pin.py")
uc, Stop, need = ri.uc, ri.Stop, ri.need
ZERO = "0" * 40
TERMINAL = {"completed", "rolled_back"}


def origin_values(pin):
    """Every local `remote.origin.url` value of the pin, read without following includes."""
    done = subprocess.run(["git", "-C", str(pin), "config", "--local", "--no-includes", "--null", "--get-all",
                           "remote.origin.url"], capture_output=True, env=ri._inert_git_env(), timeout=60,
                          check=False)
    if done.returncode == 1 and not done.stdout:
        return []
    need(done.returncode == 0, "pin_has_origin")
    return done.stdout.split(b"\0")[:-1]


def ref_value(repo, name):
    """The ref's own value (for a tag, the tag object), or None when it does not exist.
    A symbolic ref is refused, never followed; a failed read is an error, not "absent"."""
    done = subprocess.run(["git", "-C", str(repo), "for-each-ref", "--format=%(objectname) %(symref)", name],
                          capture_output=True, env=ri._inert_git_env(), timeout=60, check=False)
    need(done.returncode == 0, "ref_invalid", ref=name)
    lines = [line for line in done.stdout.decode("utf-8", "replace").splitlines() if line]
    if not lines:
        return None
    need(len(lines) == 1, "ref_invalid", ref=name)
    value, _, symref = lines[0].partition(" ")
    need(not symref, "ref_invalid", ref=name, what="symbolic")
    need(uc.COMMIT.fullmatch(value), "ref_invalid", ref=name)
    return value


def pin_object_dir(pin):
    """The pin's own object directory: a real, user-owned directory inside its .git."""
    git_dir = pin / ".git"
    objects = git_dir / "objects"
    for path in (git_dir, objects, objects / "pack"):
        st = os.lstat(path)
        need(stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode) and st.st_uid == os.getuid(),
             "unsafe_path", path=str(path.relative_to(pin)))
    return objects


def copy_objects(repo, pin, oids):
    """Every object reachable from `oids`, packed in the private repository and indexed
    straight into the pin's object store. Both commands run with the private repository
    as their Git directory: the pin's configuration is never read, and no transport runs."""
    env = ri._inert_git_env()
    packed = subprocess.run(["git", "--git-dir", str(repo / ".git"), "pack-objects", "--revs", "--stdout",
                             "--quiet"], input=("\n".join(oids) + "\n").encode(), capture_output=True, env=env,
                            timeout=300, check=False)
    need(packed.returncode == 0, "fetch_failed", helper_error="object_copy_failed")
    indexed = subprocess.run(["git", "--git-dir", str(repo / ".git"), "index-pack", "--stdin", "--fix-thin"],
                             input=packed.stdout, capture_output=True,
                             env={**env, "GIT_OBJECT_DIRECTORY": str(pin_object_dir(pin))}, timeout=300, check=False)
    need(indexed.returncode == 0, "fetch_failed", helper_error="object_copy_failed")


def require_no_unfinished_upgrade(project):
    current = uc.read_private(project.root / "current", missing=b"").decode() or None
    if current:
        txn = uc.Txn(project, current)
        need(txn.state() in TERMINAL, "upgrade_in_progress")


def pin_state(project, cache, origin):
    """The pin as the trusted verifier sees it. Nothing here runs `git status` or any
    other command that reads the pin's work-tree configuration (fsmonitor, hooks,
    filters): the verifier compares tracked files with the committed bytes itself and
    reads objects inertly, and the remaining facts come from the verified bytes."""
    report = vp.check_project(project.dir, cache)
    need(report["status"] == "verified", "pin_not_verified", status=report["status"])
    binding = cache.bindings()["bindings"].get(report["version"]) or {}
    cli = (project.pin / "ai_comm.py").read_bytes()  # verified: equal to the committed bytes
    found = re.findall(rb'^RELEASE_ORIGIN = "([^"\r\n]+)"$', cli, re.MULTILINE)
    pin = {"commit": report["commit"], "version": report["version"],
           "origin": found[0].decode("utf-8", "replace") if len(found) == 1 else None,
           "protocol_sha256": binding.get("protocol_sha256")}
    need(pin["origin"] == origin, "origin_disagreement", what="pin")
    need(origin_values(project.pin) == [], "pin_has_origin")
    return pin


def upgrade_fetch(project_dir, target, consent_id, *, channel=None, cache_dir=None, use_helper=None):
    need(uc.VERSION_RE.fullmatch(target or ""), "invalid_target")
    cache = vp.cache_for_release(cache_dir)
    origin = cache.state().get("origin")
    need(isinstance(origin, str) and origin, "origin_disagreement", what="cache")
    binding = cache.bindings()["bindings"].get(target)
    need(isinstance(binding, dict), "release_unbound", version=target)
    project = uc.Project(project_dir, channel)
    pin = pin_state(project, cache, origin)
    need(binding["protocol_sha256"] == pin["protocol_sha256"], "protocol_migration_required")
    tag_ref, index_ref = f"refs/tags/v{target}", "refs/remotes/origin/release-index"
    with project.lock():
        # Everything again under the project's upgrade lock (the lock `upgrade fetch` holds).
        # Lock order: no cache lock is ever taken here.
        uc.require_no_foreign_setup(project.dir)
        require_no_unfinished_upgrade(project)
        again = pin_state(project, cache, origin)
        need(again["commit"] == pin["commit"], "pin_not_verified", status="changed")
        need(isinstance(consent_id, str) and os.path.isfile(project.root / "consents" / f"{consent_id}.json"),
             "consent_missing")
        consent = uc.load_consent(project, consent_id)
        uc.check_consent(project, consent, scope="fetch_only", target=target, pin=again)
        need(consent["source"] == origin, "origin_disagreement", what="consent")
        need(consent["target_commit"] == binding["commit"], "consent_target_mismatch")
        need(consent["target_tag_object"] in (None, binding["tag_object"]), "consent_target_mismatch")
        helper = bool(consent["credential_helper"]) if use_helper is None else use_helper
        old_tag, old_index = ref_value(project.pin, tag_ref), ref_value(project.pin, index_ref)
        need(old_tag in (None, binding["tag_object"]), "tag_exists_different")
        temp = uc.private_dir(project.root / f"fetch-{secrets.token_hex(6)}")
        try:
            repo = temp / "repo"
            ri.git(temp, "init", "--quiet", "--template=", str(repo))
            ri.isolated_fetch(repo, origin, helper, ri.release_refspecs(target))
            records, _evidence = iu.index_records(repo)  # strict: the whole index validates
            record = next((r for r in records if r["package_version"] == target), None)
            newer = sorted((r["package_version"] for r in records
                            if iu.version_key(r["package_version"]) > iu.version_key(target)), key=iu.version_key)
            need(record is not None and iu.binding_agrees(binding, record), "release_identity_changed", what="index")
            try:
                tag = uc.tag_identity(repo, target)
            except Stop as exc:
                raise Stop("release_identity_changed", what="tag", cause=exc.code) from exc
            for key in ("tag_object", "commit", "manifest_sha256", "protocol_sha256"):
                need(tag[key] == binding[key], "release_identity_changed", what=key)
            new_index = ref_value(repo, index_ref)
            need(new_index is not None, "release_identity_changed", what="index")
            copy_objects(repo, project.pin, [binding["tag_object"], new_index])
            # Both refs at once, each only from the value read before the fetch; never through
            # a symbolic ref (refused above, and --no-deref here).
            script = "".join(["start\n",
                              f"update {tag_ref} {binding['tag_object']} {old_tag or ZERO}\n",
                              f"update {index_ref} {new_index} {old_index or ZERO}\n",
                              "prepare\n", "commit\n"])
            done = subprocess.run(["git", "-C", str(project.pin), "update-ref", "--no-deref", "--stdin"],
                                  input=script.encode(), capture_output=True, env=ri._inert_git_env(), timeout=60,
                                  check=False)
            need(done.returncode == 0, "refs_changed")
        finally:
            shutil.rmtree(temp, ignore_errors=True)
        after = vp.check_project(project.dir, cache)
        need(after["status"] == "verified" and after["commit"] == pin["commit"] and origin_values(project.pin) == [],
             "pin_not_verified", status="changed")
    return {"status": "fetched", "target": target, "tag_object": binding["tag_object"], "commit": binding["commit"],
            "refs": {tag_ref: binding["tag_object"], index_ref: new_index},
            "consent": "unconsumed",
            "newer_indexed": newer,  # reported only: the target never changes implicitly
            "note": "Objects fetched before a stop may remain unreferenced in the pin's object store.",
            "next": "Run the pin's own `ai_comm.py upgrade prepare` with an upgrade consent."}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="install.sh --user upgrade-fetch")
    parser.add_argument("--project", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--consent", required=True)
    parser.add_argument("--channel")
    parser.add_argument("--cache")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code == 0:
            return 0
        print(json.dumps({"status": "stopped", "code": "invalid_arguments"}))
        return 2
    try:
        result = upgrade_fetch(args.project, args.target, args.consent, channel=args.channel, cache_dir=args.cache)
    except Stop as exc:
        print(json.dumps({"status": "stopped", "code": exc.code, **exc.details}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
