#!/usr/bin/env python3
"""Explicit, local, Git-backed communication between cooperating sessions."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import html
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timezone

VERSION = "0.1.3"
ACTS = {"PING", "ACK", "PONG", "ASK", "FACT", "CORRECT", "PROPOSE",
        "REVIEW", "ACCEPT", "REJECT", "DECIDE"}
CONFIDENCE = {"verified", "likely", "unverified"}
CONF_ACTS = {"FACT", "CORRECT", "DECIDE"}
DRAFT_KEYS = {"act", "to", "topic", "summary", "body", "reply_to", "refs",
              "sources", "questions", "artifact", "confidence"}
META_KEYS = {"v", "id", "seq", "ts", "from", "session", "generation", "key",
             "draft_sha256", "nonce", "echo"}
HANDLE = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
IDENT = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
KEY = re.compile(r"[A-Za-z0-9_.:-]{1,96}\Z")
HEX = re.compile(r"[0-9a-f]{64}\Z")
NONCE = re.compile(r"[0-9a-f]{32}\Z")
TEXT_CONTROLS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
MULTILINE_CONTROLS = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")
MID = re.compile(r"[0-9]{8}-[a-z][a-z0-9-]{0,31}\Z")
MAX_DRAFT = 65536
MAX_MESSAGE = 131072
MAX_ARTIFACT = 1048576
REF = "refs/heads/main"


class ProtocolError(Exception):
    """A safe, user-facing failure without payload or credential disclosure."""


def require(condition, message):
    if not condition:
        raise ProtocolError(message)


def canonical(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def parse(data, limit=MAX_MESSAGE):
    require(len(data) <= limit, "JSON exceeds size limit")
    try:
        result = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(
                                ProtocolError("non-finite JSON number")))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ProtocolError("invalid UTF-8 JSON") from exc
    require(isinstance(result, dict), "JSON must be an object")
    return result


def read_input(path, limit=MAX_DRAFT):
    if path == "-":
        data = sys.stdin.buffer.read(limit + 1)
    else:
        with open(path, "rb") as stream:
            data = stream.read(limit + 1)
    require(len(data) <= limit, "input exceeds size limit")
    return data


def text_field(value, name, maximum, multiline=False):
    require(isinstance(value, str) and 0 < len(value) <= maximum,
            f"invalid {name} length or type")
    require(value.strip() != "", f"empty {name}")
    controls = MULTILINE_CONTROLS if multiline else TEXT_CONTROLS
    require(controls.search(value) is None and
            (value.isascii() or not any(unicodedata.category(c) in {"Cf", "Cs", "Zl", "Zp"}
                                       for c in value)), f"invalid control character in {name}")
    return value


def pattern(value, regex, name):
    require(isinstance(value, str) and regex.fullmatch(value) is not None,
            f"invalid {name}")
    return value


def integer(value, name, low=0, high=99999999):
    require(type(value) is int and low <= value <= high, f"invalid {name}")
    return value


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def timestamp(value):
    require(isinstance(value, str), "invalid timestamp")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ProtocolError("invalid timestamp") from exc
    require(parsed.strftime("%Y-%m-%dT%H:%M:%SZ") == value, "invalid timestamp")


def git_env(extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    env.update(extra or {})
    return env


def git(directory, *args, data=None, extra=None, ok=(0,)):
    proc = subprocess.run(["git", "--git-dir", str(directory), *args],
                          input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=git_env(extra), timeout=30)
    require(proc.returncode in ok, "Git operation failed; no success is implied")
    return proc.stdout


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, value):
    require(not path.is_symlink(), "runtime symlink refused")
    fd, name = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Channel:
    def __init__(self, path):
        raw = Path(path).expanduser().absolute()
        require(not raw.is_symlink(), "channel symlink refused")
        self.path = raw.resolve()
        require(self.path.is_dir(), "channel does not exist; initialize it explicitly")
        require(git(self.path, "rev-parse", "--is-bare-repository").strip() == b"true",
                "channel must be a dedicated bare Git repository")
        self.runtime = self.path / "ai-comm-runtime"
        require(not self.runtime.is_symlink(), "runtime symlink refused")
        self.runtime.mkdir(mode=0o700, exist_ok=True)

    @classmethod
    def init(cls, path, task, reviewers, require_confidence=(), require_sources=()):
        text_field(task, "task", 2000, True)
        require(2 <= len(reviewers) <= 16 and len(set(reviewers)) == len(reviewers),
                "at least two distinct reviewers required")
        for handle in reviewers:
            pattern(handle, HANDLE, "reviewer")
        require(set(require_confidence) <= CONF_ACTS, "invalid confidence policy")
        require(set(require_sources) <= ACTS, "invalid source policy")
        target = Path(path).expanduser().absolute()
        require(not target.exists() and not target.is_symlink(),
                "init refuses an existing path")
        target.mkdir(mode=0o700, parents=False)
        proc = subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(target)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=git_env(), timeout=30)
        require(proc.returncode == 0, "channel initialization failed")
        channel = cls(target)
        config = {"kind": "ai-comm-channel", "v": 2, "task": task,
                  "reviewers": reviewers, "policy": {
                      "require_confidence": list(require_confidence),
                      "require_sources": list(require_sources)},
                  "protocol_sha256": sha(Path(__file__).with_name("PROTOCOL.md").read_bytes())}
        with channel.lock():
            head = channel.publish(None, {"channel.json": canonical(config)}, "initialize channel", "system")
        return {"channel": str(channel.path), "head": head, "v": 2}

    @contextlib.contextmanager
    def lock(self):
        fd = os.open(self.runtime / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    require(time.monotonic() < deadline, "channel busy; retry with the same key")
                    time.sleep(0.05)
            yield
        finally:
            os.close(fd)

    def publish(self, parent, changes, title, author):
        """Commit only supplied blobs, then atomically advance the delivery ref."""
        if parent:
            for path in changes:
                require(not git(self.path, "ls-tree", parent, "--", path).strip(),
                        "durable records are append-only")
        with tempfile.TemporaryDirectory(prefix="index-", dir=self.runtime) as directory:
            env = {"GIT_INDEX_FILE": str(Path(directory) / "index"),
                   "GIT_AUTHOR_NAME": author, "GIT_AUTHOR_EMAIL": author + "@ai-comm.invalid",
                   "GIT_COMMITTER_NAME": "ai-comm", "GIT_COMMITTER_EMAIL": "ai-comm@local.invalid"}
            if parent:
                git(self.path, "read-tree", parent, extra=env)
            else:
                git(self.path, "read-tree", "--empty", extra=env)
            for path, data in changes.items():
                blob = git(self.path, "hash-object", "-w", "--stdin", data=data, extra=env).decode().strip()
                git(self.path, "update-index", "--add", "--cacheinfo", "100644", blob, path, extra=env)
            tree = git(self.path, "write-tree", extra=env).decode().strip()
            args = ["-c", "commit.gpgsign=false", "commit-tree", tree]
            if parent:
                args += ["-p", parent]
            commit = git(self.path, *args, data=(title + "\n").encode(), extra=env).decode().strip()
            self._before_publish(commit)  # failure-injection seam; no external hooks
            expected = parent or "0" * len(commit)
            git(self.path, "update-ref", REF, commit, expected)
            return commit

    def _before_publish(self, commit):
        pass

    def snapshot(self):
        head = git(self.path, "rev-parse", "--verify", REF).decode().strip()
        require(not git(self.path, "log", "--format=", "--name-only", "--diff-filter=MDRT", head).strip(),
                "channel history modified or removed a durable record")
        entries = git(self.path, "ls-tree", "-r", "-z", head).split(b"\0")
        tree = {}
        for entry in entries:
            if not entry:
                continue
            prefix, raw_path = entry.split(b"\t", 1)
            mode, kind, oid = prefix.decode().split()
            require(mode == "100644" and kind == "blob", "unexpected channel entry type")
            path = raw_path.decode("ascii")
            require(path == "channel.json" or re.fullmatch(
                r"messages/[0-9]{8}-[a-z][a-z0-9-]{0,31}\.json|"
                r"sessions/[a-z][a-z0-9-]{0,31}/[0-9]{8}\.json|"
                r"artifacts/[0-9a-f]{64}", path), "unexpected channel entry path")
            tree[path] = oid
        require("channel.json" in tree, "not an ai-comm channel")
        request = ("\n".join(tree[p] for p in tree) + "\n").encode()
        sizes = git(self.path, "cat-file", "--batch-check", data=request).splitlines()
        require(len(sizes) == len(tree), "invalid Git size response")
        total = 0
        for path, line in zip(tree, sizes):
            parts = line.split()
            require(len(parts) == 3 and parts[1] == b"blob", "invalid Git size response")
            length = int(parts[2])
            bound = MAX_ARTIFACT if path.startswith("artifacts/") else MAX_MESSAGE
            require(length <= bound, "stored object exceeds limit")
            total += length
        require(total <= 64 * 1024 * 1024, "channel snapshot exceeds 64 MiB limit; archive explicitly")
        # One batch read; Git object identifiers are generated by Git, never peer strings.
        names = list(tree)
        raw = git(self.path, "cat-file", "--batch",
                  data=("\n".join(tree[p] for p in names) + "\n").encode())
        blobs = {}
        offset = 0
        for path in names:
            end = raw.find(b"\n", offset)
            require(end >= offset, "truncated Git object response")
            fields = raw[offset:end].split()
            require(len(fields) == 3 and fields[1] == b"blob", "invalid Git object response")
            size = int(fields[2])
            require(size <= MAX_ARTIFACT, "stored object exceeds limit")
            offset = end + 1
            blobs[path] = raw[offset:offset + size]
            require(len(blobs[path]) == size and raw[offset + size:offset + size + 1] == b"\n",
                    "truncated Git object")
            offset += size + 1
        return Snapshot(head, blobs)

    def join(self, handle, platform, session, takeover=False):
        pattern(handle, HANDLE, "handle")
        pattern(platform, HANDLE, "platform")
        text_field(session, "non-secret session reference", 160)
        with self.lock():
            state = self.snapshot()
            current = state.current.get(handle)
            if current and current["platform"] == platform and current["session"] == session:
                return {"joined": current, "head": state.head, "duplicate": True}
            require(not current or takeover, "handle already active; explicit takeover required")
            require(not any(record["handle"] == handle and record["session"] == session
                            for record in state.sessions.values()),
                    "takeover requires a never-used session reference for this handle")
            generation = current["generation"] + 1 if current else 1
            record = {"handle": handle, "platform": platform, "session": session,
                      "generation": generation, "ts": now()}
            path = f"sessions/{handle}/{generation:08d}.json"
            head = self.publish(state.head, {path: canonical(record)},
                                f"join {handle} generation {generation}", handle)
            return {"joined": record, "head": head, "duplicate": False}

    def send(self, handle, session, key, draft):
        pattern(key, KEY, "retry key")
        require(len(canonical(draft)) <= MAX_DRAFT, "draft exceeds size limit")
        with self.lock():
            state = self.snapshot()
            current = state.identity(handle, session)
            digest = sha(canonical(draft))
            previous = state.retries.get((handle, current["generation"], key))
            if previous:
                require(previous["draft_sha256"] == digest, "retry key reused with changed draft")
                return {"message": previous, "head": state.head, "duplicate": True}
            state.validate_draft(draft, handle)
            seq = len(state.messages) + 1
            integer(seq, "sequence", 1)
            message = dict(draft, v=2, id=f"{seq:08d}-{handle}", seq=seq, ts=now(),
                           session=session, generation=current["generation"],
                           key=key, draft_sha256=digest)
            message["from"] = handle
            act = message["act"]
            target = state.by_id.get(message.get("reply_to"))
            if act in {"PING", "PONG"}:
                message["nonce"] = secrets.token_hex(16)
            if act == "PONG" or (act == "ACK" and target and target["act"] in {"PING", "PONG"}):
                message["echo"] = target["nonce"]
            state.add_message(message)  # validates the complete assigned envelope too
            data = canonical(message)
            require(len(data) <= MAX_MESSAGE, "assigned envelope exceeds size limit")
            head = self.publish(state.head, {f"messages/{message['id']}.json": data},
                                f"{message['id']} {act}: {message['summary']}", handle)
            return {"message": message, "head": head, "duplicate": False,
                    "warnings": ["body exceeds 2000 characters; prefer an artifact"]
                    if len(message.get("body", "")) > 2000 else []}

    def put_artifact(self, handle, session, data):
        require(len(data) <= MAX_ARTIFACT, "artifact exceeds 1 MiB")
        try:
            text_field(data.decode("utf-8"), "artifact", MAX_ARTIFACT, True)
        except UnicodeError as exc:
            raise ProtocolError("artifact must be UTF-8 text") from exc
        digest = sha(data)
        with self.lock():
            state = self.snapshot()
            state.identity(handle, session)
            if digest in state.artifacts:
                return {"artifact": digest, "head": state.head, "duplicate": True}
            head = self.publish(state.head, {f"artifacts/{digest}": data},
                                f"artifact {digest}", handle)
            return {"artifact": digest, "head": head, "duplicate": False}

    def receipt(self, state, handle, session):
        current = state.identity(handle, session)
        path = self.runtime / (handle + ".json")
        require(not path.is_symlink(), "receipt symlink refused")
        if path.exists():
            record = parse(read_input(path, 4096), 4096)
            require(set(record) == {"v", "generation", "read", "seen"} and record["v"] == 2,
                    "corrupt receipt")
            for field in ("generation", "read", "seen"):
                integer(record[field], "receipt field")
            require(1 <= record["generation"] <= current["generation"] and
                    0 <= record["read"] <= record["seen"] <= len(state.messages), "corrupt receipt bounds")
            if record["generation"] != current["generation"]:
                record = dict(record, generation=current["generation"], seen=record["read"])
        else:
            record = {"v": 2, "generation": current["generation"], "read": 0, "seen": 0}
        return path, record

    def inbox(self, handle, session, limit=10):
        integer(limit, "limit", 1, 100)
        with self.lock():
            state = self.snapshot()
            path, receipt = self.receipt(state, handle, session)
            messages, through = self._page(state, handle, receipt["read"], limit)
            receipt["seen"] = max(receipt["seen"], through)
            atomic_json(path, receipt)
            return {"head": state.head, "read_through": receipt["read"], "through": through,
                    "messages": messages, "more": through < len(state.messages)}

    @staticmethod
    def _page(state, handle, after, limit):
        messages, through, size = [], after, 0
        for message in state.messages[after:]:
            if handle in message["to"]:
                length = len(canonical(message))
                if len(messages) >= limit or size + length > 256 * 1024:
                    break
                messages.append(message)
                size += length
            through = message["seq"]
        return messages, through

    def notifications(self, handle, session, new_since, limit=10):
        """Caller-owned alert cursor; never authorize or advance a receipt."""
        integer(new_since, "notification sequence")
        integer(limit, "limit", 1, 100)
        with self.lock():
            state = self.snapshot()
            _, receipt = self.receipt(state, handle, session)
            require(new_since <= len(state.messages), "notification cursor exceeds channel history")
            after = max(receipt["read"], new_since)
            messages, through = self._page(state, handle, after, limit)
            return {"head": state.head, "mode": "notifications", "read_through": receipt["read"],
                    "notification_through": through, "messages": messages,
                    "more": through < len(state.messages)}

    def ack(self, handle, session, through):
        integer(through, "receipt sequence")
        with self.lock():
            state = self.snapshot()
            path, receipt = self.receipt(state, handle, session)
            require(receipt["read"] <= through <= receipt["seen"],
                    "ack must not regress or pass the scanned inbox")
            receipt["read"] = through
            atomic_json(path, receipt)
            return {"read_through": through, "head": state.head}

    def wait(self, handle, session, timeout=30, limit=10, new_since=None):
        require(type(timeout) in (int, float) and 0 <= timeout <= 60, "timeout must be 0–60 seconds")
        deadline = time.monotonic() + timeout
        while True:
            result = (self.inbox(handle, session, limit) if new_since is None else
                      self.notifications(handle, session, new_since, limit))
            if result["messages"] or time.monotonic() >= deadline:
                return dict(result, timed_out=not bool(result["messages"]))
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))


class Snapshot:
    def __init__(self, head, blobs):
        self.head = head
        self.config = parse(blobs["channel.json"])
        cfg = self.config
        require(set(cfg) == {"kind", "v", "task", "reviewers", "policy", "protocol_sha256"}
                and cfg["kind"] == "ai-comm-channel" and type(cfg["v"]) is int and cfg["v"] == 2,
                "unsupported or malformed channel")
        text_field(cfg["task"], "task", 2000, True)
        pattern(cfg["protocol_sha256"], HEX, "protocol digest")
        require(cfg["protocol_sha256"] == sha(Path(__file__).with_name("PROTOCOL.md").read_bytes()),
                "package contract does not match channel; select its pinned revision")
        reviewers = cfg["reviewers"]
        require(isinstance(reviewers, list) and 2 <= len(reviewers) <= 16,
                "invalid reviewer configuration")
        for handle in reviewers:
            pattern(handle, HANDLE, "reviewer")
        require(len(set(reviewers)) == len(reviewers), "reviewers must be distinct")
        policy = cfg["policy"]
        require(isinstance(policy, dict) and set(policy) == {"require_confidence", "require_sources"},
                "invalid policy")
        for field, allowed in (("require_confidence", CONF_ACTS), ("require_sources", ACTS)):
            require(isinstance(policy[field], list) and all(isinstance(a, str) and a in allowed
                    for a in policy[field]), "invalid policy acts")
        self.sessions, self.current, self.artifacts = {}, {}, {}
        for path, data in sorted(blobs.items()):
            if path.startswith("sessions/"):
                record = parse(data)
                require(set(record) == {"handle", "platform", "session", "generation", "ts"},
                        "invalid session record")
                handle = pattern(record["handle"], HANDLE, "handle")
                pattern(record["platform"], HANDLE, "platform")
                text_field(record["session"], "session reference", 160)
                generation = integer(record["generation"], "generation", 1)
                timestamp(record["ts"])
                require(path == f"sessions/{handle}/{generation:08d}.json", "session path mismatch")
                previous = self.current.get(handle)
                require(generation == (previous["generation"] + 1 if previous else 1), "session history gap")
                require(not any(old["handle"] == handle and old["session"] == record["session"]
                                for old in self.sessions.values()), "reused historical session reference")
                self.sessions[(handle, generation)] = record
                self.current[handle] = record
            elif path.startswith("artifacts/"):
                digest = path.split("/")[1]
                require(sha(data) == digest, "artifact hash mismatch")
                try:
                    text_field(data.decode("utf-8"), "stored artifact", MAX_ARTIFACT, True)
                except UnicodeError as exc:
                    raise ProtocolError("stored artifact must be UTF-8 text") from exc
                self.artifacts[digest] = data
        self.messages, self.by_id, self.retries, self.sources, self.questions = [], {}, {}, {}, {}
        for path, data in sorted(blobs.items()):
            if path.startswith("messages/"):
                message = parse(data)
                require(path == f"messages/{message.get('id')}.json", "message path mismatch")
                require(canonical(message) == data, "noncanonical stored message")
                self.add_message(message)

    def identity(self, handle, session):
        pattern(handle, HANDLE, "handle")
        text_field(session, "session reference", 160)
        record = self.current.get(handle)
        require(record is not None and record["session"] == session, "unregistered or stale session")
        return record

    def validate_draft(self, draft, author):
        require(isinstance(draft, dict) and set(draft) <= DRAFT_KEYS, "unknown draft field")
        require({"act", "to", "topic", "summary"} <= set(draft), "missing draft field")
        act = draft["act"]
        require(isinstance(act, str) and act in ACTS, "unknown act")
        recipients = draft["to"]
        require(isinstance(recipients, list) and 1 <= len(recipients) <= 32, "invalid recipients")
        for handle in recipients:
            pattern(handle, HANDLE, "recipient")
            require(handle in self.current, "recipient not registered")
        require(len(set(recipients)) == len(recipients), "duplicate recipient")
        pattern(draft["topic"], IDENT, "topic")
        text_field(draft["summary"], "summary", 300)
        if "body" in draft:
            text_field(draft["body"], "body", MAX_DRAFT, True)
        if "confidence" in draft:
            require(act in CONF_ACTS and isinstance(draft["confidence"], str)
                    and draft["confidence"] in CONFIDENCE, "invalid confidence")
        if act in self.config["policy"]["require_confidence"]:
            require("confidence" in draft, "confidence required by channel policy")
        target = None
        if "reply_to" in draft:
            pattern(draft["reply_to"], MID, "reply target")
            target = self.by_id.get(draft["reply_to"])
            require(target is not None, "reply target must be an earlier committed message")
        if act in {"CORRECT", "PONG", "ACK", "ACCEPT", "REJECT", "DECIDE"}:
            require(target is not None, "this act requires a reply target")
        if act == "PONG" or (act == "ACK" and target["act"] in {"PING", "PONG"}):
            require(target["act"] in ({"PING"} if act == "PONG" else {"PING", "PONG"})
                    and author in target["to"] and target["from"] in recipients
                    and author != target["from"], "invalid control reply relationship")
        if "artifact" in draft:
            pattern(draft["artifact"], HEX, "artifact digest")
            require(draft["artifact"] in self.artifacts, "artifact must be published first")
        if act in {"PROPOSE", "ACCEPT", "REJECT", "DECIDE"}:
            require("artifact" in draft, "this act requires an artifact")
        if act in {"ACCEPT", "REJECT", "DECIDE"}:
            require(target["act"] == "PROPOSE" and target.get("artifact") == draft["artifact"],
                    "agreement must reference the exact proposal and artifact")
            require(draft["topic"] == target["topic"], "agreement topic differs from proposal")
            require(author in self.config["reviewers"], "only configured reviewers may record agreement")
            require(author == target["from"] or author in target["to"], "reviewer not addressed by proposal")
            require(not any(m["act"] == "DECIDE" and m.get("reply_to") == target["id"]
                            for m in self.messages), "proposal already decided; propose a new revision")
            if act == "DECIDE":
                stances = {}
                for message in self.messages:
                    if message["act"] in {"ACCEPT", "REJECT"} and message.get("reply_to") == target["id"]:
                        stances[message["from"]] = message["act"]
                require(all(stances.get(r) == "ACCEPT" for r in self.config["reviewers"]),
                        "all required reviewers must ACCEPT this exact proposal")
        additions = draft.get("sources", [])
        require(isinstance(additions, list) and len(additions) <= 64, "invalid source declarations")
        sources = dict(self.sources)
        for source in additions:
            require(isinstance(source, dict) and set(source) == {"id", "kind", "ref", "detail"},
                    "invalid source declaration")
            sid = pattern(source["id"], IDENT, "source id")
            pattern(source["kind"], IDENT, "source kind")
            text_field(source["ref"], "source reference", 4000)
            text_field(source["detail"], "source detail", 8000, True)
            require(sid not in sources or sources[sid] == source, "source id is immutable")
            sources[sid] = source
        refs = draft.get("refs", [])
        require(isinstance(refs, list) and len(refs) <= 64, "invalid source references")
        for sid in refs:
            pattern(sid, IDENT, "source id")
            require(sid in sources, "unknown source reference")
        if act in self.config["policy"]["require_sources"]:
            require(bool(refs or additions), "source evidence required by channel policy")
        deltas = draft.get("questions", [])
        require(isinstance(deltas, list) and len(deltas) <= 64, "invalid question deltas")
        questions = dict(self.questions)
        changed_questions = set()
        for delta in deltas:
            require(isinstance(delta, dict) and isinstance(delta.get("op"), str), "invalid question delta")
            qid = pattern(delta.get("id"), IDENT, "question id")
            require(qid not in changed_questions, "one delta per question per message")
            changed_questions.add(qid)
            if delta["op"] == "open":
                require(set(delta) == {"op", "id", "text"} and qid not in questions, "question id already used")
                text_field(delta["text"], "question", 2000, True)
                questions[qid] = {"id": qid, "text": delta["text"], "state": "open"}
            else:
                require(delta["op"] == "close" and set(delta) == {"op", "id"}, "unknown question operation")
                require(qid in questions and questions[qid]["state"] == "open", "question is not open")
                questions[qid] = dict(questions[qid], state="closed")
        return sources, questions

    def add_message(self, message):
        required = {"v", "id", "seq", "ts", "from", "session", "generation", "key", "draft_sha256"}
        require(required <= set(message) <= (DRAFT_KEYS | META_KEYS), "invalid envelope fields")
        require(type(message["v"]) is int and message["v"] == 2, "unsupported message version")
        seq = integer(message["seq"], "sequence", 1)
        require(seq == len(self.messages) + 1, "message sequence gap or collision")
        author = pattern(message["from"], HANDLE, "sender")
        require(message["id"] == f"{seq:08d}-{author}", "message id mismatch")
        generation = integer(message["generation"], "generation", 1)
        session = self.sessions.get((author, generation))
        require(session is not None and session["session"] == message["session"], "invalid historical session")
        timestamp(message["ts"])
        pattern(message["key"], KEY, "retry key")
        pattern(message["draft_sha256"], HEX, "draft digest")
        draft = {k: v for k, v in message.items() if k in DRAFT_KEYS}
        require(len(canonical(draft)) <= MAX_DRAFT, "stored draft exceeds limit")
        require(sha(canonical(draft)) == message["draft_sha256"], "draft digest mismatch")
        sources, questions = self.validate_draft(draft, author)
        act = message["act"]
        if act in {"PING", "PONG"}:
            pattern(message.get("nonce"), NONCE, "control nonce")
        else:
            require("nonce" not in message, "unexpected control nonce")
        target = self.by_id.get(message.get("reply_to"))
        if act == "PONG" or (act == "ACK" and target["act"] in {"PING", "PONG"}):
            require(message.get("echo") == target["nonce"], "control nonce does not match")
            if act == "PONG":
                require(message["nonce"] != target["nonce"], "PONG nonce must be fresh")
        else:
            require("echo" not in message, "unexpected control echo")
        retry = (author, generation, message["key"])
        require(retry not in self.retries, "duplicate retry key in history")
        for qid, value in questions.items():
            old = self.questions.get(qid)
            if old is None:
                value = dict(value, opened_by=message["id"])
            elif old["state"] != value["state"]:
                value = dict(value, closed_by=message["id"])
            questions[qid] = value
        self.sources, self.questions = sources, questions
        self.messages.append(message)
        self.by_id[message["id"]] = message
        self.retries[retry] = message

    def state(self, budget=8192):
        result = {"v": 2, "head": self.head, "task": self.config["task"],
                  "participants": list(self.current.values()),
                  "open_questions": [{"id": q["id"], "opened_by": q["opened_by"]}
                                     for q in self.questions.values() if q["state"] == "open"],
                  "decisions": [{"id": m["id"], "artifact": m["artifact"], "proposal": m["reply_to"]}
                                for m in self.messages if m["act"] == "DECIDE"],
                  "recent": [{"id": m["id"], "act": m["act"], "summary": m["summary"]}
                             for m in self.messages[-10:]],
                  "omitted": {"participants": 0, "open_questions": 0, "decisions": 0, "recent": 0},
                  "inspect": "registry sessions|questions|sources; log --after N; show ID"}
        while len(canonical(result)) > budget:
            candidates = [k for k in result["omitted"] if result[k]]
            require(bool(candidates), "state budget too small")
            largest = max(candidates, key=lambda k: len(canonical(result[k])))
            result[largest].pop(0 if largest in {"recent", "decisions"} else -1)
            result["omitted"][largest] += 1
        return result

    def render(self, since=0):
        integer(since, "since")
        lines = ["# Channel history", "", f"Committed snapshot: `{self.head}`", ""]
        words = {"PING": "Ping", "PONG": "Pong and ping acknowledgement", "ACK": "Acknowledgement",
                 "ASK": "Question", "FACT": "Fact or observation", "CORRECT": "Correction",
                 "PROPOSE": "Proposal", "REVIEW": "Review", "ACCEPT": "Acceptance",
                 "REJECT": "Rejection", "DECIDE": "Decision"}
        # HTML-escape all text so content cannot become executable markup or links.
        def safe(value):
            return html.escape(str(value), quote=True).replace("`", "&#96;")
        source_first_seen = {}
        for message in self.messages:
            if message["seq"] <= since:
                continue
            lines += [f"## {safe(message['id'])} — {words[message['act']]}", "",
                      f"{safe(message['from'])} → {safe(', '.join(message['to']))} · {safe(message['ts'])}", "",
                      "<pre>" + safe(message["summary"]) + "</pre>", ""]
            if "body" in message:
                lines += ["<pre>" + safe(message["body"]) + "</pre>", ""]
            for field in ("reply_to", "confidence", "artifact", "nonce", "echo"):
                if field in message:
                    lines += [f"{field}: `{safe(message[field])}`", ""]
            refs = list(dict.fromkeys(message.get("refs", []) + [s["id"] for s in message.get("sources", [])]))
            for sid in refs:
                if sid in source_first_seen:
                    citation = f"Source {sid}: see {source_first_seen[sid]} above."
                else:
                    source = self.sources[sid]
                    citation = f"Source {sid} ({source['kind']}): {source['ref']}\n{source['detail']}"
                    source_first_seen[sid] = message["id"]
                lines += ["<pre>" + safe(citation) + "</pre>", ""]
            for delta in message.get("questions", []):
                question = self.questions[delta["id"]]
                lines += ["<pre>" + safe(f"Question {delta['id']}: {delta['op']} — {question['text']}") + "</pre>", ""]
        return "\n".join(lines) + "\n"


def emit(value):
    sys.stdout.buffer.write(canonical(value))
    sys.stdout.buffer.flush()


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--version", action="version", version=VERSION)
    result.add_argument("--channel", required=True, help="explicit local channel directory")
    sub = result.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init")
    p.add_argument("--task", required=True)
    p.add_argument("--reviewers", nargs="+", required=True)
    p.add_argument("--require-confidence", nargs="*", default=[])
    p.add_argument("--require-sources", nargs="*", default=[])
    p = sub.add_parser("join")
    p.add_argument("--as", dest="actor", required=True)
    p.add_argument("--platform", required=True)
    p.add_argument("--session", required=True, help="non-secret conversation reference")
    p.add_argument("--takeover", action="store_true")
    for command in ("send", "inbox", "ack", "wait", "artifact"):
        p = sub.add_parser(command)
        p.add_argument("--as", dest="actor", required=True)
        p.add_argument("--session", required=True)
        if command == "send":
            p.add_argument("--key", required=True, help="reuse for retries of this identical draft")
            p.add_argument("--draft", required=True, help="JSON file, or - for stdin")
        elif command in {"inbox", "wait"}:
            p.add_argument("--limit", type=int, default=10)
            if command == "wait":
                p.add_argument("--timeout", type=float, default=30)
                p.add_argument("--new-since", type=int,
                               help="notification cursor only; does not scan or acknowledge the inbox")
        elif command == "ack":
            p.add_argument("--through", type=int, required=True)
        elif command == "artifact":
            p.add_argument("--file", required=True)
    sub.add_parser("state")
    sub.add_parser("validate")
    p = sub.add_parser("show")
    p.add_argument("id")
    p = sub.add_parser("show-artifact")
    p.add_argument("digest")
    p = sub.add_parser("log")
    p.add_argument("--after", type=int, default=0)
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("registry")
    p.add_argument("kind", choices=["sessions", "questions", "sources"])
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("render")
    p.add_argument("--since", type=int, default=0)
    p.add_argument("--out")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            emit(Channel.init(args.channel, args.task, args.reviewers,
                              args.require_confidence, args.require_sources))
            return 0
        channel = Channel(args.channel)
        cmd = args.command
        if cmd == "join":
            value = channel.join(args.actor, args.platform, args.session, args.takeover)
        elif cmd == "send":
            value = channel.send(args.actor, args.session, args.key, parse(read_input(args.draft), MAX_DRAFT))
            value["message"] = {k: v for k, v in value["message"].items()
                                if k in {"id", "seq", "act", "from", "to", "nonce", "echo"}}
        elif cmd == "inbox":
            value = channel.inbox(args.actor, args.session, args.limit)
        elif cmd == "wait":
            value = channel.wait(args.actor, args.session, args.timeout, args.limit, args.new_since)
        elif cmd == "ack":
            value = channel.ack(args.actor, args.session, args.through)
        elif cmd == "artifact":
            value = channel.put_artifact(args.actor, args.session, read_input(args.file, MAX_ARTIFACT))
        else:
            state = channel.snapshot()
            if cmd == "state":
                value = state.state()
            elif cmd == "validate":
                value = {"valid": True, "head": state.head, "messages": len(state.messages),
                         "sessions": len(state.sessions), "artifacts": len(state.artifacts)}
            elif cmd == "show":
                pattern(args.id, MID, "message id")
                require(args.id in state.by_id, "message not found")
                value = state.by_id[args.id]
            elif cmd == "show-artifact":
                pattern(args.digest, HEX, "artifact digest")
                require(args.digest in state.artifacts, "artifact not found")
                sys.stdout.buffer.write(state.artifacts[args.digest])
                return 0
            elif cmd == "log":
                integer(args.after, "after")
                integer(args.limit, "limit", 1, 100)
                page = state.messages[args.after:args.after + args.limit]
                value = {"head": state.head, "messages": [{k: m[k] for k in
                         ("id", "seq", "from", "to", "act", "summary")} for m in page],
                         "next": page[-1]["seq"] if page else args.after,
                         "more": args.after + len(page) < len(state.messages)}
            elif cmd == "registry":
                integer(args.offset, "offset")
                integer(args.limit, "limit", 1, 100)
                mapping = getattr(state, args.kind)
                values = list(mapping.values())
                page = values[args.offset:args.offset + args.limit]
                value = {"head": state.head, "items": page, "next": args.offset + len(page),
                         "more": args.offset + len(page) < len(values)}
            elif cmd == "render":
                rendered = state.render(args.since)
                if args.out:
                    with open(args.out, "x", encoding="utf-8") as output:
                        output.write(rendered)
                    value = {"rendered": True, "head": state.head}
                else:
                    sys.stdout.write(rendered)
                    return 0
        emit(value)
        return 0
    except ProtocolError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError,
            TypeError, KeyError, RecursionError):
        print('{"error":"operation failed; inspect inputs and channel permissions; no success is implied"}',
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
