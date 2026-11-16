"""1.11.1: guided-upgrade fetch on installer-made pins (plan r2, DECIDE 00000105).

Part 1: the project installer records the manifest-bound origin before publication, so
the pin's own controller fetch works. Part 2: `install.sh --user upgrade-fetch` fetches
exactly one release tag and the index into an existing pin without an origin.

Disclosed transport fixture: the fixture releases name `https://fixture.example/ai-comm.git`
as their RELEASE_ORIGIN, and a `git` wrapper first on PATH rewrites that URL, only in
`fetch` and `ls-remote` invocations, to a local bare repository. Every origin, consent,
binding, index and ref check runs unchanged; nothing is fetched from the network. A
"pre-1.11.1 pin" here is an installer pin whose origin key was removed, which is exactly
what the 1.10.0 and 1.11.0 installers publish.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import test_install_project as tip  # noqa: E402

ip = tip.ip
ri = ip.ri
uc, Stop = ri.uc, ri.Stop
import upgrade_fetch as uf  # noqa: E402
iu = uf.iu
URL = "https://fixture.example/ai-comm.git"
TARGET = "1.5.1"


def build_source(base, url=URL):
    """Fixture releases 1.5.0 and 1.5.1 of this tree, naming `url` as their origin."""
    src = base / "origin"
    src.mkdir()
    tip.git(src, "init", "-q", "--initial-branch=main")
    for name in tip.package_files(ROOT):
        dest = src / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, dest)
    releases = []

    def set_version(version):
        cli = src / "ai_comm.py"
        text = re.sub(r'^VERSION = "[^"]+"$', f'VERSION = "{version}"', cli.read_text(), flags=re.M)
        cli.write_text(re.sub(r'^RELEASE_ORIGIN = "[^"]+"$', f'RELEASE_ORIGIN = "{url}"', text, flags=re.M))
        manifest = json.loads((src / "RELEASE-MANIFEST.json").read_text())
        for entry in manifest["files"]:
            data = (src / entry["path"]).read_bytes()
            entry["bytes"], entry["sha256"] = len(data), tip.sha(data)
        (src / "RELEASE-MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
        tip.git(src, "add", "-A")
        tip.git(src, "commit", "-q", "--allow-empty", "-m", version)
        manifest_sha = tip.sha((src / "RELEASE-MANIFEST.json").read_bytes())
        tip.git(src, "tag", "-a", f"v{version}", "-m", f"ai-comm {version} (protocol 3.0); manifest sha256 {manifest_sha}")
        releases.append({"advisory": "optional", "reason": "No advisory recorded for this release.", "summary": "s",
                         "package_version": version, "tag": f"v{version}",
                         "commit": tip.git(src, "rev-parse", "HEAD").strip(), "manifest_sha256": manifest_sha,
                         "protocol_sha256": tip.sha((src / "PROTOCOL.md").read_bytes())})

    set_version("1.5.0")
    set_version("1.5.1")
    write_index(src, releases)
    bare = base / "remote.git"
    subprocess.run(["git", "clone", "-q", "--mirror", str(src), str(bare)], check=True, capture_output=True,
                   env=tip.ENV)
    return bare, {r["package_version"]: r for r in releases}


def write_index(src, releases):
    tip.git(src, "switch", "-q", "--orphan", "release-index")
    for child in src.iterdir():
        if child.name != ".git":
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    (src / "RELEASES.json").write_text(json.dumps({"format": "ai-comm-releases/1", "releases": releases},
                                                  sort_keys=True, separators=(",", ":")) + "\n")
    tip.git(src, "add", "RELEASES.json")
    tip.git(src, "commit", "-q", "-m", "index")
    tip.git(src, "switch", "-q", "main")


def git_redirect(bin_dir, url, target):
    """A `git` first on PATH that rewrites `url` to `target` in fetch/ls-remote argv only."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / "git"
    wrapper.write_text("#!/usr/bin/env python3\nimport os, sys\n"
                       f"real, url, target = {shutil.which('git')!r}, {url!r}, {str(target)!r}\n"
                       "args = sys.argv[1:]\n"
                       "if ('fetch' in args or 'ls-remote' in args) and url in args:\n"
                       "    args[args.index(url)] = target\n"
                       "os.execv(real, [real, *args])\n")
    wrapper.chmod(0o755)
    return mock.patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})


def origin_lines(repo):
    done = subprocess.run(["git", "-C", str(repo), "config", "--local", "--get-regexp", r"^remote\."],
                          capture_output=True, text=True, env=uc.clean_env(), check=False)
    return done.stdout.splitlines()


class Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory(prefix="uf-src-")
        cls.bare, cls.releases = build_source(Path(cls.shared.name).resolve())

    @classmethod
    def tearDownClass(cls):
        cls.shared.cleanup()

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="uf-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        os.chmod(self.base, 0o700)
        redirect = git_redirect(self.base / "bin", URL, self.bare)
        redirect.start()
        self.addCleanup(redirect.stop)
        self.cache_dir = self.base / "data" / "ai-comm"
        src = {"kind": "git", "origin": URL, "version": "1.5.1", "manifest_sha256": "0" * 64}
        iu.install("1.5.0", fetch_permitted=True, use_helper=False, root=self.cache_dir, source=src,
                   run_suite=lambda repo: 7)
        iu.check(fetch_permitted=True, root=self.cache_dir, source=src)
        self.project = self.base / "project"
        self.project.mkdir()
        self.pin = self.project / ".tools" / "ai-comm"

    def install_pin(self):
        out = ip.install(self.project, "1.5.0", fetch_permitted=True, use_helper=False, origin=URL,
                         run_suite=lambda repo: 7)
        self.assertEqual(out["status"], "installed")
        self.channel = self.project / ".ai-comm"
        self.cli("init", "--task", "t", "--reviewers", "agent-a", "agent-b")
        self.cli("join", "--as", "agent-a", "--platform", "assistant", "--session", "s-a")

    def cli(self, *args):
        done = subprocess.run([sys.executable, "-I", str(self.pin / "ai_comm.py"), "--channel", str(self.channel),
                               *args], capture_output=True, env=uc.clean_env(), timeout=120, check=False)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return json.loads(done.stdout)

    def controller(self, *args):
        """The pin's own guided-upgrade controller, as a separate process."""
        done = subprocess.run([sys.executable, "-I", str(self.pin / "ai_comm.py"), "upgrade", "--project",
                               str(self.project), *args], capture_output=True, env=uc.clean_env(), timeout=300,
                              check=False)
        return done.returncode, json.loads(done.stdout)

    def consent(self, answer="yes", target=TARGET, commit=None, tag_object=None):
        args = ["record-answer", "--answer", answer, "--target", target,
                "--target-commit", commit or self.releases[target]["commit"],
                "--source", URL, "--evidence", "owner yes in a test"]
        if tag_object:
            args += ["--target-tag-object", tag_object]
        code, out = self.controller(*args)
        self.assertEqual(code, 0, out)
        return out["consent"]

    def unrecord_origin(self):
        """Make the pin what the 1.10.0 and 1.11.0 installers publish: no origin key."""
        tip.git(self.pin, "config", "--local", "--unset-all", "remote.origin.url")

    def stop(self, fn, *a, **kw):
        with self.assertRaises(Stop) as ctx:
            fn(*a, **kw)
        return ctx.exception

    def refs(self):
        return {name: uf.ref_value(self.pin, name) for name in (f"refs/tags/v{TARGET}",
                                                                 "refs/remotes/origin/release-index")}

    def fetch(self, consent, **kw):
        return uf.upgrade_fetch(self.project, TARGET, consent, cache_dir=self.cache_dir, **kw)


class InstallerRecordsOriginTests(Fixture):
    """Part 1."""

    def test_installer_pin_records_exactly_the_manifest_bound_origin_and_no_refspec(self):
        self.install_pin()
        self.assertEqual(origin_lines(self.pin), [f"remote.origin.url {URL}"])
        self.assertFalse(uc.package_identity(self.pin)["dirty"])
        self.assertEqual(uf.vp.check_project(self.project, iu.Cache(self.cache_dir).open(create=False))["status"],
                         "verified")

    def test_the_pins_own_controller_fetch_works_on_an_installer_pin(self):
        self.install_pin()
        consent = self.consent()
        code, out = self.controller("fetch", "--target", TARGET, "--consent", consent)
        self.assertEqual((code, out["status"]), (0, "fetched"), out)
        self.assertEqual(out["commit"], self.releases[TARGET]["commit"])

    def test_a_conflicting_staged_origin_stops_before_publication(self):
        real = ri.real_fetch

        def fetch_then_conflict(repo, origin, version, use_helper):
            real(repo, origin, version, use_helper)
            tip.git(repo, "config", "--local", "--add", "remote.origin.url", "https://elsewhere.invalid/x.git")
        for extra in ("other", "second"):
            with self.subTest(extra=extra):
                def fetcher(repo, origin, version, use_helper, extra=extra):
                    real(repo, origin, version, use_helper)
                    if extra == "second":
                        tip.git(repo, "config", "--local", "remote.origin.url", URL)
                    tip.git(repo, "config", "--local", "--add", "remote.origin.url", "https://elsewhere.invalid/x")
                err = self.stop(ip.install, self.project, "1.5.0", fetch_permitted=True, use_helper=False,
                                origin=URL, run_suite=lambda repo: 7, fetch=fetcher)
                self.assertEqual(err.code, "origin_conflict")
                self.assertFalse(os.path.lexists(self.pin))
                ip.recover(self.project)
        del fetch_then_conflict

    def test_interruptions_around_the_origin_write_leave_nothing_published(self):
        for where in ("before write", "after write"):
            with self.subTest(where=where):
                if where == "before write":
                    patch = mock.patch.object(ip, "record_origin", side_effect=KeyboardInterrupt)
                else:
                    real = ip.record_origin

                    def write_then_stop(repo, origin):
                        real(repo, origin)
                        raise KeyboardInterrupt
                    patch = mock.patch.object(ip, "record_origin", write_then_stop)
                with patch, self.assertRaises(KeyboardInterrupt):
                    ip.install(self.project, "1.5.0", fetch_permitted=True, use_helper=False, origin=URL,
                               run_suite=lambda repo: 7)
                self.assertFalse(os.path.lexists(self.pin))
                out = ip.recover(self.project)
                self.assertEqual([r["action"] for r in out["results"]], ["removed_unpublished"])

    def test_recovery_after_publication_never_edits_a_published_pins_config(self):
        with mock.patch.object(ip, "finish", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            ip.install(self.project, "1.5.0", fetch_permitted=True, use_helper=False, origin=URL,
                       run_suite=lambda repo: 7)
        self.unrecord_origin()  # a pin published by an older installer, whose journal has no origin step
        before = (self.pin / ".git" / "config").read_bytes()
        out = ip.recover(self.project)
        self.assertEqual([r["action"] for r in out["results"]], ["completed_after_publish"])
        self.assertEqual((self.pin / ".git" / "config").read_bytes(), before)
        self.assertEqual(origin_lines(self.pin), [])


class CacheSideFetchTests(Fixture):
    """Part 2: an existing pin without a recorded origin."""

    def setUp(self):
        super().setUp()
        self.install_pin()
        self.unrecord_origin()

    def test_old_pin_controller_fetch_stops_and_the_cache_fetch_then_prepare_proceed(self):
        consent = self.consent()
        code, out = self.controller("fetch", "--target", TARGET, "--consent", consent)
        self.assertEqual((code, out.get("helper_error")), (2, "origin_missing"))
        config = (self.pin / ".git" / "config").read_bytes()
        tree = subprocess.run(["git", "-C", str(self.pin), "status", "--porcelain", "--ignored"],
                              capture_output=True, env=uc.clean_env(), check=True).stdout
        out = self.fetch(consent)
        self.assertEqual((out["status"], out["commit"], out["consent"]),
                         ("fetched", self.releases[TARGET]["commit"], "unconsumed"))
        tag = uc.tag_identity(self.pin, TARGET)
        self.assertEqual(self.refs()[f"refs/tags/v{TARGET}"], tag["tag_object"])
        self.assertEqual((self.pin / ".git" / "config").read_bytes(), config)
        self.assertEqual(subprocess.run(["git", "-C", str(self.pin), "status", "--porcelain", "--ignored"],
                                        capture_output=True, env=uc.clean_env(), check=True).stdout, tree)
        self.assertEqual(sorted(p.name for p in (self.project / ".ai-comm-local" / "upgrade").iterdir()
                                if p.name.startswith("fetch-")), [])
        # The pin's own prepare now proceeds. Its target verification (a full inner suite)
        # is stubbed here; the genuine-1.10.0 real-boundary test runs it for real.
        root = uc.Project(self.project).channel_id()
        uc.write_private_atomic(self.project / ".ai-comm-local" / "upgrade" / "project.json", uc.canonical(
            {"v": 1, "schema": uc.PROJECT_SCHEMA, "status": "declared",
             "project_id": {"path": str(self.project), "channel": root}, "integrations": []}))
        stub = {"commit": tag["commit"], "tag_object": tag["tag_object"], "tests_ran": 1, "checks": {}, "at": 0}
        with mock.patch.object(uc, "verify_target", return_value=stub):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = uc.main(["--project", str(self.project), "prepare", "--target", TARGET, "--consent", consent,
                                "--as", "agent-a", "--session", "s-a"])
        self.assertEqual(code, 0, buf.getvalue())

    def test_a_newer_indexed_release_is_reported_not_selected(self):
        """Plan r2: no implicit target change; a newer version in the fetched index is reported."""
        consent = self.consent(target="1.5.0", commit=self.releases["1.5.0"]["commit"])
        tip.git(self.pin, "tag", "-d", "v1.5.0")  # the pin's own tag, so the fetched one is the only one
        out = uf.upgrade_fetch(self.project, "1.5.0", consent, cache_dir=self.cache_dir)
        self.assertEqual((out["target"], out["newer_indexed"]), ("1.5.0", ["1.5.1"]))
        self.assertEqual(self.fetch(self.consent())["newer_indexed"], [])

    def test_a_retry_after_a_completed_publication_recognises_the_pair(self):
        consent = self.consent()
        first = self.fetch(consent)
        self.assertEqual(self.fetch(consent)["refs"], first["refs"])

    def test_refusals_leave_both_refs_and_the_pin_as_found(self):
        consent = self.consent()
        cases = {}

        def case(name, setup, code, **kw):
            cases[name] = (setup, code, kw)
        case("pin_has_origin", lambda: tip.git(self.pin, "config", "--local", "remote.origin.url", URL),
             "pin_has_origin")
        case("two origin values", lambda: [tip.git(self.pin, "config", "--local", "--add", "remote.origin.url", v)
                                           for v in (URL, "x")], "pin_has_origin")
        case("unbound target", None, "release_unbound", target="9.9.9")
        case("unknown consent", None, "consent_missing", consent="consent-0000000000000000")
        case("cache origin differs", lambda: self.set_cache_origin("https://other.invalid/x.git"),
             "origin_disagreement")
        for name, (setup, code, kw) in cases.items():
            with self.subTest(name):
                before = self.refs()
                config = (self.pin / ".git" / "config").read_bytes() if setup is None else None
                undo = None
                if setup:
                    undo = self.snapshot_config()
                    setup()
                try:
                    err = self.stop(uf.upgrade_fetch, self.project, kw.get("target", TARGET),
                                    kw.get("consent", consent), cache_dir=self.cache_dir)
                    self.assertEqual(err.code, code)
                    self.assertEqual(self.refs(), before)
                    if config is not None:
                        self.assertEqual((self.pin / ".git" / "config").read_bytes(), config)
                finally:
                    if undo:
                        undo()

    def snapshot_config(self):
        path = self.pin / ".git" / "config"
        data = path.read_bytes()
        cache_state = (self.cache_dir / "state.json").read_bytes()

        def undo():
            path.write_bytes(data)
            uc.write_private_atomic(self.cache_dir / "state.json", cache_state)
        return undo

    def set_cache_origin(self, origin):
        cache = iu.Cache(self.cache_dir).open(create=False)
        state = cache.state()
        state["origin"] = origin
        cache.write_state(state)

    def test_a_modified_pin_is_refused_and_preserved(self):
        consent = self.consent()
        (self.pin / "README.md").write_text("changed\n")
        err = self.stop(self.fetch, consent)
        self.assertEqual(err.code, "pin_not_verified")
        self.assertEqual((self.pin / "README.md").read_text(), "changed\n")
        self.assertNotEqual(uf.vp.check_project(self.project, iu.Cache(self.cache_dir).open(create=False))["status"],
                            "verified")

    def test_a_clean_but_unverified_pin_is_refused(self):
        """The pin's release has no trusted binding in this cache: `unknown`, though clean."""
        consent = self.consent()
        cache = iu.Cache(self.cache_dir).open(create=False)
        bindings = cache.bindings()
        del bindings["bindings"]["1.5.0"]
        cache.write_bindings(bindings)
        before = self.refs()
        err = self.stop(self.fetch, consent)
        self.assertEqual((err.code, err.details.get("status")), ("pin_not_verified", "unknown"))
        self.assertEqual(self.refs(), before)

    def test_consent_rules_match_the_controllers_fetch(self):
        before = self.refs()
        with self.subTest("other target"):
            other = self.consent(target="1.5.0", commit=self.releases["1.5.0"]["commit"])
            self.assertEqual(self.stop(self.fetch, other).code, "consent_target_mismatch")
        with self.subTest("other target commit"):
            wrong = self.consent(commit=self.releases["1.5.0"]["commit"])
            self.assertEqual(self.stop(self.fetch, wrong).code, "consent_target_mismatch")
        with self.subTest("other tag object"):
            wrong = self.consent(tag_object="0" * 40)
            self.assertEqual(self.stop(self.fetch, wrong).code, "consent_target_mismatch")
        with self.subTest("expired"):
            consent = self.consent()
            with mock.patch.object(uc, "now", return_value=time.time() + 10 ** 7):
                self.assertEqual(self.stop(self.fetch, consent).code, "consent_expired")
        with self.subTest("spent"):
            consent = self.consent()
            uc.consume_consent(uc.Project(self.project), uc.load_consent(uc.Project(self.project), consent), "txn-x")
            self.assertEqual(self.stop(self.fetch, consent).code, "consent_spent")
        self.assertEqual(self.refs(), before)

    def test_guards_unfinished_setup_unfinished_upgrade_and_the_lock(self):
        consent = self.consent()
        before = self.refs()
        with self.subTest("setup"), mock.patch.object(uc, "unfinished_setup", return_value="a" * 64):
            self.assertEqual(self.stop(self.fetch, consent).code, "setup_in_progress")
        with self.subTest("upgrade"), mock.patch.object(uf, "require_no_unfinished_upgrade",
                                                        side_effect=Stop("upgrade_in_progress")):
            self.assertEqual(self.stop(self.fetch, consent).code, "upgrade_in_progress")
        with self.subTest("lock"), uc.Project(self.project).lock():
            self.assertEqual(self.stop(self.fetch, consent).code, "upgrade_locked")
        self.assertEqual(self.refs(), before)

    def test_a_changed_tag_or_index_at_the_origin_publishes_nothing(self):
        consent = self.consent()
        before = self.refs()
        work = self.base / "tamper"
        subprocess.run(["git", "clone", "-q", str(self.bare), str(work)], check=True, capture_output=True,
                       env=tip.ENV)
        saved = self.base / "saved.git"
        shutil.copytree(self.bare, saved)
        try:
            with self.subTest("tag moved"):
                tip.git(work, "tag", "-f", "-a", f"v{TARGET}", "-m", "moved", "v1.5.0^{commit}")
                tip.git(work, "push", "-q", "-f", "origin", f"refs/tags/v{TARGET}")
                self.assertEqual(self.stop(self.fetch, consent).code, "release_identity_changed")
                self.assertEqual(self.refs(), before)
            shutil.rmtree(self.bare)
            shutil.copytree(saved, self.bare)
            with self.subTest("well-formed tag on another commit"):
                # A valid annotated tag whose message names its commit's own manifest: tag_identity
                # accepts it, so only the comparison with the cache binding can refuse it.
                tip.git(work, "fetch", "-q", "-f", "--tags", "origin")
                old = tip.git(work, "rev-parse", "v1.5.0^{commit}").strip()
                manifest = tip.sha(tip.git(work, "show", f"{old}:RELEASE-MANIFEST.json").encode())
                tip.git(work, "tag", "-f", "-a", f"v{TARGET}", "-m",
                        f"ai-comm {TARGET} (protocol 3.0); manifest sha256 {manifest}", old)
                tip.git(work, "push", "-q", "-f", "origin", f"refs/tags/v{TARGET}")
                err = self.stop(self.fetch, consent)
                self.assertEqual((err.code, err.details.get("what")), ("release_identity_changed", "tag_object"))
                self.assertEqual(self.refs(), before)
            shutil.rmtree(self.bare)
            shutil.copytree(saved, self.bare)
            with self.subTest("duplicate index entry"):
                tip.git(work, "fetch", "-q", "origin", "release-index:release-index")
                tip.git(work, "switch", "-q", "release-index")
                index = json.loads((work / "RELEASES.json").read_text())
                index["releases"].append(index["releases"][0])
                (work / "RELEASES.json").write_text(json.dumps(index, sort_keys=True, separators=(",", ":")) + "\n")
                tip.git(work, "commit", "-q", "-am", "dup")
                tip.git(work, "push", "-q", "origin", "release-index")
                self.assertEqual(self.stop(self.fetch, consent).code, "release_index_duplicate_version")
                self.assertEqual(self.refs(), before)
        finally:
            shutil.rmtree(self.bare)
            shutil.copytree(saved, self.bare)

    def test_an_existing_different_tag_is_refused(self):
        consent = self.consent()
        tip.git(self.pin, "tag", "-a", f"v{TARGET}", "-m", "local", "HEAD")
        before = self.refs()
        self.assertEqual(self.stop(self.fetch, consent).code, "tag_exists_different")
        self.assertEqual(self.refs(), before)

    def test_symbolic_refs_are_refused_and_never_followed(self):
        """REVIEW 106 P1: a symbolic allowed name must not redirect the write to another ref."""
        consent = self.consent()
        head = tip.git(self.pin, "rev-parse", "HEAD").strip()
        tip.git(self.pin, "update-ref", "refs/heads/outside-allowlist", head)
        for name in ("refs/remotes/origin/release-index", f"refs/tags/v{TARGET}"):
            with self.subTest(ref=name):
                saved = uf.ref_value(self.pin, name) if name.endswith("release-index") else None
                if saved:
                    tip.git(self.pin, "update-ref", "-d", name)
                tip.git(self.pin, "symbolic-ref", name, "refs/heads/outside-allowlist")
                err = self.stop(self.fetch, consent)
                self.assertEqual((err.code, err.details.get("what")), ("ref_invalid", "symbolic"))
                self.assertEqual(tip.git(self.pin, "rev-parse", "refs/heads/outside-allowlist").strip(), head)
                self.assertEqual(tip.git(self.pin, "symbolic-ref", name).strip(), "refs/heads/outside-allowlist")
                tip.git(self.pin, "symbolic-ref", "--delete", name)
                if saved:
                    tip.git(self.pin, "update-ref", name, saved)

    def test_pin_local_transport_configuration_is_never_used(self):
        """REVIEW 106 P1: URL rewrites and core.sshCommand in the pin's own config must not run."""
        consent = self.consent()
        marker = self.base / "ssh-ran"
        program = self.base / "fake-ssh"
        program.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
        program.chmod(0o755)
        local = str(self.project / ".ai-comm-local")
        tip.git(self.pin, "config", "--local", "core.sshCommand", str(program))
        tip.git(self.pin, "config", "--local", f"url.ssh://example.invalid{local}.insteadOf", local)
        tip.git(self.pin, "config", "--local", "url.ssh://example.invalid/.insteadOf", "/")
        out = self.fetch(consent)
        self.assertEqual(out["status"], "fetched")
        self.assertFalse(marker.exists(), "a pin-local transport command ran")
        self.assertEqual(self.refs()[f"refs/tags/v{TARGET}"], uc.tag_identity(self.pin, TARGET)["tag_object"])

    def test_pin_local_work_tree_commands_never_run(self):
        """REVIEW 108 P1: a pin-local core.fsmonitor program must not run, neither before a
        refusal nor during a successful fetch."""
        consent = self.consent()  # the pin's own controller (pin code, owner-run) before the trap is set
        marker = self.base / "fsmonitor-ran"
        program = self.base / "fake-fsmonitor"
        program.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
        program.chmod(0o755)
        tip.git(self.pin, "config", "--local", "core.fsmonitor", str(program))
        self.assertEqual(self.stop(self.fetch, "consent-0000000000000000").code, "consent_missing")
        self.assertFalse(marker.exists(), "fsmonitor ran before the refusal")
        self.assertEqual(self.fetch(consent)["status"], "fetched")
        self.assertFalse(marker.exists(), "fsmonitor ran during the fetch")

    def test_a_concurrent_ref_change_aborts_the_whole_pair(self):
        consent = self.consent()
        real = ri.isolated_fetch

        def fetch_then_race(repo, origin, helper, refspecs):
            real(repo, origin, helper, refspecs)
            head = tip.git(self.pin, "rev-parse", "HEAD").strip()
            tip.git(self.pin, "update-ref", "refs/remotes/origin/release-index", head)
        with mock.patch.object(ri, "isolated_fetch", fetch_then_race):
            self.assertEqual(self.stop(self.fetch, consent).code, "refs_changed")
        refs = self.refs()
        self.assertIsNone(refs[f"refs/tags/v{TARGET}"])
        self.assertEqual(refs["refs/remotes/origin/release-index"], tip.git(self.pin, "rev-parse", "HEAD").strip())

    def test_interruption_before_and_after_the_ref_transaction(self):
        consent = self.consent()
        before = self.refs()
        real_run = subprocess.run

        def interrupt(when):
            def run(argv, *a, **kw):
                is_txn = argv[:4] == ["git", "-C", str(self.pin), "update-ref"]
                if is_txn and when == "before":
                    raise KeyboardInterrupt
                done = real_run(argv, *a, **kw)
                if is_txn and when == "after":
                    raise KeyboardInterrupt
                return done
            return run
        with mock.patch.object(uf.subprocess, "run", interrupt("before")), self.assertRaises(KeyboardInterrupt):
            self.fetch(consent)
        self.assertEqual(self.refs(), before)
        with mock.patch.object(uf.subprocess, "run", interrupt("after")), self.assertRaises(KeyboardInterrupt):
            self.fetch(consent)
        refs = self.refs()
        self.assertTrue(all(refs.values()), "the committed pair is complete")
        self.assertEqual(self.fetch(consent)["refs"], refs)  # the retry recognises it

    def test_cli_route_through_the_cache_installer(self):
        consent = self.consent()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = iu.main(["upgrade-fetch", "--project", str(self.project), "--target", TARGET, "--consent",
                            consent, "--cache", str(self.cache_dir)])
        self.assertEqual((code, json.loads(buf.getvalue())["status"]), (0, "fetched"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = iu.main(["upgrade-fetch", "--project", str(self.project)])
        self.assertEqual((code, json.loads(buf.getvalue())["code"]), (2, "invalid_arguments"))


if __name__ == "__main__":
    unittest.main()
