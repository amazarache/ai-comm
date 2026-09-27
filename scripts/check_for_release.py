#!/usr/bin/env python3
"""Optionally fetch the permitted release refs and report verified release advice."""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ai_comm as ac

REFSPECS = (
    "refs/heads/release-index:refs/remotes/origin/release-index",
    "refs/tags/v*:refs/tags/v*",
)
SCHEMA = "ai-comm-release-check/1"
RELEASE_STATUSES = {"ok", "index_missing", "index_invalid", "index_inconsistent",
                    "not_git_checkout"}
MAX_ORIGIN_LENGTH = 2048


class CheckError(Exception):
    def __init__(self, code, status="error"):
        super().__init__(code)
        self.code = code
        self.status = status


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise CheckError("invalid_arguments")


def _normalize_origin(value):
    if (not isinstance(value, str) or not value or len(value) > MAX_ORIGIN_LENGTH or
            any(char.isspace() or ord(char) < 33 or ord(char) > 126 for char in value)):
        raise CheckError("invalid_origin")
    try:
        parsed = urlsplit(value)
        # Fetch permission is tied to an explicit HTTPS origin, without URL-carried
        # credentials, query parameters, or fragments that could hide another target.
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise CheckError("invalid_origin")
        if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
            raise CheckError("credential_bearing_origin")
        _ = parsed.port  # Reject malformed ports before comparing or fetching.
    except ValueError as exc:
        raise CheckError("invalid_origin") from exc
    path = parsed.path.removesuffix("/").removesuffix(".git")
    return parsed.scheme + "://" + parsed.netloc + path


def _manifest_bound_origin(repo):
    """Read RELEASE_ORIGIN only from package code verified by its local manifest."""
    try:
        identity = ac.package_identity(repo)
    except Exception as exc:
        raise CheckError("package_identity_invalid") from exc
    if identity.get("source") == "unknown":
        raise CheckError("package_identity_invalid")
    try:
        if identity.get("source") == "git":
            source = ac.work_git(repo, "show", "HEAD:ai_comm.py")
            manifest_raw = ac.work_git(repo, "show", "HEAD:RELEASE-MANIFEST.json")
            manifest = ac.parse(manifest_raw, ac.MAX_ARTIFACT)
            matches = [entry for entry in manifest.get("files", [])
                       if isinstance(entry, dict) and entry.get("path") == "ai_comm.py"]
            if len(matches) != 1 or ac.sha(source) != matches[0].get("sha256"):
                raise CheckError("package_manifest_mismatch")
        elif identity.get("source") == "archive":
            source = (repo / "ai_comm.py").read_bytes()
        else:
            raise CheckError("package_identity_invalid")
        text = source.decode("utf-8")
    except (OSError, UnicodeError, ac.ProtocolError, KeyError, TypeError, AttributeError) as exc:
        raise CheckError("package_identity_invalid") from exc
    matches = re.findall(r'^RELEASE_ORIGIN = "([^"\r\n]+)"$', text, re.MULTILINE)
    if len(matches) != 1:
        raise CheckError("package_origin_unavailable")
    return matches[0]


def _run(argv, *, cwd=None, runner=subprocess.run):
    return runner(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                  env=ac.git_env({"GIT_NO_LAZY_FETCH": "1"}), timeout=120, check=False)


def _local_status(repo, runner):
    proc = _run([sys.executable, str(repo / "ai_comm.py"), "release-status"], runner=runner)
    if proc.returncode != 0:
        raise CheckError("release_status_failed", "index_invalid")
    try:
        value = json.loads(proc.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CheckError("release_status_invalid_json", "index_invalid") from exc
    if not isinstance(value, dict) or value.get("status") not in RELEASE_STATUSES:
        raise CheckError("release_status_invalid_shape", "index_invalid")
    return value


def _run_upgrade_plan(repo, runner):
    proc = _run([sys.executable, str(repo / "ai_comm.py"), "upgrade-plan"], runner=runner)
    if proc.returncode != 0:
        raise CheckError("upgrade_plan_failed", "index_invalid")
    try:
        value = json.loads(proc.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CheckError("upgrade_plan_invalid_json", "index_invalid") from exc
    if not isinstance(value, dict) or not isinstance(value.get("owner_notice"), str):
        raise CheckError("upgrade_plan_invalid_shape", "index_invalid")
    return value


def _active_advisory(status):
    values = status.get("active_advisories", [])
    if not isinstance(values, list):
        raise CheckError("release_status_invalid_shape", "index_invalid")
    candidates = [item for item in values if isinstance(item, dict) and
                  item.get("advisory") in {"recommended", "required"}]
    if not candidates:
        return None
    return max(candidates, key=lambda item: (
        item["advisory"] == "required", ac.semver(item.get("package_version", "0.0.0"))))


def _newer_advice(status):
    values = status.get("advice", [])
    if not isinstance(values, list):
        raise CheckError("release_status_invalid_shape", "index_invalid")
    candidates = [item for item in values if isinstance(item, dict)]
    if not candidates:
        return None
    return max(candidates, key=lambda item: ac.semver(item.get("package_version", "0.0.0")))


def _result(status="error", *, result="error", fetched=False, fetch_error=None,
            pinned=None, latest=None, channel_action=None, advisory="none",
            advisory_applies=False, owner_notice=None):
    return {"schema": SCHEMA, "result": result, "status": status,
            "fetched": bool(fetched), "fetch_error": fetch_error,
            "pinned": pinned, "latest": latest, "channel_action": channel_action,
            "advisory": advisory, "advisory_applies_to_pinned": bool(advisory_applies),
            "owner_notice": owner_notice}


def run_check(*, repo=ROOT, fetch_permitted=False, expected_origin=None,
              runner=subprocess.run):
    repo = Path(repo).resolve()
    fetched = False
    status_name = "error"
    pinned = None
    latest = None
    try:
        if fetch_permitted:
            try:
                identity = ac.package_identity(repo)
                pinned = identity.get("version")
            except Exception as exc:
                raise CheckError("package_identity_invalid") from exc
            if identity.get("source") != "git":
                raise CheckError("not_git_checkout", "not_git_checkout")
            trusted_origin = expected_origin if expected_origin is not None else _manifest_bound_origin(repo)
            expected = _normalize_origin(trusted_origin)
            remote = _run(["git", "-C", str(repo), "remote", "get-url", "origin"], runner=runner)
            if remote.returncode != 0:
                raise CheckError("origin_missing")
            if len(remote.stdout) > MAX_ORIGIN_LENGTH + 1:
                raise CheckError("invalid_origin")
            try:
                actual = remote.stdout.decode("utf-8").removesuffix("\n")
            except UnicodeError as exc:
                raise CheckError("invalid_origin") from exc
            if _normalize_origin(actual) != expected:
                raise CheckError("origin_mismatch")
            fetch_args = ["git", "-C", str(repo), "fetch", "--no-tags",
                          "--no-recurse-submodules", "origin", *REFSPECS]
            fetched = True
            proc = _run(fetch_args, runner=runner)
            if proc.returncode != 0:
                raise CheckError("fetch_failed")
        release_data = _local_status(repo, runner)
        current_status = release_data.get("status")
        status_name = current_status
        pinned = release_data.get("pinned_version")
        if current_status != "ok":
            return (_result(current_status,
                            fetched=fetched, fetch_error=current_status or "release_status_invalid",
                            pinned=pinned), 2)
        latest = release_data.get("latest_version")
        try:
            ac.semver(pinned, "pinned")
            ac.semver(latest, "latest")
        except ac.ProtocolError as exc:
            raise CheckError("release_status_invalid_version", "index_invalid") from exc
        advisory = _active_advisory(release_data)
        advice = _newer_advice(release_data)
        owner_notice = None
        if advice is not None:
            owner_notice = _run_upgrade_plan(repo, runner)["owner_notice"]
        if advisory is not None:
            return (_result("ok", result="advisory_applies", fetched=fetched,
                            pinned=pinned, latest=latest,
                            channel_action=advisory.get("channel_action"),
                            advisory=advisory["advisory"], advisory_applies=True,
                            owner_notice=owner_notice), 11)
        if advice is not None:
            latest_advisory = advice.get("advisory", "optional")
            if latest_advisory not in {"optional", "recommended", "required"}:
                raise CheckError("release_status_invalid_shape", "index_invalid")
            return (_result("ok", result="update_available", fetched=fetched,
                            pinned=pinned, latest=latest,
                            channel_action=advice.get("channel_action"),
                            advisory=latest_advisory, owner_notice=owner_notice), 10)
        return (_result("ok", result="up_to_date", fetched=fetched, pinned=pinned,
                        latest=latest), 0)
    except CheckError as exc:
        return (_result(exc.status, fetched=fetched, fetch_error=exc.code, pinned=pinned,
                        latest=latest if status_name == "ok" else None), 2)
    except Exception:
        safe_status = status_name if status_name in RELEASE_STATUSES else "error"
        return (_result(safe_status, fetched=fetched, fetch_error="operation_failed",
                        pinned=pinned, latest=latest if status_name == "ok" else None), 2)


def parser():
    result = JsonArgumentParser(description=__doc__)
    result.add_argument("--fetch-permitted", action="store_true",
                        help="owner-authorized opt-in to fetch only the release-index branch and v* tags")
    result.add_argument("--expected-origin",
                        help="explicit owner override for the expected origin URL")
    return result


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        value, code = run_check(fetch_permitted=args.fetch_permitted,
                                expected_origin=args.expected_origin)
    except CheckError as exc:
        value, code = _result(exc.status, fetch_error=exc.code), 2
    print(json.dumps(value, ensure_ascii=True, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
