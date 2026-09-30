#!/usr/bin/env python3
"""Optionally fetch the permitted release refs and report verified release advice."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
ac = None

REFSPECS = (
    "refs/heads/release-index:refs/remotes/origin/release-index",
    "refs/tags/v*:refs/tags/v*",
)
SCHEMA = "ai-comm-release-check/1"
RELEASE_STATUSES = {"ok", "index_missing", "index_invalid", "index_inconsistent",
                    "index_tags_missing", "not_git_checkout"}
MAX_ORIGIN_LENGTH = 2048
MAX_CREDENTIAL_HELPER_BYTES = 65536
MAX_CREDENTIAL_HELPER_COUNT = 128
MAX_CREDENTIAL_HELPER_VALUE_BYTES = 8192


class CheckError(Exception):
    def __init__(self, code, status="error"):
        super().__init__(code)
        self.code = code
        self.status = status


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise CheckError("invalid_arguments")


def _load_package(repo):
    """Load the package only inside run_check's JSON-producing error boundary."""
    global ac
    if ac is not None:
        return ac
    source = Path(repo) / "ai_comm.py"
    try:
        spec = importlib.util.spec_from_file_location("ai_comm_release_package", source)
        if spec is None or spec.loader is None:
            raise ImportError("package module unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
        ac = module
        return ac
    except Exception as exc:
        raise CheckError("package_unavailable", "package_unavailable") from exc


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


def _run(argv, *, cwd=None, runner=subprocess.run, env=None):
    return runner(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                  env=(env if env is not None else
                       ac.git_env({"GIT_NO_LAZY_FETCH": "1"})),
                  timeout=120, check=False)


def _credential_config_env():
    """Expose only the user's normal global config to a read-only helper lookup."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", LC_ALL="C")
    return env


def _read_credential_helpers(*, runner=subprocess.run):
    """Read bounded, NUL-delimited credential helper values from global config."""
    proc = _run(["git", "config", "--global", "--null", "--get-all",
                 "credential.helper"], runner=runner,
                env=_credential_config_env())
    stdout = proc.stdout
    if not isinstance(stdout, bytes) or len(stdout) > MAX_CREDENTIAL_HELPER_BYTES:
        raise CheckError("credential_helper_config_unreadable")
    if proc.returncode == 1 and stdout == b"":
        raise CheckError("credential_helper_not_configured")
    if proc.returncode != 0:
        raise CheckError("credential_helper_config_unreadable")
    if not stdout or not stdout.endswith(b"\0"):
        raise CheckError("credential_helper_config_unreadable")
    raw_values = stdout[:-1].split(b"\0")
    if (len(raw_values) > MAX_CREDENTIAL_HELPER_COUNT or
            any(len(value) > MAX_CREDENTIAL_HELPER_VALUE_BYTES for value in raw_values)):
        raise CheckError("credential_helper_config_unreadable")
    try:
        values = [value.decode("utf-8", "strict") for value in raw_values]
    except UnicodeError as exc:
        raise CheckError("credential_helper_config_unreadable") from exc

    # Git treats an empty helper value as a reset of the accumulated helper list.
    effective = []
    for value in values:
        if value == "":
            effective.clear()
        else:
            effective.append(value)
    if not effective:
        raise CheckError("credential_helper_not_configured")
    return values


def _fetch_error(stderr):
    """Map Git's C-locale response to bounded categories without exposing it."""
    if not isinstance(stderr, bytes):
        return "fetch_failed"
    if re.search(rb"(?:HTTP/[0-9.]+\s+|returned error:\s*)(?:401|403)(?:\D|$)",
                 stderr, re.IGNORECASE):
        return "auth_failed"
    if re.search(rb"(?:HTTP/[0-9.]+\s+|returned error:\s*)30[12378](?:\D|$)",
                 stderr, re.IGNORECASE):
        return "origin_redirect_refused"
    return "fetch_failed"


def _redirect_policy_args(origin):
    """Return redirects disabled for the isolated fetch configuration."""
    base = origin.rstrip("/")
    request_bases = (base, base + "/info/refs", base + "/git-upload-pack",
                     base + "/objects")
    args = [("http.followRedirects", "false")]
    for request_url in request_bases:
        args.append((f"http.{request_url}.followRedirects", "false"))
    return args


def _fetch_config_env(origin, helpers):
    """Pass only explicitly approved config through Git's non-argv config channel."""
    entries = [("credential.helper", "")]
    entries.extend(("credential.helper", helper) for helper in helpers)
    entries.extend(_redirect_policy_args(origin))
    env = ac.git_env({"GIT_CONFIG_GLOBAL": os.devnull,
                      "GIT_CONFIG_NOSYSTEM": "1",
                      "GIT_TERMINAL_PROMPT": "0",
                      "GIT_NO_LAZY_FETCH": "1",
                      "GIT_CONFIG_COUNT": str(len(entries))})
    for index, (key, value) in enumerate(entries):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def _git_result(argv, *, runner, env):
    proc = _run(argv, runner=runner, env=env)
    if proc.returncode != 0:
        raise CheckError("fetch_failed")
    return proc.stdout


def _read_release_refs(location, *, runner, env, worktree=False):
    prefix = ["git", "-C", str(location)] if worktree else ["git", f"--git-dir={location}"]
    raw = _git_result([*prefix, "for-each-ref",
                       "--format=%(refname)%00%(objectname)%00%(symref)",
                       "refs/remotes/origin/release-index", "refs/tags/v*"],
                      runner=runner, env=env)
    if not isinstance(raw, bytes) or (raw and not raw.endswith(b"\n")):
        raise CheckError("fetch_failed")
    refs = {}
    for line in raw.splitlines():
        fields = line.split(b"\0")
        if (len(fields) != 3 or fields[2] != b"" or
                re.fullmatch(rb"[0-9a-f]{40,64}", fields[1]) is None):
            raise CheckError("fetch_failed")
        try:
            name, oid, _symref = (item.decode("ascii", "strict") for item in fields)
        except UnicodeError as exc:
            raise CheckError("fetch_failed") from exc
        if (name != "refs/remotes/origin/release-index" and
                not (name.startswith("refs/tags/v") and len(name) > len("refs/tags/v"))):
            raise CheckError("fetch_failed")
        refs[name] = oid
    return refs


def _publish_release_refs(repo, refs, *, runner, env):
    if not refs:
        return
    old = _read_release_refs(repo, runner=runner, env=env, worktree=True)
    commands = ["start", "option no-deref"]
    zero = "0" * len(next(iter(refs.values())))
    for name, oid in sorted(refs.items()):
        commands.append(f"update {name} {oid} {old.get(name, zero)}")
    commands.extend(("prepare", "commit", ""))
    proc = runner(["git", "-C", str(repo), "update-ref", "--stdin"],
                  input="\n".join(commands).encode("ascii"),
                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                  timeout=120, check=False)
    if proc.returncode != 0:
        raise CheckError("fetch_failed")


def _fetch_isolated(repo, origin, helpers, *, runner=subprocess.run):
    """Fetch through a fresh Git directory, sharing only the target object store."""
    clean_env = ac.git_env({"GIT_CONFIG_GLOBAL": os.devnull,
                            "GIT_CONFIG_NOSYSTEM": "1",
                            "GIT_NO_LAZY_FETCH": "1"})
    object_format_raw = _git_result(["git", "-C", str(repo), "rev-parse", "--show-object-format"],
                                    runner=runner, env=clean_env)
    try:
        object_format = object_format_raw.decode("ascii").strip()
    except (AttributeError, UnicodeError) as exc:
        raise CheckError("fetch_failed") from exc
    if object_format not in {"sha1", "sha256"}:
        raise CheckError("fetch_failed")
    objects_raw = _git_result(["git", "-C", str(repo), "rev-parse", "--git-path", "objects"],
                              runner=runner, env=clean_env)
    try:
        objects = Path(objects_raw.decode("utf-8").strip())
        if not objects.is_absolute():
            objects = repo / objects
        objects = objects.resolve()
    except (AttributeError, UnicodeError, OSError) as exc:
        raise CheckError("fetch_failed") from exc
    if not objects.is_absolute() or not objects.is_dir():
        raise CheckError("fetch_failed")
    with tempfile.TemporaryDirectory(prefix="ai-comm-release-fetch-") as temp:
        git_dir = Path(temp) / "git"
        init_args = ["git", "init", "--bare"]
        if object_format == "sha256":
            init_args.append("--object-format=sha256")
        init_args.append(str(git_dir))
        _git_result(init_args, runner=runner, env=clean_env)
        fetch_env = _fetch_config_env(origin, helpers) | {"GIT_OBJECT_DIRECTORY": str(objects)}
        # Seed only the exact permitted refs so normal tag/release-index update rules apply.
        existing = _read_release_refs(repo, runner=runner, env=clean_env, worktree=True)
        for name, oid in existing.items():
            _git_result(["git", f"--git-dir={git_dir}", "update-ref", name, oid],
                        runner=runner, env=fetch_env)
        fetch_args = ["git", f"--git-dir={git_dir}", "fetch", "--refmap=", "--no-tags",
                      "--no-recurse-submodules", origin, *REFSPECS]
        proc = _run(fetch_args, runner=runner, env=fetch_env)
        if proc.returncode != 0:
            raise CheckError(_fetch_error(proc.stderr))
        fetched_refs = _read_release_refs(git_dir, runner=runner, env=fetch_env)
        _publish_release_refs(repo, fetched_refs, runner=runner, env=clean_env)


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
        raise CheckError("upgrade_plan_failed", "ok")
    try:
        value = json.loads(proc.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CheckError("upgrade_plan_invalid_json", "ok") from exc
    if not isinstance(value, dict) or not isinstance(value.get("owner_notice"), str):
        raise CheckError("upgrade_plan_invalid_shape", "ok")
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


def run_check(*, repo=ROOT, fetch_permitted=False, use_git_credential_helper=False,
              expected_origin=None,
              runner=subprocess.run):
    repo = Path(repo).resolve()
    fetched = False
    status_name = "error"
    pinned = None
    latest = None
    try:
        _load_package(repo)
        if use_git_credential_helper and not fetch_permitted:
            raise CheckError("credential_helper_requires_fetch_permission")
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
            remote = _run(["git", "-C", str(repo), "config", "--local", "--null",
                           "--get-all", "remote.origin.url"], runner=runner)
            if remote.returncode != 0:
                raise CheckError("origin_missing")
            if (not isinstance(remote.stdout, bytes) or len(remote.stdout) > MAX_ORIGIN_LENGTH + 1 or
                    not remote.stdout.endswith(b"\0") or remote.stdout.count(b"\0") != 1):
                raise CheckError("invalid_origin")
            try:
                actual = remote.stdout[:-1].decode("utf-8")
            except UnicodeError as exc:
                raise CheckError("invalid_origin") from exc
            if _normalize_origin(actual) != expected:
                raise CheckError("origin_mismatch")
            helpers = (_read_credential_helpers(runner=runner)
                       if use_git_credential_helper else [])
            fetched = True
            _fetch_isolated(repo, trusted_origin, helpers, runner=runner)
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
        try:
            advisory = _active_advisory(release_data)
            advice = _newer_advice(release_data)
        except CheckError:
            raise
        except Exception as exc:
            raise CheckError("release_status_invalid_shape", "index_invalid") from exc
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
                        latest=latest if exc.status == "ok" else None), 2)
    except Exception:
        safe_status = status_name if status_name in RELEASE_STATUSES else "error"
        return (_result(safe_status, fetched=fetched, fetch_error="operation_failed",
                        pinned=pinned, latest=latest if status_name == "ok" else None), 2)


def parser():
    result = JsonArgumentParser(description=__doc__)
    result.add_argument("--fetch-permitted", action="store_true",
                        help="owner-authorized opt-in to fetch only the release-index branch and v* tags")
    result.add_argument("--use-git-credential-helper", action="store_true",
                        help="run configured global Git credential helper(s) for this verified-origin fetch only")
    result.add_argument("--expected-origin",
                        help="explicit owner override for the expected origin URL")
    return result


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        value, code = run_check(fetch_permitted=args.fetch_permitted,
                                use_git_credential_helper=args.use_git_credential_helper,
                                expected_origin=args.expected_origin)
    except CheckError as exc:
        value, code = _result(exc.status, fetch_error=exc.code), 2
    print(json.dumps(value, ensure_ascii=True, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
