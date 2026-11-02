"""Tests for the per-user cache's objects outside the cache and its own offers
(018c sections 8.2, 8.3, 9, 10.2 and 10.3): skill registration, the launcher and
project_cli.py, update offers, the origin rule, and uninstall of owned objects.
Every test uses temporary directories; the real home is never read or written."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import install_user as iu  # noqa: E402
import test_install_project as tip  # noqa: E402
import test_verify_pin as tvp  # noqa: E402

DAY = 24 * 3600


class ObjectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory(prefix="uo-src-")
        cls.source, cls.releases = tip.build_source(Path(cls.shared.name).resolve())

    @classmethod
    def tearDownClass(cls):
        cls.shared.cleanup()

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="uo-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        os.chmod(self.base, 0o700)
        self.data = self.base / "data"
        self.root = self.data / "ai-comm"
        self.skills = self.base / "skills"
        self.skills.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()

    def src(self, origin=None):
        return {"kind": "git", "origin": str(origin or self.source), "version": "1.5.3",
                "manifest_sha256": "0" * 64}

    def install(self, version="1.5.1", **kw):
        return iu.install(version, fetch_permitted=True, use_helper=False, root=self.root,
                          source=kw.pop("source", None) or self.src(), run_suite=lambda repo: 7, **kw)

    def stop(self, fn, *a, **kw):
        with self.assertRaises(iu.Stop) as ctx:
            fn(*a, **kw)
        return ctx.exception

    def skill(self, directory, name, skill_name):
        path = directory / name
        path.mkdir()
        (path / "SKILL.md").write_text(f"---\nname: {skill_name}\ndescription: x\n---\nbody\n")
        return path

    def owned(self):
        return json.loads((self.root / "state.json").read_text())["owned"]

    # -- effective names

    def test_frontmatter_name(self):
        cases = {b"---\nname: ai-comm\n---\n": "ai-comm", b"---\nname: 'ai-comm'\n---\n": "ai-comm",
                 b'---\ndescription: d\nname: "x y"\n---\n': "x y", b"name: ai-comm\n": None,
                 b"---\ndescription: d\n---\nname: ai-comm\n": None, b"": None}
        for data, name in cases.items():
            with self.subTest(data=data):
                self.assertEqual(iu.frontmatter_name(data), name)

    def test_effective_names_follow_one_link_level_only(self):
        real = self.skill(self.base, "real", "ai-comm")
        os.symlink(real, self.skills / "direct")
        os.symlink(self.skills / "direct", self.skills / "chained")
        (self.skills / "plain-file").write_text("x\n")
        os.symlink(self.base / "nowhere", self.skills / "dangling")
        names = dict((p.name, n) for p, n in iu.effective_names(self.skills))
        self.assertEqual(names, {"direct": "ai-comm", "chained": None, "dangling": None})

    # -- registration

    def test_register_links_the_entry_skill_and_is_idempotent(self):
        self.install()
        out = iu.register(str(self.skills), root=self.root)
        link = self.skills / "ai-comm"
        self.assertEqual((out["status"], os.readlink(link)), ("registered", str(self.root / "current/entry/ai-comm")))
        self.assertEqual(iu.frontmatter_name((link / "SKILL.md").read_bytes()), "ai-comm")
        self.assertEqual(self.owned(), [{"kind": "skill_link", "path": str(link), "target": os.readlink(link)}])
        self.assertEqual(iu.register(str(self.skills), root=self.root)["status"], "already_registered")
        self.assertEqual(len(self.owned()), 1)

    def test_alias_and_unrelated_content_are_conflicts_and_nothing_changes(self):
        self.install()
        self.skill(self.skills, "comm", "ai-comm")
        err = self.stop(iu.register, str(self.skills), root=self.root)
        self.assertEqual((err.code, err.details["entries"]), ("skill_name_conflict", [str(self.skills / "comm")]))
        self.assertFalse(os.path.lexists(self.skills / "ai-comm"))
        other = self.base / "other"
        other.mkdir()
        self.skill(other, "ai-comm", "something-else")
        err = self.stop(iu.register, str(other), root=self.root)
        self.assertEqual(err.details["entries"], [str(other / "ai-comm")])
        self.assertEqual(self.owned(), [])

    def test_second_directory_gets_its_link_with_a_warning_and_aliases_anywhere_conflict(self):
        self.install()
        iu.register(str(self.skills), root=self.root)
        second = self.base / "second"
        second.mkdir()
        out = iu.register(str(second), root=self.root)
        self.assertEqual(len(out["registered"]), 2)
        self.assertIn("duplicate", out["warning"])
        self.skill(self.skills, "alias", "ai-comm")  # an alias in an already registered directory
        third = self.base / "third"
        third.mkdir()
        err = self.stop(iu.register, str(third), root=self.root)
        self.assertEqual(err.details["entries"], [str(self.skills / "alias")])

    def test_changed_link_is_no_longer_ours(self):
        self.install()
        iu.register(str(self.skills), root=self.root)
        link = self.skills / "ai-comm"
        os.unlink(link)
        mine = self.skill(self.base, "mine", "ai-comm")
        os.symlink(mine, link)
        self.assertEqual(self.stop(iu.register, str(self.skills), root=self.root).code, "skill_name_conflict")
        out = iu.unregister(str(self.skills), root=self.root)
        self.assertEqual((out["status"], out["kept"]), ("partially_removed", [str(link)]))
        self.assertEqual(os.readlink(link), str(mine))
        self.assertEqual(self.owned(), [])

    def test_unregister_removes_only_the_exact_link_and_keeps_the_directory(self):
        self.install()
        iu.register(str(self.skills), root=self.root)
        out = iu.unregister(root=self.root)
        self.assertEqual(out["removed"], [str(self.skills / "ai-comm")])
        self.assertTrue(self.skills.is_dir())
        self.assertEqual(self.stop(iu.unregister, root=self.root).code, "not_registered")

    def test_register_needs_a_release_with_the_entry_skill_and_a_real_directory(self):
        self.assertEqual(self.stop(iu.register, str(self.skills), root=self.root).code, "cache_missing")
        self.install()
        with mock.patch.object(iu, "ENTRY_SKILL", ("entry", "missing", "SKILL.md")):
            self.assertEqual(self.stop(iu.register, str(self.skills), root=self.root).code,
                             "release_has_no_entry_skill")
        self.assertEqual(self.stop(iu.register, "relative/dir", root=self.root).code, "skills_dir_invalid")
        os.symlink(self.skills, self.base / "skills-link")
        self.assertEqual(self.stop(iu.register, str(self.base / "skills-link"), root=self.root).code,
                         "skills_dir_invalid")

    def test_current_never_switches_to_a_release_without_what_owned_objects_reach(self):
        self.install("1.5.1")
        self.install("1.5.0")
        iu.activate("1.5.0", root=self.root)  # current 1.5.0, previous 1.5.1
        iu.register(str(self.skills), root=self.root)
        iu.install_launcher(str(self.bin), root=self.root)
        with mock.patch.object(iu, "ENTRY_SKILL", ("entry", "missing", "SKILL.md")):
            self.assertEqual(self.stop(iu.activate, "1.5.1", root=self.root).code, "release_has_no_entry_skill")
            self.assertEqual(self.stop(iu.rollback, root=self.root).code, "release_has_no_entry_skill")
        iu.unregister(root=self.root)
        real = Path.is_file
        with mock.patch.object(Path, "is_file", lambda p: False if p.name == "project_cli.py" else real(p)):
            self.assertEqual(self.stop(iu.rollback, root=self.root).code, "release_has_no_launcher")
        self.assertEqual(os.readlink(self.root / "current"), "releases/1.5.0")
        iu.remove_launcher(root=self.root)
        self.assertEqual(iu.rollback(root=self.root)["current"], "1.5.1")

    def test_activating_install_refuses_a_release_without_the_entry_skill(self):
        self.install("1.5.1", activate=True)
        iu.register(str(self.skills), root=self.root)
        with mock.patch.object(iu, "ENTRY_SKILL", ("entry", "missing", "SKILL.md")):
            err = self.stop(self.install, "1.5.0", activate=True)
        self.assertEqual(err.code, "release_has_no_entry_skill")
        self.assertFalse((self.root / "releases" / "1.5.0").exists())
        self.assertEqual([p for p in (self.root / "staging").glob("install-*") if (p / "repo").exists()], [],
                         "no staged checkout is left")
        self.assertEqual(iu.status(root=self.root)["pending"], [])

    # -- launcher

    def test_launcher_is_written_once_and_removed_only_if_unchanged(self):
        self.install()
        out = iu.install_launcher(str(self.bin), root=self.root)
        path = self.bin / "ai-comm"
        self.assertEqual((out["status"], os.stat(path).st_mode & 0o777), ("launcher_installed", 0o755))
        self.assertEqual(path.read_bytes(), iu.launcher_script(self.root))
        self.assertEqual(self.stop(iu.install_launcher, str(self.bin), root=self.root).code, "launcher_path_taken")
        self.assertEqual(iu.remove_launcher(root=self.root)["removed"], [str(path)])
        iu.install_launcher(str(self.bin), root=self.root)
        path.write_text("#!/bin/sh\necho mine\n")
        self.assertEqual(iu.remove_launcher(root=self.root)["kept"], [str(path)])
        self.assertEqual(path.read_text(), "#!/bin/sh\necho mine\n")
        self.assertEqual(sorted(p.name for p in self.bin.iterdir()), ["ai-comm"])

    def launch(self, cwd, *args, path_prefix=None):
        env = {"PATH": (f"{path_prefix}:" if path_prefix else "") + os.environ.get("PATH", "/usr/bin:/bin"),
               "HOME": str(self.base), "XDG_DATA_HOME": str(self.data)}
        return subprocess.run([str(self.bin / "ai-comm"), *args], cwd=cwd, env=env, capture_output=True,
                              timeout=120, check=False)

    def project(self):
        project = self.base / "project"
        project.mkdir()
        tvp.make_pin(project, self.source, "1.5.1")
        return project

    def test_launcher_runs_a_verified_pin_from_a_quoted_cache_path(self):
        self.data = self.base / "data dir $HOME 'q' \"d\" ;&|`x`"
        self.root = self.data / "ai-comm"
        self.install()
        iu.install_launcher(str(self.bin), root=self.root)
        project = self.project()
        (project / "sub").mkdir()
        done = self.launch(project / "sub", "--version")
        self.assertEqual((done.returncode, done.stdout.decode().strip()), (0, "1.5.1"), done.stderr)

    def test_launcher_refuses_an_unverified_pin_without_running_it(self):
        self.install()
        iu.install_launcher(str(self.bin), root=self.root)
        project = self.project()
        sentinel = self.base / "pin-ran"
        (project / ".tools/ai-comm/ai_comm.py").write_text(f"open({str(sentinel)!r}, 'w').write('x')\n")
        done = self.launch(project, "--version")
        self.assertEqual(done.returncode, 2)
        report = json.loads(done.stderr)
        self.assertEqual((report["status"], report["what"]), ("modified", "content"))
        self.assertFalse(sentinel.exists())
        elsewhere = self.base / "no-project"
        elsewhere.mkdir()
        self.assertEqual(json.loads(self.launch(elsewhere, "--version").stderr)["status"], "absent")

    def test_launcher_resolves_current_once(self):
        self.install("1.5.1")
        self.install("1.5.0")
        iu.install_launcher(str(self.bin), root=self.root)
        stub = self.base / "stub"
        stub.mkdir()
        (stub / "python3").write_text(f"#!/bin/sh\nln -sfn releases/1.5.0 '{self.root}/current'\nprintf '%s\\n' \"$@\"\n")
        (stub / "python3").chmod(0o700)
        done = self.launch(self.base, "--version", path_prefix=stub)
        argv = done.stdout.decode().splitlines()
        self.assertEqual(argv[:2], ["-I", f"{self.root}/releases/1.5.1/scripts/project_cli.py"])
        self.assertEqual(os.readlink(self.root / "current"), "releases/1.5.0", "the switch happened meanwhile")

    def test_launcher_refuses_a_current_link_outside_releases(self):
        self.install()
        iu.install_launcher(str(self.bin), root=self.root)
        for bad in ("releases/1.5.1/../../elsewhere", "elsewhere", "releases/x/1.5.1"):
            with self.subTest(link=bad):
                os.unlink(self.root / "current")
                os.symlink(bad, self.root / "current")
                done = self.launch(self.base, "--version")
                self.assertEqual((done.returncode, json.loads(done.stderr)["code"]), (2, "cache_link_invalid"))

    def test_project_cli_outside_a_cache_and_a_changed_cached_release(self):
        done = subprocess.run([sys.executable, "-I", str(ROOT / "scripts" / "project_cli.py"), "--version"],
                              capture_output=True, timeout=60, check=False)
        self.assertEqual((done.returncode, json.loads(done.stderr)["code"]), (2, "not_a_cache_release"))
        self.install()
        iu.install_launcher(str(self.bin), root=self.root)
        project = self.project()
        sentinel = self.base / "changed-verifier-ran"
        (self.root / "releases/1.5.1/scripts/verify_pin.py").write_text(
            f"open({str(sentinel)!r}, 'w').write('x')\nraise SystemExit(0)\n")
        done = self.launch(project, "--version")
        report = json.loads(done.stderr)
        self.assertEqual((done.returncode, report["code"], report["path"]),
                         (2, "launcher_release_changed", "scripts/verify_pin.py"))
        self.assertFalse(sentinel.exists(), "a changed verifier never runs")

    # -- offers

    def refreshed(self, now=1000):
        self.install("1.5.0")
        iu.check(fetch_permitted=True, root=self.root, source=self.src(), now=lambda: now)

    def claim(self, session, now=1000):
        return iu.offer_claim(session, root=self.root, now=lambda: now)

    def answer(self, session, answer, now=1000, version="1.5.1"):
        return iu.offer_answer(session, version, answer, root=self.root, now=lambda: now)

    def test_two_sequential_sessions_see_one_offer(self):
        self.refreshed()
        self.assertEqual(self.claim("session-a")["status"], "claimed")
        self.assertEqual(self.claim("session-b", 1001)["status"], "claimed_by_other")
        self.assertEqual(self.claim("session-a", 1002)["status"], "claimed")
        self.assertEqual(iu.offer_status(root=self.root, now=lambda: 1003)["claim"]["session"], "session-a")

    def test_two_concurrent_sessions_are_serialized(self):
        self.refreshed()
        fd = os.open(self.root / "lock", os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.assertEqual(self.stop(self.claim, "session-b").code, "cache_locked")
        fcntl.flock(fd, fcntl.LOCK_UN)
        self.assertEqual(self.claim("session-a")["status"], "claimed")
        self.assertEqual(self.claim("session-b")["status"], "claimed_by_other")

    def test_abandoned_claim_may_be_reclaimed(self):
        self.refreshed()
        self.claim("session-a")
        self.assertEqual(self.claim("session-b", 1000 + DAY)["status"], "claimed")

    def test_not_now_snoozes_with_growing_steps(self):
        self.refreshed()
        self.claim("session-a")
        self.assertEqual(self.answer("session-a", "not_now")["snooze_until"], 1000 + DAY)
        self.assertEqual(self.claim("session-b", 1000 + DAY - 1), {"status": "suppressed", "version": "1.5.1",
                                                                    "reason": "snoozed"})
        self.assertEqual(self.claim("session-b", 1000 + DAY)["status"], "claimed")
        self.assertEqual(self.answer("session-b", "not_now", 1000 + DAY)["snooze_until"], 1000 + 3 * DAY)

    def test_skip_never_and_yes(self):
        for answer, reason in (("skip", "skipped"), ("never", "never"), ("yes", "answered"),
                               ("fetch_only", "answered")):
            with self.subTest(answer=answer):
                self.refreshed()
                self.claim("session-a")
                out = self.answer("session-a", answer)
                if answer == "yes":
                    self.assertEqual(out["permits"], ["install --version 1.5.1 --fetch-permitted --no-activate",
                                                      "activate 1.5.1"])
                if answer == "fetch_only":
                    self.assertEqual(out["permits"], ["install --version 1.5.1 --fetch-permitted --no-activate"])
                self.assertEqual(self.claim("session-b", 2000)["reason"], reason)
                shutil.rmtree(self.root)

    def test_only_the_notifier_answers_its_own_offer(self):
        self.refreshed()
        self.claim("session-a")
        self.assertEqual(self.stop(self.answer, "session-b", "yes").code, "offer_not_claimed")
        self.assertEqual(self.stop(self.answer, "session-a", "yes", version="1.5.0").code, "offer_not_claimed")
        self.assertEqual(self.stop(self.answer, "session-a", "yes", 1000 + DAY).code, "offer_not_claimed")
        self.assertEqual(self.stop(self.claim, "bad session").code, "invalid_session")

    def test_missing_or_stale_metadata_is_never_up_to_date(self):
        self.install("1.5.1")
        self.assertEqual(iu.check(root=self.root)["verdict"], "up_to_date")
        index = json.loads((self.root / "index.json").read_text())
        for refreshed, metadata in ((1, "stale"), (10 ** 12, "stale"), (None, "stale")):
            with self.subTest(refreshed=refreshed):
                iu.Cache(self.root).write_index(index["releases"], refreshed)
                out = iu.check(root=self.root)
                self.assertEqual((out["metadata"], out["verdict"]), (metadata, "unknown"))
        (self.root / "index.json").unlink()
        out = iu.check(root=self.root)
        self.assertEqual((out["metadata"], out["verdict"]), ("missing", "unknown"))
        self.assertEqual(self.claim("session-a")["status"], "no_offer")

    def test_changed_origin_stops(self):
        self.install("1.5.0")
        other = self.base / "other-origin"
        shutil.copytree(self.source, other, symlinks=True)
        err = self.stop(iu.check, fetch_permitted=True, root=self.root, source=self.src(other))
        self.assertEqual((err.code, err.details["recorded"]), ("origin_changed", str(self.source)))
        self.assertEqual(self.stop(self.install, "1.5.1", source=self.src(other)).code, "origin_changed")
        self.assertEqual(iu.status(root=self.root)["origin"], str(self.source))

    # -- uninstall and interruption

    def test_uninstall_removes_owned_objects_and_keeps_changed_ones(self):
        self.install()
        iu.register(str(self.skills), root=self.root)
        iu.install_launcher(str(self.bin), root=self.root)
        out = iu.uninstall(confirm=True, root=self.root)
        self.assertEqual(out["status"], "uninstalled")
        self.assertFalse(os.path.lexists(self.skills / "ai-comm") or os.path.lexists(self.bin / "ai-comm"))
        self.assertTrue(self.skills.is_dir() and self.bin.is_dir())
        self.install()
        iu.register(str(self.skills), root=self.root)
        iu.install_launcher(str(self.bin), root=self.root)
        (self.bin / "ai-comm").write_text("#!/bin/sh\necho mine\n")
        os.unlink(self.skills / "ai-comm")
        os.symlink(self.base, self.skills / "ai-comm")
        out = iu.uninstall(confirm=True, root=self.root)
        self.assertEqual(out["kept"], [str(self.bin / "ai-comm"), str(self.skills / "ai-comm")])
        self.assertEqual((self.bin / "ai-comm").read_text(), "#!/bin/sh\necho mine\n")
        self.assertEqual(out["status"], "partially_uninstalled")
        self.assertFalse(self.root.exists(), "the kept objects are outside the cache and no longer recorded")

    def interrupt_each(self, setup, operation, finish):
        real = iu.uc.Journal.append
        n = 1
        while True:
            setup()
            calls = [0]

            def flaky(journal, kind, data, n=n):
                calls[0] += 1
                if calls[0] == n:
                    raise KeyboardInterrupt
                return real(journal, kind, data)
            with self.subTest(boundary=n):
                with mock.patch.object(iu.uc.Journal, "append", flaky):
                    try:
                        operation()
                        interrupted = False
                    except KeyboardInterrupt:
                        interrupted = True
                if interrupted:
                    iu.recover(root=self.root)
                finish()
            if not interrupted:
                return n
            n += 1

    def fresh(self, register=False, launcher=False):
        shutil.rmtree(self.root, ignore_errors=True)
        for directory in (self.skills, self.bin):
            for child in directory.iterdir():
                child.unlink()
        self.install()
        if register:
            iu.register(str(self.skills), root=self.root)
        if launcher:
            iu.install_launcher(str(self.bin), root=self.root)

    def consistent(self):
        """Each object exists exactly when it is recorded, nothing is pending, no temp files."""
        status = iu.status(root=self.root)
        self.assertEqual(status["pending"], [])
        recorded = {o["path"] for o in status["owned"]}
        for path in (self.skills / "ai-comm", self.bin / "ai-comm"):
            self.assertEqual(os.path.lexists(path), str(path) in recorded, path)
        self.assertEqual([p.name for p in self.bin.iterdir() if p.name.endswith(".tmp")], [])

    def test_register_unregister_and_launcher_at_every_boundary(self):
        cases = {
            "register": (lambda: self.fresh(), lambda: iu.register(str(self.skills), root=self.root)),
            "unregister": (lambda: self.fresh(register=True), lambda: iu.unregister(root=self.root)),
            "launcher": (lambda: self.fresh(), lambda: iu.install_launcher(str(self.bin), root=self.root)),
            "launcher --remove": (lambda: self.fresh(launcher=True), lambda: iu.remove_launcher(root=self.root)),
        }
        for label, (setup, operation) in cases.items():
            with self.subTest(operation=label):
                self.assertGreater(self.interrupt_each(setup, operation, self.consistent), 2)

    def test_uninstall_with_owned_objects_at_every_boundary(self):
        def finish():
            if self.root.exists():
                status = iu.status(root=self.root)
                self.assertEqual(status["pending"], [])
                iu.uninstall(confirm=True, root=self.root)
            self.assertFalse(self.root.exists())
            self.assertFalse(os.path.lexists(self.skills / "ai-comm") or os.path.lexists(self.bin / "ai-comm"))
        self.assertGreater(self.interrupt_each(lambda: self.fresh(register=True, launcher=True),
                                               lambda: iu.uninstall(confirm=True, root=self.root), finish), 4)

    def test_replacement_between_interruption_and_recovery_is_kept(self):
        self.fresh()
        real = iu.uc.Journal.append

        def stop_before_outcome(journal, kind, data):
            if kind == "outcome":
                raise KeyboardInterrupt
            return real(journal, kind, data)
        with mock.patch.object(iu.uc.Journal, "append", stop_before_outcome):
            with self.assertRaises(KeyboardInterrupt):
                iu.register(str(self.skills), root=self.root)
        os.unlink(self.skills / "ai-comm")
        os.symlink(self.base, self.skills / "ai-comm")  # the user's own entry
        out = iu.recover(root=self.root)
        self.assertEqual([r["action"] for r in out["results"]], ["not_ours"])
        self.assertEqual(os.readlink(self.skills / "ai-comm"), str(self.base))
        self.assertEqual(iu.status(root=self.root)["owned"], [])


class EntrySkillTests(unittest.TestCase):
    """entry/ai-comm/SKILL.md: what it cites must exist (host neutrality is covered by
    test_neutrality, which reads every released file)."""

    def setUp(self):
        self.text = (ROOT / "entry" / "ai-comm" / "SKILL.md").read_text()

    def test_front_matter_name(self):
        self.assertEqual(iu.frontmatter_name(self.text.encode()), "ai-comm")

    def test_cited_files_and_commands_exist(self):
        import re
        paths = set(re.findall(r'\$REL/([A-Za-z0-9_./-]+)', self.text))
        self.assertTrue({"scripts/verify_pin.py", "scripts/declare_project.py", "install.sh"} <= paths, paths)
        for path in paths:
            with self.subTest(path=path):
                self.assertTrue((ROOT / path).is_file())
        for command in set(re.findall(r"--user ([a-z]+)", self.text)):
            with self.subTest(command=command):
                done = subprocess.run([sys.executable, "-I", str(ROOT / "scripts" / "install_user.py"), command,
                                       "--help"], capture_output=True, timeout=60, check=False)
                self.assertEqual(done.returncode, 0, command)
        for status in ("verified", "unknown", "modified", "absent", "controller", "controller-check-workaround",
                       "legacy-manual", "unsupported", "no_trusted_binding", "claimed_by_other", "permits"):
            with self.subTest(term=status):
                self.assertIn(f"`{status}`", self.text)


if __name__ == "__main__":
    unittest.main()
