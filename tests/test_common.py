"""Contract tests for adapters/common.py, the shared wake-adapter helpers."""
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "adapters"))
sys.path.insert(0, str(ROOT))
import common  # noqa: E402
import ai_comm  # noqa: E402

CHANNEL = "a" * 40


def identity(path="/srv/project", channel=CHANNEL):
    return {"path": path, "channel": channel, "pin_path": path + "/.tools/ai-comm",
            "channel_path": path + "/.ai-comm"}


class HandleTests(unittest.TestCase):
    def test_grammar_matches_protocol(self):
        self.assertEqual(common.HANDLE.pattern, ai_comm.HANDLE.pattern)
        for good in ("left", "right", "a", "agent-2", "x" + "y" * 31):
            self.assertEqual(common.validate_handle(good), good)
        for bad in ("", "Left", "2nd", "-a", "a_b", "a b", "x" * 33, None, 7, "a\n"):
            with self.assertRaises(common.WakeError) as ctx:
                common.validate_handle(bad)
            self.assertEqual(ctx.exception.code, "handle_invalid")

    def test_no_handle_is_special(self):
        # The same handle on both sides of a pair in different projects is legal; names carry no role.
        for name in ("left", "right", "one", "two"):
            self.assertEqual(common.validate_handle(name), name)


class SessionTests(unittest.TestCase):
    def test_protocol_boundary(self):
        for good in ("s", "ed418113-3a37-4e58-8806-3a7c306a28b0", "terminal 3 / tab 2", "x" * 160, "ünï"):
            self.assertEqual(common.validate_session(good), good)
        for bad in ("", "   ", "x" * 161, "a\nb", "a\tb", "a\x7fb", "a\x85b", "a b",
                    "a​b", "a‮b", None, 3):
            with self.assertRaises(common.WakeError) as ctx:
                common.validate_session(bad)
            self.assertEqual(ctx.exception.code, "session_invalid")

    def test_protocol_rejects_what_common_rejects(self):
        # Every reference refused here is also refused by the protocol's text rule.
        for bad in ("", "x" * 161, "a\nb", "a\x7fb", "a b", "a​b"):
            with self.assertRaises(Exception):
                ai_comm.text_field(bad, "session", 160)

    def test_host_rule_narrows_only(self):
        uuid = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
        ok = "ed418113-3a37-4e58-8806-3a7c306a28b0"
        self.assertEqual(common.validate_session(ok, uuid), ok)
        with self.assertRaises(common.WakeError) as ctx:
            common.validate_session("terminal 3", uuid)
        self.assertEqual(ctx.exception.code, "session_not_host_format")
        # A host rule cannot widen the protocol boundary.
        with self.assertRaises(common.WakeError) as ctx:
            common.validate_session("a\nb", re.compile(r"(?s).*"))
        self.assertEqual(ctx.exception.code, "session_invalid")

    def test_generation(self):
        self.assertEqual(common.validate_generation(1), 1)
        for bad in (0, -1, True, 1.0, "1", 100000000):
            with self.assertRaises(common.WakeError):
                common.validate_generation(bad)


class ProjectKeyTests(unittest.TestCase):
    def test_canonical_value(self):
        canonical = json.dumps({"channel": CHANNEL, "path": "/srv/project"}, sort_keys=True, separators=(",", ":"))
        self.assertEqual(common.project_key(identity()), hashlib.sha256(canonical.encode()).hexdigest()[:16])
        self.assertRegex(common.project_key(identity()), r"\A[0-9a-f]{16}\Z")

    def test_only_path_and_channel_count(self):
        other = identity()
        other["pin_path"], other["channel_path"] = "/elsewhere/pin", "/elsewhere/channel"
        self.assertEqual(common.project_key(other), common.project_key(identity()))

    def test_distinct_paths_and_channels(self):
        keys = {common.project_key(identity("/srv/a")), common.project_key(identity("/srv/b")),
                common.project_key(identity("/srv/a", "b" * 40))}
        self.assertEqual(len(keys), 3)

    def test_refuses_malformed_identity(self):
        bad = [None, [], {}, {"path": "/srv/p"}, {"channel": CHANNEL}, identity("relative"),
               identity("/srv/p/"), identity("/srv/../p"), identity("/srv/p", "A" * 40),
               identity("/srv/p", "a" * 39), identity("/srv/p", "a" * 64), identity("/srv/p", 5),
               {"path": 1, "channel": CHANNEL}]
        for value in bad:
            with self.assertRaises(common.WakeError) as ctx:
                common.project_key(value)
            self.assertEqual(ctx.exception.code, "project_identity_invalid")


class InstanceNameTests(unittest.TestCase):
    def test_format_and_grammar(self):
        key = common.project_key(identity())
        name = common.instance_name(key, "left")
        self.assertEqual(name, key + "-left")
        self.assertRegex(name, r"\A[A-Za-z0-9:_.\-]+\Z")
        longest = common.instance_name(key, "x" * 32)
        self.assertLessEqual(len("queue-wake@" + longest + ".service"), 255)

    def test_refusals(self):
        with self.assertRaises(common.WakeError) as ctx:
            common.instance_name("ABCDEF0123456789", "left")
        self.assertEqual(ctx.exception.code, "project_key_invalid")
        for key in ("", "0" * 15, "0" * 17, None, "../../../../etc/x"):
            with self.assertRaises(common.WakeError):
                common.instance_name(key, "left")
        for handle in ("Left", "a/b", "a@b", "a.service"):
            with self.assertRaises(common.WakeError) as ctx:
                common.instance_name("0" * 16, handle)
            self.assertEqual(ctx.exception.code, "handle_invalid")


class FailureClassTests(unittest.TestCase):
    def test_exit_codes(self):
        self.assertEqual(common.CLASSES, {"ok": 0, "transient": 75, "permanent": 78, "paused": 0})
        self.assertEqual(common.WakeError("x").exit_code, 78)
        self.assertEqual(common.WakeError("x", "transient").exit_code, 75)

    def test_line_is_fixed_and_content_free(self):
        self.assertEqual(common.WakeError("lock_busy").line(), "WAKE-ERROR lock_busy")
        for code in ("", "Upper", "has space", "a\nb", "x" * 65, "1x"):
            with self.assertRaises(ValueError):
                common.WakeError(code)
        for cls in ("ok", "paused", "other"):
            with self.assertRaises(ValueError):
                common.WakeError("x", cls)


class StateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_private_dir_creates_and_tightens(self):
        path = os.path.join(self.root, "state")
        self.assertEqual(common.private_dir(path), path)
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o700)
        os.chmod(path, 0o755)
        common.private_dir(path)
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o700)

    def test_private_dir_refusals(self):
        real = os.path.join(self.root, "real")
        os.mkdir(real)
        link = os.path.join(self.root, "link")
        os.symlink(real, link)
        cases = {
            "state_path_not_absolute": "relative/state",
            "state_path_not_normalized": self.root + "/x/../state",
            "state_path_symlink": os.path.join(link, "state"),
            "state_parent_missing": os.path.join(self.root, "missing", "state"),
        }
        for code, path in cases.items():
            with self.assertRaises(common.WakeError) as ctx:
                common.private_dir(path)
            self.assertEqual(ctx.exception.code, code)
        with self.assertRaises(common.WakeError) as ctx:
            common.private_dir(link)
        self.assertEqual(ctx.exception.code, "state_path_symlink")
        regular = os.path.join(self.root, "file")
        Path(regular).write_text("x")
        with self.assertRaises(common.WakeError) as ctx:
            common.private_dir(regular)
        self.assertEqual(ctx.exception.code, "state_dir_not_directory")
        with self.assertRaises(common.WakeError) as ctx:
            common.private_dir(os.path.join(regular, "state"))
        self.assertEqual(ctx.exception.code, "state_parent_not_directory")

    def test_json_round_trip_atomic_private(self):
        common.write_private_json(self.root, "config.json", {"b": 1, "a": [True, None, "x"]})
        target = os.path.join(self.root, "config.json")
        self.assertEqual(stat.S_IMODE(os.lstat(target).st_mode), 0o600)
        self.assertEqual(Path(target).read_bytes(), b'{"a":[true,null,"x"],"b":1}\n')
        self.assertEqual(common.read_small_json(target), {"a": [True, None, "x"], "b": 1})
        common.write_private_json(self.root, "config.json", {"c": 2})
        self.assertEqual(common.read_small_json(target), {"c": 2})
        self.assertEqual(sorted(os.listdir(self.root)), ["config.json"])  # no temporary left behind

    def test_write_refusals(self):
        for name in ("", "../x", "a/b", ".hidden", "x" * 65):
            with self.assertRaises(common.WakeError) as ctx:
                common.write_private_json(self.root, name, {})
            self.assertEqual(ctx.exception.code, "state_name_invalid")
        with self.assertRaises(common.WakeError) as ctx:
            common.write_private_json(self.root, "big.json", {"x": "y" * common.MAX_SMALL_JSON})
        self.assertEqual(ctx.exception.code, "state_file_too_large")
        with self.assertRaises(ValueError):
            common.write_private_json(self.root, "nan.json", {"x": float("nan")})
        self.assertEqual(os.listdir(self.root), [])

    def test_read_refusals(self):
        self.assertEqual(common.read_small_json(os.path.join(self.root, "absent"), missing={}), {})
        bodies = {
            "dup.json": b'{"a":1,"a":2}',
            "nan.json": b'{"a":NaN}',
            "inf.json": b'{"a":-Infinity}',
            "list.json": b"[1]",
            "bad.json": b"{",
            "bin.json": b"\xff\xfe",
            "big.json": b'{"a":"' + b"x" * common.MAX_SMALL_JSON + b'"}',
        }
        for name, body in bodies.items():
            path = os.path.join(self.root, name)
            Path(path).write_bytes(body)
            with self.assertRaises(common.WakeError, msg=name) as ctx:
                common.read_small_json(path)
            self.assertEqual(ctx.exception.code, "state_file_invalid")
        link = os.path.join(self.root, "link.json")
        os.symlink(os.path.join(self.root, "list.json"), link)
        with self.assertRaises(common.WakeError) as ctx:
            common.read_small_json(link)
        self.assertEqual(ctx.exception.code, "state_file_symlink")
        fifo = os.path.join(self.root, "fifo")
        os.mkfifo(fifo)
        with self.assertRaises(common.WakeError) as ctx:
            common.read_small_json(fifo)  # O_NONBLOCK: never hangs on a pipe
        self.assertEqual(ctx.exception.code, "state_file_invalid")

    def test_lock_is_exclusive(self):
        sleeps = []
        fd = common.acquire_lock(self.root, sleep=sleeps.append)
        try:
            with self.assertRaises(common.WakeError) as ctx:
                common.acquire_lock(self.root, sleep=sleeps.append, attempts=3)
            self.assertEqual(ctx.exception.code, "another_instance_running")
            self.assertEqual(ctx.exception.exit_code, 78)
            self.assertEqual(sleeps, [common.LOCK_RETRY_SECONDS] * 2)
        finally:
            os.close(fd)
        os.close(common.acquire_lock(self.root, sleep=sleeps.append))

    def test_lock_refuses_symlink_and_non_file(self):
        os.symlink(os.path.join(self.root, "elsewhere"), os.path.join(self.root, "lock"))
        with self.assertRaises(common.WakeError) as ctx:
            common.acquire_lock(self.root)
        self.assertEqual(ctx.exception.code, "state_file_symlink")
        self.assertFalse(os.path.exists(os.path.join(self.root, "elsewhere")))
        os.mkdir(os.path.join(self.root, "dirlock"))
        with self.assertRaises(common.WakeError) as ctx:
            common.acquire_lock(self.root, name="dirlock")
        self.assertEqual(ctx.exception.code, "state_file_not_regular")

    def test_os_errors_become_fixed_codes(self):
        locked = os.path.join(self.root, "locked")
        os.mkdir(locked, 0o500)
        self.addCleanup(os.chmod, locked, 0o700)
        if os.geteuid() == 0:
            self.skipTest("root bypasses permissions")
        with self.assertRaises(common.WakeError) as ctx:
            common.write_private_json(locked, "x.json", {})
        self.assertEqual(ctx.exception.code, "state_access_denied")
        with self.assertRaises(common.WakeError) as ctx:
            common.private_dir(os.path.join(locked, "state"))
        self.assertEqual(ctx.exception.code, "state_access_denied")



class FaultInjectionTests(unittest.TestCase):
    """Regressions for review 00001292: partial writes, raw OS errors, descriptor leaks, overflow."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.target = os.path.join(self.root, "config.json")
        common.write_private_json(self.root, "config.json", {"binding": "original"})
        self.original = Path(self.target).read_bytes()

    def test_partial_writes_complete_the_file(self):
        real_write = os.write
        calls = []

        def one_byte(fd, data):
            calls.append(len(data))
            return real_write(fd, bytes(data[:1]))

        with mock.patch.object(common.os, "write", one_byte):
            common.write_private_json(self.root, "config.json", {"binding": "replacement"})
        self.assertEqual(common.read_small_json(self.target), {"binding": "replacement"})
        self.assertGreater(len(calls), 1)
        self.assertEqual(os.listdir(self.root), ["config.json"])

    def test_partial_write_then_failure_keeps_the_original(self):
        real_write = os.write
        state = {"n": 0}

        def flaky(fd, data):
            state["n"] += 1
            if state["n"] == 1:
                return real_write(fd, bytes(data[:1]))
            raise OSError(28, "synthetic private diagnostic")

        with mock.patch.object(common.os, "write", flaky):
            with self.assertRaises(common.WakeError) as ctx:
                common.write_private_json(self.root, "config.json", {"binding": "replacement"})
        self.assertEqual(ctx.exception.code, "state_io_failed")
        self.assertEqual(ctx.exception.exit_code, 75)
        self.assertEqual(Path(self.target).read_bytes(), self.original)
        self.assertEqual(os.listdir(self.root), ["config.json"])

    def test_zero_progress_write_is_refused(self):
        with mock.patch.object(common.os, "write", lambda fd, data: 0):
            with self.assertRaises(common.WakeError) as ctx:
                common.write_private_json(self.root, "config.json", {"binding": "replacement"})
        self.assertEqual(ctx.exception.code, "state_io_failed")
        self.assertEqual(Path(self.target).read_bytes(), self.original)
        self.assertEqual(os.listdir(self.root), ["config.json"])

    def test_lock_io_error_is_mapped_and_closes_the_descriptor(self):
        opened = []
        real_open = common._open_nofollow

        def recording_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd

        def eio(fd, op):
            raise OSError(5, "synthetic private diagnostic")

        with mock.patch.object(common, "_open_nofollow", recording_open), \
                mock.patch.object(common.fcntl, "flock", eio):
            with self.assertRaises(common.WakeError) as ctx:
                common.acquire_lock(self.root, sleep=lambda s: None)
        self.assertEqual(ctx.exception.code, "state_io_failed")
        self.assertEqual(ctx.exception.exit_code, 75)
        self.assertNotIn("synthetic", ctx.exception.line())
        self.assertEqual(len(opened), 1)
        with self.assertRaises(OSError):
            os.fstat(opened[0])  # closed

    def test_lock_contention_closes_the_descriptor(self):
        holder = common.acquire_lock(self.root)
        self.addCleanup(os.close, holder)
        opened = []
        real_open = common._open_nofollow
        with mock.patch.object(common, "_open_nofollow",
                               lambda *a, **k: opened.append(real_open(*a, **k)) or opened[-1]):
            with self.assertRaises(common.WakeError) as ctx:
                common.acquire_lock(self.root, sleep=lambda s: None, attempts=2)
        self.assertEqual(ctx.exception.code, "another_instance_running")
        with self.assertRaises(OSError):
            os.fstat(opened[0])

    def test_lock_in_missing_directory(self):
        with self.assertRaises(common.WakeError) as ctx:
            common.acquire_lock(os.path.join(self.root, "missing"))
        self.assertEqual(ctx.exception.code, "state_parent_missing")

    def test_chmod_eperm_is_mapped(self):
        path = os.path.join(self.root, "state")
        os.mkdir(path, 0o755)

        def eperm(*args, **kwargs):
            raise PermissionError(1, "synthetic private diagnostic")

        with mock.patch.object(common.os, "chmod", eperm):
            with self.assertRaises(common.WakeError) as ctx:
                common.private_dir(path)
        self.assertEqual(ctx.exception.code, "state_access_denied")
        self.assertEqual(ctx.exception.exit_code, 78)

    def test_cleanup_failure_does_not_replace_the_mapped_error(self):
        def fail_fsync(fd):
            raise OSError(5, "synthetic private diagnostic")

        def fail_unlink(path):
            raise PermissionError(1, "synthetic private diagnostic")

        with mock.patch.object(common.os, "fsync", fail_fsync), \
                mock.patch.object(common.os, "unlink", fail_unlink):
            with self.assertRaises(common.WakeError) as ctx:
                common.write_private_json(self.root, "config.json", {"binding": "replacement"})
        self.assertEqual(ctx.exception.code, "state_io_failed")
        self.assertEqual(Path(self.target).read_bytes(), self.original)

    def test_directory_sync_failure_after_replace_is_commit_uncertain(self):
        real_fsync = os.fsync
        calls = []

        def second_fails(fd):
            calls.append(fd)
            if len(calls) == 2:  # the first syncs the temporary file, the second the directory
                raise OSError(5, "synthetic private diagnostic")
            return real_fsync(fd)

        with mock.patch.object(common.os, "fsync", second_fails):
            with self.assertRaises(common.WakeError) as ctx:
                common.write_private_json(self.root, "config.json", {"binding": "replacement"})
        self.assertEqual(ctx.exception.code, "state_commit_uncertain")
        self.assertEqual(ctx.exception.exit_code, 75)
        self.assertEqual(len(calls), 2)
        # The new bytes are in place; the caller reads back rather than restoring the old value.
        self.assertEqual(common.read_small_json(self.target), {"binding": "replacement"})
        self.assertEqual(os.listdir(self.root), ["config.json"])

    def test_close_failure_closes_once_and_keeps_the_original(self):
        real_close = os.close
        closed = []

        def failing_close(fd):
            closed.append(fd)
            real_close(fd)
            if len(closed) == 1:
                raise OSError(5, "synthetic private diagnostic")

        with mock.patch.object(common.os, "close", failing_close):
            with self.assertRaises(common.WakeError) as ctx:
                common.write_private_json(self.root, "config.json", {"binding": "replacement"})
        self.assertEqual(ctx.exception.code, "state_io_failed")
        self.assertEqual(len(closed), 1)
        self.assertEqual(Path(self.target).read_bytes(), self.original)
        self.assertEqual(os.listdir(self.root), ["config.json"])

    def test_inaccessible_ancestor_is_permanent(self):
        if os.geteuid() == 0:
            self.skipTest("root bypasses permissions")
        sealed = os.path.join(self.root, "sealed")
        os.mkdir(sealed, 0o000)
        self.addCleanup(os.chmod, sealed, 0o700)
        with self.assertRaises(common.WakeError) as ctx:
            common.private_dir(os.path.join(sealed, "inner", "state"))
        self.assertEqual(ctx.exception.code, "state_access_denied")
        self.assertEqual(ctx.exception.exit_code, 78)

    def test_numeric_overflow_is_refused(self):
        bodies = [b'{"n":1e400}', b'{"n":-1e400}', b'{"a":[{"b":1E+999}]}', b'{"n":1.5e309}']
        for body in bodies:
            Path(self.target).write_bytes(body)
            with self.assertRaises(common.WakeError, msg=body) as ctx:
                common.read_small_json(self.target)
            self.assertEqual(ctx.exception.code, "state_file_invalid")

    def test_ordinary_numbers_still_accepted(self):
        Path(self.target).write_bytes(b'{"a":1.5,"b":-2e10,"c":1e-400,"d":12345678901234567890,"e":0}')
        value = common.read_small_json(self.target)
        self.assertEqual(value["a"], 1.5)
        self.assertEqual(value["c"], 0.0)  # underflow to zero is finite
        self.assertEqual(value["d"], 12345678901234567890)

    def test_excessive_nesting_is_refused(self):
        depth = (common.MAX_SMALL_JSON - 8) // 2
        Path(self.target).write_bytes(b'{"a":' + b"[" * depth + b"]" * depth + b"}")
        with self.assertRaises(common.WakeError) as ctx:
            common.read_small_json(self.target)
        self.assertEqual(ctx.exception.code, "state_file_invalid")

    def test_depth_limit_boundary(self):
        limit = common.MAX_JSON_DEPTH
        Path(self.target).write_bytes(b'{"a":' + b"[" * (limit - 1) + b"]" * (limit - 1) + b"}")
        self.assertIn("a", common.read_small_json(self.target))
        Path(self.target).write_bytes(b'{"a":' + b"[" * limit + b"]" * limit + b"}")
        with self.assertRaises(common.WakeError):
            common.read_small_json(self.target)


class NoContentTests(unittest.TestCase):
    def test_module_never_touches_channel_operations(self):
        source = (ROOT / "adapters" / "common.py").read_text(encoding="utf-8")
        for word in ("subprocess", "inbox", "import ai_comm", "socket", "urllib"):
            self.assertNotIn(word, source.replace("reads inbox content", ""))


if __name__ == "__main__":
    unittest.main()
