#!/usr/bin/env python3
"""Plan, install and remove the optional wake adapters (design 024, installer design v4).

Run as ``install.sh --user wake plan|install|remove …`` from a verified per-user cache
release. It verifies that release and then the project's pin with the trusted
verifier, and continues only for a ``verified`` pin. No unverified pin code runs; the
two permitted pin-derived paths, used only after verification, are the pin CLI's
read-only ``registry sessions`` (a subprocess, every page) and the queue renderer,
loaded from bytes checked against the trusted manifest.

- ``plan`` is read-only: it writes nothing, reads no runtime socket and starts nothing.
  It prints the exact objects ``install`` would write and the owner's next steps.
- ``install`` recomputes the plan and requires its digest. For exit it freezes the
  adapter bundle; for queue it also publishes ``config.json`` and then the instance's own
  unit file, under the shared ``instance.lock``. It never runs ``init``, never reloads,
  enables or starts a unit.
- ``remove`` deletes only this instance's unchanged data, its config and its recorded
  unit, never a lock file, bundle, receipt, the channel or the pin. Queue removal writes a
  durable ``remove-intent.json`` first, so any interrupted removal can be rerun.

Output is one JSON object. A refusal is ``{"status": "stopped", "code": CODE}`` with
exit 75 (transient) or 78 (permanent). Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
RELEASE = Path(__file__).resolve().parents[1]


def _load(name, path):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


common = _load("ai_comm_release_wake_common", RELEASE / "adapters" / "common.py")
WakeError = common.WakeError

KINDS = {"exit": ("exit_wake.py", "common.py"),
         "queue": ("queue_wake.py", "common.py", "queue-wake@.service", "queue_wake.lock")}
LOCK_NAMES = frozenset({"lock", "phase.lock", "observer.lock", "guided.lock", "instance.lock"})
EXIT_DATA = ("cursor.json", "run.json", "pause", "pause-ack", "peer-alarm.json")
QUEUE_DATA = ("config.json", "state.json", "paused.json")
INTENT = "remove-intent.json"
REBIND = re.compile(r"rebind-[0-9a-f]{12}\Z")
HEX = re.compile(r"[0-9a-f]{64}\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
MIN_SYSTEMD = 254
MAX_FILE = 4 * 1024 * 1024
MAX_CLI_OUTPUT = 256 * 1024
SYSTEMCTL = "/usr/bin/systemctl"
REGISTRY_PAGE = 100
REGISTRY_MAX_PAGES = 100


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    """The exact bytes common.write_private_json writes."""
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def read_regular(path, limit=MAX_FILE):
    """Bytes of a regular file opened without following a final symlink, bounded."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise common._os_error(exc) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise WakeError("file_not_regular")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > limit:
                raise WakeError("file_too_large")
            chunks.append(chunk)
    except OSError as exc:
        raise common._os_error(exc) from exc
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- environment


class Host:
    """Everything that reaches outside this process; tests replace parts of it."""

    def __init__(self, env=None):
        self.env = dict(os.environ if env is None else env)

    def verify(self, project):
        vp = _load("ai_comm_verify_pin", RELEASE / "scripts" / "verify_pin.py")
        try:
            return vp.check_project(project, vp.cache_for_release())
        except vp.Stop as exc:
            raise WakeError("pin_check_stopped") from exc

    def cli_json(self, argv):
        try:
            done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WakeError("channel_state_unavailable", "transient") from exc
        if done.returncode != 0 or len(done.stdout) > MAX_CLI_OUTPUT:
            raise WakeError("channel_state_unavailable", "transient")
        try:
            value = json.loads(done.stdout.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise WakeError("channel_state_invalid") from exc
        if not isinstance(value, dict):
            raise WakeError("channel_state_invalid")
        return value

    def registry(self, cli, channel):
        """Every handle's current binding from the complete paged registry (review 1345 Q1).

        `state` is a size-trimmed summary, so it is never used as the registry. A handle's
        current binding is its highest generation; every page must share one head."""
        current, offset, head = {}, 0, None
        for _ in range(REGISTRY_MAX_PAGES):
            page = self.cli_json([sys.executable, "-I", "-B", str(cli), "--channel", str(channel), "registry",
                                  "sessions", "--offset", str(offset), "--limit", str(REGISTRY_PAGE)])
            items, more, nxt = page.get("items"), page.get("more"), page.get("next")
            if (not isinstance(items, list) or type(more) is not bool or type(nxt) is not int
                    or nxt != offset + len(items) or not isinstance(page.get("head"), str)):
                raise WakeError("channel_state_invalid")
            if head is not None and page["head"] != head:
                raise WakeError("registry_changed", "transient")
            head = page["head"]
            for item in items:
                if (not isinstance(item, dict) or not isinstance(item.get("handle"), str)
                        or not isinstance(item.get("session"), str) or type(item.get("generation")) is not int):
                    raise WakeError("channel_state_invalid")
                old = current.get(item["handle"])
                if old is None or item["generation"] > old["generation"]:
                    current[item["handle"]] = {k: item[k] for k in ("handle", "session", "generation")}
            if not more:
                return {"participants": list(current.values())}
            if not items:
                raise WakeError("channel_state_invalid")
            offset = nxt
        raise WakeError("registry_limit")

    def systemctl(self, *args):
        try:
            done = subprocess.run([SYSTEMCTL, *args], stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
                                  check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WakeError("service_manager_unavailable", "transient") from exc
        return done.returncode, done.stdout[:65536].decode("utf-8", "replace")

    def unit_dir(self):
        base = self.env.get("XDG_CONFIG_HOME") or os.path.join(self.env.get("HOME", ""), ".config")
        if not os.path.isabs(base):
            raise WakeError("unit_dir_invalid")
        return Path(os.path.normpath(base)) / "systemd" / "user"


def systemd_version(host):
    code, out = host.systemctl("--version")
    match = re.match(r"systemd ([0-9]{1,5})\b", out)
    if code != 0 or not match:
        raise WakeError("service_manager_unavailable", "transient")
    if int(match.group(1)) < MIN_SYSTEMD:
        raise WakeError("service_manager_unsupported")
    return int(match.group(1))


# --------------------------------------------------------------------------- the verified pin


class Pin:
    """The project's verified pin: its manifest, package identity and adapter bytes."""

    def __init__(self, project, host):
        report = host.verify(project)
        if not isinstance(report, dict) or report.get("status") != "verified":
            raise WakeError("pin_not_verified")
        self.dir = Path(report["pin"])
        raw = read_regular(self.dir / "RELEASE-MANIFEST.json")
        if raw is None:
            raise WakeError("pin_not_verified")
        manifest = json.loads(raw.decode("utf-8"))
        self.entries = {e["path"]: e for e in manifest["files"]}
        if "ai_comm.py" not in self.entries or not HEX.fullmatch(str(manifest.get("protocol_sha256"))):
            raise WakeError("pin_not_verified")
        self.package = {"commit": report["commit"], "manifest_sha256": sha256(raw),
                        "protocol_sha256": manifest["protocol_sha256"],
                        "cli_sha256": self.entries["ai_comm.py"]["sha256"]}

    def adapter_files(self, kind):
        """Each adapter file's bytes, re-read and compared with the trusted manifest."""
        files = {}
        for name in KINDS[kind]:
            entry = self.entries.get("adapters/" + name)
            if entry is None:
                raise WakeError("adapters_unsupported")
            data = read_regular(self.dir / "adapters" / name)
            if data is None or sha256(data) != entry["sha256"]:
                raise WakeError("pin_changed")
            files[name] = data
        return files


def load_queue_module(directory, files, tag):
    """The queue adapter from `directory`, after checking its files are exactly `files`."""
    for name, data in files.items():
        if read_regular(Path(directory) / name) != data:
            raise WakeError("pin_changed")
    # The module name includes its directory, so a module loaded from one directory is
    # never reused for another (its own HERE checks the files beside it).
    where = sha256(str(Path(directory)).encode("utf-8"))[:16]
    return _load(f"ai_comm_wake_queue_{tag}_{where}", Path(directory) / "queue_wake.py")


def renderer_call(fn, *args, module):
    """Call the queue module; its own error types become a fixed code (ACK 1319)."""
    try:
        return fn(*args)
    except (module.Invalid, module.common.WakeError) as exc:
        raise WakeError("queue_render_refused") from exc


# --------------------------------------------------------------------------- plan


def instance_dir(project, instance):
    return Path(project) / ".ai-comm-local" / "wake" / instance


def bundle_root(project):
    return Path(project) / ".ai-comm-local" / "upgrade" / "adapters"


def check_binding(state, handle, session, generation):
    participants = state.get("participants")
    records = [p for p in participants if isinstance(p, dict) and p.get("handle") == handle] \
        if isinstance(participants, list) else None
    if records is None or len(records) > 1:
        raise WakeError("channel_state_invalid")
    if not records:
        raise WakeError("handle_not_joined")
    if records[0].get("session") != session:
        raise WakeError("session_mismatch")
    if records[0].get("generation") != generation:
        raise WakeError("generation_mismatch")


def advisory_kind_check(kind, directory):
    names = ("config.json",) if kind == "exit" else ("cursor.json", "run.json")
    if any(os.path.lexists(directory / n) for n in names):
        raise WakeError("instance_kind_conflict")


def compute(args, host):
    """Everything plan prints and install writes. Writes nothing."""
    uc = _load("ai_comm_upgrade_controller", RELEASE / "upgrade_controller.py")
    try:
        project = uc.Project(args.project)
        identity = project.project_id()
    except uc.Stop as exc:
        raise WakeError("project_invalid") from exc
    pin = Pin(identity["path"], host)
    files = pin.adapter_files(args.kind)
    check_binding(host.registry(pin.dir / "ai_comm.py", identity["channel_path"]),
                  args.handle, args.session, args.generation)
    key = common.project_key(identity)
    instance = common.instance_name(key, args.handle)
    directory = instance_dir(identity["path"], instance)
    advisory_kind_check(args.kind, directory)
    bundle_doc = {"kind": args.kind, "package": {"commit": pin.package["commit"],
                                                 "manifest_sha256": pin.package["manifest_sha256"]},
                  "files": {name: sha256(data) for name, data in files.items()}}
    bundle_sha = sha256(canonical(bundle_doc))
    bundle_dir = bundle_root(identity["path"]) / bundle_sha
    plan = {"v": 1, "kind": args.kind, "instance": instance, "project": identity, "handle": args.handle,
            "session": args.session, "generation": args.generation,
            "bundle": {"dir": str(bundle_dir), "sha256": bundle_sha, "files": bundle_doc["files"]}}
    private = {"files": files, "bundle_doc": bundle_doc, "pin": pin}
    if args.kind == "exit":
        launch = [sys.executable, "-I", str(bundle_dir / "exit_wake.py"), "--project", identity["path"],
                  "--project-key", key, "--handle", args.handle, f"--session={args.session}",
                  "--generation", str(args.generation)]
        if args.peer:
            launch += ["--peer", args.peer]
        plan["launch"] = launch
    else:
        systemd_version(host)
        queue = load_queue_module(pin.dir / "adapters", files, "pin")
        config_path = directory / "config.json"
        unit_path = host.unit_dir() / f"queue-wake@{instance}.service"
        config = {"v": 1, "schema": queue.SCHEMA, "project": identity, "instance": instance,
                  "handle": args.handle, "session": args.session, "generation": args.generation,
                  "runtime": {"socket": args.socket, "binary": args.binary, "sha256": args.binary_sha256,
                              "approved_thread": args.thread},
                  "package": pin.package, "service": None}
        template = files["queue-wake@.service"].decode("utf-8")
        unit = renderer_call(queue.render_service, config, config_path, args.python,
                             str(bundle_dir / "queue_wake.py"), template, module=queue)
        config["service"] = {"path": str(unit_path), "sha256": sha256(unit)}
        renderer_call(queue.validate_config, config, module=queue)
        plan.update({"config_path": str(config_path), "config": config, "config_sha256": sha256(canonical(config)),
                     "unit_path": str(unit_path), "unit_sha256": sha256(unit)})
        private.update({"unit": unit, "template": template})
    plan["plan_sha256"] = sha256(canonical(plan))
    plan["steps"] = steps(args, plan)
    return plan, private


def steps(args, plan):
    """The owner's next commands, bound to this release's own path, never to `current`."""
    install = ["sh", str(RELEASE / "install.sh"), "--user", "wake", "install", *identity_argv(args),
               "--plan-sha256", plan["plan_sha256"], "--confirm"]
    if plan["kind"] == "exit":
        return [install, plan["launch"]]
    bundle = Path(plan["bundle"]["dir"])
    init = [args.python, "-I", "-B", str(bundle / "queue_wake.py"), "init", "--config", plan["config_path"],
            "--handle", args.handle, "--session", args.session, "--generation", str(args.generation)]
    return [install, init, [SYSTEMCTL, "--user", "daemon-reload"],
            [SYSTEMCTL, "--user", "enable", "--now", f"queue-wake@{plan['instance']}.service"]]


def identity_argv(args):
    argv = ["--project", args.project, "--handle", args.handle, "--kind", args.kind,
            f"--session={args.session}", "--generation", str(args.generation)]
    if args.kind == "exit" and args.peer:
        argv += ["--peer", args.peer]
    if args.kind == "queue":
        argv += ["--python", args.python, "--socket", args.socket, "--binary", args.binary,
                 "--binary-sha256", args.binary_sha256, "--thread", args.thread]
    return argv


# --------------------------------------------------------------------------- install


def _uc():
    return _load("ai_comm_upgrade_controller", RELEASE / "upgrade_controller.py")


def trusted_ancestry(path, code):
    """Every existing component of `path`: not a symlink, a directory owned by root or this
    user, not writable by another account unless sticky (the controller's ancestor rule)."""
    uc = _uc()
    current = Path(Path(path).anchor)
    for part in Path(path).parts[1:]:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return
        if not uc._mode_ok_ancestor(info):
            raise WakeError(code)


def owned_private_dir(path, code):
    """An existing directory: real, owned by this user, no group or other bits."""
    info = os.lstat(path)
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise WakeError(code)


def instance_path_guard(directory):
    """Read-only, before even a missing-instance no-op: no component of the instance path
    may be a symlink (dangling or not), and .ai-comm-local passes the ancestor rule
    (plan 054)."""
    try:
        common._reject_symlinked_ancestors(str(directory))
    except WakeError as exc:
        raise WakeError("instance_dir_unsafe") from exc
    trusted_ancestry(Path(directory).parents[1], "instance_dir_unsafe")


def instance_dir_check(directory):
    """Read-only: no component of the instance path is a symlink, and the wake root and the
    instance directory are real private directories owned by this user (review 1379 R6)."""
    try:
        common._reject_symlinked_ancestors(str(directory))
    except WakeError as exc:
        raise WakeError("instance_dir_unsafe") from exc
    trusted_ancestry(Path(directory).parents[1], "instance_dir_unsafe")
    owned_private_dir(Path(directory).parent, "instance_dir_unsafe")
    owned_private_dir(directory, "instance_dir_unsafe")


def bundle_chain(project):
    local = Path(project) / ".ai-comm-local"
    return local, local / "upgrade", local / "upgrade" / "adapters"


def bundle_state(bundle_dir, files, bundle_doc):
    """'absent', 'identical', or a refusal (review 1371 R1).

    An existing bundle is reused only if every directory on its chain is private and owned
    by this user, and every file is a regular 0400 file owned by this user with exactly the
    expected bytes. Modes are never changed silently."""
    project = Path(bundle_dir).parents[3]
    local, upgrade, adapters = bundle_chain(project)
    trusted_ancestry(local, "bundle_unsafe")
    for directory in (upgrade, adapters):
        if os.path.lexists(directory):
            owned_private_dir(directory, "bundle_unsafe")
    if not os.path.lexists(bundle_dir):
        return "absent"
    owned_private_dir(bundle_dir, "bundle_unsafe")
    expected = dict(files, **{"BUNDLE.json": canonical(bundle_doc)})
    if sorted(os.listdir(bundle_dir)) != sorted(expected):
        raise WakeError("bundle_conflict")
    for name, data in expected.items():
        info = os.lstat(bundle_dir / name)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o400):
            raise WakeError("bundle_unsafe")
        if read_regular(bundle_dir / name) != data:
            raise WakeError("bundle_conflict")
    return "identical"


def freeze_bundle(project, bundle_dir, files, bundle_doc):
    if bundle_state(bundle_dir, files, bundle_doc) == "identical":
        return
    _, upgrade, adapters = bundle_chain(project)
    for directory in (upgrade, adapters):  # created 0700 if missing; an existing one was checked above
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass
        owned_private_dir(directory, "bundle_unsafe")
    root = str(adapters)
    tmp = tempfile.mkdtemp(prefix=".bundle-", dir=root)
    try:
        for name, data in dict(files, **{"BUNDLE.json": canonical(bundle_doc)}).items():
            fd = os.open(os.path.join(tmp, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
            try:
                os.fchmod(fd, 0o400)
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
        os.chmod(tmp, 0o700)
        try:
            os.rename(tmp, bundle_dir)
            tmp = None
        except OSError:
            pass  # another install published it first: it must then be identical
    finally:
        if tmp is not None:
            for name in os.listdir(tmp):
                os.unlink(os.path.join(tmp, name))
            os.rmdir(tmp)
    if bundle_state(bundle_dir, files, bundle_doc) != "identical":
        raise WakeError("bundle_conflict")


def unit_destination_check(unit_path):
    """The unit directory's existing ancestors pass the ancestor rule; the directory itself,
    if it exists, is owned by this user; an existing unit is a regular file owned by this
    user and not writable by another account (review 1371 R1)."""
    unit_dir = Path(unit_path).parent
    trusted_ancestry(unit_dir, "unit_dir_unsafe")
    if os.path.lexists(unit_dir):
        info = os.lstat(unit_dir)
        # The terminal directory gets no sticky or private-group exemption (review 1397):
        # owned by this user, writable by nobody else.
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise WakeError("unit_dir_unsafe")
    if os.path.lexists(unit_path):
        info = os.lstat(unit_path)
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o022):
            raise WakeError("unit_unsafe")


def make_unit_dir(unit_dir):
    """Create each missing component 0700, only below a validated existing ancestor."""
    missing = []
    current = Path(unit_dir)
    while not os.path.lexists(current):
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        os.mkdir(directory, 0o700)
    trusted_ancestry(unit_dir, "unit_dir_unsafe")


def destination_check(directory, unit_path, config_bytes, unit_bytes):
    """Read-only preflight of every queue destination. Returns (config_present, unit_present)."""
    if any(os.path.lexists(directory / n) for n in ("cursor.json", "run.json")):
        raise WakeError("instance_kind_conflict")
    if os.path.lexists(directory / INTENT):
        raise WakeError("removal_in_progress")
    unit_destination_check(unit_path)
    config = read_regular(directory / "config.json") if os.path.lexists(directory / "config.json") else None
    if os.path.lexists(directory / "config.json") and config != config_bytes:
        raise WakeError("config_exists")
    unit = read_regular(unit_path) if os.path.lexists(unit_path) else None
    if os.path.lexists(unit_path):
        if unit != unit_bytes:
            raise WakeError("unit_exists")
        if config is None:
            raise WakeError("unit_orphan")
    return config is not None, unit is not None


def write_config(directory, config, config_bytes):
    try:
        common.write_private_json(str(directory), "config.json", config)
    except WakeError as exc:
        if exc.code != "state_commit_uncertain" or read_regular(directory / "config.json") != config_bytes:
            raise


def publish_unit(unit_path, unit_bytes):
    """Exclusive publication: write a temporary file, then link it to the final name."""
    unit_dir = unit_path.parent
    make_unit_dir(unit_dir)
    unit_destination_check(unit_path)
    fd, tmp = tempfile.mkstemp(prefix=".queue-wake.", dir=unit_dir)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(unit_bytes)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
        os.close(fd)
        fd = None
        try:
            os.link(tmp, unit_path)
        except FileExistsError:
            if read_regular(unit_path) != unit_bytes:
                raise WakeError("unit_exists") from None
    finally:
        if fd is not None:
            os.close(fd)
        os.unlink(tmp)
    try:
        dfd = os.open(unit_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError as exc:
        if read_regular(unit_path) != unit_bytes:
            raise WakeError("state_commit_uncertain", "transient") from exc


def install(args, host):
    plan, private = compute(args, host)
    if plan["plan_sha256"] != args.plan_sha256:
        raise WakeError("plan_changed")
    project = plan["project"]["path"]
    bundle_dir = Path(plan["bundle"]["dir"])
    files, bundle_doc = private["files"], private["bundle_doc"]
    if args.kind == "exit":
        freeze_bundle(project, bundle_dir, files, bundle_doc)
        return {"status": "installed", "kind": "exit", "instance": plan["instance"],
                "bundle": plan["bundle"]["dir"], "steps": plan["steps"][1:]}
    directory = Path(plan["config_path"]).parent
    unit_path = Path(plan["unit_path"])
    config_bytes = canonical(plan["config"])
    bundle_state(bundle_dir, files, bundle_doc)
    destination_check(directory, unit_path, config_bytes, private["unit"])
    freeze_bundle(project, bundle_dir, files, bundle_doc)
    queue = load_queue_module(bundle_dir, files, "bundle_" + plan["bundle"]["sha256"][:16])
    null_config = dict(plan["config"], service=None)
    rendered = renderer_call(queue.render_service, null_config, Path(plan["config_path"]), args.python,
                             str(bundle_dir / "queue_wake.py"), private["template"], module=queue)
    if rendered != private["unit"]:
        raise WakeError("plan_changed")
    common.private_dir(str(directory.parent))
    common.private_dir(str(directory))
    gate = common.acquire_lock(str(directory), name="instance.lock")
    try:
        config_present, unit_present = destination_check(directory, unit_path, config_bytes, private["unit"])
        if not config_present:
            write_config(directory, plan["config"], config_bytes)
        if not unit_present:
            publish_unit(unit_path, private["unit"])
    finally:
        os.close(gate)
    return {"status": "installed", "kind": "queue", "instance": plan["instance"], "config": plan["config_path"],
            "unit": plan["unit_path"], "steps": plan["steps"][1:]}


# --------------------------------------------------------------------------- remove


def check_entries(directory, allowed):
    """Every entry is an allowed private regular file; refuse anything else, change nothing."""
    for name in sorted(os.listdir(directory)):
        path = directory / name
        if REBIND.fullmatch(name):
            raise WakeError("recovery_data_present")
        info = os.lstat(path)
        if name not in allowed or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise WakeError("instance_unexpected_files")


def hold(directory, names):
    """Take each lock in order, without waiting; contention means the instance is active."""
    fds = []
    try:
        for name in names:
            try:
                fds.append(common.acquire_lock(str(directory), name=name, attempts=1))
            except WakeError as exc:
                if exc.code == "another_instance_running":
                    raise WakeError("instance_active") from exc
                raise
        return fds
    except BaseException:
        for fd in fds:
            os.close(fd)
        raise


def removal_identity(args, host):
    """Who is being removed. Trust (cache release, verified pin) is unchanged; nothing is
    rendered (review 1371 R3). Exit keeps the current registry check, because an exit
    instance holds no durable record of its session (review 1373); queue is checked
    against its own installed records instead."""
    uc = _uc()
    try:
        identity = uc.Project(args.project).project_id()
    except uc.Stop as exc:
        raise WakeError("project_invalid") from exc
    pin = Pin(identity["path"], host)
    if args.kind == "exit":
        check_binding(host.registry(pin.dir / "ai_comm.py", identity["channel_path"]),
                      args.handle, args.session, args.generation)
    instance = common.instance_name(common.project_key(identity), args.handle)
    return {"kind": args.kind, "project": identity, "instance": instance, "handle": args.handle,
            "session": args.session, "generation": args.generation}, pin


def remove_exit(ident):
    directory = instance_dir(ident["project"]["path"], ident["instance"])
    instance_path_guard(directory)
    if not os.path.lexists(directory):
        return {"status": "removed", "kind": "exit", "instance": ident["instance"], "removed": []}
    instance_dir_check(directory)
    check_entries(directory, LOCK_NAMES | set(EXIT_DATA))
    fds = hold(directory, ("phase.lock", "lock", "instance.lock"))
    try:
        instance_dir_check(directory)
        check_entries(directory, LOCK_NAMES | set(EXIT_DATA))
        removed = []
        for name in EXIT_DATA:
            if os.path.lexists(directory / name):
                os.unlink(directory / name)
                removed.append(name)
    finally:
        for fd in reversed(fds):
            os.close(fd)
    return {"status": "removed", "kind": "exit", "instance": ident["instance"], "removed": removed}


SERVICE_PROPERTIES = ("LoadState", "ActiveState", "UnitFileState", "FragmentPath")


def unit_inactive(host, instance, unit_path):
    """Affirmative proof that the unit is stopped and disabled; anything else refuses (R2)."""
    code, out = host.systemctl("--user", "show", f"queue-wake@{instance}.service",
                               *(f"--property={p}" for p in SERVICE_PROPERTIES))
    values = {}
    for line in out.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in SERVICE_PROPERTIES or key in values:
            raise WakeError("service_state_invalid")
        values[key] = value
    if set(values) != set(SERVICE_PROPERTIES) or values["LoadState"] not in ("loaded", "not-found"):
        raise WakeError("service_state_invalid")
    if code != 0 and values["LoadState"] != "not-found":
        raise WakeError("service_manager_unavailable", "transient")
    if values["ActiveState"] not in ("inactive", "failed"):
        raise WakeError("instance_active")
    if values["LoadState"] == "loaded":
        if values["FragmentPath"] != str(unit_path):
            raise WakeError("wrong_service_fragment")
        if values["UnitFileState"] != "disabled":
            raise WakeError("instance_active")
    elif values["FragmentPath"] != "" or values["UnitFileState"] not in ("", "disabled"):
        raise WakeError("service_state_invalid")


def validate_intent(intent, ident, unit_path):
    """The removal intent's exact schema, validated whether or not its files remain (R4)."""
    keys = {"v", "instance", "project", "handle", "session", "generation", "config_sha256", "unit",
            "state_sha256", "pending"}
    if (not isinstance(intent, dict) or set(intent) != keys or type(intent["v"]) is not int or intent["v"] != 1
            or type(intent["generation"]) is not int
            or any(intent[k] != ident[k] for k in ("instance", "project", "handle", "session", "generation"))
            or intent["pending"] is not None
            or not all(isinstance(intent[k], str) and HEX.fullmatch(intent[k])
                       for k in ("config_sha256", "state_sha256"))
            or not isinstance(intent["unit"], dict) or set(intent["unit"]) != {"path", "sha256"}
            or intent["unit"]["path"] != str(unit_path)
            or not isinstance(intent["unit"]["sha256"], str) or not HEX.fullmatch(intent["unit"]["sha256"])):
        raise WakeError("removal_intent_invalid")
    return intent


def recorded_config(directory, ident, unit_path, args, queue):
    """The installed config, valid and bound to exactly the requested instance (R3).

    It may name any frozen bundle and any package: a guided rebind keeps the original unit."""
    raw = read_regular(directory / "config.json")
    try:
        config = queue.validate_config(common.read_small_json(str(directory / "config.json")))
    except (queue.Invalid, queue.common.WakeError) as exc:
        raise WakeError("config_invalid") from exc
    runtime = {"socket": args.socket, "binary": args.binary, "sha256": args.binary_sha256,
               "approved_thread": args.thread}
    if (any(config[k] != ident[k] for k in ("project", "instance", "handle", "session", "generation"))
            or config["runtime"] != runtime or config["service"] is None
            or config["service"]["path"] != str(unit_path)):
        raise WakeError("config_binding_mismatch")
    return config, raw


def queue_removal_record(directory, ident, unit_path, args, queue):
    """(intent or None, config or None, config bytes, unit digest) from what is installed."""
    intent = None
    if os.path.lexists(directory / INTENT):
        intent = validate_intent(common.read_small_json(str(directory / INTENT)), ident, unit_path)
    config, raw = (recorded_config(directory, ident, unit_path, args, queue)
                   if os.path.lexists(directory / "config.json") else (None, None))
    if intent is None and config is None:
        raise WakeError("config_missing")
    if intent is not None and raw is not None and sha256(raw) != intent["config_sha256"]:
        raise WakeError("removal_intent_invalid")
    unit_sha = intent["unit"]["sha256"] if intent is not None else config["service"]["sha256"]
    if intent is not None and config is not None and config["service"]["sha256"] != unit_sha:
        raise WakeError("removal_intent_invalid")
    return intent, config, raw, unit_sha


def remove_queue(ident, pin, host, args):
    directory = instance_dir(ident["project"]["path"], ident["instance"])
    unit_path = host.unit_dir() / f"queue-wake@{ident['instance']}.service"
    instance_path_guard(directory)
    if not os.path.lexists(directory) or (
            not any(os.path.lexists(directory / n) for n in (*QUEUE_DATA, INTENT))
            and not os.path.lexists(unit_path)):
        return {"status": "removed", "kind": "queue", "instance": ident["instance"], "removed": []}
    instance_dir_check(directory)  # read-only, before anything inside is examined (review 1379 R6)
    # Read-only refusal before any lock file can be created (decision 1335 note).
    if not os.path.lexists(directory / INTENT) and not os.path.lexists(directory / "state.json") \
            and os.path.lexists(directory / "config.json"):
        raise WakeError("state_missing")
    allowed = LOCK_NAMES | set(QUEUE_DATA) | {INTENT}
    check_entries(directory, allowed)
    queue = load_queue_module(pin.dir / "adapters", pin.adapter_files("queue"), "pin")
    queue_removal_record(directory, ident, unit_path, args, queue)
    unit_inactive(host, ident["instance"], unit_path)
    fds = hold(directory, ("guided.lock", "observer.lock", "instance.lock"))
    try:
        # Everything is re-validated under the locks, before the intent is written (R5).
        instance_dir_check(directory)
        check_entries(directory, allowed)
        intent, config, raw, unit_sha = queue_removal_record(directory, ident, unit_path, args, queue)
        unit_inactive(host, ident["instance"], unit_path)
        unit_destination_check(unit_path)
        unit = read_regular(unit_path) if os.path.lexists(unit_path) else None
        if unit is not None and sha256(unit) != unit_sha:
            raise WakeError("unit_changed")
        state_raw = read_regular(directory / "state.json") if os.path.lexists(directory / "state.json") else None
        if intent is None:
            if state_raw is None:
                raise WakeError("state_missing")
            try:
                state = queue.Store(directory, config).load()
            except (queue.Invalid, queue.common.WakeError) as exc:
                raise WakeError("state_invalid") from exc
            if state["pending"] is not None:
                raise WakeError("pending_attempt")
            intent = dict(ident, v=1, config_sha256=sha256(raw), unit={"path": str(unit_path), "sha256": unit_sha},
                          state_sha256=sha256(state_raw), pending=None)
            del intent["kind"]
            try:
                common.write_private_json(str(directory), INTENT, intent)
            except WakeError as exc:
                if exc.code != "state_commit_uncertain" or read_regular(directory / INTENT) != canonical(intent):
                    raise
        elif state_raw is not None and sha256(state_raw) != intent["state_sha256"]:
            raise WakeError("removal_state_changed")
        removed = []
        if unit is not None:
            if read_regular(unit_path) != unit:  # checked again immediately before unlinking
                raise WakeError("unit_changed")
            os.unlink(unit_path)
            removed.append(str(unit_path))
        for name in ("paused.json", "state.json", "config.json", INTENT):
            if os.path.lexists(directory / name):
                os.unlink(directory / name)
                removed.append(name)
    finally:
        for fd in reversed(fds):
            os.close(fd)
    return {"status": "removed", "kind": "queue", "instance": ident["instance"], "removed": removed}


def remove(args, host):
    ident, pin = removal_identity(args, host)
    if args.kind == "exit":
        return remove_exit(ident)
    return remove_queue(ident, pin, host, args)


# --------------------------------------------------------------------------- integration


PHASE_STEPS = ("pause", "rebind", "check", "resume")


def selected_bundle(project, kind, digest):
    """The explicitly selected, already frozen bundle (decision 1396, proposal 056 R2).

    It holds exactly the kind's fixed file set plus BUNDLE.json; BUNDLE.json's canonical
    digest is the selected digest and names this kind; every file is private 0400 with
    the recorded digest. Never a freshly computed bundle from the current pin."""
    bundle_dir = bundle_root(project) / digest
    trusted_ancestry(Path(project) / ".ai-comm-local", "bundle_unsafe")
    if not os.path.lexists(bundle_dir):
        raise WakeError("bundle_missing")
    owned_private_dir(bundle_dir, "bundle_unsafe")
    info = os.lstat(bundle_dir / "BUNDLE.json") if os.path.lexists(bundle_dir / "BUNDLE.json") else None
    if info is None or not stat.S_ISREG(info.st_mode):
        raise WakeError("bundle_conflict")
    raw = read_regular(bundle_dir / "BUNDLE.json")
    try:
        # Strict parsing: duplicate keys, NaN/Infinity, overflow and excessive depth refuse (review 1400 I1).
        doc = common.read_small_json(str(bundle_dir / "BUNDLE.json"))
    except WakeError as exc:
        raise WakeError("bundle_conflict") from exc
    package = doc.get("package")
    files_doc = doc.get("files")
    if (set(doc) != {"kind", "package", "files"} or doc["kind"] != kind
            or not isinstance(package, dict) or set(package) != {"commit", "manifest_sha256"}
            or not isinstance(package["commit"], str) or not COMMIT_RE.fullmatch(package["commit"])
            or not isinstance(package["manifest_sha256"], str) or not HEX.fullmatch(package["manifest_sha256"])
            or not isinstance(files_doc, dict) or set(files_doc) != set(KINDS[kind])
            or not all(isinstance(v, str) and HEX.fullmatch(v) for v in files_doc.values())
            or canonical(doc) != raw or sha256(raw) != digest):
        raise WakeError("bundle_conflict")
    files = {}
    for name in KINDS[kind]:
        data = read_regular(bundle_dir / name)
        if data is None or sha256(data) != doc["files"][name]:
            raise WakeError("bundle_conflict")
        files[name] = data
    bundle_state(bundle_dir, files, doc)  # private chain, 0400 user-owned files, exact names and bytes
    return bundle_dir, files


def integration(args, host):
    """One exact declaration item for the owner to review; writes nothing (proposal 056)."""
    uc = _uc()
    try:
        identity = uc.Project(args.project).project_id()
    except uc.Stop as exc:
        raise WakeError("project_invalid") from exc
    pin = Pin(identity["path"], host)
    check_binding(host.registry(pin.dir / "ai_comm.py", identity["channel_path"]),
                  args.handle, args.session, args.generation)
    key = common.project_key(identity)
    instance = common.instance_name(key, args.handle)
    bundle_dir, files = selected_bundle(identity["path"], args.kind, args.bundle_sha256)
    code_files = [str(bundle_dir.relative_to(identity["path"]) / name) for name in sorted(KINDS[args.kind])]
    pairs = [list(pair) for pair in args.pair]
    if args.kind == "exit":
        base = [sys.executable, "-I", str(bundle_dir / "exit_wake.py")]
        tail = ["--project", identity["path"], "--project-key", key, "--handle", args.handle,
                f"--session={args.session}", "--generation", str(args.generation)]
        argv = {phase: base + [phase] + tail for phase in ("probe", *PHASE_STEPS)}
    else:
        directory = instance_dir(identity["path"], instance)
        if args.config != str(directory / "config.json"):
            raise WakeError("config_path_invalid")
        instance_dir_check(directory)
        queue = load_queue_module(bundle_dir, files, "bundle_" + args.bundle_sha256[:16])
        ident = {"project": identity, "instance": instance, "handle": args.handle, "session": args.session,
                 "generation": args.generation}
        unit_path = host.unit_dir() / f"queue-wake@{instance}.service"
        config, _ = recorded_config(directory, ident, unit_path, args, queue)
        try:
            transition = queue.load_transition(config, Path(args.transition), args.transition_sha256)
        except (queue.Invalid, queue.common.WakeError) as exc:
            raise WakeError("transition_invalid") from exc
        allowed = {(transition["from"]["commit"], transition["to"]["commit"]),
                   (transition["to"]["commit"], transition["from"]["commit"])}
        if any(tuple(pair) not in allowed for pair in pairs):
            raise WakeError("pair_not_in_transition")
        base = [args.python, "-I", "-B", str(bundle_dir / "queue_wake.py")]
        tail = ["--config", args.config, "--handle", args.handle, "--session", args.session,
                "--generation", str(args.generation), "--transition", args.transition,
                "--sha256", args.transition_sha256]
        argv = {phase: base + [phase] + tail for phase in ("probe", *PHASE_STEPS)}
    item = {"id": f"{args.kind}-wake-{args.handle}", "owner": args.handle, "supports": {"pairs": pairs},
            "probe": {"argv": argv["probe"], "run_by": "owner"},
            "steps": {phase: argv[phase] for phase in PHASE_STEPS}, "code_files": code_files}
    return {"status": "integration", "item": item, "item_sha256": sha256(canonical(item))}


# --------------------------------------------------------------------------- arguments


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise WakeError("arguments_invalid")


def parse_args(argv):
    p = _Parser(prog="install.sh --user wake", add_help=False)
    p.add_argument("command", choices=("plan", "install", "remove", "integration"))
    p.add_argument("--project", required=True)
    p.add_argument("--handle", required=True)
    p.add_argument("--kind", required=True, choices=tuple(KINDS))
    p.add_argument("--session", required=True)
    p.add_argument("--generation", required=True)
    p.add_argument("--peer")
    for name in ("--python", "--socket", "--binary", "--binary-sha256", "--thread", "--plan-sha256"):
        p.add_argument(name)
    p.add_argument("--confirm", action="store_true")
    for name in ("--bundle-sha256", "--config", "--transition", "--transition-sha256"):
        p.add_argument(name)
    p.add_argument("--pair", nargs=2, action="append", default=[])
    args = p.parse_args(argv)
    integration_args = (args.bundle_sha256, args.config, args.transition, args.transition_sha256)
    if args.command != "integration":
        if any(v is not None for v in integration_args) or args.pair:
            raise WakeError("arguments_invalid")
    else:
        if not (args.bundle_sha256 and HEX.fullmatch(args.bundle_sha256)) or not args.pair \
                or args.confirm or args.plan_sha256:
            raise WakeError("arguments_invalid")
        if not all(COMMIT_RE.fullmatch(c) for pair in args.pair for c in pair) \
                or any(a == b for a, b in args.pair) or len({tuple(p) for p in args.pair}) != len(args.pair):
            raise WakeError("pair_invalid")
        queue_only = (args.config, args.transition, args.transition_sha256)
        if args.kind == "queue":
            if any(v is None for v in queue_only) or not HEX.fullmatch(args.transition_sha256) \
                    or not all(os.path.isabs(v) and os.path.normpath(v) == v
                               for v in (args.config, args.transition)):
                raise WakeError("arguments_invalid")
        elif any(v is not None for v in queue_only):
            raise WakeError("arguments_invalid")
    if not os.path.isabs(args.project) or os.path.normpath(args.project) != args.project:
        raise WakeError("project_path_invalid")
    common.validate_handle(args.handle)
    common.validate_session(args.session)
    if not re.fullmatch(r"[1-9][0-9]{0,7}", args.generation):
        raise WakeError("generation_invalid")
    args.generation = int(args.generation)
    if args.peer is not None and (not common.HANDLE.fullmatch(args.peer) or args.peer == args.handle):
        raise WakeError("peer_invalid")
    queue_args = (args.python, args.socket, args.binary, args.binary_sha256, args.thread)
    if args.kind == "queue":
        if any(v is None for v in queue_args) or args.peer is not None:
            raise WakeError("arguments_invalid")
        if not all(os.path.isabs(v) and os.path.normpath(v) == v for v in (args.python, args.socket, args.binary)):
            raise WakeError("path_invalid")
        if not HEX.fullmatch(args.binary_sha256) or args.thread != args.session:
            raise WakeError("runtime_binding_invalid")
    elif any(v is not None for v in queue_args):
        raise WakeError("arguments_invalid")
    if args.command == "plan" and (args.confirm or args.plan_sha256):
        raise WakeError("arguments_invalid")
    if args.command == "install" and not (args.confirm and args.plan_sha256 and HEX.fullmatch(args.plan_sha256)):
        raise WakeError("confirmation_required")
    if args.command == "remove" and not args.confirm:
        raise WakeError("confirmation_required")
    return args


def main(argv=None, host=None, out=sys.stdout):
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
        host = host or Host()
        if args.command == "plan":
            result = compute(args, host)[0]
        elif args.command == "install":
            result = install(args, host)
        elif args.command == "integration":
            result = integration(args, host)
        else:
            result = remove(args, host)
    except WakeError as exc:
        print(json.dumps({"status": "stopped", "code": exc.code}, sort_keys=True), file=out)
        return exc.exit_code
    except OSError:
        print(json.dumps({"status": "stopped", "code": "os_error"}, sort_keys=True), file=out)
        return common.EXIT_TRANSIENT
    print(json.dumps(result, sort_keys=True), file=out)
    return common.EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
