#!/usr/bin/env python3
"""Check a project's ai-comm pin with trusted code (design 018c v3, section 6).

Runs from a verified per-user cache release, never from the pin it checks, and runs
nothing from that pin. Git is used only for inert object reads (`rev-parse`,
`cat-file`) with a clean environment; every commit and tree object is re-hashed
here. The pin's working files are opened by safe relative path and hashed against
the manifest bound to a trusted binding (a record obtained only by this cache's own
verified fetches, `bindings.json`). Output is JSON:

  verified   the pin is exactly a trusted release; `class` names its upgrade path
  unknown    no trusted binding for its commit, or an unsupported layout
  modified   its commit is trusted but something differs (reported, never repaired)
  absent     the project has no pin

Exit 0 when verified, 1 for any other report, 2 when the check itself stopped.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

sys.dont_write_bytecode = True
RELEASE = Path(__file__).resolve().parents[1]
MANIFEST_FORMAT = "ai-comm-review-manifest/1"
MAX_ENTRY = 16 * 1024 * 1024
MAX_MANIFEST = 1024 * 1024


def _load(name, path):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


iu = _load("ai_comm_install_user", RELEASE / "scripts" / "install_user.py")
ri, uc, Stop, need = iu.ri, iu.uc, iu.Stop, iu.need


class Mismatch(Exception):
    """The pin's commit is trusted but this part of it differs."""

    def __init__(self, what, path=None):
        super().__init__(what)
        self.what, self.path = what, path


# --------------------------------------------------------------------------- classes


def compat_class(version):
    """The upgrade path the entry skill uses for a verified pin of `version`."""
    key = iu.version_key(version)
    if key >= (1, 5, 2):
        return "controller"
    if key in ((1, 5, 0), (1, 5, 1)):
        return "controller-check-workaround"
    if (1, 4, 0) <= key <= (1, 4, 2):
        return "legacy-manual"
    return "unsupported"


# --------------------------------------------------------------------------- inert git


def pin_git(pin, *args):
    """One inert Git read against the pin's own object store: explicit --git-dir,
    no work tree or index, no replacement objects, the clean controller environment
    (no global/system config, hooks disabled, core.fsmonitor=false, no lazy fetch)."""
    try:
        done = subprocess.run(["git", "--git-dir", str(Path(pin) / ".git"), "--no-replace-objects", *args],
                              capture_output=True, env=ri.inert_git_env(), timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Stop("git_unavailable") from exc
    return done.returncode, done.stdout


PROMISOR_KEY = re.compile(r"(?:extensions\.partialclone|core\.partialclonefilter|remote\..*\.(?:promisor|partialclonefilter))\Z")


def promisor_layout(pin):
    """Fail closed, before any object read, for every layout in which a missing
    object could make Git fetch: any partial-clone or promisor setting in the pin's
    effective configuration (including included files). Reading configuration starts
    no transport. Returns the reason, or None."""
    code, out = pin_git(pin, "config", "--list", "-z")
    if code != 0:
        return "config_unreadable"
    for entry in out.split(b"\x00"):
        key = entry.split(b"\n", 1)[0].decode("utf-8", "replace").lower()
        if key and PROMISOR_KEY.match(key):
            return "promisor_remote"
    return None


def _ref(pin, name):
    code, out = pin_git(pin, "rev-parse", "--verify", "--quiet", name)
    value = out.decode("ascii", "replace").strip()
    return value if code == 0 and ri._COMMIT_OID.fullmatch(value) else None


# --------------------------------------------------------------------------- working files


def read_entry(root_fd, relpath, limit=MAX_ENTRY):
    """Bytes and mode of one working file, by a safe relative path: no absolute
    path, no empty, `.` or `..` component, no symbolic link in any component, a
    regular file of bounded size, opened without following links or blocking."""
    parts = relpath.split("/")
    if not relpath or relpath.startswith("/") or any(p in ("", ".", "..") for p in parts) or "\x00" in relpath:
        raise Mismatch("unsafe_path", relpath)
    fds = []
    try:
        fd = root_fd
        for part in parts[:-1]:
            try:
                fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as exc:
                raise Mismatch("not_regular", relpath) from exc
            fds.append(fd)
        try:
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except FileNotFoundError as exc:
            raise Mismatch("missing", relpath) from exc
        except OSError as exc:
            raise Mismatch("not_regular", relpath) from exc
        try:
            st = os.fstat(file_fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
                raise Mismatch("not_regular", relpath)
            chunks, size = [], 0
            while True:
                chunk = os.read(file_fd, 1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise Mismatch("not_regular", relpath)
                chunks.append(chunk)
            return b"".join(chunks), st.st_mode
        finally:
            os.close(file_fd)
    finally:
        for opened in fds:
            os.close(opened)


# --------------------------------------------------------------------------- verification


def find_project(start):
    """The nearest directory at or above `start` that has a pin, else `start`."""
    here = Path(os.path.abspath(start))
    for candidate in (here, *here.parents):
        if os.path.lexists(candidate / ".tools" / "ai-comm"):
            return candidate
    return here


def _binding_for(bindings, commit):
    matches = [b for b in bindings.values() if isinstance(b, dict) and b.get("commit") == commit
               and all(isinstance(b.get(k), str) for k in iu.BINDING_KEYS)]
    return matches[0] if len(matches) == 1 else None


def verify(project, bindings):
    """The report for `project`'s pin against trusted `bindings` ({version: binding})."""
    project = Path(os.path.abspath(project))
    pin = project / ".tools" / "ai-comm"
    report = {"project": str(project), "pin": str(pin), "status": "absent", "version": None, "commit": None,
              "class": None}
    if not os.path.lexists(pin):
        return report
    uc.check_ancestry(project)
    pin_real = os.path.realpath(pin)
    need(not (str(RELEASE) == pin_real or str(RELEASE).startswith(pin_real + os.sep)), "verifier_inside_pin")
    try:
        return _verify(pin, bindings, report)
    except Mismatch as exc:
        report.update({"status": "modified", "what": exc.what})
        if exc.path:
            report["path"] = exc.path
        return report


def _unknown(report, reason, **extra):
    report.update({"status": "unknown", "reason": reason, **extra})
    return report


def _verify(pin, bindings, report):
    st = os.lstat(pin)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise Mismatch("pin_path")
    git_dir = pin / ".git"
    git_st = os.lstat(git_dir) if os.path.lexists(git_dir) else None
    if git_st is None or not stat.S_ISDIR(git_st.st_mode):
        return _unknown(report, "not_a_git_checkout")
    for name in ("alternates", "http-alternates"):
        if os.path.lexists(git_dir / "objects" / "info" / name):
            return _unknown(report, "alternate_object_store")
    layout = promisor_layout(pin)
    if layout:
        return _unknown(report, layout)
    head = _ref(pin, "HEAD")
    if head is None:
        return _unknown(report, "no_head")
    report["commit"] = head
    binding = _binding_for(bindings, head)
    if binding is None:
        return _unknown(report, "no_trusted_binding",
                        hint="a consented `install.sh --user check --fetch-permitted` records bindings for "
                             "every indexed release")
    version = binding["version"]
    report["version"] = version
    tag = _ref(pin, f"refs/tags/v{version}")
    if tag is not None and tag != binding["tag_object"]:
        raise Mismatch("tag")
    try:
        expected = ri.commit_tree(git_dir, head, "pin_object")
        if "RELEASE-MANIFEST.json" not in expected:
            raise Mismatch("manifest")
        committed = ri.git_object(git_dir, "blob", expected["RELEASE-MANIFEST.json"][0], "pin_object")
    except Stop as exc:
        raise Mismatch("object") from exc
    if uc.sha256_bytes(committed) != binding["manifest_sha256"]:
        raise Mismatch("manifest")
    try:
        manifest = json.loads(committed.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise Mismatch("manifest") from exc
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if (not isinstance(files, list) or manifest.get("format") != MANIFEST_FORMAT
            or manifest.get("protocol_sha256") != binding["protocol_sha256"]):
        raise Mismatch("manifest")
    entries = {}
    for entry in files:
        if (not isinstance(entry, dict) or not isinstance(entry.get("path"), str)
                or not isinstance(entry.get("sha256"), str) or not isinstance(entry.get("bytes"), int)
                or entry["path"] in entries):
            raise Mismatch("manifest")
        entries[entry["path"]] = entry
    if set(entries) != set(expected) - {"RELEASE-MANIFEST.json"}:
        raise Mismatch("file_set")
    if "PROTOCOL.md" in entries and entries["PROTOCOL.md"]["sha256"] != binding["protocol_sha256"]:
        raise Mismatch("protocol")
    root_fd = ri.open_dir_nofollow(str(pin))
    try:
        working, _ = read_entry(root_fd, "RELEASE-MANIFEST.json", MAX_MANIFEST)
        if working != committed:
            raise Mismatch("working_manifest", "RELEASE-MANIFEST.json")
        for path in sorted(entries):
            data, mode = read_entry(root_fd, path)
            entry = entries[path]
            if len(data) != entry["bytes"] or uc.sha256_bytes(data) != entry["sha256"]:
                raise Mismatch("content", path)
            if bool(mode & 0o100) != expected[path][1]:
                raise Mismatch("mode", path)
    finally:
        os.close(root_fd)
    report.update({"status": "verified", "tag_object": binding["tag_object"], "class": compat_class(version)})
    return report


# --------------------------------------------------------------------------- offers


def newer_verified(cache, version):
    """Trusted releases newer than `version`: in the cache's index and bound."""
    index = cache.index() or {}
    bindings = cache.bindings()["bindings"]
    out = []
    for record in index.get("releases", []):
        candidate = record.get("package_version") if isinstance(record, dict) else None
        if (isinstance(candidate, str) and uc.VERSION_RE.fullmatch(candidate)
                and iu.version_key(candidate) > iu.version_key(version)
                and iu.binding_agrees(bindings.get(candidate), record)):
            out.append(candidate)
    return sorted(out, key=iu.version_key)


OFFER_CLASSES = ("controller", "controller-check-workaround")


def controller_module():
    """This cache release's own upgrade controller (trusted code, never the pin's),
    the copy the installer already loaded."""
    return uc


PROMPT_KEYS = {"v", "schema", "versions", "never"}
PROMPT_ENTRY_TYPES = {"snooze_until": int, "snooze_level": int, "skip": bool, "required_shown": bool}
MAX_PROMPT = 1 << 20


def _read_prompt_file(path):
    """Bytes of an existing private regular file, or None when it does not exist.

    Opened without following a symlink and without blocking (a FIFO or device is
    refused from its descriptor before any read)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise Stop("unsafe_runtime_path") from exc
    with os.fdopen(fd, "rb") as stream:
        st = os.fstat(stream.fileno())
        need(stat.S_ISREG(st.st_mode), "unsafe_runtime_path")
        need(st.st_uid == os.getuid(), "unsafe_runtime_owner")
        need(stat.S_IMODE(st.st_mode) & 0o077 == 0, "unsafe_runtime_mode")
        data = stream.read(MAX_PROMPT + 1)
    need(len(data) <= MAX_PROMPT, "runtime_file_too_large")
    return data


def _exact(value, kind):
    return type(value) is kind and (kind is not int or value >= 0)


def valid_prompt_state(state, ctl):
    """Exactly the controller's prompt.json shape, with every consumed field typed."""
    return (isinstance(state, dict) and set(state) == PROMPT_KEYS and _exact(state["v"], int)
            and state["v"] == 1 and state["schema"] == ctl.PROMPT_SCHEMA and _exact(state["never"], bool)
            and isinstance(state["versions"], dict)
            and all(isinstance(version, str) and ctl.VERSION_RE.fullmatch(version) and isinstance(entry, dict)
                    and set(entry) <= set(PROMPT_ENTRY_TYPES)
                    and all(_exact(entry[k], PROMPT_ENTRY_TYPES[k]) for k in entry)
                    for version, entry in state["versions"].items()))


def prompt_state(project):
    """The project's recorded offer answers, read only: (state, None) or (None, code).

    Follows the controller's file rules (no symlinks, user-owned, private modes),
    never blocks, and creates, locks or repairs nothing. No upgrade directory or no
    prompt file means never asked; an existing empty or malformed file is invalid."""
    ctl = controller_module()
    fresh = {"v": 1, "schema": ctl.PROMPT_SCHEMA, "versions": {}, "never": False}
    try:
        local = Path(project) / ".ai-comm-local"
        root = local / "upgrade"
        if not os.path.lexists(local) or not os.path.lexists(root):
            return fresh, None
        need(ctl._mode_ok_ancestor(os.lstat(local)), "unsafe_runtime_path")
        st = os.lstat(root)
        need(stat.S_ISDIR(st.st_mode), "unsafe_runtime_path")
        need(st.st_uid == os.getuid(), "unsafe_runtime_owner")
        need(stat.S_IMODE(st.st_mode) & 0o077 == 0, "unsafe_runtime_mode")
        data = _read_prompt_file(root / "prompt.json")
        if data is None:
            return fresh, None
        try:
            state = json.loads(data.decode("utf-8"))
        except (UnicodeError, ValueError):
            return None, "prompt_state_invalid"
        if not valid_prompt_state(state, ctl):
            return None, "prompt_state_invalid"
        return state, None
    except Stop as exc:
        return None, exc.code
    except OSError:
        return None, "unsafe_runtime_path"


def project_offer(cache, project, report, at=None):
    """The owner offer for a verified pin older than a trusted release, or None.

    Every target value comes from this cache's index and bindings, never from the
    pin's refs. "now" maps to the controller answer `yes`, "later" to `not_now`."""
    if report.get("class") not in OFFER_CLASSES or not report.get("newer_verified"):
        return None
    target = report["newer_verified"][-1]
    bindings = cache.bindings()["bindings"]
    bound, current = bindings.get(target), bindings.get(report["version"]) or {}
    record = next((r for r in (cache.index() or {}).get("releases", [])
                   if isinstance(r, dict) and r.get("package_version") == target), {})
    offer = {"pinned": report["version"], "pinned_commit": report["commit"], "target": target, "target_tag": bound["tag"],
             "target_commit": bound["commit"], "target_tag_object": bound["tag_object"],
             "advisory": record.get("advisory"), "protocol_change":
             bound.get("protocol_sha256") != current.get("protocol_sha256"),
             "answers": {"now": "yes", "later": "not_now"}, "should_ask": False}
    if offer["protocol_change"]:
        offer["blocked"] = "protocol_migration_required"
        return offer
    state, blocked = prompt_state(project)
    if blocked:
        offer["blocked"] = blocked
        return offer
    ctl = controller_module()
    # The cache index keeps no `affects` range, so a required advisory's applicability is
    # unknown here: it never overrides the owner's skip or never (the pin's check still can).
    offer["should_ask"] = bool(ctl.should_ask(state, target, "none", ctl.now() if at is None else at))
    until = state["versions"].get(target, {}).get("snooze_until")
    if not offer["should_ask"] and isinstance(until, int):
        offer["snooze_until"] = until
    return offer


def legacy_status(pin, version):
    """For a verified 1.4.x pin (no `upgrade` command): newer versions its own
    fetched release index lists, read with inert plumbing. Untrusted information,
    reported as such; offers use the cache's own verified bindings."""
    ref = _ref(pin, "refs/remotes/origin/release-index")
    if ref is None:
        return {"pin_index": "absent"}
    code, data = pin_git(pin, "cat-file", "blob", f"{ref}:RELEASES.json")
    try:
        index = json.loads(data.decode("utf-8")) if code == 0 and len(data) <= MAX_MANIFEST else None
    except (UnicodeError, ValueError):
        index = None
    if not isinstance(index, dict) or not isinstance(index.get("releases"), list):
        return {"pin_index": "unreadable"}
    listed = sorted({r["package_version"] for r in index["releases"] if isinstance(r, dict)
                     and isinstance(r.get("package_version"), str) and uc.VERSION_RE.fullmatch(r["package_version"])
                     and iu.version_key(r["package_version"]) > iu.version_key(version)}, key=iu.version_key)
    return {"pin_index": "read", "pin_index_newer": listed, "trust": "untrusted pin data; informational only"}


# --------------------------------------------------------------------------- entry points


def cache_for_release(cache_dir=None):
    """The cache whose bindings this check uses. A cached release checks itself
    first, so a changed verifier never vouches for a pin."""
    if cache_dir is None and RELEASE.parent.name == "releases":
        cache_dir = RELEASE.parent.parent
    cache = iu.Cache(cache_dir or iu.cache_root()).open(create=False)
    if RELEASE.parent == cache.root / "releases":
        try:
            cache.check_release(RELEASE.name)
        except Stop as exc:
            raise Stop("verifier_release_changed", what=exc.details.get("what")) from exc
    return cache


def check_project(project, cache):
    report = verify(project, cache.bindings()["bindings"])
    if report["status"] == "verified":
        report["newer_verified"] = newer_verified(cache, report["version"])
        offer = project_offer(cache, project, report)
        if offer is not None:
            report["offer"] = offer
        if report["class"] == "legacy-manual":
            report["legacy"] = legacy_status(Path(report["pin"]), report["version"])
    index = cache.index()
    report["cache_index_refreshed_at"] = index.get("refreshed_at") if index else None
    report["verifier_release"] = str(RELEASE)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", help="project directory (default: the nearest one with a pin, from here)")
    parser.add_argument("--cache", help="per-user cache (default: the cache this release belongs to)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code == 0:
            return 0  # --help
        print(json.dumps({"status": "stopped", "code": "invalid_arguments"}))
        return 2
    try:
        cache = cache_for_release(args.cache)
        report = check_project(args.project or find_project(os.getcwd()), cache)
        print(json.dumps(report, sort_keys=True))
        return 0 if report["status"] == "verified" else 1
    except Stop as exc:
        safe = {k: v for k, v in exc.details.items() if isinstance(v, (str, int, bool)) or v is None}
        print(json.dumps({"status": "stopped", "code": exc.code, **safe}, sort_keys=True))
        return 2
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"status": "stopped", "code": "operation_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
