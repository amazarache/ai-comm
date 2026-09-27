import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ai_comm as ac


class UpgradePlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ai-comm-upgrade-plan-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "package with spaces"
        self.repo.mkdir()
        self._git("init", "--initial-branch=main", str(self.repo))
        self.env = os.environ.copy()
        self.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="test",
                        GIT_AUTHOR_EMAIL="test@example.invalid",
                        GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="test@example.invalid")
        self.protocol = (Path(ac.__file__).resolve().parent / "PROTOCOL.md").read_bytes()
        self._write_package("1.0.0", b'{"release":"1.0.0"}\n', self.protocol)
        self._git("add", "ai_comm.py", "RELEASE-MANIFEST.json", "PROTOCOL.md")
        self._git("commit", "-m", "pinned 1.0.0")
        self.pinned_commit = self._git("rev-parse", "HEAD").decode().strip()
        self.pinned_manifest = hashlib.sha256((self.repo / "RELEASE-MANIFEST.json").read_bytes()).hexdigest()
        self._git("tag", "-a", "v1.0.0", "-m", "ai-comm 1.0.0 manifest sha256 " + self.pinned_manifest)
        self.target_protocol = b"new protocol contract\n"
        self._git("switch", "-c", "target")
        self._write_package("1.1.0", b'{"release":"1.1.0"}\n', self.target_protocol)
        self._git("add", "ai_comm.py", "RELEASE-MANIFEST.json", "PROTOCOL.md")
        self._git("commit", "-m", "target 1.1.0")
        self.target_commit = self._git("rev-parse", "HEAD").decode().strip()
        self.target_manifest = hashlib.sha256((self.repo / "RELEASE-MANIFEST.json").read_bytes()).hexdigest()
        self._git("tag", "-a", "v1.1.0", "-m", "ai-comm 1.1.0 manifest sha256 " + self.target_manifest)
        self._git("switch", "main")
        self._git("switch", "-c", "release-index", self.target_commit)
        self.index = {"format": "ai-comm-releases/1", "releases": [
            {"package_version": "1.0.0", "commit": self.pinned_commit, "tag": "v1.0.0",
             "manifest_sha256": self.pinned_manifest,
             "protocol_sha256": hashlib.sha256(self.protocol).hexdigest(),
             "summary": "Pinned release", "advisory": "required",
             "reason": "A marker string is present only in channel content.",
             "affects": ">=1.0.0,<1.1.0", "fixed_in": "1.1.0"},
            {"package_version": "1.1.0", "commit": self.target_commit, "tag": "v1.1.0",
             "manifest_sha256": self.target_manifest,
             "protocol_sha256": hashlib.sha256(self.target_protocol).hexdigest(),
             "summary": "Target release", "advisory": "optional",
             "reason": "No advisory recorded for this release."},
        ]}
        (self.repo / "RELEASES.json").write_bytes(ac.canonical(self.index))
        self._git("add", "RELEASES.json")
        self._git("commit", "-m", "release index")
        self._git("update-ref", "refs/remotes/origin/release-index", "HEAD")
        self._git("switch", "main")
        self.channel_path = self.root / 'migration channel ;$(literal) `quoted` "double"\nline'
        ac.Channel.init(self.channel_path, "Marker: USER_NOTICE_SECRET", ["alpha", "beta"])
        self.channel = ac.Channel(self.channel_path)
        self.channel.join("alpha", "codex", "alpha-session")
        self.channel.join("beta", "claude", "beta-session")
        self.channel.join("guest", "codex", "guest-session")

    def tearDown(self):
        self.temp.cleanup()

    def _git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=getattr(self, "env", os.environ.copy()), timeout=30).stdout

    def _write_package(self, version, manifest, protocol):
        (self.repo / "ai_comm.py").write_text(f'VERSION = "{version}"\n', encoding="utf-8")
        (self.repo / "RELEASE-MANIFEST.json").write_bytes(manifest)
        (self.repo / "PROTOCOL.md").write_bytes(protocol)

    def _channel_tree_hash(self):
        digest = hashlib.sha256()
        for path in sorted(self.channel_path.rglob("*")):
            if path.is_file():
                digest.update(path.relative_to(self.channel_path).as_posix().encode())
                digest.update(path.read_bytes())
        return digest.hexdigest()

    def test_release_status_adds_fields_only_from_verified_digests(self):
        status = ac.release_status(self.repo)
        self.assertEqual("ok", status["status"])
        self.assertEqual((True, "new_channel_required"),
                         (status["advice"][0]["protocol_changed"], status["advice"][0]["channel_action"]))
        self.assertEqual((False, "unchanged"),
                         (status["active_advisories"][0]["protocol_changed"],
                          status["active_advisories"][0]["channel_action"]))
        missing = ac._annotate_release_entries([{"package_version": "9.9.9"}], [], "a" * 64)
        self.assertEqual((None, "unknown"),
                         (missing[0]["protocol_changed"], missing[0]["channel_action"]))
        for result_status in ("index_missing", "index_invalid", "index_inconsistent", "not_git_checkout"):
            with mock.patch.object(ac, "release_status", return_value={"status": result_status}):
                with self.assertRaises(ac.ProtocolError) as caught:
                    ac.upgrade_plan(directory=self.repo)
            self.assertEqual("upgrade_plan_unavailable", caught.exception.details["code"])
            self.assertEqual(result_status, caught.exception.details["status"])

    def test_upgrade_notice_fields_require_a_matching_verified_record(self):
        state = self.channel.snapshot()
        entry = ac.release_status(self.repo)["verified_releases"][-1]
        record = {"kind": "ai-comm-release", "package_version": entry["package_version"],
                  "commit": entry["commit"], "tag": "v1.1.0",
                  "manifest_sha256": entry["manifest_sha256"],
                  "protocol_sha256": entry["protocol_sha256"], "advisory": "optional",
                  "reason": entry["reason"]}
        raw = ac.canonical(record)
        digest = ac.sha(raw)
        state.artifacts[digest] = raw
        state._release_index_status = {"status": "ok"}
        state._verified_releases = {entry["package_version"]: entry}
        notice = state.release_notice({"act": "FACT", "topic": "upgrade", "artifact": digest,
                                       "summary": "Release notice"})
        self.assertEqual((True, "new_channel_required"),
                         (notice["protocol_changed"], notice["channel_action"]))
        state._verified_releases = {}
        unknown = state.release_notice({"act": "FACT", "topic": "upgrade", "artifact": digest,
                                        "summary": "Release notice"})
        self.assertEqual((None, "unknown"),
                         (unknown["protocol_changed"], unknown["channel_action"]))
        legacy = state.release_notice({"act": "FACT", "topic": "upgrade", "summary": "legacy"})
        self.assertEqual((None, "unknown"),
                         (legacy["protocol_changed"], legacy["channel_action"]))

    def test_upgrade_plan_is_read_only_and_returns_arg_argv_and_notice_data(self):
        before_repo = (self._git("rev-parse", "HEAD"), self._git("status", "--porcelain"))
        before_channel = self._channel_tree_hash()
        runtime = self.channel_path / "ai-comm-runtime"
        self.assertTrue(runtime.exists())
        plan = ac.upgrade_plan(directory=self.repo, channel_path=str(self.channel_path))
        self.assertEqual(("1.0.0", "1.1.0"), (plan["from"], plan["to"]))
        self.assertEqual((True, "new_channel_required"),
                         (plan["protocol_changed"], plan["channel_action"]))
        self.assertEqual("required", plan["applicable_advisory"]["advisory"])
        self.assertEqual("1.1.0", plan["fixed_in"])
        self.assertNotIn("USER_NOTICE_SECRET", plan["owner_notice"])
        self.assertIn("changed, so a new channel is needed", plan["owner_notice"])
        self.assertIn("python3 scripts/check_tag.py v1.1.0", plan["owner_notice"])
        self.assertIn("update the project's activation note, announce on the channel", plan["owner_notice"])
        self.assertTrue(all(isinstance(step, list) and all(isinstance(arg, str) for arg in step)
                            for step in plan["repin_steps"]))
        self.assertIn(str(self.repo), plan["repin_steps"][0])
        self.assertEqual(["alpha", "beta", "guest"], plan["migration"]["participants_outstanding"])
        self.assertEqual([], plan["migration"]["participants_unjoined"])
        self.assertFalse(plan["migration"]["complete"])
        self.assertNotIn("participants", plan["migration"].get("handoff", {}))
        partial = ac.upgrade_plan(directory=self.repo, channel_path=str(self.channel_path),
                                  participant_values=["alpha=will_upgrade"])
        self.assertFalse(partial["migration"]["complete"])
        self.assertEqual(["beta", "guest"], partial["migration"]["participants_outstanding"])
        self.assertEqual(before_repo, (self._git("rev-parse", "HEAD"), self._git("status", "--porcelain")))
        self.assertEqual(before_channel, self._channel_tree_hash())
        self.assertTrue(runtime.exists())

    def test_explicit_participant_statuses_create_only_a_valid_handoff(self):
        plan = ac.upgrade_plan(directory=self.repo, channel_path=str(self.channel_path),
                               participant_values=["alpha=will_upgrade", "beta=will_upgrade",
                                                   "guest=stays_behind"])
        migration = plan["migration"]
        self.assertTrue(migration["complete"])
        self.assertFalse(migration["reviewer_join_required"])
        self.assertEqual(ac.sha(ac.canonical(migration["handoff"])), migration["handoff_sha256"])
        self.assertEqual(["alpha", "beta", "guest"],
                         [p["handle"] for p in migration["handoff"]["participants"]])
        ac.validate_handoff(migration["handoff"], target_protocol_digest=plan["protocol_sha256"])
        self.assertTrue(all(step["kind"] in {"manual", "command"} for step in migration["steps"]))
        self.assertTrue(all((step["kind"] == "manual" and isinstance(step.get("text"), str)) or
                            (step["kind"] == "command" and isinstance(step.get("argv"), list) and
                             all(isinstance(arg, str) for arg in step["argv"]))
                            for step in migration["steps"]))
        self.assertEqual(["manual"] * 4 + ["command"],
                         [step["kind"] for step in migration["steps"]])
        init_args = migration["steps"][-1]["argv"]
        self.assertIn(str(self.channel_path), init_args)
        self.assertIn('$(literal)', str(self.channel_path))
        self.assertIn('`quoted`', str(self.channel_path))
        self.assertIn('"double"\nline', str(self.channel_path))
        cases = [
            (["alpha=will_upgrade", "alpha=will_upgrade"], "upgrade_participant_duplicate"),
            (["ghost=will_upgrade"], "upgrade_participant_unknown"),
            (["alpha=maybe"], "upgrade_participant_status_invalid"),
            (["alpha=stays_behind"], "upgrade_reviewer_stays_behind"),
            (["malformed"], "upgrade_participant_malformed"),
        ]
        for values, expected in cases:
            with self.subTest(values=values), self.assertRaises(ac.ProtocolError) as caught:
                ac.upgrade_plan(directory=self.repo, channel_path=str(self.channel_path),
                               participant_values=values)
            self.assertEqual(expected, caught.exception.details["code"])

    def test_unjoined_reviewer_is_visible_and_blocks_handoff_until_joined(self):
        path = self.root / "unjoined-reviewer"
        ac.Channel.init(path, "Review a migration", ["alpha", "beta"])
        channel = ac.Channel(path)
        channel.join("alpha", "codex", "alpha-only")
        plan = ac.upgrade_plan(directory=self.repo, channel_path=str(path))
        migration = plan["migration"]
        self.assertFalse(migration["complete"])
        self.assertEqual(["alpha", "beta"], migration["participants_outstanding"])
        self.assertEqual(["beta"], migration["participants_unjoined"])
        self.assertTrue(migration["reviewer_join_required"])
        partial = ac.upgrade_plan(directory=self.repo, channel_path=str(path),
                                  participant_values=["alpha=will_upgrade"])
        self.assertEqual(["beta"], partial["migration"]["participants_outstanding"])
        with self.assertRaises(ac.ProtocolError) as caught:
            ac.upgrade_plan(directory=self.repo, channel_path=str(path),
                            participant_values=["alpha=will_upgrade", "beta=will_upgrade"])
        self.assertEqual("upgrade_participant_missing", caught.exception.details["code"])
        channel.join("beta", "claude", "beta-session")
        complete = ac.upgrade_plan(directory=self.repo, channel_path=str(path),
                                   participant_values=["alpha=will_upgrade", "beta=will_upgrade"])
        self.assertTrue(complete["migration"]["complete"])

    def test_upgrade_plan_docs_define_fields_and_unknown_is_not_unchanged(self):
        docs = (Path(ac.__file__).resolve().parent / "docs" / "RELEASES.md").read_text()
        for field in ("protocol_changed", "channel_action", "participants_outstanding",
                      "repin_steps", "owner_notice", "migration.complete"):
            self.assertIn(field, docs)
        self.assertIn("Unknown is\n  never treated as unchanged", docs)
        self.assertIn("publisher-provided free text", docs)
        self.assertIn('"kind":"manual"', docs)

    def test_plan_target_validation_and_cli_channel_option_positions(self):
        cases = [("not-a-version", "invalid_upgrade_target"),
                 ("9.9.9", "upgrade_target_unverified"),
                 ("1.0.0", "upgrade_target_not_newer")]
        for target, expected in cases:
            with self.subTest(target=target), self.assertRaises(ac.ProtocolError) as caught:
                ac.upgrade_plan(target, directory=self.repo)
            self.assertEqual(expected, caught.exception.details["code"])
        args_before = ac.parser().parse_args(["--channel", str(self.channel_path), "upgrade-plan"])
        args_after = ac.parser().parse_args(["upgrade-plan", "--channel", str(self.channel_path)])
        self.assertEqual(str(self.channel_path), args_before.channel)
        self.assertEqual(str(self.channel_path), args_after.channel)
        with self.assertRaises(ac.ProtocolError) as caught:
            ac.upgrade_plan(directory=self.repo, participant_values=["alpha=will_upgrade"])
        self.assertEqual("upgrade_participant_requires_channel", caught.exception.details["code"])

    def test_unchanged_protocol_needs_no_migration_and_rejects_participant_flags(self):
        self._git("switch", "target")
        self._write_package("1.1.0", b'{"release":"1.1.0"}\n', self.protocol)
        self._git("add", "ai_comm.py", "RELEASE-MANIFEST.json", "PROTOCOL.md")
        self._git("commit", "-m", "target same protocol")
        target_commit = self._git("rev-parse", "HEAD").decode().strip()
        target_manifest = hashlib.sha256((self.repo / "RELEASE-MANIFEST.json").read_bytes()).hexdigest()
        self._git("tag", "-d", "v1.1.0")
        self._git("tag", "-a", "v1.1.0", "-m", "ai-comm 1.1.0 manifest sha256 " + target_manifest)
        self.index["releases"][1].update(commit=target_commit, manifest_sha256=target_manifest,
                                        protocol_sha256=hashlib.sha256(self.protocol).hexdigest())
        self._git("switch", "release-index")
        (self.repo / "RELEASES.json").write_bytes(ac.canonical(self.index))
        self._git("add", "RELEASES.json")
        self._git("commit", "-m", "refresh release index")
        self._git("update-ref", "refs/remotes/origin/release-index", "HEAD")
        self._git("switch", "main")
        plan = ac.upgrade_plan(directory=self.repo, channel_path=str(self.channel_path))
        self.assertEqual((False, "unchanged"), (plan["protocol_changed"], plan["channel_action"]))
        self.assertNotIn("migration", plan)
        with self.assertRaises(ac.ProtocolError) as caught:
            ac.upgrade_plan(directory=self.repo, channel_path=str(self.channel_path),
                            participant_values=["alpha=will_upgrade"])
        self.assertEqual("upgrade_participant_not_required", caught.exception.details["code"])

    def test_channel_path_rejects_symlinks_and_non_channels(self):
        link = self.root / "channel-link"
        link.symlink_to(self.channel_path, target_is_directory=True)
        with self.assertRaises(ac.ProtocolError) as caught:
            ac.upgrade_plan(directory=self.repo, channel_path=str(link))
        self.assertEqual("upgrade_channel_symlink", caught.exception.details["code"])
        non_bare = self.root / "ordinary-directory"
        non_bare.mkdir()
        with self.assertRaises(ac.ProtocolError) as caught:
            ac.upgrade_plan(directory=self.repo, channel_path=str(non_bare))
        self.assertEqual("upgrade_channel_invalid", caught.exception.details["code"])
        with self.assertRaises(ac.ProtocolError) as caught:
            ac.upgrade_plan(directory=self.repo, channel_path="~codex-no-such-user/channel")
        self.assertEqual("upgrade_channel_invalid", caught.exception.details["code"])
        with self.assertRaises(ac.ProtocolError) as caught:
            ac.Channel("~codex-no-such-user/channel")
        self.assertEqual("channel_does_not_exist_initialize_it_explicitly",
                         caught.exception.details["code"])

        parent_link = self.root / "parent-link"
        parent_link.symlink_to(self.root, target_is_directory=True)
        through_parent = parent_link / self.channel_path.name
        with self.assertRaises(ac.ProtocolError) as caught:
            ac.upgrade_plan(directory=self.repo, channel_path=str(through_parent))
        self.assertEqual("upgrade_channel_symlink", caught.exception.details["code"])

        unrelated_bare = self.root / "bare-but-not-channel.git"
        subprocess.run(["git", "init", "--bare", str(unrelated_bare)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        with self.assertRaises(ac.ProtocolError) as caught:
            ac.upgrade_plan(directory=self.repo, channel_path=str(unrelated_bare))
        self.assertEqual("upgrade_channel_invalid", caught.exception.details["code"])

    def test_upgrade_plan_never_executes_network_git_operations(self):
        wrapper_dir = self.root / "git-wrapper"
        wrapper_dir.mkdir()
        log = self.root / "git-calls.jsonl"
        real_git = shutil.which("git")
        wrapper = wrapper_dir / "git"
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            f"real = {real_git!r}\n"
            f"log = {str(log)!r}\n"
            "args = sys.argv[1:]\n"
            "with open(log, 'a', encoding='utf-8') as stream:\n"
            "    stream.write(json.dumps({'args': args, 'lazy': os.environ.get('GIT_NO_LAZY_FETCH')}) + '\\n')\n"
            "if any(arg in {'fetch', 'clone', 'push', 'pull'} for arg in args):\n"
            "    sys.exit(91)\n"
            "os.execv(real, ['git', *args])\n", encoding="utf-8")
        wrapper.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(wrapper_dir) + os.pathsep + os.environ["PATH"]}):
            plan = ac.upgrade_plan(directory=self.repo, channel_path=str(self.channel_path))
        self.assertEqual("new_channel_required", plan["channel_action"])
        calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(calls)
        self.assertTrue(all(call["lazy"] == "1" for call in calls))
        self.assertFalse(any(arg in {"fetch", "clone", "push", "pull"}
                             for call in calls for arg in call["args"]))


if __name__ == "__main__":
    unittest.main()
