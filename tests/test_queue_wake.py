"""Queue adapter failure-path tests; synthetic fixtures never queue a live session."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("queue_wake_tested", ROOT / "adapters/queue_wake.py")
wake = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wake)
SESSION = "11111111-2222-3333-4444-555555555555"


def config_at(root, handle="left"):
    project = {"path": str(root), "channel": "a" * 40,
               "pin_path": str(root / ".tools/ai-comm"), "channel_path": str(root / ".ai-comm")}
    return {"v": 1, "schema": wake.SCHEMA, "project": project,
            "instance": wake.common.instance_name(wake.common.project_key(project), handle),
            "handle": handle, "session": SESSION, "generation": 1,
            "runtime": {"socket": str(root / "runtime.sock"), "binary": str(root / "runtime"),
                        "sha256": "b" * 64, "approved_thread": SESSION},
            "package": {"commit": "c" * 40, "manifest_sha256": "d" * 64,
                        "cli_sha256": "e" * 64, "protocol_sha256": wake.PROTOCOL}, "service": None}


def message(seq, sender="right", recipients=None, act="FACT"):
    return {"seq": seq, "id": f"{seq:08d}-{sender}", "from": sender,
            "to": ["left"] if recipients is None else recipients, "act": act,
            "body": "Private body must never reach the runtime or state."}


class Feed:
    def __init__(self, messages=(), read=0):
        self.messages, self.read, self.head = list(messages), read, "f" * 40
        self.calls = []

    def inspect(self, after):
        self.calls.append(after)
        end = max([self.read, after] + [m["seq"] for m in self.messages])
        return {"read": self.read, "scanned": end, "head": self.head,
                "messages": [m for m in self.messages if m["seq"] > max(after, self.read)]}


class Transport:
    def __init__(self, status="idle", phase="queued"):
        self.readiness, self.phase, self.sent = status, phase, []

    def status(self):
        return self.readiness

    def queue(self, through):
        self.sent.append(through)
        if isinstance(self.phase, Exception):
            raise self.phase
        return self.phase


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="queue-wake-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = config_at(self.root)
        self.directory = wake.state_directory(self.config)
        self.directory.mkdir(parents=True, mode=0o700)
        self.store = wake.Store(self.directory, self.config)
        self.store.save(wake.initial_state(self.config))

    def fails(self, code, operation):
        with self.assertRaises(wake.Invalid) as ctx:
            operation()
        self.assertEqual(ctx.exception.code, code)


class DispatchTests(Fixture):
    def test_filters_and_coalesces_without_persisting_content(self):
        feed = Feed([message(1, act="ACK"), message(2, sender="left"),
                     message(3, recipients=["third"]), message(4), message(5, sender="third")])
        runtime = Transport()
        result = wake.tick(self.store, feed, runtime, now=100)
        self.assertEqual(runtime.sent, [5])
        self.assertEqual(result["pending"], {"through": 5, "phase": "queued"})
        self.assertNotIn("Private", self.store.path.read_text())

    def test_restart_preserves_pending_until_model_receipt(self):
        feed, runtime = Feed([message(1)]), Transport()
        wake.tick(self.store, feed, runtime, now=100)
        restarted = wake.Store(self.directory, self.config)
        feed.messages.append(message(2))
        wake.tick(restarted, feed, runtime, now=200)
        self.assertEqual(runtime.sent, [1])
        feed.read = 1
        wake.tick(restarted, feed, runtime, now=201)
        self.assertEqual(runtime.sent, [1, 2])
        feed.read = 2
        self.assertEqual(wake.tick(restarted, feed, runtime, now=202)["status"], "caught_up")

    def test_all_nonidle_states_defer_without_reservation(self):
        for status in ("active", "notLoaded", "systemError"):
            with self.subTest(status=status):
                runtime = Transport(status)
                result = wake.tick(self.store, Feed([message(1)]), runtime, now=100)
                self.assertEqual(result["status"], "deferred_" + status.lower())
                self.assertIsNone(result["pending"])
                self.assertEqual(runtime.sent, [])

    def test_uncertain_attempt_is_never_automatically_repeated(self):
        for phase in ("uncertain", TimeoutError(), wake.Unavailable("runtime_unavailable")):
            with self.subTest(phase=type(phase).__name__):
                self.store.save(wake.initial_state(self.config))
                runtime = Transport(phase=phase)
                wake.tick(self.store, Feed([message(1)]), runtime, now=100)
                wake.tick(self.store, Feed([message(1)]), runtime, now=200)
                self.assertEqual(runtime.sent, [1])
                self.assertEqual(self.store.load()["pending"]["phase"], "uncertain")

    def test_permanent_dispatch_failure_keeps_reservation(self):
        runtime = Transport(phase=wake.Invalid("runtime_binary_changed"))
        self.fails("runtime_binary_changed", lambda: wake.tick(self.store, Feed([message(1)]), runtime, now=100))
        self.assertEqual(self.store.load()["pending"]["phase"], "reserved")
        wake.tick(self.store, Feed([message(1)]), runtime, now=200)
        self.assertEqual(runtime.sent, [1])

    def test_rate_limit_after_acknowledged_attempt(self):
        runtime, feed = Transport(), Feed([message(1)])
        wake.tick(self.store, feed, runtime, now=100)
        feed.read = 1
        feed.messages.append(message(2))
        self.assertEqual(wake.tick(self.store, feed, runtime, now=110)["status"], "rate_limited")
        self.assertEqual(runtime.sent, [1])
        wake.tick(self.store, feed, runtime, now=130)
        self.assertEqual(runtime.sent, [1, 2])

    def test_pause_avoids_source_and_transport(self):
        wake.common.write_private_json(str(self.directory), "paused.json", {"paused": True})
        feed, runtime = mock.Mock(), mock.Mock()
        self.assertEqual(wake.tick(self.store, feed, runtime)["status"], "paused")
        feed.inspect.assert_not_called()
        runtime.queue.assert_not_called()

    def test_receipt_regression_refuses_dispatch(self):
        state = self.store.load()
        state.update(read=2, scanned=2)
        self.store.save(state)
        runtime = Transport()
        self.fails("receipt_regressed", lambda: wake.tick(self.store, Feed([message(3)], read=1), runtime, now=100))
        self.assertEqual(runtime.sent, [])

    def test_observation_change_before_reservation_does_not_dispatch(self):
        feed, runtime = Feed([message(1)]), Transport()
        initial = feed.inspect(0)
        for fresh in (dict(initial, head="e" * 40), dict(initial, read=1)):
            with self.subTest(fresh=fresh):
                source = mock.Mock()
                source.inspect.side_effect = [initial, fresh]
                self.fails("observation_changed", lambda: wake.tick(self.store, source, runtime, now=100))
                self.assertIsNone(self.store.load()["pending"])
        self.assertEqual(runtime.sent, [])

    def test_uncertain_reservation_commit_stops_before_transport(self):
        real = wake.common.write_private_json

        def uncertain(directory, name, value):
            real(directory, name, value)
            raise wake.Invalid("state_commit_uncertain", "transient")

        runtime = Transport()
        with mock.patch.object(wake.common, "write_private_json", side_effect=uncertain):
            self.fails("state_commit_uncertain", lambda: wake.tick(self.store, Feed([message(1)]), runtime, now=100))
        self.assertEqual(runtime.sent, [])
        self.assertEqual(self.store.load()["pending"]["phase"], "reserved")
        wake.tick(self.store, Feed([message(1)]), runtime, now=200)
        self.assertEqual(runtime.sent, [])


class IdentityTests(Fixture):
    def test_removal_intent_blocks_init_and_run_without_runtime_or_state_change(self):
        config_path = self.directory / "config.json"
        wake.common.write_private_json(str(self.directory), "config.json", self.config)
        marker = self.directory / "remove-intent.json"
        before_config, before_state = config_path.read_bytes(), self.store.path.read_bytes()
        for dangling in (False, True):
            if dangling:
                marker.symlink_to(self.root / "missing-intent-target")
            else:
                wake.common.write_private_json(str(self.directory), "remove-intent.json", {"fixture": "installer-owned"})
            try:
                with mock.patch.object(wake.Source, "inspect") as inspect, \
                        mock.patch.object(wake.Runtime, "status") as status, \
                        mock.patch.object(wake, "observe_loop") as loop:
                    self.fails("removal_in_progress", lambda: wake.initialize(self.config))
                    for command in ("run", "once"):
                        self.assertEqual(wake.main([command, "--config", str(config_path), "--handle", "left",
                                                   "--session", SESSION, "--generation", "1"]), 78)
                    inspect.assert_not_called()
                    status.assert_not_called()
                    loop.assert_not_called()
                    self.assertEqual(config_path.read_bytes(), before_config)
                    self.assertEqual(self.store.path.read_bytes(), before_state)
                    self.assertTrue(os.path.lexists(marker))
            finally:
                marker.unlink()

    def test_startup_rechecks_intent_after_serialized_publication(self):
        holder = wake.common.acquire_lock(str(self.directory), name="instance.lock")
        real = wake.common.acquire_lock
        published = []

        def acquiring(directory, name="lock", **kwargs):
            if name == "instance.lock":
                wake.common.write_private_json(str(self.directory), "remove-intent.json", {"fixture": "new-intent"})
                os.close(holder)
                published.append(True)
            return real(directory, name=name, **kwargs)

        with mock.patch.object(wake.common, "acquire_lock", side_effect=acquiring), \
                mock.patch.object(wake.Source, "inspect") as inspect, \
                mock.patch.object(wake.Runtime, "status") as status:
            self.fails("removal_in_progress", lambda: wake.initialize(self.config))
            inspect.assert_not_called()
            status.assert_not_called()
        self.assertEqual(published, [True])

    def test_init_and_run_refuse_exit_kind_files_before_any_runtime_call(self):
        config_path = self.directory / "config.json"
        wake.common.write_private_json(str(self.directory), "config.json", self.config)
        for name in ("cursor.json", "run.json"):
            marker = self.directory / name
            marker.symlink_to(self.root / "missing-target")
            try:
                with mock.patch.object(wake.Source, "inspect") as inspect, \
                        mock.patch.object(wake.Runtime, "status") as status, \
                        mock.patch.object(wake, "observe_loop") as loop:
                    self.fails("instance_kind_conflict", lambda: wake.initialize(self.config))
                    for command in ("run", "once"):
                        args = [command, "--config", str(config_path), "--handle", "left",
                                "--session", SESSION, "--generation", "1"]
                        self.assertEqual(wake.main(args), 78)
                    inspect.assert_not_called()
                    status.assert_not_called()
                    loop.assert_not_called()
            finally:
                marker.unlink()

    def test_instance_gate_serializes_check_after_other_kind_publication(self):
        reached, release = threading.Event(), threading.Event()
        result = []
        errors = []
        real = wake.common.acquire_lock
        holder = real(str(self.directory), name="instance.lock")

        def acquiring(directory, name="lock", **kwargs):
            if name == "instance.lock":
                reached.set()
                if not release.wait(5):
                    raise AssertionError("publication barrier expired")
            return real(directory, name=name, **kwargs)

        def initialize():
            try:
                wake.initialize(self.config)
            except wake.Invalid as exc:
                result.append(exc.code)
            except BaseException as exc:
                errors.append(exc)

        with mock.patch.object(wake.common, "acquire_lock", side_effect=acquiring), \
                mock.patch.object(wake.Source, "inspect") as inspect, \
                mock.patch.object(wake.Runtime, "status") as status:
            thread = threading.Thread(target=initialize)
            thread.start()
            try:
                self.assertTrue(reached.wait(5))
                wake.common.write_private_json(str(self.directory), "cursor.json", {"v": 1, "notification_through": 0})
            finally:
                os.close(holder)
                release.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(result, ["instance_kind_conflict"])
            inspect.assert_not_called()
            status.assert_not_called()

    def test_two_independent_instances_can_hold_startup_gate_together(self):
        other = config_at(self.root, "right")
        wake.state_directory(other).mkdir(mode=0o700)
        with wake.instance_gate(self.config), wake.instance_gate(other):
            self.assertNotEqual(wake.state_directory(other), self.directory)

    def snapshot(self):
        return {"participants": [{"handle": "left", "session": SESSION, "generation": 1},
                                 {"handle": "right", "session": "terminal 2 / tab", "generation": 3},
                                 {"handle": "third", "session": "another host", "generation": 1}],
                "local_package": {"commit": self.config["package"]["commit"], "dirty": False,
                                  "protocol_sha256": wake.PROTOCOL}}

    def test_peer_session_is_opaque_and_third_participant_allowed(self):
        self.assertEqual(len(wake.bindings(self.snapshot(), self.config)), 3)

    def test_own_takeover_never_inherits_approval(self):
        for field, value in (("session", "22222222-2222-3333-4444-555555555555"), ("generation", 2)):
            snapshot = self.snapshot()
            snapshot["participants"][0][field] = value
            self.fails("session_changed", lambda: wake.bindings(snapshot, self.config))

    def test_instances_separate_by_handle_and_project(self):
        second = config_at(self.root, "right")
        other_project = config_at(self.root / "other")
        self.assertEqual(len({self.config["instance"], second["instance"], other_project["instance"]}), 3)

    def test_state_never_reused_for_another_binding(self):
        other = copy.deepcopy(self.config)
        other["generation"] = 2
        self.fails("invalid_state_binding", lambda: wake.Store(self.directory, other).load())

    def test_missing_state_is_not_silently_reset(self):
        self.store.path.unlink()
        self.fails("state_missing", self.store.load)

    def test_malformed_pending_is_controlled_failure(self):
        for phase in ([], {}, None, 3):
            state = wake.initial_state(self.config)
            state.update(scanned=1, wanted=1, pending={"through": 1, "phase": phase})
            with self.subTest(phase=phase), self.assertRaises(wake.Invalid):
                self.store.validate(state)

    def test_config_rejects_paths_and_runtime_target_mismatch(self):
        for field, value in (("socket", "relative"), ("approved_thread", "another-thread")):
            config = copy.deepcopy(self.config)
            config["runtime"][field] = value
            with self.subTest(field=field), self.assertRaises(wake.Invalid):
                wake.validate_config(config)


class RuntimeTests(Fixture):
    def rpc(self, thread):
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        sock.bind(self.config["runtime"]["socket"])
        os.chmod(self.config["runtime"]["socket"], 0o600)
        ws = mock.MagicMock()
        ws.__enter__.return_value = ws
        ws.recv.side_effect = [json.dumps({"id": 1, "result": {}}),
                               json.dumps({"id": 2, "result": {"thread": thread}})]
        client, exceptions = types.ModuleType("websockets.sync.client"), types.ModuleType("websockets.exceptions")
        client.unix_connect = mock.Mock(return_value=ws)
        exceptions.WebSocketException = type("WebSocketException", (Exception,), {})
        exceptions.ConnectionClosed = type("ConnectionClosed", (exceptions.WebSocketException,), {})
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.sys.modules, {"websockets": types.ModuleType("websockets"),
            "websockets.sync": types.ModuleType("websockets.sync"),
            "websockets.sync.client": client, "websockets.exceptions": exceptions}).start()
        mock.patch.object(wake, "file_digest", return_value="b" * 64).start()
        return ws, client

    def test_runtime_reads_summary_without_loading_or_subscribing(self):
        ws, _ = self.rpc({"id": SESSION, "cwd": str(self.root), "status": {"type": "idle"}})
        self.assertEqual(wake.Runtime(self.config).status(), "idle")
        calls = [json.loads(call.args[0]) for call in ws.send.call_args_list]
        self.assertEqual([call["method"] for call in calls], ["initialize", "initialized", "thread/read"])
        self.assertEqual(calls[-1]["params"], {"threadId": SESSION, "includeTurns": False})

    def test_runtime_rejects_wrong_thread_or_project(self):
        ws, _ = self.rpc({"id": "other", "cwd": str(self.root), "status": {"type": "idle"}})
        self.fails("wrong_thread", lambda: wake.Runtime(self.config).status())
        ws.recv.side_effect = [json.dumps({"id": 1, "result": {}}), json.dumps({"id": 2, "result": {
            "thread": {"id": SESSION, "cwd": str(self.root / "other"), "status": {"type": "idle"}}}})]
        self.fails("wrong_thread", lambda: wake.Runtime(self.config).status())

    def test_runtime_rejects_malformed_summary(self):
        ws, _ = self.rpc([])
        self.fails("invalid_thread", lambda: wake.Runtime(self.config).status())
        for status in ({"type": []}, {"type": "unexpected"}, {"type": "idle", "activeFlags": ["busy"]}):
            ws.recv.side_effect = [json.dumps({"id": 1, "result": {}}), json.dumps({"id": 2, "result": {
                "thread": {"id": SESSION, "cwd": str(self.root), "status": status}}})]
            with self.subTest(status=status), self.assertRaises(wake.Invalid):
                wake.Runtime(self.config).status()

    def test_runtime_connection_refused_is_transient(self):
        _, client = self.rpc({})
        client.unix_connect.side_effect = ConnectionRefusedError(wake.errno.ECONNREFUSED, "refused")
        with self.assertRaises(wake.Unavailable) as ctx:
            wake.Runtime(self.config).status()
        self.assertEqual(ctx.exception.exit_code, 75)

    def test_handshake_exception_base_is_transient(self):
        _, client = self.rpc({})
        base = os.sys.modules["websockets.exceptions"].WebSocketException
        for name in ("InvalidHandshake", "InvalidMessage", "InvalidStatus", "InvalidURI"):
            client.unix_connect.side_effect = type(name, (base,), {})("private fixture")
            with self.subTest(name=name), self.assertRaises(wake.Unavailable) as ctx:
                wake.Runtime(self.config).status()
            self.assertEqual((ctx.exception.code, ctx.exception.exit_code), ("runtime_unavailable", 75))

    def test_queue_argv_contains_only_fixed_notification(self):
        with mock.patch.object(wake, "file_digest", return_value="b" * 64), \
                mock.patch.object(wake, "run", return_value=(0, b"queued", b"")) as run:
            self.assertEqual(wake.Runtime(self.config).queue(42), "queued")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], [self.config["runtime"]["binary"], "queue", "--thread", SESSION])
        self.assertIn("sequence 42", argv[argv.index("--message") + 1])
        self.assertNotIn("Private body", " ".join(argv))

    def test_exit_zero_with_warning_is_uncertain(self):
        for result in ((0, b"Warning: resume failed", b""), (0, b"", b"warning"), (1, b"", b"")):
            with mock.patch.object(wake, "file_digest", return_value="b" * 64), \
                    mock.patch.object(wake, "run", return_value=result):
                self.assertEqual(wake.Runtime(self.config).queue(42), "uncertain")

    def test_binary_change_refused_before_any_queue(self):
        with mock.patch.object(wake, "file_digest", return_value="f" * 64), mock.patch.object(wake, "run") as run:
            self.fails("runtime_binary_changed", lambda: wake.Runtime(self.config).queue(1))
        run.assert_not_called()


class SourceTests(Fixture):
    def setUp(self):
        super().setUp()
        channel = Path(self.config["project"]["channel_path"])
        self.receipts = channel / "ai-comm-runtime"
        self.receipts.mkdir(parents=True, mode=0o700)
        self.write_receipt()
        self.messages = [message(1), message(2, sender="third")]
        self.snapshot_value = dict(IdentityTests.snapshot(self), head="f" * 40,
                                   recent=[{"id": m["id"]} for m in self.messages])
        self.registry = copy.deepcopy(self.snapshot_value["participants"])
        self.page = {"head": "f" * 40, "messages": self.messages, "more": False, "next": 2}
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(wake, "check_package").start()
        mock.patch.object(wake, "git_read", return_value="a" * 40).start()
        self.cli = mock.patch.object(wake, "cli", side_effect=self.answer).start()

    def write_receipt(self, **updates):
        value = dict(v=2, generation=1, read=0, seen=2)
        value.update(updates)
        wake.common.write_private_json(str(self.receipts), "left.json", value)

    def answer(self, config, command, *args):
        if command == "state":
            return self.snapshot_value
        if command == "registry":
            offset = int(args[args.index("--offset") + 1])
            items = self.registry[offset:offset + 100]
            return {"head": "f" * 40, "items": items, "next": offset + len(items),
                    "more": offset + len(items) < len(self.registry)}
        if args[-1] == "1":
            after = int(args[1])
            items = self.page["messages"][after:after + 1]
            return dict(self.page, messages=items, next=after + len(items),
                        more=after + len(items) < len(self.page["messages"]))
        return self.page

    def test_source_uses_own_receipt_and_strips_message_bodies(self):
        result = wake.Source(self.config).inspect(0)
        self.assertEqual(result["read"], 0)
        self.assertEqual(result["scanned"], 2)
        self.assertEqual(set(result["messages"][0]), {"seq", "from", "to", "act"})
        self.assertNotIn("Private", json.dumps(result))
        self.assertEqual([call.args[1] for call in self.cli.call_args_list], ["state", "registry", "log", "log"])

    def test_malformed_page_fields_are_rejected_without_tracebacks(self):
        for field, bad in (("id", []), ("from", {}), ("act", []), ("to", [{}])):
            original = copy.deepcopy(self.page)
            self.page["messages"][0][field] = bad
            with self.subTest(field=field), self.assertRaises(wake.Invalid):
                wake.Source(self.config).inspect(0)
            self.page = original

    def test_bad_snapshot_package_is_controlled_failure(self):
        self.snapshot_value["local_package"] = []
        self.fails("invalid_channel_state", lambda: wake.Source(self.config).inspect(0))

    def test_trimmed_state_participants_and_recent_are_not_authoritative(self):
        self.snapshot_value["participants"] = []
        self.snapshot_value["recent"][0]["id"] = 3
        self.assertEqual(wake.Source(self.config).inspect(0)["scanned"], 2)
        self.snapshot_value["recent"] = []
        self.assertEqual(wake.Source(self.config).inspect(0)["scanned"], 2)

    def test_receipt_generation_boolean_is_not_integer_identity(self):
        self.write_receipt(generation=True)
        self.fails("invalid_receipt", lambda: wake.Source(self.config).inspect(0))

    def test_missing_head_is_controlled_failure(self):
        self.snapshot_value.pop("head")
        self.fails("invalid_channel_head", lambda: wake.Source(self.config).inspect(0))

    def test_changed_head_is_deferred(self):
        self.page["head"] = "e" * 40
        with self.assertRaises(wake.Deferred) as ctx:
            wake.Source(self.config).inspect(0)
        self.assertEqual((ctx.exception.code, ctx.exception.exit_code), ("observation_changed", 75))

    def test_receipt_ahead_of_snapshot_is_deferred(self):
        self.write_receipt(read=3, seen=3)
        self.fails("receipt_snapshot_changed", lambda: wake.Source(self.config).inspect(0))

    def test_corrupt_receipt_bounds_are_permanent(self):
        self.write_receipt(read=2, seen=1)
        self.fails("invalid_receipt_bounds", lambda: wake.Source(self.config).inspect(0))

    def test_noncontiguous_history_and_empty_more_page_refused(self):
        self.write_receipt(seen=0)
        self.page["messages"][0]["seq"] = 2
        self.fails("noncontiguous_page", lambda: wake.Source(self.config).inspect(0))
        self.page.update(messages=[], more=True, next=0)
        self.fails("empty_page", lambda: wake.Source(self.config).inspect(0))

    def test_bounded_backlog_returns_scanned_cursor_for_next_tick(self):
        all_messages = [message(i) for i in range(1, 602)]
        self.snapshot_value["recent"] = [{"id": all_messages[-1]["id"]}]

        def pages(config, command, *args):
            if command != "log":
                return self.answer(config, command, *args)
            after = int(args[1])
            messages = all_messages[after:after + int(args[-1])]
            cursor = after + len(messages)
            return {"head": "f" * 40, "messages": messages, "more": cursor < 601, "next": cursor}

        self.cli.side_effect = pages
        first = wake.Source(self.config).inspect(0)
        self.assertEqual(first["scanned"], 500)
        second = wake.Source(self.config).inspect(first["scanned"])
        self.assertEqual(second["scanned"], 601)
        self.assertEqual(second["messages"][0]["seq"], 501)

    def test_registry_paging_selects_latest_generation_and_keeps_omitted_peer(self):
        self.registry += [{"handle": "peer" + str(i), "session": "opaque-peer", "generation": 1}
                          for i in range(201)]
        self.registry += [{"handle": "right", "session": "replacement", "generation": 2}]
        self.snapshot_value["participants"] = []
        self.assertEqual(wake.Source(self.config).inspect(0)["scanned"], 2)
        self.assertEqual(sum(call.args[1] == "registry" for call in self.cli.call_args_list), 3)
        self.registry.append({"handle": "left", "session": "replacement", "generation": 2})
        self.fails("session_changed", lambda: wake.Source(self.config).inspect(0))

    def test_registry_duplicates_and_bad_offset_refused(self):
        self.registry.append(copy.deepcopy(self.registry[0]))
        self.fails("invalid_participants", lambda: wake.Source(self.config).inspect(0))
        self.registry.pop()
        original = self.answer
        def bad(config, command, *args):
            page = original(config, command, *args)
            return dict(page, next=999) if command == "registry" else page
        self.cli.side_effect = bad
        self.fails("invalid_registry_cursor", lambda: wake.Source(self.config).inspect(0))

    def test_registry_head_change_is_transient_and_bound_is_explicit(self):
        original = self.answer
        def changed(config, command, *args):
            page = original(config, command, *args)
            return dict(page, head="e" * 40) if command == "registry" else page
        self.cli.side_effect = changed
        with self.assertRaises(wake.Deferred) as ctx:
            wake.Source(self.config).inspect(0)
        self.assertEqual(ctx.exception.exit_code, 75)
        self.registry = [{"handle": "peer" + str(i), "session": "opaque", "generation": 1}
                         for i in range(10001)]
        self.cli.side_effect = original
        self.fails("registry_limit", lambda: wake.Source(self.config).inspect(0))

    def test_history_rewind_and_future_scanned_cursor_cannot_pass_empty_log(self):
        self.write_receipt(seen=0)
        self.fails("history_regressed", lambda: wake.Source(self.config).inspect(3))

    def test_missing_receipt_is_specific_permanent_error(self):
        (self.receipts / "left.json").unlink()
        self.fails("receipt_missing", lambda: wake.Source(self.config).inspect(0))

    def test_read_only_observation_retries_are_bounded(self):
        source = mock.Mock()
        source.inspect.side_effect = [wake.Deferred("observation_changed"), {"read": 0}]
        with mock.patch.object(wake.time, "sleep"):
            self.assertEqual(wake.observe(source, 0), {"read": 0})
        self.assertEqual(source.inspect.call_count, 2)
        source.inspect.reset_mock()
        source.inspect.side_effect = wake.Deferred("observation_changed")
        with mock.patch.object(wake.time, "sleep"), self.assertRaises(wake.Deferred) as ctx:
            wake.observe(source, 0)
        self.assertEqual((source.inspect.call_count, ctx.exception.exit_code), (3, 75))

    def test_impossible_receipt_boundary_stops_after_bounded_reads_without_dispatch(self):
        self.page.update(messages=[], next=0, more=False)
        self.write_receipt(seen=5)
        for after in (0, 3):
            state = wake.initial_state(self.config)
            state["scanned"] = after
            self.store.save(state)
            before = self.store.path.read_bytes()
            runtime = Transport()
            with self.subTest(after=after), mock.patch.object(wake.time, "sleep"), \
                    self.assertRaises(wake.Invalid) as ctx:
                wake.observe_loop(self.store, wake.Source(self.config), runtime, once=True)
            self.assertEqual((ctx.exception.code, ctx.exception.exit_code), ("history_regressed", 78))
            self.assertEqual(self.store.path.read_bytes(), before)
            self.assertEqual(runtime.sent, [])

    def test_one_raced_receipt_can_recover_without_permanent_error(self):
        source = mock.Mock()
        source.inspect.side_effect = [wake.Deferred("receipt_snapshot_changed"), {"read": 0}]
        with mock.patch.object(wake.time, "sleep"):
            self.assertEqual(wake.observe(source, 0), {"read": 0})
        self.assertEqual(source.inspect.call_count, 2)

    def test_impossible_receipt_main_reports_fixed_permanent_error(self):
        self.page.update(messages=[], next=0, more=False)
        self.write_receipt(seen=5)
        args = ["once", "--config", str(self.directory / "config.json"), "--handle", "left",
                "--session", SESSION, "--generation", "1"]
        with mock.patch.object(wake, "load_config", return_value=self.config), \
                mock.patch.object(wake.time, "sleep"), mock.patch("sys.stderr") as stderr, \
                mock.patch.object(wake.Runtime, "queue") as queue:
            self.assertEqual(wake.main(args), 78)
        self.assertEqual(stderr.write.call_args_list[0].args[0], "WAKE-ERROR history_regressed")
        queue.assert_not_called()


class PackageFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.pin = Path(self.config["project"]["pin_path"])
        self.pin.mkdir(parents=True)
        (self.pin / "ai_comm.py").write_text("raise SystemExit('fixture: must not execute')\n")
        (self.pin / "PROTOCOL.md").write_bytes((ROOT / "PROTOCOL.md").read_bytes())
        files = []
        for name in ("ai_comm.py", "PROTOCOL.md"):
            raw = (self.pin / name).read_bytes()
            files.append({"path": name, "bytes": len(raw), "sha256": wake.digest(raw)})
        manifest = {"format": "ai-comm-review-manifest/1", "protocol_sha256": wake.PROTOCOL, "files": files}
        (self.pin / "RELEASE-MANIFEST.json").write_bytes(wake.canonical(manifest))
        for args in (("init", "-q"), ("add", "."), ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "-c", "core.hooksPath=/dev/null", "commit", "-qm", "Synthetic package")):
            subprocess.run(["/usr/bin/git", "-C", str(self.pin), *args], check=True, capture_output=True)
        self.config["package"].update(commit=wake.git_read(self.pin, "rev-parse", "HEAD"),
            manifest_sha256=wake.digest((self.pin / "RELEASE-MANIFEST.json").read_bytes()),
            cli_sha256=wake.digest((self.pin / "ai_comm.py").read_bytes()))


class PackageTests(PackageFixture):
    def test_missing_pin_input_is_specific_permanent_error(self):
        for name in ("RELEASE-MANIFEST.json", "ai_comm.py", "PROTOCOL.md"):
            path = self.pin / name
            raw = path.read_bytes()
            path.unlink()
            self.fails("pin_file_missing", lambda: wake.check_package(self.config))
            path.write_bytes(raw)
    def test_verified_fixture_is_checked_without_executing_cli(self):
        wake.check_package(self.config)

    def test_tampered_manifest_refused(self):
        with (self.pin / "RELEASE-MANIFEST.json").open("ab") as stream:
            stream.write(b" ")
        self.fails("manifest_changed", lambda: wake.check_package(self.config))

    def test_tampered_cli_refused(self):
        (self.pin / "ai_comm.py").write_text("print('unreviewed')\n")
        self.fails("package_file_changed", lambda: wake.check_package(self.config))

    def test_untracked_debris_refused(self):
        (self.pin / "extra.py").write_text("unreviewed")
        self.fails("pin_mismatch", lambda: wake.check_package(self.config))

    def test_symlinked_protocol_refused(self):
        protocol = self.pin / "PROTOCOL.md"
        raw = protocol.read_bytes()
        protocol.unlink()
        target = self.root / "protocol"
        target.write_bytes(raw)
        protocol.symlink_to(target)
        self.fails("symlink_refused", lambda: wake.check_package(self.config))


class LocalService:
    """Models only the service/lock contract; never calls a service manager."""
    def __init__(self, directory):
        self.directory, self.fd, self.commands = directory, None, []

    def state(self):
        return {"loaded": True, "active": self.fd is not None}

    def command(self, verb):
        self.commands.append(verb)
        if verb == "stop" and self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if verb == "start" and self.fd is None:
            self.fd = wake.common.acquire_lock(str(self.directory), name="observer.lock", attempts=1)


class ServiceTests(Fixture):
    def setUp(self):
        super().setUp()
        self.unit = self.root / wake.unit_name(self.config)
        self.unit.write_bytes(b"reviewed unit bytes\n")
        os.chmod(self.unit, 0o600)
        self.config["service"] = {"path": str(self.unit), "sha256": wake.digest(self.unit.read_bytes())}
        self.output = ("LoadState=loaded\nActiveState=inactive\nMainPID=0\nFragmentPath=" + str(self.unit) + "\n").encode()

    def test_state_checks_exact_fragment_and_command_targets_own_unit(self):
        with mock.patch.object(wake, "run", side_effect=[(0, self.output, b""), (0, b"", b"")]) as run:
            wake.Service(self.config).command("start")
        self.assertEqual(run.call_args_list[-1].args[0],
                         ["/usr/bin/systemctl", "--user", "start", self.unit.name])

    def test_wrong_fragment_refused_before_any_start(self):
        bad = self.output.replace(str(self.unit).encode(), b"/another/instance.service")
        with mock.patch.object(wake, "run", return_value=(0, bad, b"")) as run:
            self.fails("wrong_service_fragment", lambda: wake.Service(self.config).command("start"))
        self.assertEqual(run.call_count, 1)

    def test_modified_unit_refused_before_service_manager_call(self):
        self.unit.write_bytes(b"changed unit\n")
        with mock.patch.object(wake, "run") as run:
            self.fails("service_file_changed", lambda: wake.Service(self.config).command("stop"))
        run.assert_not_called()

    def test_inconsistent_active_pid_refused(self):
        for output in (self.output.replace(b"ActiveState=inactive", b"ActiveState=active"),
                       self.output.replace(b"MainPID=0", b"MainPID=123")):
            with mock.patch.object(wake, "run", return_value=(0, output, b"")):
                self.fails("service_state_invalid", lambda: wake.Service(self.config).state())

    def test_activating_backoff_unit_can_be_stopped(self):
        output = self.output.replace(b"ActiveState=inactive", b"ActiveState=activating")
        with mock.patch.object(wake, "run", return_value=(0, output, b"")):
            self.assertTrue(wake.Service(self.config).state()["transitioning"])
        with mock.patch.object(wake, "run", side_effect=[(0, output, b""), (0, b"", b"")]) as run:
            wake.Service(self.config).command("stop")
        self.assertEqual(run.call_args.args[0][-2:], ["stop", self.unit.name])

    def test_renderer_digest_is_independent_of_service_field(self):
        args = (wake.state_directory(self.config) / "config.json", Path(os.sys.executable),
                ROOT / "adapters/queue_wake.py")
        complete = wake.render_service(self.config, *args)
        without = wake.render_service(dict(self.config, service=None), *args)
        self.assertEqual(complete, without)


class GuidedTests(PackageFixture):
    def test_stopped_body_releases_instance_gate_but_retains_observer_lock(self):
        wake.common.write_private_json(str(self.directory), "paused.json", {"paused": True})
        with wake.exclusive(self.directory, "guided.lock"):
            with self.adapter().stopped():
                self.assertTrue(wake.lock_held(self.directory))
                fd = wake.common.acquire_lock(str(self.directory), name="instance.lock", attempts=1)
                os.close(fd)

    def test_removal_intent_blocks_every_phase_without_pair_or_marker_change(self):
        before = {p.name: p.read_bytes() for p in (self.config_path, self.store.path)}
        (self.directory / "remove-intent.json").symlink_to(self.root / "missing-intent-target")
        for phase in ("probe", "pause", "check", "rebind", "resume", "recover"):
            with self.subTest(phase=phase):
                self.fails("removal_in_progress", lambda: self.adapter().execute(phase, "rebind-123456789abc"))
            self.assertEqual({p.name: p.read_bytes() for p in (self.config_path, self.store.path)}, before)
            self.assertFalse(os.path.lexists(self.directory / "paused.json"))
        self.assertEqual(self.service.commands, [])

    def test_pause_stops_own_service_with_unavailable_runtime(self):
        self.service.command("start")
        with mock.patch.object(wake.Runtime, "status", side_effect=wake.Unavailable("runtime_unavailable")) as status:
            self.assertTrue(self.adapter().execute("pause")["paused"])
        status.assert_not_called()
        self.assertFalse(self.service.state()["active"])

    def test_pause_with_unavailable_runtime_preserves_unread_attempt(self):
        state = self.store.load()
        state.update(scanned=1, wanted=1, pending={"through": 1, "phase": "uncertain"})
        self.store.save(state)
        self.service.command("start")
        with mock.patch.object(wake.Runtime, "status", side_effect=wake.Unavailable("runtime_unavailable")):
            self.fails("unread_pending_wake", lambda: self.adapter().execute("pause"))
        self.assertTrue(self.store.paused())
        self.assertFalse(self.service.state()["active"])
        self.assertEqual(self.store.load()["pending"], state["pending"])

    def test_transitioning_service_never_counts_as_stopped_or_stable_probe(self):
        wake.common.write_private_json(str(self.directory), "paused.json", {"paused": True})
        with mock.patch.object(self.service, "state", return_value={"active": False, "loaded": True,
                                                                  "transitioning": True}):
            self.fails("service_transitioning", lambda: self.adapter().execute("probe"))
            self.fails("observer_must_be_stopped", lambda: self.adapter().execute("rebind"))

    def test_resume_tolerates_post_start_snapshot_race_without_second_start(self):
        adapter = self.adapter()
        adapter.execute("pause")
        real_probe = adapter.probe
        count = [0]
        def raced_probe():
            count[0] += 1
            if count[0] == 2:
                raise wake.Deferred("observation_changed")
            return real_probe()
        with mock.patch.object(adapter, "probe", side_effect=raced_probe), mock.patch.object(wake.time, "sleep"):
            self.assertTrue(adapter.execute("resume")["running"])
        self.assertEqual(self.service.commands, ["stop", "start"])
        self.assertFalse(self.store.paused())

    def controller_transaction(self, mutable_code=False):
        spec = importlib.util.spec_from_file_location("queue_controller_tested", ROOT / "upgrade_controller.py")
        controller = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(controller)
        channel = self.root / ".ai-comm"
        channel.mkdir()
        subprocess.run(["/usr/bin/git", "-C", str(channel), "init", "-q"], check=True, capture_output=True)
        subprocess.run(["/usr/bin/git", "-C", str(channel), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.invalid", "-c", "core.hooksPath=/dev/null",
                        "commit", "--allow-empty", "-qm", "Synthetic channel"], check=True, capture_output=True)
        project = controller.Project(self.root)
        project.runtime()
        code_files = ([".tools/ai-comm/ai_comm.py"] if mutable_code else
                      [(self.bundle / name).relative_to(self.root).as_posix() for name in sorted(wake.CODE_FILES)])
        argv = [os.sys.executable, str(self.bundle / "queue_wake.py")]
        item = {"id": "queue-wake-left", "owner": "left", "code_files": code_files,
                "supports": {"pairs": [[self.old["commit"], self.new["commit"]]]},
                "probe": {"argv": argv + ["probe"], "run_by": "owner"},
                "steps": {phase: argv + [phase] for phase in ("pause", "rebind", "check", "resume")}}
        identity = project.project_id()
        manifest = {"v": 1, "schema": controller.PROJECT_SCHEMA, "status": "declared",
                    "project_id": {"path": identity["path"], "channel": identity["channel"]}, "integrations": [item]}
        raw = wake.canonical(manifest)
        controller.write_private_atomic(project.root / "project.json", raw)
        controller.load_project_manifest(project)
        txn_id = "txn-0123456789abcdef"
        directory = project.root / "txn" / txn_id
        directory.mkdir(mode=0o700)
        frozen = {"txn": txn_id, "project": identity, "project_manifest_sha256": wake.digest(raw),
                  "integrations": [{"id": item["id"], "code": controller.code_hashes(project, item),
                                    "initial_source": None}]}
        controller.Journal(directory).append("start", frozen)
        return controller, controller.Txn(project, txn_id)

    def test_actual_controller_accepts_frozen_bundle_across_pin_change(self):
        _, transaction = self.controller_transaction()
        transaction.check_static()
        self.git("checkout", "--detach", "-q", self.new["commit"])
        transaction.check_static()

    def test_actual_controller_refuses_code_files_inside_changing_pin(self):
        controller, transaction = self.controller_transaction(mutable_code=True)
        transaction.check_static()
        self.git("checkout", "--detach", "-q", self.new["commit"])
        with self.assertRaises(controller.Stop) as ctx:
            transaction.check_static()
        self.assertEqual(ctx.exception.code, "upgrade_identity_changed")
        self.assertEqual(ctx.exception.details["what"], "integration_code")

    def git(self, *args):
        result = subprocess.run(["/usr/bin/git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                                 "-c", "core.hooksPath=/dev/null", "-C", str(self.pin), *args],
                                check=True, capture_output=True)
        return result.stdout.decode().strip()

    def endpoint(self, version):
        self.git("tag", "-a", "v" + version, "-m", "Synthetic endpoint")
        return dict(self.config["package"], version=version, tag="v" + version,
                    tag_object=self.git("rev-parse", "refs/tags/v" + version))

    def setUp(self):
        super().setUp()
        self.old = self.endpoint("1.0.0")
        # Different adapter bytes in each pin prove the running bundle is separate.
        (self.pin / "endpoint-adapter.txt").write_text("different candidate adapter bytes")
        raw = (self.pin / "ai_comm.py").read_bytes() + b"# next endpoint\n"
        (self.pin / "ai_comm.py").write_bytes(raw)
        manifest = json.loads((self.pin / "RELEASE-MANIFEST.json").read_bytes())
        entry = next(e for e in manifest["files"] if e["path"] == "ai_comm.py")
        entry.update(bytes=len(raw), sha256=wake.digest(raw))
        extra = (self.pin / "endpoint-adapter.txt").read_bytes()
        manifest["files"].append({"path": "endpoint-adapter.txt", "bytes": len(extra), "sha256": wake.digest(extra)})
        (self.pin / "RELEASE-MANIFEST.json").write_bytes(wake.canonical(manifest))
        self.git("add", ".")
        self.git("commit", "-qm", "Next synthetic package")
        self.config["package"].update(commit=self.git("rev-parse", "HEAD"), cli_sha256=wake.digest(raw),
            manifest_sha256=wake.digest((self.pin / "RELEASE-MANIFEST.json").read_bytes()))
        self.new = self.endpoint("1.1.0")
        self.git("checkout", "--detach", "-q", self.old["commit"])
        self.config["package"] = wake.package_of(self.old)
        self.config_path = self.directory / "config.json"
        wake.common.write_private_json(str(self.directory), "config.json", self.config)
        self.store = wake.Store(self.directory, self.config)
        self.store.save(wake.initial_state(self.config))
        with wake.exclusive(self.directory):
            pass
        self.service = LocalService(self.directory)
        self.addCleanup(self.service.command, "stop")
        self.bundle = self.root / "frozen"
        self.bundle.mkdir(mode=0o700)
        for name in wake.CODE_FILES:
            (self.bundle / name).write_bytes((wake.HERE / name).read_bytes())
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(wake, "HERE", self.bundle).start()
        self.transition_path = self.root / "transition.json"
        transition = {"v": 1, "schema": wake.TRANSITION_SCHEMA, "identity": wake.transition_identity(self.config),
                      "from": self.old, "to": self.new,
                      "code": {name: wake.digest((self.bundle / name).read_bytes()) for name in wake.CODE_FILES}}
        wake.common.write_private_json(str(self.root), "transition.json", transition)
        self.transition_sha = wake.digest(self.transition_path.read_bytes())
        self.feed = Feed()
        mock.patch.object(wake.Source, "inspect", side_effect=self.feed.inspect).start()
        mock.patch.object(wake.Runtime, "status", return_value="idle").start()

    def adapter(self):
        config = wake.load_config(self.config_path, "left", SESSION, 1)
        return wake.Guided(config, self.config_path, self.transition_path, self.transition_sha, service=self.service)

    def test_pause_rebind_resume_then_reverse_keep_bundle_and_cursors(self):
        self.service.command("start")
        self.assertTrue(self.adapter().execute("pause")["paused"])
        self.git("checkout", "--detach", "-q", self.new["commit"])
        rebound = self.adapter().execute("rebind")
        self.assertEqual(rebound["status"], "rebound")
        self.assertEqual(self.adapter().execute("check")["bound_cli_sha256"], self.new["cli_sha256"])
        self.assertTrue(self.adapter().execute("resume")["running"])
        self.adapter().execute("pause")
        self.git("checkout", "--detach", "-q", self.old["commit"])
        self.adapter().execute("rebind")
        self.assertTrue(self.adapter().execute("resume")["running"])
        self.assertEqual(self.adapter().probe()["bound_cli_sha256"], self.old["cli_sha256"])
        self.assertEqual(self.adapter().probe()["source_sha256"], wake.digest((self.bundle / "queue_wake.py").read_bytes()))

    def test_unread_pending_blocks_rebind_without_erasing_reservation(self):
        state = self.store.load()
        state.update(scanned=1, wanted=1, pending={"through": 1, "phase": "uncertain"})
        self.store.save(state)
        wake.common.write_private_json(str(self.directory), "paused.json", {"paused": True})
        self.fails("unread_pending_wake", lambda: self.adapter().execute("rebind"))
        self.assertEqual(self.store.load()["pending"]["phase"], "uncertain")

    def test_read_receipt_regression_blocks_phases(self):
        state = self.store.load()
        state.update(scanned=2, read=2)
        self.store.save(state)
        self.fails("receipt_regressed", lambda: self.adapter().execute("pause"))

    def test_changed_frozen_code_refuses_before_phase_effect(self):
        (self.bundle / "queue_wake.py").write_bytes(b"changed")
        self.fails("adapter_code_changed", self.adapter)
        self.assertEqual(self.service.commands, [])

    def test_changed_transition_refuses_before_phase_effect(self):
        with self.transition_path.open("ab") as stream:
            stream.write(b" ")
        self.fails("transition_changed", self.adapter)
        self.assertEqual(self.service.commands, [])

    def test_rebind_requires_paused_stopped_observer(self):
        self.fails("observer_must_be_paused", lambda: self.adapter().execute("rebind"))

    def test_completed_rebind_cannot_restore_stale_backup(self):
        self.adapter().execute("pause")
        self.git("checkout", "--detach", "-q", self.new["commit"])
        backup = self.adapter().execute("rebind")["backup"]
        self.fails("completed_transaction_use_reverse", lambda: self.adapter().execute("recover", backup))

    def test_partial_pair_failure_recovers_without_receipt_or_pin_write(self):
        self.adapter().execute("pause")
        self.git("checkout", "--detach", "-q", self.new["commit"])
        real = wake.common.write_private_json

        def fail_state(directory, name, value):
            if name == "state.json":
                raise wake.Invalid("state_write_failed")
            return real(directory, name, value)

        with mock.patch.object(wake.common, "write_private_json", side_effect=fail_state):
            self.fails("state_write_failed", lambda: self.adapter().execute("rebind"))
        backups = list(self.directory.glob("rebind-*"))
        self.assertEqual(len(backups), 1)
        result = self.adapter().execute("recover", backups[0].name)
        self.assertTrue(result["paused"])
        self.assertEqual(result["pin_required"], self.old["commit"])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.new["commit"])
        self.assertEqual(wake.load_config(self.config_path, "left", SESSION, 1)["package"], wake.package_of(self.old))

    def test_uncertain_config_commit_is_read_back_then_requires_recovery(self):
        self.adapter().execute("pause")
        self.git("checkout", "--detach", "-q", self.new["commit"])
        real = wake.common.write_private_json

        def uncertain(directory, name, value):
            real(directory, name, value)
            if name == "config.json":
                raise wake.Invalid("state_commit_uncertain", "transient")

        with mock.patch.object(wake.common, "write_private_json", side_effect=uncertain):
            self.fails("state_commit_uncertain", lambda: self.adapter().execute("rebind"))
        self.assertEqual(wake.common.read_small_json(str(self.config_path))["package"], wake.package_of(self.new))
        self.assertEqual(wake.common.read_small_json(str(self.directory / "state.json"))["binding"], self.store.binding)
        backup = next(self.directory.glob("rebind-*"))
        self.assertEqual(self.adapter().execute("recover", backup.name)["status"], "restored_before_pair")

    def test_recovery_does_not_restore_files_after_identity_takeover(self):
        self.adapter().execute("pause")
        self.git("checkout", "--detach", "-q", self.new["commit"])
        real = wake.common.write_private_json

        def fail_state(directory, name, value):
            if name == "state.json":
                raise wake.Invalid("state_write_failed")
            return real(directory, name, value)

        with mock.patch.object(wake.common, "write_private_json", side_effect=fail_state):
            self.fails("state_write_failed", lambda: self.adapter().execute("rebind"))
        before = self.config_path.read_bytes()
        backup = next(self.directory.glob("rebind-*"))
        with mock.patch.object(wake.Source, "inspect", side_effect=wake.Invalid("session_changed")):
            self.fails("session_changed", lambda: self.adapter().execute("recover", backup.name))
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_resume_start_failure_restores_pause(self):
        self.adapter().execute("pause")
        original = self.service.command

        def fail_start(verb):
            if verb == "start":
                raise wake.Invalid("service_command_failed")
            return original(verb)

        with mock.patch.object(self.service, "command", side_effect=fail_start):
            self.fails("service_command_failed", lambda: self.adapter().execute("resume"))
        self.assertTrue(self.store.paused())
        self.assertFalse(self.service.state()["active"])

    def test_resume_cleanup_still_stops_service_when_pause_write_fails(self):
        self.adapter().execute("pause")
        original_command = self.service.command
        calls = []

        def fail_start(verb):
            calls.append(verb)
            if verb == "start":
                raise wake.Invalid("service_command_failed")
            return original_command(verb)

        with mock.patch.object(self.service, "command", side_effect=fail_start), \
                mock.patch.object(wake.common, "write_private_json", side_effect=wake.Invalid("state_write_failed")):
            self.fails("resume_cleanup_failed", lambda: self.adapter().execute("resume"))
        self.assertEqual(calls, ["start", "stop"])
        self.assertFalse(self.service.state()["active"])


class InputTests(Fixture):
    def argv(self, command, *extra):
        return [command, "--config", str(self.directory / "config.json"), "--handle", "left",
                "--session", SESSION, "--generation", "1", *extra]

    def test_closed_pipe_wait_timeout_is_fixed_transient(self):
        code = "import os,time;os.close(1);os.close(2);time.sleep(30)"
        with self.assertRaises(wake.Unavailable) as ctx:
            wake.run([os.sys.executable, "-I", "-B", "-c", code], self.root, timeout=0.1)
        self.assertEqual((ctx.exception.code, ctx.exception.exit_code), ("command_timeout", 75))

    def test_unexpected_exception_has_fixed_no_traceback_main_error(self):
        with mock.patch.object(wake, "load_config", side_effect=RuntimeError("sensitive raw payload")), \
                mock.patch("sys.stderr") as stderr:
            self.assertEqual(wake.main(self.argv("status")), 78)
        self.assertEqual(stderr.write.call_args_list[0].args[0], "WAKE-ERROR internal_error")

    def test_retry_removal_refusal_keeps_pending_attempt(self):
        state = self.store.load()
        state.update(scanned=1, wanted=1, pending={"through": 1, "phase": "uncertain"})
        self.store.save(state)
        (self.directory / "remove-intent.json").write_text("unparsed")
        with mock.patch.object(wake, "load_config", return_value=self.config), mock.patch("sys.stderr"):
            self.assertEqual(wake.main(self.argv("retry", "--confirm-not-queued", "1")), 78)
        self.assertEqual(self.store.load()["pending"], state["pending"])

    def test_startup_survives_short_read_only_probe_lock_collision(self):
        held = threading.Event()
        def probe():
            with wake.exclusive(self.directory):
                held.set()
                time.sleep(0.25)
        thread = threading.Thread(target=probe)
        thread.start()
        try:
            self.assertTrue(held.wait(3))
            with mock.patch.object(wake, "load_config", return_value=self.config), \
                    mock.patch.object(wake, "observe_loop", return_value=0) as loop, mock.patch("sys.stderr"):
                self.assertEqual(wake.main(self.argv("once")), 0)
            loop.assert_called_once()
        finally:
            thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_timeout_kills_descendant_even_after_parent_exits(self):
        pid_file = self.root / "descendant.pid"
        code = ("import os,time;from pathlib import Path;child=os.fork();"
                "os._exit(0) if child else None;"
                "Path(" + repr(str(pid_file)) + ").write_text(str(os.getpid()));time.sleep(30)")
        try:
            self.fails("command_timeout", lambda: wake.run([os.sys.executable, "-I", "-B", "-c", code],
                                                          self.root, timeout=0.4))
            self.assertTrue(pid_file.exists(), "descendant must have started before timeout")
            pid = int(pid_file.read_text())
            for _ in range(20):
                process = Path("/proc") / str(pid) / "stat"
                if not process.exists() or process.read_text().split()[2] == "Z":
                    break
                time.sleep(0.02)
            else:
                self.fail("timed-out process left a live descendant")
        finally:
            if pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_isolated_script_import_does_not_write_into_bundle(self):
        bundle = self.root / "bundle"
        bundle.mkdir()
        for name in wake.CODE_FILES:
            shutil.copyfile(ROOT / "adapters" / name, bundle / name)
        result = subprocess.run([os.sys.executable, "-I", str(bundle / "queue_wake.py"), "bad-command"],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 78)
        self.assertEqual({p.name for p in bundle.iterdir()}, wake.CODE_FILES)

    def test_pause_marker_rejects_numeric_boolean_and_public_mode(self):
        wake.common.write_private_json(str(self.directory), "paused.json", {"paused": 1})
        self.fails("invalid_pause_file", self.store.paused)
        wake.common.write_private_json(str(self.directory), "paused.json", {"paused": True})
        os.chmod(self.directory / "paused.json", 0o644)
        self.fails("unsafe_file_mode", self.store.paused)

    def test_json_rejects_duplicates_nonfinite_and_deep_values(self):
        for value in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}', b'{"x":' + b'[' * 40 + b'0' + b']' * 40 + b'}'):
            with self.subTest(value=value[:30]), self.assertRaises(wake.Invalid):
                wake.parse(value)

    def test_service_renderer_preserves_literal_metacharacters(self):
        root = self.root / 'a $VALUE %i "quote"'
        config = config_at(root)
        output = wake.render_service(config, wake.state_directory(config) / "config.json",
                                     Path("/usr/bin/python3"), root / "frozen/queue_wake.py").decode()
        self.assertIn("ExecStart=:", output)
        self.assertIn("$VALUE %%i \\\"quote\\\"", output)
        self.assertNotIn("@CONFIG@", output)
        self.assertIn("RestartPreventExitStatus=78", output)

    def test_working_directory_uses_literal_path_not_argv_quotes(self):
        for suffix in ('space "quotes" %i', 'trailing ', 'backslash\\'):
            root = self.root / suffix
            config = config_at(root)
            output = wake.render_service(config, wake.state_directory(config) / "config.json",
                                         Path("/usr/bin/python3"), root / "queue_wake.py").decode()
            working = next(line for line in output.splitlines() if line.startswith("WorkingDirectory="))
            self.assertEqual(working, "WorkingDirectory=" + str(root).replace("%", "%%") + "/.")

    def test_rendered_unit_passes_available_service_manager_verifier(self):
        verifier = shutil.which("systemd-analyze")
        if verifier is None:
            self.skipTest("service-manager syntax verifier is not installed")
        version = subprocess.run([verifier, "--version"], capture_output=True, text=True, timeout=10)
        match = re.match(r"systemd ([0-9]+)", version.stdout)
        if version.returncode != 0 or match is None or int(match.group(1)) < 254:
            self.skipTest("service-manager syntax verifier version >=254 is required")
        root = self.root / 'space $VALUE %i "quote" backslash\\'
        root.mkdir()
        config = config_at(root)
        unit = self.root / wake.unit_name(config)
        unit.write_bytes(wake.render_service(config, wake.state_directory(config) / "config.json",
                                            Path(os.sys.executable), ROOT / "adapters/queue_wake.py"))
        runtime = self.root / "verify-runtime"
        runtime.mkdir(mode=0o700)
        env = dict(os.environ, HOME=str(self.root), XDG_RUNTIME_DIR=str(runtime))
        result = subprocess.run([verifier, "--user", "verify", str(unit)], capture_output=True,
                                text=True, timeout=20, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")

    def test_bad_cli_does_not_echo_untrusted_argument(self):
        result = subprocess.run([os.sys.executable, "-I", "-B", str(ROOT / "adapters/queue_wake.py"),
                                 "untrusted-private-token"], capture_output=True)
        self.assertEqual(result.returncode, 78)
        self.assertNotIn(b"untrusted-private-token", result.stderr)
        self.assertNotIn(b"Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
