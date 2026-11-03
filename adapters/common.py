"""Shared contract for the optional wake adapters (design 024 v3, work split 023).

Both adapters import only this module from the package. It defines:

- the derived project key used to name state and service instances;
- protocol handle validation (handles are opaque labels; no name is special);
- the protocol session-reference boundary, with an optional narrower host rule;
- safe service-instance names;
- private on-disk state primitives (directories, bounded JSON, atomic writes, a lock);
- fixed failure classes and their exit codes.

Nothing here reads inbox content, acknowledges, sends, or starts a process. Changes to
this contract need a renewed independent review. Standard library only.
"""
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time
import unicodedata

# Protocol grammar, identical to ai_comm.py's HANDLE and the 160-character session reference.
HANDLE = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
SESSION_MAX = 160
_SESSION_CONTROLS = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")
PROJECT_KEY = re.compile(r"[0-9a-f]{16}\Z")
# systemd unit names: [A-Za-z0-9:_.\\-] plus "@" for templates, at most 255 characters.
UNIT_TEMPLATE_RESERVE = len("queue-wake@.service")
UNIT_NAME_MAX = 255

MAX_SMALL_JSON = 16 * 1024
MAX_JSON_DEPTH = 32  # state and config files are shallow; deeper input is refused
LOCK_ATTEMPTS = 10
LOCK_RETRY_SECONDS = 0.2

# Failure classes and their exit codes (shared by both adapters; see docs/WAKE.md).
EXIT_OK = 0
EXIT_TRANSIENT = 75   # EX_TEMPFAIL: retry with bounded backoff
EXIT_PERMANENT = 78   # EX_CONFIG: unsafe or mismatched setup; never restarted automatically
CLASSES = {"ok": EXIT_OK, "transient": EXIT_TRANSIENT, "permanent": EXIT_PERMANENT, "paused": EXIT_OK}


class WakeError(Exception):
    """A failure with a fixed, content-free code and a failure class."""

    def __init__(self, code, failure_class="permanent"):
        if failure_class not in ("transient", "permanent"):
            raise ValueError("invalid failure class")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            raise ValueError("invalid error code")
        super().__init__(code)
        self.code = code
        self.failure_class = failure_class

    @property
    def exit_code(self):
        return CLASSES[self.failure_class]

    def line(self):
        """The single content-free line an adapter prints for this failure."""
        return f"WAKE-ERROR {self.code}"


def validate_handle(value):
    """Return the handle if it matches the protocol grammar. No handle is special."""
    if not isinstance(value, str) or not HANDLE.fullmatch(value):
        raise WakeError("handle_invalid")
    return value


def validate_session(value, host_rule=None):
    """The protocol's session-reference boundary, plus an optional narrower host rule.

    The protocol accepts nonempty text of at most 160 characters without control or
    formatting characters. A host adapter may pass a compiled regular expression for
    its own, narrower format; this module assumes no particular format (no UUID rule).
    """
    if (not isinstance(value, str) or not 0 < len(value) <= SESSION_MAX or value.strip() == ""
            or _SESSION_CONTROLS.search(value)
            or any(unicodedata.category(c) in {"Cf", "Cs", "Zl", "Zp"} for c in value)):
        raise WakeError("session_invalid")
    if host_rule is not None and not host_rule.fullmatch(value):
        raise WakeError("session_not_host_format")
    return value


def validate_generation(value):
    if type(value) is not int or not 1 <= value <= 99999999:
        raise WakeError("generation_invalid")
    return value


def project_key(identity):
    """First 16 hex digits of sha256 over canonical {"channel", "path"} of a validated identity.

    `identity` is upgrade_controller.Project.project_id(): path, channel, pin_path, channel_path.
    The full identity is kept in configs; only this key is used in names. Different project
    paths always give different keys, even with identical channel histories.
    """
    if not isinstance(identity, dict):
        raise WakeError("project_identity_invalid")
    path, channel = identity.get("path"), identity.get("channel")
    if (not isinstance(path, str) or not os.path.isabs(path) or os.path.normpath(path) != path
            or not isinstance(channel, str) or not re.fullmatch(r"[0-9a-f]{40}", channel)):
        raise WakeError("project_identity_invalid")
    canonical = json.dumps({"channel": channel, "path": path}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def instance_name(key, handle):
    """`<project-key>-<handle>`, checked against the systemd unit-name grammar and length."""
    if not isinstance(key, str) or not PROJECT_KEY.fullmatch(key):
        raise WakeError("project_key_invalid")
    name = f"{key}-{validate_handle(handle)}"
    if not re.fullmatch(r"[A-Za-z0-9:_.\-]+", name) or len(name) + UNIT_TEMPLATE_RESERVE > UNIT_NAME_MAX:
        raise WakeError("instance_name_invalid")
    return name


def _reject_symlinked_ancestors(path):
    if not isinstance(path, str) or not os.path.isabs(path):
        raise WakeError("state_path_not_absolute")
    if os.path.normpath(path) != path:
        raise WakeError("state_path_not_normalized")
    current = os.sep
    for part in [p for p in path.split(os.sep) if p]:
        current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return
        except NotADirectoryError as exc:
            raise WakeError("state_parent_not_directory") from exc
        except OSError as exc:
            raise WakeError("state_io_failed", "transient") from exc
        if stat.S_ISLNK(info.st_mode):
            raise WakeError("state_path_symlink")


def private_dir(path):
    """Create or validate a real directory owned by this user, mode 0700.

    The parent must already exist. No component of the path may be a symlink.
    A group- or world-accessible directory is tightened to 0700.
    """
    _reject_symlinked_ancestors(path)
    parent = os.path.dirname(path)
    try:
        pinfo = os.lstat(parent)
    except FileNotFoundError as exc:
        raise WakeError("state_parent_missing") from exc
    except OSError as exc:
        raise _os_error(exc) from exc
    if not stat.S_ISDIR(pinfo.st_mode):
        raise WakeError("state_parent_not_directory")
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise _os_error(exc) from exc
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise _os_error(exc) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise WakeError("state_dir_not_directory")
    if info.st_uid != os.getuid():
        raise WakeError("state_dir_wrong_owner")
    if stat.S_IMODE(info.st_mode) & 0o077:
        try:
            os.chmod(path, 0o700)
        except OSError as exc:
            raise _os_error(exc) from exc
    return path


def _open_nofollow(path, flags, mode=0o600):
    try:
        return os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise _os_error(exc) from exc


def _os_error(exc):
    """Map a filesystem error to a fixed code; never echo paths or OS text."""
    if exc.errno == errno.ELOOP:
        return WakeError("state_file_symlink")
    if exc.errno in (errno.EISDIR, errno.ENXIO):
        return WakeError("state_file_not_regular")
    if exc.errno == errno.ENOTDIR:
        return WakeError("state_parent_not_directory")
    if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
        return WakeError("state_access_denied")
    return WakeError("state_io_failed", "transient")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _no_constant(name):
    raise ValueError("non-finite number")


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):  # an exponent overflow such as 1e400 or -1e400
        raise ValueError("non-finite number")
    return value


def _within_depth(value, limit=MAX_JSON_DEPTH):
    """Iteratively check container nesting, so a deep document cannot exhaust the stack later."""
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, (dict, list)):
            if depth > limit:
                return False
            children = item.values() if isinstance(item, dict) else item
            stack.extend((child, depth + 1) for child in children)
    return True


def _close_quietly(fd):
    """Release a descriptor on a failure path without replacing the mapped error."""
    try:
        os.close(fd)
    except OSError:
        pass


def read_small_json(path, missing=None, limit=MAX_SMALL_JSON):
    """A bounded JSON object from a regular, non-symlinked file owned by this user.

    Duplicate keys, NaN/Infinity (literal or by exponent overflow), nesting deeper
    than MAX_JSON_DEPTH, non-objects and oversize files are refused.
    Returns `missing` when the file does not exist.
    """
    try:
        fd = _open_nofollow(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return missing
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit or info.st_uid != os.getuid():
            raise WakeError("state_file_invalid")
        raw = os.read(fd, limit + 1)
    except OSError as exc:
        raise _os_error(exc) from exc
    finally:
        _close_quietly(fd)
    if len(raw) > limit:
        raise WakeError("state_file_invalid")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=_no_constant, parse_float=_finite_float)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise WakeError("state_file_invalid") from exc
    if not isinstance(value, dict) or not _within_depth(value):
        raise WakeError("state_file_invalid")
    return value


def write_private_json(directory, name, value):
    """Atomically replace `directory/name` with canonical JSON, mode 0600, fsynced.

    `name` must be a plain file name; the directory must be a private directory.
    The target is replaced only after every byte is written and fsynced; on any
    failure the previous file is left as it was and the temporary file is removed.
    """
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
        raise WakeError("state_name_invalid")
    data = (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    if len(data) > MAX_SMALL_JSON:
        raise WakeError("state_file_too_large")
    try:
        fd, tmp = tempfile.mkstemp(prefix="." + name + ".", dir=directory)
    except OSError as exc:
        raise _os_error(exc) from exc
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise WakeError("state_io_failed", "transient")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(tmp, os.path.join(directory, name))
        tmp = None
        dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError as exc:
        raise _os_error(exc) from exc
    finally:
        if fd is not None:
            _close_quietly(fd)
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def acquire_lock(directory, name="lock", sleep=time.sleep, attempts=LOCK_ATTEMPTS):
    """Take an exclusive, non-blocking flock on `directory/name`, retrying briefly.

    Returns the open descriptor; the lock is held until it is closed. A lock held
    by another live process gives the permanent code `another_instance_running`.
    """
    try:
        fd = _open_nofollow(os.path.join(directory, name), os.O_RDWR | os.O_CREAT)
    except FileNotFoundError as exc:
        raise WakeError("state_parent_missing") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise WakeError("lock_not_regular_file")
        for attempt in range(attempts):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if attempt + 1 < attempts:
                    sleep(LOCK_RETRY_SECONDS)
        raise WakeError("another_instance_running")
    except OSError as exc:
        _close_quietly(fd)
        raise _os_error(exc) from exc
    except BaseException:
        _close_quietly(fd)
        raise
