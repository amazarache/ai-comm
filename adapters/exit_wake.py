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
import json  # noqa: E402
import re  # noqa: E402
import secrets  # noqa: E402
import selectors  # noqa: E402
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
        commit_json(state_dir, RUN_NAME, {
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
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
        return watch(args, runner, out, clock, sleep, wall, git_run)
    except common.WakeError as exc:
        print(exc.line(), file=out)
        return exc.exit_code
    except OSError:
        print("WAKE-ERROR os_error", file=out)
        return common.EXIT_TRANSIENT


if __name__ == "__main__":
    sys.exit(main())
