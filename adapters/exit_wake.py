#!/usr/bin/env python3
"""exit-wake: a wake adapter for hosts that start a model turn when a background command exits.

The host's own background-task mechanism launches this process. It exits when a
new message addressed to its handle arrives (``WAKE``), when its bounded lifetime
ends (``WAKE-EXPIRED``), when an optional peer alarm fires (``PEER-OVERDUE``) or on
any failure (``WAKE-ERROR <code>``). The exit itself is the notification: the model
then reads and acknowledges its own inbox. This adapter never reads inbox pages,
never acknowledges, never sends and prints no message text.

Identity is explicit and checked before the first wait (design 024 v3, section 1):
- the project path, and the project key that ``wake plan`` derived from it and
  the channel's root commit; a mismatch stops the instance;
- the handle, an opaque protocol label (no name is special);
- the bound session and its channel generation, compared with the channel's
  current binding; a takeover or any change stops the instance until the owner
  rebinds it.

State lives in ``<project>/.ai-comm-local/wake/<project-key>-<handle>/`` (private,
symlinks refused): lock, cursor, ``run.json``, ``pause``/``pause-ack`` and
``peer-alarm.json``. The pause protocol is the one the guided upgrade phases
already drive: while a nonce-bearing ``pause`` request exists, the adapter writes
``pause-ack`` = {nonce, run_id} between rounds and makes no CLI call.

One adapter kind per handle (decision 1314): under the shared ``instance.lock``
gate a run refuses an instance directory holding the queue adapter's
``config.json`` (a link counts and is never followed), creates ``cursor.json`` at
0 if it is absent, so the instance stays marked even when it only ever expires,
and publishes ``run.json``. The gate is never held across a channel wait.

Guided upgrade phases: ``exit_wake.py probe|pause|rebind|check|resume`` with the
same identity arguments print one JSON object (``running``, ``paused``,
``pending``, ``bound_cli_sha256``) or ``{"error": CODE}`` with exit 75 or 78. A
probe creates nothing. Before any change, and again under the phase lock, every
phase checks the channel's current binding for this handle (session and
generation), the receipt, the kind marker and any live run's record; a refusal
changes nothing. Phases never launch a run: only the host does that.

Exit codes come from common.py: 0 for a wake, expiry or alarm; 75 for a transient
failure; 78 for a permanent one (unsafe state, identity or binding mismatch).
Standard library only, plus the sibling common.py from the same directory.
"""
import os
import sys

sys.dont_write_bytecode = True  # -I ignores PYTHONDONTWRITEBYTECODE; never write into a frozen bundle
_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)  # -I drops the script's directory; common.py is its sibling

import argparse  # noqa: E402
import errno  # noqa: E402
import fcntl  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import secrets  # noqa: E402
import selectors  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

import common  # noqa: E402

KEY = re.compile(r"[0-9a-f]{16}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
NONCE = re.compile(r"[0-9a-f]{32}\Z")
MESSAGE_ID = re.compile(r"[0-9]{8}-[a-z][a-z0-9-]{0,31}\Z")
TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
ACT = re.compile(r"[A-Z]{2,16}\Z")
MAX_CURSOR = 99999999
MAX_OUTPUT = 256 * 1024
MAX_STDERR = 16 * 1024
CURSOR_NAME = "cursor.json"
RUN_NAME = "run.json"
PAUSE_NAME = "pause"
ACK_NAME = "pause-ack"
ALARM_NAME = "peer-alarm.json"
LOCK_NAME = "lock"
INSTANCE_LOCK = "instance.lock"  # the cross-kind gate (decision 1314)
QUEUE_MARKER = "config.json"  # the queue adapter's persistent kind marker
PHASE_LOCK = "phase.lock"
PHASES = ("probe", "pause", "rebind", "check", "resume")
# A run checks the pause request between waits; a wait may take wait_timeout (<= 60 s) + 30 s.
PAUSE_DEADLINE_SECONDS = 60 + 30 + 5
PHASE_LOCK_SECONDS = PAUSE_DEADLINE_SECONDS + 10
RUN_RECORD_ATTEMPTS = 5
PHASE_POLL_SECONDS = 0.5
MAX_PID = 4194304
MAX_CLI_BYTES = 8 * 1024 * 1024
PAUSE_POLL_SECONDS = 1.0
CLI_TIMEOUT = 30
GIT_TIMEOUT = 30
MAX_FUTURE_SKEW = 60
CLASSES = (("unseen", "unseen"), ("seen", "seen-unacknowledged"))
GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS",
                "GIT_CONFIG_COUNT", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES")
# CLI error codes that mean this binding is no longer current: fail closed (permanent).
BINDING_CODES = {"unregistered_or_stale_session", "handle_already_active_explicit_takeover_required"}


class CliError(common.WakeError):
    """A CLI failure; carries the CLI's own documented code (never its text)."""

    def __init__(self, code, failure_class="transient", cli_code=None):
        super().__init__(code, failure_class)
        self.cli_code = cli_code


class Layout:
    """Fixed paths derived from the project root, as the upgrade controller lays them out."""

    def __init__(self, project, key, handle):
        self.project = project
        self.channel = os.path.join(project, ".ai-comm")
        self.pin = os.path.join(project, ".tools", "ai-comm")
        self.cli = os.path.join(self.pin, "ai_comm.py")
        self.wake_root = os.path.join(project, ".ai-comm-local", "wake")
        self.instance = common.instance_name(key, handle)
        self.state = os.path.join(self.wake_root, self.instance)


def git_env():
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV_DROP}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
                "GIT_CONFIG_VALUE_0": os.devnull, "LC_ALL": "C"})
    return env


def channel_root(channel, run=subprocess.run):
    """The channel's single root commit, read with hooks and user/system Git config disabled."""
    try:
        result = run(["git", "-C", channel, "rev-list", "--max-parents=0", "HEAD"], capture_output=True,
                     stdin=subprocess.DEVNULL, env=git_env(), timeout=GIT_TIMEOUT, check=False)
    except subprocess.TimeoutExpired as exc:
        raise common.WakeError("git_unavailable", "transient") from exc
    except OSError as exc:
        raise common.WakeError("git_unavailable", "transient") from exc
    if result.returncode != 0 or len(result.stdout) > 4096:
        raise common.WakeError("channel_identity_unavailable")
    roots = result.stdout.decode("ascii", "replace").split()
    if len(roots) != 1 or not COMMIT.fullmatch(roots[0]):
        raise common.WakeError("channel_identity_unavailable")
    return roots[0]


def project_identity(layout, run=subprocess.run):
    return {"path": layout.project, "channel": channel_root(layout.channel, run),
            "pin_path": os.path.realpath(layout.pin), "channel_path": os.path.realpath(layout.channel)}


def run_cli(cmd, timeout, max_stdout=MAX_OUTPUT, max_stderr=MAX_STDERR):
    """Run one bounded CLI call; return its JSON object or raise CliError.

    Both pipes are read incrementally with byte limits and a wall-clock deadline;
    the owned child is killed on overflow or timeout."""
    try:
        child = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, close_fds=True)
    except OSError as exc:
        raise CliError("cli_not_runnable", "permanent") from exc
    limits = {child.stdout: max_stdout, child.stderr: max_stderr}
    buffers = {child.stdout: bytearray(), child.stderr: bytearray()}
    deadline = time.monotonic() + timeout
    failure = None
    with selectors.DefaultSelector() as sel:
        for stream in limits:
            os.set_blocking(stream.fileno(), False)
            sel.register(stream, selectors.EVENT_READ)
        while sel.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = "cli_timed_out"
                break
            for key, _ in sel.select(min(remaining, 1.0)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    sel.unregister(key.fileobj)
                    continue
                buffers[key.fileobj] += chunk
                if len(buffers[key.fileobj]) > limits[key.fileobj]:
                    failure = "cli_output_too_large"
                    break
            if failure:
                break
    if failure:
        child.kill()
    try:
        returncode = child.wait(timeout=max(0.1, deadline - time.monotonic()) if not failure else 5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
        failure = failure or "cli_timed_out"
    finally:
        child.stdout.close()
        child.stderr.close()
    if failure:
        raise CliError(failure)
    if returncode != 0:
        cli_code = _cli_error_code(bytes(buffers[child.stderr]))
        if cli_code in BINDING_CODES:
            raise CliError("session_not_current", "permanent", cli_code)
        if cli_code == "invalid_cli_arguments":
            raise CliError("cli_arguments_refused", "permanent", cli_code)
        raise CliError("cli_failed", "transient", cli_code)
    try:
        result = json.loads(bytes(buffers[child.stdout]).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CliError("cli_output_invalid") from exc
    if not isinstance(result, dict):
        raise CliError("cli_output_invalid")
    return result


def _cli_error_code(raw):
    """The CLI's documented error code from bounded stderr JSON, or None. Never the text."""
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    code = value.get("code") if isinstance(value, dict) else None
    return code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else None


def check_binding(state, handle, session, generation):
    """The channel's current binding for this handle must be exactly the configured one."""
    participants = state.get("participants") if isinstance(state, dict) else None
    if not isinstance(participants, list):
        raise CliError("cli_output_invalid")
    records = [p for p in participants if isinstance(p, dict) and p.get("handle") == handle]
    if not records:
        raise common.WakeError("handle_not_joined")
    if len(records) != 1:
        raise CliError("cli_output_invalid")
    record = records[0]
    if record.get("session") != session:
        raise common.WakeError("session_mismatch")
    if record.get("generation") != generation:
        raise common.WakeError("generation_mismatch")


def commit_json(directory, name, value):
    """Write private JSON; if only the directory sync failed, accept the write after reading it back."""
    try:
        common.write_private_json(directory, name, value)
    except common.WakeError as exc:
        if exc.code != "state_commit_uncertain":
            raise
        if common.read_small_json(os.path.join(directory, name)) != value:
            raise
    return value


def publish_run(state_dir, sleep, record):
    """Under the shared instance gate: refuse a queue instance, keep a persistent cursor
    marker (created at 0 if absent, an existing one validated and preserved), then
    publish run.json. The gate is never held across a channel wait."""
    gate = common.acquire_lock(state_dir, name=INSTANCE_LOCK, sleep=sleep)
    try:
        if os.path.lexists(os.path.join(state_dir, QUEUE_MARKER)):  # a link counts; never followed
            raise common.WakeError("instance_kind_conflict")
        if not os.path.lexists(os.path.join(state_dir, CURSOR_NAME)):
            write_cursor(state_dir, 0)
        read_cursor(state_dir)
        commit_json(state_dir, RUN_NAME, record)
    finally:
        os.close(gate)


def remove_file(directory, name):
    try:
        os.unlink(os.path.join(directory, name))
    except FileNotFoundError:
        pass


def read_cursor(state_dir):
    value = common.read_small_json(os.path.join(state_dir, CURSOR_NAME))
    if value is None:
        return 0
    through = value.get("notification_through")
    if set(value) != {"v", "notification_through"} or value["v"] != 1 or not _is_int(through):
        raise common.WakeError("cursor_invalid")
    return through


def write_cursor(state_dir, through):
    if not _is_int(through):
        raise common.WakeError("cursor_invalid")
    commit_json(state_dir, CURSOR_NAME, {"v": 1, "notification_through": through})


def read_pause_request(state_dir):
    """Return the pending pause nonce, or None. A malformed request is an error."""
    value = common.read_small_json(os.path.join(state_dir, PAUSE_NAME))
    if value is None:
        return None
    nonce = value.get("nonce")
    if value.get("v") != 1 or not isinstance(nonce, str) or not NONCE.fullmatch(nonce):
        raise common.WakeError("pause_request_invalid")
    return nonce


def honor_pause(state_dir, run_id, deadline, clock=time.monotonic, sleep=time.sleep):
    """Between wait calls only. While a pause is requested, acknowledge its nonce for this
    run and make no CLI call. Returns False if the bounded lifetime ends while paused."""
    acked = None
    while True:
        nonce = read_pause_request(state_dir)
        if nonce is None:
            if acked is not None:
                remove_file(state_dir, ACK_NAME)
            return True
        if nonce != acked:
            commit_json(state_dir, ACK_NAME, {"v": 1, "nonce": nonce, "run_id": run_id})
            acked = nonce
        if clock() >= deadline:
            return False
        sleep(PAUSE_POLL_SECONDS)


def _is_int(value, low=0):
    return type(value) is int and low <= value <= MAX_CURSOR


def parse_timestamp(value):
    """Strict UTC ``YYYY-MM-DDTHH:MM:SSZ`` to epoch seconds."""
    if not isinstance(value, str) or not TIMESTAMP.fullmatch(value):
        raise CliError("outstanding_invalid")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError as exc:
        raise CliError("outstanding_invalid") from exc


def validate_outstanding(result, peer, cls, after):
    """Validate one filtered ``outstanding`` page. Returns (generation, count, entry or None)."""
    info, receipt = result.get("peer"), None
    if isinstance(info, dict):
        receipt = info.get("receipt")
    if (not isinstance(info, dict) or info.get("handle") != peer or not _is_int(info.get("generation"), 1)
            or not isinstance(receipt, dict) or type(receipt.get("present")) is not bool
            or not _is_int(receipt.get("generation"), 1) or not _is_int(receipt.get("read_through"))
            or not _is_int(receipt.get("seen_through"))
            or receipt["read_through"] > receipt["seen_through"]):
        raise CliError("outstanding_invalid")
    count, matched, messages = result.get("count"), result.get("matched"), result.get("messages")
    if (not _is_int(count) or not _is_int(matched) or matched > count or not isinstance(messages, list)
            or len(messages) > 1 or (matched > 0) != (len(messages) == 1)):
        raise CliError("outstanding_invalid")
    if not messages:
        return info["generation"], count, None
    entry = messages[0]
    if (not isinstance(entry, dict) or set(entry) != {"id", "seq", "act", "ts", "state", "replied"}
            or not isinstance(entry["id"], str) or not MESSAGE_ID.fullmatch(entry["id"])
            or not _is_int(entry["seq"], 1) or entry["seq"] <= after
            or entry["seq"] <= receipt["read_through"]
            or not isinstance(entry["act"], str) or not ACT.fullmatch(entry["act"]) or entry["act"] == "ACK"
            or entry["state"] != cls or entry["replied"] is not False
            or (cls == "unseen") != (entry["seq"] > receipt["seen_through"])):
        raise CliError("outstanding_invalid")
    parse_timestamp(entry["ts"])
    return info["generation"], count, entry


def read_watermarks(state_dir):
    """Stored ``{peer, generation, unseen, seen}``, or None. A malformed file is an error."""
    value = common.read_small_json(os.path.join(state_dir, ALARM_NAME))
    if value is None:
        return None
    if (set(value) != {"v", "peer", "generation", "unseen_seq", "seen_seq"} or value["v"] != 2
            or not isinstance(value["peer"], str) or not common.HANDLE.fullmatch(value["peer"])
            or not _is_int(value["generation"], 1) or not _is_int(value["unseen_seq"])
            or not _is_int(value["seen_seq"])):
        raise common.WakeError("state_file_invalid")
    return {"peer": value["peer"], "generation": value["generation"],
            "unseen": value["unseen_seq"], "seen": value["seen_seq"]}


def write_watermarks(state_dir, peer, generation, marks):
    commit_json(state_dir, ALARM_NAME, {"v": 2, "peer": peer, "generation": generation,
                                        "unseen_seq": marks["unseen"], "seen_seq": marks["seen"]})


def query_outstanding(cmd_base, peer, cls, after, runner):
    # A terminal ACK needs no response, so it is excluded before the limit.
    cmd = [*cmd_base, "--to", peer, "--state", cls, "--unreplied", "--exclude-act", "ACK",
           "--after", str(after), "--limit", "1"]
    try:
        result = runner(cmd, CLI_TIMEOUT)
    except CliError as exc:
        if exc.cli_code == "invalid_cli_arguments":
            raise CliError("outstanding_unsupported", "permanent") from exc
        if exc.failure_class == "permanent":
            raise
        raise CliError("outstanding_unavailable") from exc
    return validate_outstanding(result, peer, cls, after)


def query_classes(cmd_base, peer, marks, runner):
    """One filtered query per class; both must report the same peer generation."""
    pages = {cls: query_outstanding(cmd_base, peer, cls, marks[cls], runner) for cls, _ in CLASSES}
    generations = {generation for generation, _, _ in pages.values()}
    if len(generations) != 1:
        raise CliError("outstanding_invalid")
    return generations.pop(), pages


def check_peer(args, state_dir, cmd_base, runner, wall):
    """Return one ``PEER-OVERDUE`` line, or None. Writes watermarks before returning a line."""
    stored = read_watermarks(state_dir)
    marks = ({"unseen": stored["unseen"], "seen": stored["seen"]}
             if stored and stored["peer"] == args.peer else {"unseen": 0, "seen": 0})
    generation, pages = query_classes(cmd_base, args.peer, marks, runner)
    if stored is None or stored["peer"] != args.peer or stored["generation"] != generation:
        # Watermarks from another peer or generation cannot select this generation's
        # oldest candidates: reset both and query again from the start.
        if marks != {"unseen": 0, "seen": 0}:
            marks = {"unseen": 0, "seen": 0}
            again, pages = query_classes(cmd_base, args.peer, marks, runner)
            if again != generation:
                raise CliError("outstanding_generation_changed")
        write_watermarks(state_dir, args.peer, generation, marks)
    now = wall()
    thresholds = {"unseen": args.overdue_after, "seen": args.seen_overdue_after}
    overdue = []
    for cls, label in CLASSES:
        _, _, entry = pages[cls]
        if entry is None:
            continue
        sent = parse_timestamp(entry["ts"])
        if sent - now > MAX_FUTURE_SKEW:
            raise CliError("outstanding_clock_skew")
        age = max(0, int(now - sent))
        if age >= thresholds[cls]:
            overdue.append((cls, label, entry, age))
    if not overdue:
        return None
    for cls, _, entry, _ in overdue:
        marks[cls] = entry["seq"]
    write_watermarks(state_dir, args.peer, generation, marks)
    count = pages["unseen"][1]
    parts = [f"{label} {entry['id']} sent {entry['ts']} age {age}s" for _, label, entry, age in overdue]
    return f"PEER-OVERDUE {args.peer} outstanding {count} " + " ".join(parts)


def poll_once(cmd_base, cursor, wait_timeout, runner):
    """One wait call. Returns (new_cursor, woke)."""
    cmd = [*cmd_base, "--timeout", str(wait_timeout), "--new-since", str(cursor)]
    result = runner(cmd, wait_timeout + 30)
    through = result.get("notification_through")
    messages = result.get("messages")
    if type(through) is not int or not cursor <= through <= MAX_CURSOR:
        raise CliError("notification_cursor_invalid")
    if not isinstance(messages, list):
        raise CliError("cli_output_invalid")
    return through, bool(messages)


def cli_base(layout, subcommand, args, identity=True):
    # The session is passed in --session=VALUE form, so a reference that starts with "-"
    # can never be read as an option.
    base = [sys.executable, "-I", "-B", layout.cli, "--channel", layout.channel, subcommand]
    if identity:
        base += ["--as", args.handle, f"--session={args.session}"]
    return base


def watch(args, runner=run_cli, out=sys.stdout, clock=time.monotonic, sleep=time.sleep, wall=time.time,
          git_run=subprocess.run):
    layout = Layout(args.project, args.project_key, args.handle)
    identity = project_identity(layout, git_run)
    if common.project_key(identity) != args.project_key:
        raise common.WakeError("project_key_mismatch")
    common.private_dir(layout.wake_root)
    state_dir = common.private_dir(layout.state)
    lock_fd = common.acquire_lock(state_dir, sleep=sleep)
    run_id = secrets.token_hex(16)
    try:
        check_binding(runner(cli_base(layout, "state", args, identity=False), CLI_TIMEOUT),
                      args.handle, args.session, args.generation)
        publish_run(state_dir, sleep, {
            "v": 1, "adapter": "exit-wake", "run_id": run_id, "pid": os.getpid(), "instance": layout.instance,
            "project": identity, "handle": args.handle, "session": args.session,
            "generation": args.generation})
        deadline = clock() + args.rounds * args.wait_timeout
        cursor = read_cursor(state_dir)
        wait_base = cli_base(layout, "wait", args)
        outstanding_base = cli_base(layout, "outstanding", args)
        for _ in range(args.rounds):
            if not honor_pause(state_dir, run_id, deadline, clock, sleep):
                break
            new_cursor, woke = poll_once(wait_base, cursor, args.wait_timeout, runner)
            if new_cursor != cursor:
                write_cursor(state_dir, new_cursor)
                cursor = new_cursor
            if woke:
                print(f"WAKE new messages for {args.handle}; notification cursor {cursor}", file=out)
                return common.EXIT_OK
            if args.peer is not None and read_pause_request(state_dir) is None:
                line = check_peer(args, state_dir, outstanding_base, runner, wall)
                if line is not None:
                    print(line, file=out)
                    return common.EXIT_OK
        print(f"WAKE-EXPIRED re-arm; notification cursor {cursor}", file=out)
        return common.EXIT_OK
    finally:
        try:
            current = common.read_small_json(os.path.join(state_dir, RUN_NAME))
            if current and current.get("run_id") == run_id:
                remove_file(state_dir, RUN_NAME)
        except (common.WakeError, OSError):
            pass
        os.close(lock_fd)


# --------------------------------------------------------------------------- guided phases
# Owner phases for the upgrade controller: probe, pause, rebind, check, resume. Each prints
# one JSON object; a failure prints {"error": CODE} and exits 75 or 78. They never read inbox
# pages, acknowledge, send, reset a cursor or launch a run: the host starts runs.


def existing_private_dir(path):
    """Validate without creating or changing anything. Returns False if absent."""
    common._reject_symlinked_ancestors(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise common._os_error(exc) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise common.WakeError("state_dir_not_directory")
    if info.st_uid != os.getuid():
        raise common.WakeError("state_dir_wrong_owner")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise common.WakeError("state_dir_not_private")
    return True


def lock_held(state_dir):
    """True if a run holds the run lock. Opens read-only, never creates it."""
    try:
        fd = os.open(os.path.join(state_dir, LOCK_NAME), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise common._os_error(exc) from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise common.WakeError("lock_not_regular_file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError as exc:
            raise common._os_error(exc) from exc
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError, PermissionError):
        return False  # gone, or not ours: cannot be this instance's run
    return True


def file_digest(path):
    """sha256 of a regular, non-symlinked file, bounded while reading."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise common.WakeError("cli_invalid") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise common.WakeError("cli_invalid")
        digest, total = hashlib.sha256(), 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                return digest.hexdigest()
            total += len(chunk)
            if total > MAX_CLI_BYTES:
                raise common.WakeError("cli_invalid")
            digest.update(chunk)
    except OSError as exc:
        raise common.WakeError("cli_invalid") from exc
    finally:
        os.close(fd)


class Instance:
    """One declared instance: identity verified exactly as a run verifies it."""

    def __init__(self, args, git_run=subprocess.run, runner=None):
        self.args = args
        self.runner = runner or run_cli
        self.layout = Layout(args.project, args.project_key, args.handle)
        self.identity = project_identity(self.layout, git_run)
        if common.project_key(self.identity) != args.project_key:
            raise common.WakeError("project_key_mismatch")
        if os.path.realpath(self.layout.cli) != self.layout.cli:
            raise common.WakeError("cli_path_not_canonical")
        self.state = self.layout.state


def validated_run(inst, sleep):
    """The run lock is held: its holder must have recorded exactly this instance."""
    for attempt in range(RUN_RECORD_ATTEMPTS):
        run = common.read_small_json(os.path.join(inst.state, RUN_NAME))
        if run is not None:
            break
        if attempt + 1 < RUN_RECORD_ATTEMPTS:
            sleep(0.2)
    else:
        raise common.WakeError("run_identity_unverified")
    a = inst.args
    expected = {"v": 1, "adapter": "exit-wake", "instance": inst.layout.instance, "project": inst.identity,
                "handle": a.handle, "session": a.session, "generation": a.generation}
    pid, run_id = run.get("pid"), run.get("run_id")
    if (any(run.get(k) != v for k, v in expected.items())
            or type(pid) is not int or not 1 <= pid <= MAX_PID
            or not isinstance(run_id, str) or not NONCE.fullmatch(run_id) or not pid_alive(pid)):
        raise common.WakeError("run_identity_unverified")
    return run


def receipt_read(inst):
    """This handle's real read receipt, validated for the declared generation."""
    path = os.path.join(inst.layout.channel, "ai-comm-runtime", inst.args.handle + ".json")
    value = common.read_small_json(path)
    if value is None:
        raise common.WakeError("receipt_missing")
    if (set(value) != {"v", "generation", "read", "seen"} or value["v"] != 2
            or any(type(value[k]) is not int for k in ("generation", "read", "seen"))
            or value["generation"] != inst.args.generation
            or not 0 <= value["read"] <= value["seen"] <= MAX_CURSOR):
        raise common.WakeError("receipt_invalid")
    return value["read"]


def observe(inst, sleep=time.sleep):
    present = existing_private_dir(inst.state)
    if present and os.path.lexists(os.path.join(inst.state, QUEUE_MARKER)):
        raise common.WakeError("instance_kind_conflict")
    running = present and lock_held(inst.state)
    run = validated_run(inst, sleep) if running else None
    nonce = ack = None
    cursor = 0
    if present:
        nonce = read_pause_request(inst.state)
        cursor = read_cursor(inst.state)
        ack = common.read_small_json(os.path.join(inst.state, ACK_NAME))
    acked = (run is not None and ack is not None and ack.get("v") == 1
             and ack.get("nonce") == nonce and ack.get("run_id") == run["run_id"])
    paused = nonce is not None and (not running or acked)
    return {"running": bool(running), "paused": paused, "pending": cursor > receipt_read(inst),
            "bound_cli_sha256": file_digest(inst.layout.cli)}


def preflight(inst, sleep):
    """Every check a phase needs before it may change anything; reads only (review 1326).

    The channel's current binding for this handle must be exactly the declared session
    and generation, even when no run is active; the receipt must be valid for that
    generation; a queue instance is refused; and a running instance must have
    recorded exactly this identity."""
    a = inst.args
    check_binding(inst.runner(cli_base(inst.layout, "state", a, identity=False), CLI_TIMEOUT),
                  a.handle, a.session, a.generation)
    receipt_read(inst)
    if existing_private_dir(inst.state):
        if os.path.lexists(os.path.join(inst.state, QUEUE_MARKER)):
            raise common.WakeError("instance_kind_conflict")
        if lock_held(inst.state):
            validated_run(inst, sleep)


class PhaseLock:
    def __init__(self, state_dir, clock, sleep):
        self.state_dir, self.clock, self.sleep = state_dir, clock, sleep
        self.fd = None

    def __enter__(self):
        deadline = self.clock() + PHASE_LOCK_SECONDS
        while True:
            try:
                self.fd = common.acquire_lock(self.state_dir, name=PHASE_LOCK, sleep=self.sleep, attempts=1)
                return self
            except common.WakeError as exc:
                if exc.code != "another_instance_running":
                    raise
                if self.clock() >= deadline:
                    raise common.WakeError("phase_busy", "transient") from exc
                self.sleep(PHASE_POLL_SECONDS)

    def __exit__(self, *exc):
        os.close(self.fd)
        return False


def run_phase(phase, inst, clock=time.monotonic, sleep=time.sleep):
    if phase not in PHASES:
        raise common.WakeError("unknown_phase")
    preflight(inst, sleep)  # before creating, tightening or writing anything
    if phase == "probe":
        return observe(inst, sleep)
    common.private_dir(inst.layout.wake_root)
    common.private_dir(inst.state)
    with PhaseLock(inst.state, clock, sleep):
        preflight(inst, sleep)  # again, immediately before the mutation
        if phase in ("check", "rebind"):
            # A run holds no package binding: it calls the pin's CLI each round.
            return observe(inst, sleep)
        if phase == "pause":
            commit_json(inst.state, PAUSE_NAME, {"v": 1, "nonce": secrets.token_hex(16)})
            deadline = clock() + PAUSE_DEADLINE_SECONDS
            while True:
                state = observe(inst, sleep)
                if state["paused"] and not state["pending"]:
                    return state
                if clock() >= deadline:
                    raise common.WakeError("run_did_not_pause", "transient")  # the request stays
                sleep(PHASE_POLL_SECONDS)
        if phase == "resume":
            remove_file(inst.state, PAUSE_NAME)
            remove_file(inst.state, ACK_NAME)
            return observe(inst, sleep)
    raise common.WakeError("unknown_phase")


def phase_main(argv, out, clock, sleep, git_run, runner=None):
    try:
        phase, rest = argv[0], argv[1:]
        args = parse_args(rest)
        result = run_phase(phase, Instance(args, git_run, runner), clock, sleep)
    except common.WakeError as exc:
        print(json.dumps({"error": exc.code}), file=out)
        return exc.exit_code
    except OSError:
        print(json.dumps({"error": "os_error"}), file=out)
        return common.EXIT_TRANSIENT
    print(json.dumps(result, sort_keys=True), file=out)
    return common.EXIT_OK


def _bounded_int(text, low, high, code):
    if not re.fullmatch(r"[0-9]{1,8}", text or ""):
        raise common.WakeError(code)
    value = int(text)
    if not low <= value <= high:
        raise common.WakeError(code)
    return value


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # never echo argument text
        raise common.WakeError("arguments_invalid")


def parse_args(argv):
    p = _Parser(add_help=False)
    p.add_argument("--project", required=True)
    p.add_argument("--project-key", required=True)
    p.add_argument("--handle", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--generation", required=True)
    p.add_argument("--peer")
    p.add_argument("--rounds", default="55")
    p.add_argument("--wait-timeout", default="30")
    p.add_argument("--overdue-after", default="600")
    p.add_argument("--seen-overdue-after", default="3600")
    args = p.parse_args(argv)
    if (not os.path.isabs(args.project) or os.path.normpath(args.project) != args.project
            or args.project == os.sep):
        raise common.WakeError("project_path_invalid")
    if not KEY.fullmatch(args.project_key):
        raise common.WakeError("project_key_invalid")
    common.validate_handle(args.handle)
    common.validate_session(args.session)
    if not re.fullmatch(r"[1-9][0-9]{0,7}", args.generation):
        raise common.WakeError("generation_invalid")
    args.generation = common.validate_generation(int(args.generation))
    if args.peer is not None and (not common.HANDLE.fullmatch(args.peer) or args.peer == args.handle):
        raise common.WakeError("peer_invalid")
    args.rounds = _bounded_int(args.rounds, 1, 120, "bounds_invalid")
    args.wait_timeout = _bounded_int(args.wait_timeout, 1, 60, "bounds_invalid")
    args.overdue_after = _bounded_int(args.overdue_after, 120, 3600, "bounds_invalid")
    args.seen_overdue_after = _bounded_int(args.seen_overdue_after, 600, 14400, "bounds_invalid")
    if args.seen_overdue_after < args.overdue_after:
        raise common.WakeError("bounds_invalid")
    return args


def main(argv=None, runner=run_cli, out=sys.stdout, clock=time.monotonic, sleep=time.sleep, wall=time.time,
         git_run=subprocess.run):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in PHASES:
        return phase_main(argv, out, clock, sleep, git_run, runner)
    try:
        args = parse_args(argv)
        return watch(args, runner, out, clock, sleep, wall, git_run)
    except common.WakeError as exc:
        print(exc.line(), file=out)
        return exc.exit_code
    except OSError:
        print("WAKE-ERROR os_error", file=out)
        return common.EXIT_TRANSIENT


if __name__ == "__main__":
    sys.exit(main())
