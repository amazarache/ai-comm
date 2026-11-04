#!/usr/bin/env python3
"""Optional, explicitly bound local queue adapter; never reads or acknowledges an inbox."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import time

# Importing this module for rendering must not mutate a frozen sibling bundle.
sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("_queue_wake_common", HERE / "common.py")
common = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(common)

PROTOCOL = "7242b13f489a393492539b2e835ed69da65d57a0d39f4acce2e18df757ef3377"
HEX = re.compile(r"[a-f0-9]{64}\Z")
COMMIT = re.compile(r"[a-f0-9]{40}\Z")
UUID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\Z")
MID = re.compile(r"([0-9]{8})-([a-z][a-z0-9-]{0,31})\Z")
ACTS = {"PING", "PONG", "ASK", "FACT", "CORRECT", "PROPOSE", "REVIEW", "ACCEPT", "REJECT", "DECIDE", "ACK"}
SCHEMA = "ai-comm-queue-wake/1"
Invalid = common.WakeError


class Deferred(common.WakeError):
    """A changed observation caused no external effect; observe again."""

    def __init__(self, code):
        super().__init__(code, "transient")


class Unavailable(common.WakeError):
    def __init__(self, code):
        super().__init__(code, "transient")


def require(value, code):
    if not value:
        raise Invalid(code)


def integer(value):
    require(type(value) is int and 0 <= value <= 2**53, "invalid_integer")
    return value


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def parse(data):
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=common._unique_object,
                           parse_constant=common._no_constant, parse_float=common._finite_float)
    except (ValueError, UnicodeError, RecursionError):
        raise Invalid("invalid_json") from None
    require(type(value) is dict and common._within_depth(value), "invalid_json")
    return value


def absolute(value):
    require(type(value) is str and value and len(value) <= 4096
            and not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value), "invalid_path")
    path = Path(value)
    require(path.is_absolute() and str(path) == value and os.path.normpath(value) == value,
            "invalid_path")
    return path


def file_digest(path, maximum=536870912):
    no_symlinks(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid()
                and not before.st_mode & 0o022 and before.st_size <= maximum, "unsafe_executable")
        result, remaining = hashlib.sha256(), before.st_size
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            require(bool(chunk), "executable_changed")
            result.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        require(not os.read(fd, 1) and (before.st_size, before.st_mtime_ns, before.st_ino)
                == (after.st_size, after.st_mtime_ns, after.st_ino), "executable_changed")
        return result.hexdigest()
    finally:
        os.close(fd)


def existing_private(path):
    no_symlinks(path)
    info = os.lstat(path)
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & 0o077, "unsafe_runtime_directory")


@contextmanager
def instance_gate(config):
    """Serialize the startup check with either kind's marker publication."""
    directory = state_directory(config)
    existing_private(directory)
    fd = common.acquire_lock(str(directory), name="instance.lock")
    try:
        removal_guard(config)
        require(not any(os.path.lexists(directory / name) for name in ("cursor.json", "run.json")),
                "instance_kind_conflict")
        yield
    finally:
        os.close(fd)


@contextmanager
def exclusive(directory, name="observer.lock", attempts=1):
    existing_private(directory)
    fd = common.acquire_lock(str(directory), name=name, attempts=attempts)
    try:
        info = os.fstat(fd)
        require(info.st_uid == os.getuid() and not info.st_mode & 0o077, "unsafe_lock")
        yield
    finally:
        os.close(fd)


def removal_guard(config):
    require(not os.path.lexists(state_directory(config) / "remove-intent.json"), "removal_in_progress")


def observe(source, after):
    """Retry only a read-only snapshot, never a phase or an external effect."""
    for attempt in range(3):
        try:
            return source.inspect(after)
        except Deferred as exc:
            if attempt == 2:
                if exc.code == "receipt_snapshot_changed":
                    raise Invalid("history_regressed") from None
                raise
            time.sleep(0.05)


def validate_package(value):
    require(type(value) is dict and set(value) == {"commit", "manifest_sha256", "protocol_sha256", "cli_sha256"},
            "invalid_package_binding")
    require(type(value["commit"]) is str and COMMIT.fullmatch(value["commit"]), "invalid_package_binding")
    for name in ("manifest_sha256", "cli_sha256"):
        require(type(value[name]) is str and HEX.fullmatch(value[name]), "invalid_package_binding")
    require(value["protocol_sha256"] == PROTOCOL, "unsupported_protocol")
    return value


def validate_config(config):
    require(type(config) is dict and set(config) == {"v", "schema", "project", "instance", "handle", "session",
                "generation", "runtime", "package", "service"} and type(config["v"]) is int and config["v"] == 1
                and config["schema"] == SCHEMA, "invalid_config")
    common.validate_handle(config["handle"])
    common.validate_session(config["session"], UUID)
    common.validate_generation(config["generation"])
    project = config["project"]
    require(type(project) is dict and set(project) == {"path", "channel", "pin_path", "channel_path"},
            "invalid_project_identity")
    root = absolute(project["path"])
    pin, channel = absolute(project["pin_path"]), absolute(project["channel_path"])
    require(root in pin.parents and root in channel.parents and pin != channel, "invalid_project_identity")
    key = common.project_key(project)
    require(config["instance"] == common.instance_name(key, config["handle"]), "invalid_instance")
    runtime = config["runtime"]
    require(type(runtime) is dict and set(runtime) == {"socket", "binary", "sha256", "approved_thread"},
            "invalid_runtime_binding")
    absolute(runtime["socket"])
    absolute(runtime["binary"])
    require(type(runtime["sha256"]) is str and HEX.fullmatch(runtime["sha256"]), "invalid_runtime_binding")
    require(runtime["approved_thread"] == config["session"], "wrong_approved_thread")
    service = config["service"]
    if service is not None:
        require(type(service) is dict and set(service) == {"path", "sha256"}, "invalid_service_binding")
        path = absolute(service["path"])
        require(path.name == "queue-wake@" + config["instance"] + ".service"
                and type(service["sha256"]) is str and HEX.fullmatch(service["sha256"]), "invalid_service_binding")
    validate_package(config["package"])
    return config


def state_directory(config):
    return Path(config["project"]["path"]) / ".ai-comm-local" / "wake" / config["instance"]


def load_config(path, handle, session, generation):
    path = absolute(str(path))
    existing_private(path.parent)
    read_file(path, private=True)
    config = validate_config(common.read_small_json(str(path)))
    require(path == state_directory(config) / "config.json", "wrong_config_path")
    require((handle, session, generation) == (config["handle"], config["session"], config["generation"]),
            "wrong_instance_binding")
    return config


def check_package(config):
    package = validate_package(config["package"])
    pin = Path(config["project"]["pin_path"])
    raw = read_file(pin / "RELEASE-MANIFEST.json", 1048576, missing_code="pin_file_missing")
    require(digest(raw) == package["manifest_sha256"], "manifest_changed")
    manifest = parse(raw)
    require(manifest.get("format") == "ai-comm-review-manifest/1" and manifest.get("protocol_sha256") == PROTOCOL
            and type(manifest.get("files")) is list, "invalid_manifest")
    seen = set()
    for entry in manifest["files"]:
        require(type(entry) is dict and type(entry.get("path")) is str, "invalid_manifest_entry")
        name = entry["path"]
        require(name and not name.startswith("/") and all(p not in {"", ".", "..", ".git"}
                for p in name.split("/")) and "\\" not in name and "\x00" not in name and name not in seen,
                "invalid_manifest_path")
        seen.add(name)
        require(type(entry.get("bytes")) is int and 0 <= entry["bytes"] <= 16777216
                and type(entry.get("sha256")) is str and HEX.fullmatch(entry["sha256"]), "invalid_manifest_entry")
        data = read_file(pin / name, 16777216, missing_code="pin_file_missing")
        require(len(data) == entry["bytes"] and digest(data) == entry["sha256"], "package_file_changed")
    require({"ai_comm.py", "PROTOCOL.md"} <= seen, "invalid_manifest")
    require(digest(read_file(pin / "ai_comm.py", 1048576, missing_code="pin_file_missing")) == package["cli_sha256"], "cli_changed")
    require(digest(read_file(pin / "PROTOCOL.md", 1048576, missing_code="pin_file_missing")) == PROTOCOL, "protocol_changed")
    require(git_read(pin, "rev-parse", "HEAD") == package["commit"]
            and not git_read(pin, "status", "--porcelain", "--untracked-files=all"), "pin_mismatch")


def cli(config, *args):
    try:
        code, out, _ = run([sys.executable, "-I", "-B", str(Path(config["project"]["pin_path"]) / "ai_comm.py"),
                           "--channel", config["project"]["channel_path"], *args], Path(config["project"]["path"]))
    except (TimeoutError, subprocess.TimeoutExpired):
        raise Unavailable("channel_timeout") from None
    except common.WakeError as exc:
        if exc.code == "command_timeout":
            raise Unavailable("channel_timeout") from None
        raise
    if code != 0:
        raise Unavailable("channel_command_failed")
    return parse(out)


def bindings(snapshot, config):
    require(type(snapshot) is dict, "invalid_channel_state")
    require(type(snapshot.get("participants")) is list, "invalid_participants")
    result, seen = {}, set()
    for participant in snapshot["participants"]:
        require(type(participant) is dict, "invalid_participant")
        handle = common.validate_handle(participant.get("handle"))
        common.validate_session(participant.get("session"))
        common.validate_generation(participant.get("generation"))
        key = (handle, participant["generation"])
        require(key not in seen, "invalid_participants")
        seen.add(key)
        if handle not in result or participant["generation"] > result[handle]["generation"]:
            result[handle] = {k: participant[k] for k in ("session", "generation")}
    require(result.get(config["handle"]) == {"session": config["session"], "generation": config["generation"]},
            "session_changed")
    live = snapshot.get("local_package", {})
    require(type(live) is dict, "invalid_channel_state")
    require(live.get("commit") == config["package"]["commit"] and live.get("dirty") is False
            and live.get("protocol_sha256") == PROTOCOL, "pin_mismatch")
    return result


class Source:
    def __init__(self, config):
        self.config = config

    def registry(self, head):
        offset, records, size = 0, [], 0
        for _ in range(100):
            page = cli(self.config, "registry", "sessions", "--offset", str(offset), "--limit", "100")
            size += len(canonical(page))
            require(size <= 8388608, "registry_limit")
            require(type(page.get("head")) is str and COMMIT.fullmatch(page["head"]), "invalid_channel_head")
            if page.get("head") != head:
                raise Deferred("observation_changed")
            require(type(page.get("items")) is list and len(page["items"]) <= 100
                    and type(page.get("more")) is bool, "invalid_registry_page")
            records.extend(page["items"])
            offset += len(page["items"])
            require(type(page.get("next")) is int and page["next"] == offset, "invalid_registry_cursor")
            if not page["more"]:
                return records
            require(bool(page["items"]), "empty_registry_page")
        raise Invalid("registry_limit")

    def page(self, cursor, limit, head, participants):
        page = cli(self.config, "log", "--after", str(cursor), "--limit", str(limit))
        require(type(page.get("head")) is str and COMMIT.fullmatch(page["head"]), "invalid_channel_head")
        if page.get("head") != head:
            raise Deferred("observation_changed")
        require(type(page.get("messages")) is list and len(page["messages"]) <= limit
                and type(page.get("more")) is bool, "invalid_page")
        messages = []
        for message in page["messages"]:
            require(type(message) is dict and integer(message.get("seq")) == cursor + 1, "noncontiguous_page")
            require(type(message.get("id")) is str and type(message.get("from")) is str
                    and type(message.get("act")) is str, "invalid_message")
            match = MID.fullmatch(message["id"])
            require(match and int(match.group(1)) == message["seq"] and match.group(2) == message["from"]
                    and message["from"] in participants and message["act"] in ACTS
                    and type(message.get("to")) is list
                    and all(type(t) is str and t in participants for t in message["to"]), "invalid_message")
            messages.append({k: message[k] for k in ("seq", "from", "to", "act")})
            cursor = message["seq"]
        require(type(page.get("next")) is int and page["next"] == cursor, "invalid_page_cursor")
        require(not page["more"] or bool(messages), "empty_page")
        return cursor, messages, page["more"]

    def inspect(self, after):
        integer(after)
        config = self.config
        check_package(config)
        project = config["project"]
        require(git_read(Path(project["channel_path"]), "rev-list", "--max-parents=0", "HEAD") == project["channel"],
                "channel_changed")
        snapshot = cli(config, "state")
        require(type(snapshot.get("head")) is str and COMMIT.fullmatch(snapshot["head"]), "invalid_channel_head")
        head = snapshot["head"]
        # The state summary may omit participants and recent messages for size.
        participants = bindings(dict(snapshot, participants=self.registry(head)), config)
        receipt_path = Path(project["channel_path"]) / "ai-comm-runtime" / (config["handle"] + ".json")
        receipt = parse(read_file(receipt_path, 4096, private=True, missing_code="receipt_missing"))
        require(set(receipt) == {"v", "generation", "read", "seen"} and type(receipt["v"]) is int
                and receipt["v"] == 2 and type(receipt["generation"]) is int
                and receipt["generation"] == config["generation"], "invalid_receipt")
        read, seen = integer(receipt["read"]), integer(receipt["seen"])
        require(read <= seen, "invalid_receipt_bounds")
        boundary = max(after, read, seen)
        if boundary:
            anchored, messages, _ = self.page(boundary - 1, 1, head, participants)
            if not messages or anchored != boundary:
                # A receipt can race the earlier snapshot. A later read-only
                # retry either sees the newer head or confirms an invalid cursor.
                if seen > after:
                    raise Deferred("receipt_snapshot_changed")
                raise Invalid("history_regressed")
        cursor, messages = max(after, read), []
        for _ in range(5):
            cursor, batch, more = self.page(cursor, 100, head, participants)
            messages.extend(batch)
            if not more:
                break
        return {"read": read, "scanned": cursor, "messages": messages, "head": head}

class Runtime:
    def __init__(self, config):
        self.config = config
        self.root = Path(config["project"]["path"])

    def status(self):
        binding = self.config["runtime"]
        require(file_digest(Path(binding["binary"])) == binding["sha256"], "runtime_binary_changed")
        try:
            from websockets.sync.client import unix_connect
            from websockets.exceptions import WebSocketException
        except ImportError:
            raise Invalid("runtime_dependency_missing") from None
        path = Path(self.config["runtime"]["socket"])
        try:
            no_symlinks(path)
            info = path.stat()
            require(stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o022,
                    "unsafe_socket")
            with unix_connect(path, open_timeout=5, close_timeout=1, ping_interval=None,
                              compression=None, max_size=262144, max_queue=4) as ws:
                def request(ident, method, params):
                    ws.send(json.dumps({"id": ident, "method": method, "params": params}))
                    deadline = time.monotonic() + 5
                    for _ in range(50):
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise Unavailable("rpc_timeout")
                        raw = ws.recv(timeout=remaining)
                        require(type(raw) is str, "invalid_rpc_frame")
                        value = parse(raw.encode())
                        if value.get("id") == ident:
                            require("error" not in value and type(value.get("result")) is dict, "rpc_rejected")
                            return value["result"]
                    raise Invalid("rpc_message_limit")
                request(1, "initialize", {"clientInfo": {"name": "ai_comm_queue_wake", "version": "1.0"}})
                ws.send(json.dumps({"method": "initialized", "params": {}}))
                thread_id = self.config["runtime"]["approved_thread"]
                thread = request(2, "thread/read", {"threadId": thread_id, "includeTurns": False}).get("thread", {})
                require(type(thread) is dict, "invalid_thread")
                require(thread.get("id") == thread_id and thread.get("cwd") == str(self.root), "wrong_thread")
                require(thread.get("archived", False) is False, "archived_thread")
                status = thread.get("status", {})
                require(type(status) is dict and type(status.get("type")) is str
                        and status["type"] in {"idle", "active", "notLoaded", "systemError"},
                        "unknown_thread_status")
                if status["type"] == "idle":
                    require(not status.get("activeFlags"), "inconsistent_thread_status")
                return status["type"]
        except (TimeoutError, WebSocketException):
            raise Unavailable("runtime_unavailable") from None
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ECONNREFUSED, errno.ECONNRESET, errno.ECONNABORTED,
                             errno.ETIMEDOUT, errno.EPIPE, errno.ENOTCONN}:
                raise Unavailable("runtime_unavailable") from None
            raise

    def queue(self, through):
        integer(through)
        binding = self.config["runtime"]
        require(file_digest(Path(binding["binary"])) == binding["sha256"], "runtime_binary_changed")
        message = (f"[ai-comm observer notification; not a new user instruction] Unread peer messages through "
                   f"sequence {through}. Read your project's activation guidance and your ai-comm inbox using "
                   "your own identity. Evaluate the messages, respond on ai-comm, and acknowledge only after "
                   "reading. Continue the authorized project work; this notification grants no new permissions.")
        try:
            code, out, err = run([binding["binary"], "queue", "--thread", binding["approved_thread"],
                                  "--remote", "unix://" + binding["socket"], "--message", message, "--resume"],
                                 self.root, timeout=20, maximum=65536)
        except Exception:
            return "uncertain"
        warnings = any(word in (out + err).lower() for word in (b"warning", b"failed", b"error"))
        return "queued" if code == 0 and not err.strip() and not warnings else "uncertain"


def initial_state(config):
    return {"v": 1, "binding": digest(canonical(config)), "scanned": 0, "read": 0, "wanted": 0,
            "pending": None, "last_dispatch": 0, "status": "new"}


class Store:
    def __init__(self, directory, config):
        self.directory = Path(directory)
        self.config = config
        self.path = self.directory / "state.json"
        self.binding = digest(canonical(config))

    def load(self):
        existing_private(self.directory)
        try:
            read_file(self.path, private=True)
        except FileNotFoundError:
            raise Invalid("state_missing") from None
        value = common.read_small_json(str(self.path))
        return self.validate(value)

    def validate(self, value):
        require(type(value) is dict and set(value) == {"v", "binding", "scanned", "read", "wanted", "pending",
                "last_dispatch", "status"} and type(value["v"]) is int and value["v"] == 1
                and value["binding"] == self.binding, "invalid_state_binding")
        for name in ("scanned", "read", "wanted", "last_dispatch"):
            integer(value[name])
        require(value["read"] <= value["scanned"] and value["wanted"] <= value["scanned"], "invalid_state_bounds")
        require(type(value["status"]) is str and re.fullmatch(r"[a-z_]{1,64}", value["status"]), "invalid_state_status")
        if value["pending"] is not None:
            pending = value["pending"]
            require(type(pending) is dict and set(pending) == {"through", "phase"}
                    and type(pending["phase"]) is str and pending["phase"] in {"reserved", "queued", "uncertain"}
                    and 0 < integer(pending["through"]) <= value["wanted"], "invalid_pending")
        return value

    def save(self, value):
        try:
            common.write_private_json(str(self.directory), "state.json", value)
        except common.WakeError as exc:
            if exc.code == "state_commit_uncertain":
                # Establish what is visible, but never act on an uncertain reservation.
                self.load()
            raise

    def paused(self):
        path = self.directory / "paused.json"
        try:
            read_file(path, private=True)
        except FileNotFoundError:
            return False
        value = common.read_small_json(str(path))
        require(type(value) is dict and set(value) == {"paused"} and value["paused"] is True,
                "invalid_pause_file")
        return value is not None


def tick(store, source, runtime, now=None):
    state = store.load()
    old = canonical(state)
    now = integer(int(time.time()) if now is None else now)
    if store.paused():
        state["status"] = "paused"
    else:
        observation = observe(source, state["scanned"])
        read = observation["read"]
        require(read >= state["read"], "receipt_regressed")
        state["read"], state["scanned"] = read, observation["scanned"]
        handle = store.config["handle"]
        for message in observation["messages"]:
            if message["from"] != handle and handle in message["to"] and message["act"] != "ACK":
                state["wanted"] = max(state["wanted"], message["seq"])
        if state["pending"] and read >= state["pending"]["through"]:
            state["pending"] = None
        if state["wanted"] <= read:
            state["wanted"] = 0
        if state["pending"]:
            state["status"] = "queued_unread" if state["pending"]["phase"] == "queued" else "uncertain"
        elif not state["wanted"]:
            state["status"] = "caught_up"
        elif now < state["last_dispatch"] + 30:
            state["status"] = "rate_limited"
        else:
            status = runtime.status()
            if status != "idle":
                state["status"] = "deferred_" + status.lower()
            else:
                fresh = observe(source, state["scanned"])
                if fresh["head"] != observation["head"] or fresh["read"] != read or store.paused():
                    raise Deferred("observation_changed")
                state["pending"] = {"through": state["wanted"], "phase": "reserved"}
                state["last_dispatch"], state["status"] = now, "dispatch_reserved"
                store.save(state)
                try:
                    phase = runtime.queue(state["wanted"])
                    require(phase in {"queued", "uncertain"}, "invalid_dispatch_result")
                except common.WakeError as exc:
                    if exc.failure_class == "permanent":
                        raise
                    phase = "uncertain"
                except Exception:
                    phase = "uncertain"
                state["pending"]["phase"] = phase
                state["status"] = "queued_unread" if phase == "queued" else "uncertain"
    if canonical(state) != old:
        store.save(state)
    return state


def summary(state):
    return {k: state[k] for k in ("status", "read", "scanned", "wanted", "pending", "last_dispatch")}


def observe_loop(store, source, runtime, once=False, sleep=time.sleep):
    previous = None
    while True:
        try:
            result = summary(tick(store, source, runtime))
        except Deferred as exc:
            result = {"status": "deferred", "code": exc.code}
        text = json.dumps(result, sort_keys=True)
        if text != previous:
            print(text, flush=True)
            previous = text
        if once or result.get("status") == "paused":
            return 0
        sleep(5)


def no_symlinks(path):
    require(path.is_absolute(), "absolute_path_required")
    for parent in [*reversed(path.parents), path]:
        require(not parent.is_symlink(), "symlink_refused")

def read_file(path, maximum=16384, private=False, missing_code=None):
    no_symlinks(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if missing_code is not None:
            raise Invalid(missing_code) from None
        raise
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid(), "unsafe_file")
        # Project sources may use the shared project's group-writable umask;
        # executable pin hashes are checked separately. Runtime state is private.
        require(not (info.st_mode & (0o077 if private else 0o002)), "unsafe_file_mode")
        require(info.st_size <= maximum, "file_too_large")
        with os.fdopen(fd, "rb", closefd=False) as source:
            data = source.read(maximum + 1)
        require(len(data) <= maximum, "file_too_large")
        return data
    finally:
        os.close(fd)

def run(argv, cwd, timeout=15, maximum=1048576, env=None):
    """Bound both pipes and kill only our subprocess group on timeout/overflow."""
    proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True, env=env)
    streams = [bytearray(), bytearray()]
    deadline = time.monotonic() + timeout
    completed = False
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ, 0)
            selector.register(proc.stderr, selectors.EVENT_READ, 1)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Unavailable("command_timeout")
                for key, _ in selector.select(min(remaining, 0.2)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        streams[key.data].extend(chunk)
                        require(sum(map(len, streams)) <= maximum, "command_output_limit")
            code = proc.wait(timeout=max(0.01, deadline-time.monotonic()))
            completed = True
            return code, bytes(streams[0]), bytes(streams[1])
    except subprocess.TimeoutExpired:
        raise Unavailable("command_timeout") from None
    finally:
        if not completed or proc.poll() is None:
            # An exited parent can leave descendants holding the output pipes.
            # On failure, terminate our group even when that parent has exited.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        proc.stdout.close()
        proc.stderr.close()

def git_read(root, *args):
    """Read local Git state without inherited config, hooks, fsmonitor or writes."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", LC_ALL="C")
    code, out, _ = run(["/usr/bin/git", "-c", "core.hooksPath=" + os.devnull,
                       "-c", "core.fsmonitor=false", "-C", str(root), *args],
                      root, timeout=15, env=env)
    require(code == 0, "package_git_failed")
    return out.decode("utf-8").strip()


def unit_name(config):
    return "queue-wake@" + config["instance"] + ".service"


def unit_quote(value):
    """Quote one unit-file argument. ExecStart disables environment substitution."""
    require(type(value) is str and value and not any(ord(c) < 32 or ord(c) == 127 for c in value),
            "invalid_unit_argument")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def render_service(config, config_path, interpreter, adapter_file, template=None):
    """Pure rendering: no installation, activation, subprocess or state change."""
    validate_config(config)
    require(absolute(str(config_path)) == state_directory(config) / "config.json", "wrong_config_path")
    values = {"PYTHON": str(absolute(str(interpreter))), "ADAPTER": str(absolute(str(adapter_file))),
              "CONFIG": str(config_path), "ROOT": config["project"]["path"], "HANDLE": config["handle"],
              "SESSION": config["session"], "GENERATION": str(config["generation"])}
    if template is None:
        template = read_file(HERE / "queue-wake@.service", 16384).decode("utf-8")
    tokens = set(re.findall(r"@([A-Z_]+)@", template))
    require(tokens == set(values) | {"INSTANCE"}, "invalid_unit_template")
    values = {k: unit_quote(v) for k, v in values.items()}
    # This directive parses a literal path, not an ExecStart argument. A final
    # '/.' preserves a trailing space/backslash before config-line processing.
    values["ROOT"] = config["project"]["path"].replace("%", "%%") + "/."
    values["INSTANCE"] = config["instance"]
    return re.sub(r"@([A-Z_]+)@", lambda match: values[match.group(1)], template).encode("utf-8")


class Service:
    """Lifecycle operations target only this explicitly bound existing instance."""
    def __init__(self, config):
        self.root = Path(config["project"]["path"])
        self.name = unit_name(config)
        self.binding = config["service"]

    def state(self):
        require(self.binding is not None, "service_not_configured")
        require(digest(read_file(Path(self.binding["path"]), 16384, private=True)) == self.binding["sha256"],
                "service_file_changed")
        code, out, _ = run(["/usr/bin/systemctl", "--user", "show", self.name, "--property=LoadState",
                            "--property=ActiveState", "--property=MainPID", "--property=FragmentPath"],
                           self.root, timeout=15, maximum=65536)
        values = {}
        for line in out.decode("utf-8").splitlines():
            key, separator, value = line.partition("=")
            require(separator and key not in values, "service_state_invalid")
            values[key] = value
        require(set(values) == {"LoadState", "ActiveState", "MainPID", "FragmentPath"}, "service_state_invalid")
        require(code == 0 or values["LoadState"] == "not-found", "service_state_unavailable")
        if values["LoadState"] != "not-found":
            require(values["FragmentPath"] == self.binding["path"], "wrong_service_fragment")
        require(values["ActiveState"] in {"active", "inactive", "failed", "activating", "deactivating", "reloading"}
                and values["MainPID"].isdigit(), "service_state_invalid")
        active = values["ActiveState"] == "active"
        transitioning = values["ActiveState"] in {"activating", "deactivating", "reloading"}
        require(transitioning or active == (int(values["MainPID"]) > 0), "service_state_invalid")
        return {"loaded": values["LoadState"] != "not-found", "active": active, "transitioning": transitioning}

    def command(self, verb):
        require(verb in {"stop", "start"}, "invalid_service_command")
        require(self.state()["loaded"], "service_not_installed")
        code, _, _ = run(["/usr/bin/systemctl", "--user", verb, self.name], self.root, timeout=25, maximum=65536)
        require(code == 0, "service_command_failed")


def lock_held(directory):
    """Read-only inspection; never creates a lock or holds it after returning."""
    existing_private(directory)
    fd = os.open(directory / "observer.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077,
                "unsafe_lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)


def initialize(config):
    directory = state_directory(config)
    existing_private(directory)
    with exclusive(directory):
        with instance_gate(config):
            pass
        require(not os.path.lexists(directory / "state.json"), "already_initialized")
        observe(Source(config), 0)
        Runtime(config).status()
        common.write_private_json(str(directory), "state.json", initial_state(config))
    return {"status": "initialized"}


def check(config):
    removal_guard(config)
    store = Store(state_directory(config), config)
    state = store.load()
    observation = observe(Source(config), state["scanned"])
    require(observation["read"] >= state["read"], "receipt_regressed")
    status = Runtime(config).status()
    return {"status": "ok", "thread_status": status, "read": observation["read"],
            "scanned": observation["scanned"], "paused": store.paused()}


TRANSITION_SCHEMA = "ai-comm-queue-transition/1"
CODE_FILES = {"queue_wake.py", "common.py", "queue-wake@.service", "queue_wake.lock"}
BACKUP = re.compile(r"rebind-[0-9a-f]{12}\Z")


def package_of(endpoint):
    return {k: endpoint[k] for k in ("commit", "manifest_sha256", "protocol_sha256", "cli_sha256")}


def endpoint_valid(endpoint):
    require(type(endpoint) is dict and set(endpoint) == {"version", "tag", "tag_object", "commit",
            "manifest_sha256", "protocol_sha256", "cli_sha256"}, "invalid_endpoint")
    validate_package(package_of(endpoint))
    require(type(endpoint["version"]) is str and re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)",
            endpoint["version"]) and endpoint["tag"] == "v" + endpoint["version"]
            and type(endpoint["tag_object"]) is str and COMMIT.fullmatch(endpoint["tag_object"]), "invalid_endpoint")


def transition_identity(config):
    return {k: config[k] for k in ("project", "instance", "handle", "session", "generation", "runtime", "service")}


def load_transition(config, path, sha256):
    require(path is not None and type(sha256) is str and HEX.fullmatch(sha256), "transition_required")
    path = absolute(str(path))
    require(Path(config["project"]["path"]) in path.parents, "invalid_transition_path")
    raw = read_file(path, private=True)
    require(digest(raw) == sha256, "transition_changed")
    value = common.read_small_json(str(path))
    require(type(value) is dict and set(value) == {"v", "schema", "identity", "from", "to", "code"}
            and type(value["v"]) is int and value["v"] == 1 and value["schema"] == TRANSITION_SCHEMA
            and value["identity"] == transition_identity(config), "invalid_transition")
    endpoint_valid(value["from"])
    endpoint_valid(value["to"])
    require(value["from"]["commit"] != value["to"]["commit"], "invalid_transition")
    require(type(value["code"]) is dict and set(value["code"]) == CODE_FILES, "invalid_transition_code")
    for name, expected in value["code"].items():
        require(type(expected) is str and HEX.fullmatch(expected)
                and digest(read_file(HERE / name, 1048576)) == expected, "adapter_code_changed")
    require(config["package"] in (package_of(value["from"]), package_of(value["to"])), "binding_not_in_transition")
    return value


class Guided:
    """Owner-operated frozen phase hooks. No pin checkout, fetch or receipt writes."""
    def __init__(self, config, config_path, transition_path, sha256, service=None):
        self.config_path = Path(config_path)
        self.config = config
        self.directory = state_directory(config)
        self.transition_path, self.sha256 = transition_path, sha256
        self.transition = load_transition(config, transition_path, sha256)
        self.service = service or Service(config)

    def pair(self):
        config = load_config(self.config_path, self.config["handle"], self.config["session"], self.config["generation"])
        require(transition_identity(config) == self.transition["identity"], "runtime_identity_changed")
        require(config["package"] in (package_of(self.transition["from"]), package_of(self.transition["to"])),
                "binding_not_in_transition")
        return config, Store(self.directory, config).load()

    def live_endpoint(self):
        pin = Path(self.config["project"]["pin_path"])
        head = git_read(pin, "rev-parse", "HEAD")
        matches = [e for e in (self.transition["from"], self.transition["to"]) if e["commit"] == head]
        require(len(matches) == 1, "pin_not_in_transition")
        endpoint = matches[0]
        config = dict(self.config, package=package_of(endpoint))
        check_package(config)
        ref = "refs/tags/" + endpoint["tag"]
        require(git_read(pin, "cat-file", "-t", ref) == "tag"
                and git_read(pin, "rev-parse", ref) == endpoint["tag_object"]
                and git_read(pin, "rev-parse", ref + "^{commit}") == head, "tag_changed")
        return endpoint

    def observation(self, config, state):
        endpoint = self.live_endpoint()
        effective = dict(config, package=package_of(endpoint))
        observation = observe(Source(effective), state["scanned"])
        require(observation["read"] >= state["read"], "receipt_regressed")
        return observation

    def no_pending(self, config, state, ready=True):
        observation = self.observation(config, state)
        require(not state["pending"] or observation["read"] >= state["pending"]["through"], "unread_pending_wake")
        if ready:
            require(Runtime(config).status() in {"active", "idle"}, "runtime_not_ready")

    def probe(self):
        config, state = self.pair()
        paused = Store(self.directory, config).paused()
        service = self.service.state()
        if service.get("transitioning", False):
            raise Deferred("service_transitioning")
        active, held = service["active"], lock_held(self.directory)
        require(active == held, "service_lock_inconsistent")
        observation = self.observation(config, state)
        return {"paused": paused and not active, "running": active and not paused,
                "pending": bool(state["pending"] and observation["read"] < state["pending"]["through"]),
                "source_sha256": digest(read_file(HERE / "queue_wake.py", 1048576)),
                "bound_cli_sha256": config["package"]["cli_sha256"]}

    @contextmanager
    def stopped(self):
        config, _ = self.pair()
        require(Store(self.directory, config).paused(), "observer_must_be_paused")
        service = self.service.state()
        require(not service["active"] and not service.get("transitioning", False), "observer_must_be_stopped")
        with exclusive(self.directory):
            with instance_gate(config):
                pass
            yield

    def checked(self):
        config, state = self.pair()
        require(config["package"] == package_of(self.live_endpoint()), "binding_pin_mismatch")
        self.no_pending(config, state)
        return config, state

    def pause(self):
        config, state = self.pair()
        self.observation(config, state)
        common.write_private_json(str(self.directory), "paused.json", {"paused": True})
        self.service.command("stop")
        with self.stopped():
            config, state = self.pair()
            self.no_pending(config, state, ready=False)
        return self.probe()

    def after_pair(self, config, state, endpoint):
        after_config = dict(config, package=package_of(endpoint))
        validate_config(after_config)
        after_state = dict(state, binding=digest(canonical(after_config)), pending=None, status="rebound")
        Store(self.directory, after_config).validate(after_state)
        return after_config, after_state

    def write_pair(self, config, state):
        try:
            common.write_private_json(str(self.directory), "config.json", config)
            common.write_private_json(str(self.directory), "state.json", state)
        except common.WakeError as exc:
            if exc.code == "state_commit_uncertain":
                # Read both files for reconciliation, without assuming an old pair.
                common.read_small_json(str(self.config_path))
                common.read_small_json(str(self.directory / "state.json"))
            raise

    def rebind(self):
        import secrets
        with self.stopped():
            before_config, before_state = self.pair()
            self.no_pending(before_config, before_state)
            after = self.after_pair(before_config, before_state, self.live_endpoint())
            if (before_config, before_state) == after:
                return {"status": "already_bound"}
            backup_name = "rebind-" + secrets.token_hex(6)
            backup = self.directory / backup_name
            common.private_dir(str(backup))
            record = {"v": 1, "transition_sha256": self.sha256, "before_config": before_config,
                      "before_state": before_state, "after_config": after[0], "after_state": after[1]}
            try:
                common.write_private_json(str(backup), "transaction.json", record)
            except common.WakeError as exc:
                if exc.code == "state_commit_uncertain":
                    common.read_small_json(str(backup / "transaction.json"))
                raise
            self.write_pair(*after)
            require(self.pair() == after, "rebind_verification_failed")
            return {"status": "rebound", "backup": backup_name}

    def recover(self, name):
        require(type(name) is str and BACKUP.fullmatch(name), "invalid_backup_name")
        require(Store(self.directory, self.config).paused(), "observer_must_be_paused")
        service = self.service.state()
        require(not service["active"] and not service.get("transitioning", False), "observer_must_be_stopped")
        with exclusive(self.directory):
            with instance_gate(self.config):
                pass
            backup = self.directory / name
            existing_private(backup)
            record = common.read_small_json(str(backup / "transaction.json"))
            require(type(record) is dict and set(record) == {"v", "transition_sha256", "before_config", "before_state",
                    "after_config", "after_state"} and type(record["v"]) is int and record["v"] == 1
                    and record["transition_sha256"] == self.sha256, "invalid_transaction")
            before_config, after_config = validate_config(record["before_config"]), validate_config(record["after_config"])
            for config in (before_config, after_config):
                require(transition_identity(config) == self.transition["identity"]
                        and config["package"] in (package_of(self.transition["from"]), package_of(self.transition["to"])),
                        "invalid_transaction")
            before_state = Store(self.directory, before_config).validate(record["before_state"])
            after_state = Store(self.directory, after_config).validate(record["after_state"])
            endpoint = next(e for e in (self.transition["from"], self.transition["to"])
                            if package_of(e) == after_config["package"])
            require(self.after_pair(before_config, before_state, endpoint) == (after_config, after_state),
                    "invalid_transaction_changes")
            config = common.read_small_json(str(self.config_path))
            state = common.read_small_json(str(self.directory / "state.json"))
            require(config in (before_config, after_config) and state in (before_state, after_state), "stale_transaction")
            require((config, state) != (after_config, after_state), "completed_transaction_use_reverse")
            # A stale session cannot restore its files after a channel takeover.
            self.observation(before_config, before_state)
            self.write_pair(before_config, before_state)
            require(self.pair() == (before_config, before_state), "recovery_verification_failed")
            return {"status": "restored_before_pair", "paused": True, "pin_required": before_config["package"]["commit"]}

    def resume(self):
        current = self.probe()
        if current["running"]:
            self.checked()
            return current
        self.checked()
        require(not current["pending"], "unread_pending_wake")
        if not current["paused"]:
            common.write_private_json(str(self.directory), "paused.json", {"paused": True})
        with self.stopped():
            self.checked()
            os.unlink(self.directory / "paused.json")
        try:
            self.service.command("start")
            for _ in range(50):
                try:
                    result = self.probe()
                    if result["running"]:
                        return result
                except common.WakeError as exc:
                    if not isinstance(exc, Deferred) and exc.code != "service_lock_inconsistent":
                        raise
                time.sleep(0.1)
            raise Invalid("service_start_unconfirmed")
        except BaseException:
            cleanup_failed = False
            try:
                common.write_private_json(str(self.directory), "paused.json", {"paused": True})
            except BaseException:
                cleanup_failed = True
            try:
                self.service.command("stop")
            except BaseException:
                cleanup_failed = True
            if cleanup_failed:
                raise Invalid("resume_cleanup_failed") from None
            raise

    def execute(self, operation, backup=None):
        removal_guard(self.config)
        if operation == "probe":
            return self.probe()
        with exclusive(self.directory, "guided.lock"):
            with instance_gate(self.config):
                pass
            if operation == "check":
                self.checked()
                return self.probe()
            if operation == "pause":
                return self.pause()
            if operation == "resume":
                return self.resume()
            if operation == "recover":
                return self.recover(backup)
            require(operation == "rebind", "invalid_phase")
            return self.rebind()


class QuietParser(argparse.ArgumentParser):
    def error(self, message):
        raise Invalid("invalid_arguments")


def main(argv=None):
    parser = QuietParser(description=__doc__)
    parser.add_argument("command", choices=["init", "check", "status", "run", "once", "probe", "pause",
                                           "rebind", "resume", "recover", "retry"])
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--handle", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument("--transition", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--backup")
    parser.add_argument("--confirm-not-queued")
    try:
        args = parser.parse_args(argv)
        try:
            generation = int(args.generation)
        except ValueError:
            raise Invalid("generation_invalid") from None
        config = load_config(args.config, args.handle, args.session, generation)
        directory = state_directory(config)
        store = Store(directory, config)
        if args.command == "init":
            result = initialize(config)
        elif args.command == "check":
            result = (Guided(config, args.config, args.transition, args.sha256).execute("check")
                      if args.transition is not None else check(config))
        elif args.command == "status":
            result = dict(summary(store.load()), paused=store.paused())
        elif args.command in {"run", "once"}:
            with exclusive(directory, attempts=20):
                with instance_gate(config):
                    pass
                return observe_loop(store, Source(config), Runtime(config), once=args.command == "once")
        elif args.command == "retry":
            removal_guard(config)
            try:
                confirmed = integer(int(args.confirm_not_queued or ""))
            except ValueError:
                raise Invalid("confirmation_required") from None
            with exclusive(directory):
                with instance_gate(config):
                    pass
                state = store.load()
                require(state["pending"] is not None and state["pending"]["through"] == confirmed,
                        "confirmation_mismatch")
                observation = observe(Source(config), state["scanned"])
                require(observation["read"] >= state["read"], "receipt_regressed")
                state["pending"], state["status"] = None, "retry_confirmed"
                store.save(state)
            result = {"status": "retry_confirmed"}
        else:
            adapter = Guided(config, args.config, args.transition, args.sha256)
            result = adapter.execute(args.command, args.backup)
        print(json.dumps(result, sort_keys=True))
        return 0
    except common.WakeError as exc:
        print(exc.line(), file=sys.stderr)
        return exc.exit_code
    except (OSError, UnicodeError, ValueError) as exc:
        if isinstance(exc, OSError):
            mapped = common._os_error(exc)
        else:
            mapped = Invalid("invalid_data")
        print(mapped.line(), file=sys.stderr)
        return mapped.exit_code
    except Exception:
        mapped = Invalid("internal_error")
        print(mapped.line(), file=sys.stderr)
        return mapped.exit_code


if __name__ == "__main__":
    sys.exit(main())
