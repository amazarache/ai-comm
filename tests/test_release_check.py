import json
import os
from pathlib import Path
import contextlib
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ai_comm as package_module
from scripts import check_for_release as checker
checker.ac = package_module


class FakeRunner:
    def __init__(self, *, remote=checker.ac.RELEASE_ORIGIN, status=None,
                 plan=None, fetch_code=0, fetch_stderr=b"must not leak", plan_code=0):
        self.remote = remote
        self.status = status or {"status": "ok", "pinned_version": "1.1.0",
                                 "latest_version": "1.1.0", "advice": [],
                                 "active_advisories": []}
        self.plan = plan or {"owner_notice": "review update"}
        self.fetch_code = fetch_code
        self.fetch_stderr = fetch_stderr
        self.plan_code = plan_code
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if argv[:4] == ["git", "config", "--global", "--null"]:
            return subprocess.run(argv, stdout=kwargs["stdout"], stderr=kwargs["stderr"],
                                  env=kwargs["env"], timeout=kwargs["timeout"], check=False)
        if (argv[:4] == ["git", "-C", str(self.calls_root), "config"] and
                "--show-scope" in argv):
            return subprocess.run(argv, stdout=kwargs["stdout"], stderr=kwargs["stderr"],
                                  env=kwargs["env"], timeout=kwargs["timeout"], check=False)
        if argv[:5] == ["git", "-C", str(self.calls_root), "config", "--local"]:
            return subprocess.CompletedProcess(argv, 0, self.remote.encode() + b"\0", b"")
        if "--show-object-format" in argv:
            return subprocess.CompletedProcess(argv, 0, b"sha1\n", b"")
        if "--git-path" in argv and "objects" in argv:
            return subprocess.CompletedProcess(argv, 0, b".git/objects\n", b"")
        if "for-each-ref" in argv:
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if "update-ref" in argv:
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if "init" in argv and "--bare" in argv:
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if "fetch" in argv:
            return subprocess.CompletedProcess(argv, self.fetch_code, b"", self.fetch_stderr)
        if argv[-1:] == ["release-status"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.status).encode(), b"")
        if argv[-1:] == ["upgrade-plan"]:
            return subprocess.CompletedProcess(argv, self.plan_code, json.dumps(self.plan).encode(), b"")
        return subprocess.CompletedProcess(argv, 1, b"", b"")

    def run(self, **kwargs):
        return checker.run_check(runner=self, **kwargs)


class CredentialTransportRunner(FakeRunner):
    """HTTPS transport stub: exercise Git's helper protocol without network access."""

    def __init__(self, *, remote=checker.ac.RELEASE_ORIGIN, fetch_code=0,
                 fetch_stderr=b"", redirect=False):
        super().__init__(remote=remote, fetch_code=fetch_code, fetch_stderr=fetch_stderr)
        self.redirect = redirect
        self.credential_stdout = b""

    def __call__(self, argv, **kwargs):
        if "fetch" not in argv:
            return super().__call__(argv, **kwargs)
        self.calls.append((list(argv), kwargs))
        credential = subprocess.run(
            ["git", "credential", "fill"],
            input=b"protocol=https\nhost=fixture.example\npath=amazarache/ai-comm.git\n\n",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=kwargs["env"], timeout=10,
            check=False)
        self.credential_stdout = credential.stdout
        if credential.returncode != 0:
            return subprocess.CompletedProcess(argv, 1, b"", b"HTTP/1.1 401 Unauthorized")
        if self.redirect:
            # The stub models a redirect response; the fetch command's explicit
            # followRedirects=false prevents a second request/helper lookup.
            return subprocess.CompletedProcess(
                argv, 1, b"", b"fatal: The requested URL returned error: 302")
        return subprocess.CompletedProcess(argv, self.fetch_code, b"", self.fetch_stderr)


class ReleaseCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="release check path ")
        self.repo = Path(self.temp.name) / "package with spaces"
        self.repo.mkdir()
        self._init_package_repo()

    def _init_package_repo(self):
        env = checker.ac.git_env({"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                                  "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid"})
        source = (f'VERSION = "1.2.0"\nRELEASE_ORIGIN = "{checker.ac.RELEASE_ORIGIN}"\n').encode()
        protocol = b"protocol fixture\n"
        (self.repo / "ai_comm.py").write_bytes(source)
        (self.repo / "PROTOCOL.md").write_bytes(protocol)
        files = []
        for name in ("ai_comm.py", "PROTOCOL.md"):
            content = (self.repo / name).read_bytes()
            files.append({"path": name, "bytes": len(content), "sha256": checker.ac.sha(content)})
        manifest = {"format": "ai-comm-review-manifest/1",
                    "protocol_sha256": checker.ac.sha(protocol), "files": files}
        (self.repo / "RELEASE-MANIFEST.json").write_bytes(checker.ac.canonical(manifest))
        subprocess.run(["git", "init", "--initial-branch=main", str(self.repo)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=20)
        subprocess.run(["git", "-C", str(self.repo), "add", "ai_comm.py", "PROTOCOL.md",
                        "RELEASE-MANIFEST.json"], check=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, env=env, timeout=20)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "fixture"], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=20)
        subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin",
                        checker.ac.RELEASE_ORIGIN], check=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, env=env, timeout=20)

    def tearDown(self):
        self.temp.cleanup()

    def runner(self, **kwargs):
        value = FakeRunner(**kwargs)
        value.calls_root = self.repo
        return value

    @contextlib.contextmanager
    def _global_git_config(self, contents, *, xdg=False, extra_env=None):
        home = Path(self.temp.name) / ("credential home xdg" if xdg else "credential home")
        home.mkdir(exist_ok=True)
        if xdg:
            config = home / "xdg" / "git" / "config"
            config.parent.mkdir(parents=True, exist_ok=True)
            xdg_home = config.parents[1]
        else:
            config = home / ".gitconfig"
            xdg_home = None
        config.write_text(contents, encoding="utf-8")
        clean_env = {key: value for key, value in os.environ.items()
                     if not key.startswith("GIT_")}
        clean_env["HOME"] = str(home)
        clean_env.pop("XDG_CONFIG_HOME", None)
        if xdg_home is not None:
            clean_env["XDG_CONFIG_HOME"] = str(xdg_home)
        clean_env.update(extra_env or {})
        with mock.patch.dict(os.environ, clean_env, clear=True):
            yield home

    def _copy_real_package(self, target):
        shutil.copytree(Path(checker.__file__).resolve().parents[1], target,
                        ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv"))

    def _git(self, *args, cwd=None, env=None):
        return subprocess.run(["git", *map(str, args)], cwd=cwd, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=30, check=True)

    def test_real_helper_reports_package_unavailable_as_json(self):
        for broken in (False, True):
            with self.subTest(broken=broken):
                package = Path(self.temp.name) / ("broken" if broken else "missing")
                (package / "scripts").mkdir(parents=True)
                helper = Path(checker.__file__)
                shutil.copy2(helper, package / "scripts" / helper.name)
                if broken:
                    (package / "ai_comm.py").write_text("this is not valid python !\n")
                proc = subprocess.run([sys.executable, str(package / "scripts" / helper.name)],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
                self.assertEqual(2, proc.returncode)
                self.assertEqual(b"", proc.stderr)
                lines = proc.stdout.decode().splitlines()
                self.assertEqual(1, len(lines))
                value = json.loads(lines[0])
                self.assertEqual(("error", "package_unavailable", "package_unavailable"),
                                 (value["result"], value["status"], value["fetch_error"]))

    def test_real_helper_lock_file_is_ignored_and_does_not_dirty_package(self):
        package = Path(self.temp.name) / "real-lock-package"
        self._copy_real_package(package)
        env = package_module.git_env({"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                                      "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid"})
        self._git("init", "--initial-branch=main", cwd=package, env=env)
        self._git("add", "-A", cwd=package, env=env)
        self._git("commit", "-m", "fixture", cwd=package, env=env)
        lock = package / ".release-check.lock"
        proc = subprocess.run([shutil.which("flock"), "-n", str(lock), sys.executable,
                               str(package / "scripts" / "check_for_release.py")],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, env=env)
        self.assertEqual(2, proc.returncode)
        self.assertEqual(1, len(proc.stdout.decode().splitlines()))
        self.assertTrue(lock.exists())
        self.assertEqual(b"", self._git("status", "--porcelain", cwd=package, env=env).stdout)
        self.assertEqual("git", package_module.package_identity(package)["source"])
        self.assertFalse(package_module.package_identity(package)["dirty"])
        self.assertFalse((package / "__pycache__").exists())

    def test_real_helper_fetch_ignores_configured_refmaps(self):
        package = Path(self.temp.name) / "real-fetch-package"
        self._copy_real_package(package)
        seed = Path(self.temp.name) / "seed"
        bare = Path(self.temp.name) / "remote.git"
        seed.mkdir()
        self._git("init", "--initial-branch=main", cwd=seed)
        (seed / "RELEASES.json").write_text("{}\n")
        self._git("add", "RELEASES.json", cwd=seed)
        env_git = package_module.git_env({"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                                          "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid"})
        self._git("commit", "-m", "index seed", cwd=seed, env=env_git)
        self._git("branch", "release-index", cwd=seed)
        self._git("tag", "v9.9.9", cwd=seed)
        self._git("clone", "--bare", str(seed), str(bare))
        env = package_module.git_env({"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                                      "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid"})
        self._git("init", "--initial-branch=main", cwd=package, env=env)
        self._git("add", "-A", cwd=package, env=env)
        self._git("commit", "-m", "fixture", cwd=package, env=env)
        self._git("remote", "add", "origin", "https://fixture.example/ai-comm.git", cwd=package, env=env)
        self._git("config", "--add", "remote.origin.fetch", "+refs/tags/*:refs/remotes/leak/*", cwd=package, env=env)
        self._git("config", "--local", "url.https://attacker.invalid/.insteadOf",
                  "https://fixture.example/", cwd=package, env=env)
        self._git("config", "--local", "http.proxy", "http://attacker.invalid:8080",
                  cwd=package, env=env)
        self._git("config", "--local", "credential.helper", "!echo local-secret",
                  cwd=package, env=env)

        real_git = shutil.which("git")
        bin_dir = Path(self.temp.name) / "bin"
        bin_dir.mkdir()
        wrapper = bin_dir / "git"
        wrapper.write_text("#!/usr/bin/env python3\nimport os, sys\n"
                           f"real = {real_git!r}\n"
                           "args = sys.argv[1:]\n"
                           f"bare = {str(bare)!r}\n"
                           "if 'fetch' in args and 'https://fixture.example/ai-comm.git' in args:\n"
                           "    args[args.index('https://fixture.example/ai-comm.git')] = bare\n"
                           "os.execv(real, [real, *args])\n")
        wrapper.chmod(0o755)
        run_env = os.environ.copy()
        run_env.update(env)
        run_env["PATH"] = str(bin_dir) + os.pathsep + run_env.get("PATH", "")
        proc = subprocess.run([sys.executable, str(package / "scripts" / "check_for_release.py"),
                               "--fetch-permitted", "--expected-origin", "https://fixture.example/ai-comm.git"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, env=run_env)
        self.assertEqual(2, proc.returncode)
        value = json.loads(proc.stdout.decode().splitlines()[0])
        self.assertTrue(value["fetched"])
        refs = self._git("for-each-ref", "--format=%(refname)", cwd=package, env=env).stdout.decode().splitlines()
        self.assertIn("refs/remotes/origin/release-index", refs)
        self.assertIn("refs/tags/v9.9.9", refs)
        self.assertFalse(any(ref.startswith("refs/remotes/leak/") for ref in refs), refs)
        self.assertFalse((package / ".git" / "FETCH_HEAD").exists())
        self.assertFalse((package / "__pycache__").exists())

    def test_no_fetch_without_permission_and_reports_up_to_date(self):
        fake = self.runner()
        result, code = fake.run(repo=self.repo, fetch_permitted=False)
        self.assertEqual(0, code)
        self.assertEqual("up_to_date", result["result"])
        self.assertFalse(result["fetched"])
        self.assertEqual(["release-status"], [call[0][-1] for call in fake.calls])

    def test_fetch_uses_exact_refs_and_reports_optional_update(self):
        status = {"status": "ok", "pinned_version": "1.1.0", "latest_version": "1.2.0",
                  "advice": [{"package_version": "1.2.0", "advisory": "optional",
                              "channel_action": "unchanged"}], "active_advisories": []}
        fake = self.runner(status=status)
        result, code = fake.run(repo=self.repo, fetch_permitted=True)
        self.assertEqual((10, "update_available", "review update"),
                         (code, result["result"], result["owner_notice"]))
        fetch, fetch_kwargs = next((args, kwargs) for args, kwargs in fake.calls if "fetch" in args)
        self.assertEqual(["fetch", "--refmap=", "--no-tags", "--no-recurse-submodules",
                          checker.ac.RELEASE_ORIGIN, *checker.REFSPECS], fetch[2:])
        fetch_env = fetch_kwargs["env"]
        self.assertEqual("1", fetch_env["GIT_NO_LAZY_FETCH"])
        self.assertTrue(result["fetched"])
        self.assertEqual("unchanged", result["channel_action"])

    def test_helper_flag_requires_fetch_permission(self):
        fake = self.runner()
        result, code = fake.run(repo=self.repo, fetch_permitted=False,
                                use_git_credential_helper=True)
        self.assertEqual((2, "credential_helper_requires_fetch_permission", False),
                         (code, result["fetch_error"], result["fetched"]))
        self.assertEqual([], fake.calls)

    def test_local_redirect_config_is_ignored_by_isolated_fetch(self):
        fake = self.runner()
        result, code = fake.run(repo=self.repo, fetch_permitted=True)
        self.assertEqual((0, None, True), (code, result["fetch_error"], result["fetched"]))
        self.assertTrue(any("fetch" in args for args, _ in fake.calls))

    def test_helper_config_uses_clean_global_read_and_only_helpers_reach_fetch(self):
        config = ("[url \"https://rewritten.example/\"]\n"
                  "\tinsteadOf = https://github.com/\n"
                  "[http]\n\tproxy = http://proxy.invalid/\n"
                  "[core]\n\thooksPath = /untrusted/hooks\n"
                  "[credential]\n\thelper = first-helper\n\thelper =\n"
                  "\thelper = second-helper\n")
        self._git("config", "--local",
                  f"http.{checker.ac.RELEASE_ORIGIN}.followRedirects", "true",
                  cwd=self.repo, env=package_module.git_env())
        self._git("config", "--local",
                  f"http.{checker.ac.RELEASE_ORIGIN}/info/refs.followRedirects", "true",
                  cwd=self.repo, env=package_module.git_env())
        self._git("config", "--local",
                  f"http.{checker.ac.RELEASE_ORIGIN}/objects/info.followRedirects", "true",
                  cwd=self.repo, env=package_module.git_env())
        self._git("config", "--local", "url.https://attacker.invalid/.insteadOf",
                  checker.ac.RELEASE_ORIGIN, cwd=self.repo, env=package_module.git_env())
        self._git("config", "--local", "http.proxy", "http://attacker.invalid:8080",
                  cwd=self.repo, env=package_module.git_env())
        self._git("config", "--local", "credential.helper", "!echo local-secret",
                  cwd=self.repo, env=package_module.git_env())
        injected = {"GIT_CONFIG_GLOBAL": "/untrusted/global",
                    "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper",
                    "GIT_CONFIG_VALUE_0": "injected-helper",
                    "GIT_CONFIG_PARAMETERS": "'credential.helper=injected-parameters'"}
        with self._global_git_config(config, xdg=True, extra_env=injected) as home:
            fake = self.runner()
            result, code = fake.run(repo=self.repo, fetch_permitted=True,
                                    use_git_credential_helper=True)
        self.assertEqual(0, code)
        self.assertTrue(result["fetched"])
        read_args, read_kwargs = next((args, kwargs) for args, kwargs in fake.calls
                                      if args[:4] == ["git", "config", "--global", "--null"])
        self.assertEqual(["git", "config", "--global", "--null", "--get-all",
                          "credential.helper"], read_args)
        read_env = read_kwargs["env"]
        self.assertEqual(str(home), read_env["HOME"])
        self.assertTrue(read_env["XDG_CONFIG_HOME"].endswith("/xdg"))
        self.assertEqual(("1", "C"), (read_env["GIT_CONFIG_NOSYSTEM"], read_env["LC_ALL"]))
        for key in injected:
            self.assertNotIn(key, read_env)
        fetch_args, fetch_kwargs = next((args, kwargs) for args, kwargs in fake.calls
                                        if "fetch" in args)
        self.assertEqual(["fetch", "--refmap=", "--no-tags", "--no-recurse-submodules",
                          checker.ac.RELEASE_ORIGIN, *checker.REFSPECS], fetch_args[2:])
        self.assertNotIn("first-helper", " ".join(fetch_args))
        fetch_env = fetch_kwargs["env"]
        self.assertEqual(os.devnull, fetch_env["GIT_CONFIG_GLOBAL"])
        self.assertEqual("1", fetch_env["GIT_CONFIG_NOSYSTEM"])
        config_count = int(fetch_env["GIT_CONFIG_COUNT"])
        configured = [(fetch_env[f"GIT_CONFIG_KEY_{i}"], fetch_env[f"GIT_CONFIG_VALUE_{i}"])
                      for i in range(config_count)]
        self.assertEqual([("credential.helper", ""), ("credential.helper", "first-helper"),
                          ("credential.helper", ""), ("credential.helper", "second-helper")],
                         configured[:4])
        self.assertNotIn(("credential.helper", "injected-helper"), configured)
        self.assertNotIn(("credential.helper", "injected-parameters"), configured)
        self.assertNotIn("credential.helper=first-helper", " ".join(fetch_args))
        self.assertIn(checker.ac.RELEASE_ORIGIN, fetch_args)
        self.assertNotIn("attacker.invalid", " ".join(fetch_args))
        self.assertNotIn("attacker.invalid", " ".join(fetch_env.values()))
        self.assertNotIn("local-secret", " ".join(fetch_env.values()))
        for key in injected:
            if key == "GIT_CONFIG_GLOBAL":
                self.assertNotEqual(injected[key], fetch_env[key])
            elif key == "GIT_CONFIG_KEY_0":
                self.assertEqual("credential.helper", fetch_env[key])
            elif key in {"GIT_CONFIG_COUNT", "GIT_CONFIG_VALUE_0"}:
                self.assertNotEqual(injected[key], fetch_env[key])
            else:
                self.assertNotIn(key, fetch_env)
        for request_url in (
                checker.ac.RELEASE_ORIGIN,
                checker.ac.RELEASE_ORIGIN + "/info/refs?service=git-upload-pack",
                checker.ac.RELEASE_ORIGIN + "/git-upload-pack",
                checker.ac.RELEASE_ORIGIN + "/objects/info/packs"):
            effective_redirects = subprocess.run(
                ["git", "config", "--get-urlmatch",
                 "http.followRedirects", request_url], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=fetch_env, timeout=10, check=False)
            self.assertEqual((0, b"false\n"),
                             (effective_redirects.returncode, effective_redirects.stdout))

    def test_newline_in_helper_config_is_preserved_without_delimiter_ambiguity(self):
        with self._global_git_config('[credential]\n\thelper = "first\\ncontinued"\n'
                                     '\thelper = final\n'):
            fake = self.runner()
            result, code = fake.run(repo=self.repo, fetch_permitted=True,
                                    use_git_credential_helper=True)
        self.assertEqual(0, code)
        fetch, fetch_kwargs = next((args, kwargs) for args, kwargs in fake.calls if "fetch" in args)
        configured = [fetch_kwargs["env"][f"GIT_CONFIG_VALUE_{i}"]
                      for i in range(int(fetch_kwargs["env"]["GIT_CONFIG_COUNT"]))]
        self.assertIn("first\ncontinued", configured)
        self.assertIn("final", configured)

    def test_no_effective_helper_stops_before_fetch(self):
        for contents in ("", "[credential]\n\thelper =\n"):
            with self.subTest(contents=contents), self._global_git_config(contents):
                fake = self.runner()
                result, code = fake.run(repo=self.repo, fetch_permitted=True,
                                        use_git_credential_helper=True)
                self.assertEqual((2, "credential_helper_not_configured", False),
                                 (code, result["fetch_error"], result["fetched"]))
                self.assertFalse(any("fetch" in args for args, _ in fake.calls))

    def test_unreadable_helper_config_stops_without_disclosing_git_error(self):
        with self._global_git_config("[credential\n\thelper = broken\n"):
            fake = self.runner()
            result, code = fake.run(repo=self.repo, fetch_permitted=True,
                                    use_git_credential_helper=True)
        self.assertEqual((2, "credential_helper_config_unreadable", False),
                         (code, result["fetch_error"], result["fetched"]))
        self.assertFalse(any("fetch" in args for args, _ in fake.calls))
        self.assertNotIn("broken", json.dumps(result))

    def test_error_classifier_is_bounded_and_never_echoes_stderr(self):
        cases = ((b"HTTP/1.1 401 Unauthorized", "auth_failed"),
                 (b"fatal: The requested URL returned error: 403", "auth_failed"),
                 (b"fatal: The requested URL returned error: 302", "origin_redirect_refused"),
                 (b"remote failure secret-marker", "fetch_failed"))
        for stderr, expected in cases:
            with self.subTest(stderr=stderr):
                fake = self.runner(fetch_code=1, fetch_stderr=stderr)
                result, code = fake.run(repo=self.repo, fetch_permitted=True)
                self.assertEqual((2, expected), (code, result["fetch_error"]))
                self.assertNotIn(b"secret-marker", json.dumps(result).encode())

    def test_credential_transport_stub_and_cross_origin_redirect_boundary(self):
        helper = Path(self.temp.name) / "credential helper stub.py"
        host_log = Path(self.temp.name) / "credential helper hosts.log"
        helper.write_text(
            "#!/usr/bin/env python3\nimport os, sys\n"
            "request = sys.stdin.buffer.read()\n"
            "if sys.argv[1] == 'get':\n"
            " host = next((line[5:] for line in request.splitlines() if line.startswith(b'host=')), b'')\n"
            " with open(os.environ['AI_COMM_TEST_HELPER_LOG'], 'ab') as out: out.write(host + b'\\n')\n"
            " sys.stdout.buffer.write(b'username=fixture-user\\npassword=sentinel-secret\\n\\n')\n",
            encoding="utf-8")
        helper.chmod(0o700)
        helper_value = "!" + shlex.quote(sys.executable) + " " + shlex.quote(str(helper))
        config = f"[credential]\n\thelper = {helper_value}\n"
        test_env = {"AI_COMM_TEST_HELPER_LOG": str(host_log)}
        with self._global_git_config(config, extra_env=test_env):
            remote = "https://fixture.example/ai-comm.git"
            without_helper = CredentialTransportRunner(remote=remote)
            without_helper.calls_root = self.repo
            result, code = without_helper.run(repo=self.repo, fetch_permitted=True,
                                              expected_origin=remote)
            self.assertEqual((2, "auth_failed"), (code, result["fetch_error"]))
            self.assertFalse(host_log.exists())

            transport = CredentialTransportRunner(remote=remote)
            transport.calls_root = self.repo
            result, code = transport.run(repo=self.repo, fetch_permitted=True,
                                         use_git_credential_helper=True,
                                         expected_origin=remote)
            self.assertEqual((0, "up_to_date"), (code, result["result"]))
            self.assertIn(b"password=sentinel-secret", transport.credential_stdout)
            self.assertEqual(b"fixture.example\n", host_log.read_bytes())
            self.assertNotIn("sentinel-secret", json.dumps(result))

            host_log.unlink()
            redirect = CredentialTransportRunner(remote=remote, redirect=True)
            redirect.calls_root = self.repo
            result, code = redirect.run(repo=self.repo, fetch_permitted=True,
                                        use_git_credential_helper=True,
                                        expected_origin=remote)
            self.assertEqual((2, "origin_redirect_refused"),
                             (code, result["fetch_error"]))
            self.assertEqual(b"fixture.example\n", host_log.read_bytes())

    def test_applicable_advisory_has_priority_without_newer_release(self):
        status = {"status": "ok", "pinned_version": "1.1.0", "latest_version": "1.1.0",
                  "advice": [], "active_advisories": [
                      {"package_version": "1.1.0", "advisory": "required",
                       "channel_action": "unchanged"}]}
        fake = self.runner(status=status)
        result, code = fake.run(repo=self.repo, fetch_permitted=False)
        self.assertEqual((11, "advisory_applies", "required", True),
                         (code, result["result"], result["advisory"],
                          result["advisory_applies_to_pinned"]))
        self.assertIsNone(result["owner_notice"])

    def test_index_failures_always_exit_two_with_no_latest(self):
        for status_name in ("index_missing", "index_invalid", "index_inconsistent",
                            "index_tags_missing",
                            "not_git_checkout"):
            with self.subTest(status=status_name):
                fake = self.runner(status={"status": status_name, "pinned_version": "1.1.0"})
                result, code = fake.run(repo=self.repo, fetch_permitted=False)
                self.assertEqual((2, "error", status_name, None),
                                 (code, result["result"], result["status"], result["latest"]))
                self.assertIsNotNone(result["fetch_error"])

    def test_upgrade_plan_failure_preserves_valid_index_status(self):
        status = {"status": "ok", "pinned_version": "1.1.0", "latest_version": "1.2.0",
                  "advice": [{"package_version": "1.2.0", "advisory": "optional"}],
                  "active_advisories": []}
        fake = self.runner(status=status, plan_code=1)
        result, code = fake.run(repo=self.repo, fetch_permitted=False)
        self.assertEqual(2, code)
        self.assertEqual(("error", "ok", "upgrade_plan_failed", "1.2.0"),
                         (result["result"], result["status"], result["fetch_error"], result["latest"]))

    def test_archive_with_fetch_permission_fails_as_not_git_without_network(self):
        archive = Path(self.temp.name) / "archive package"
        archive.mkdir()
        fake = self.runner()
        result, code = fake.run(repo=archive, fetch_permitted=True)
        self.assertEqual((2, "not_git_checkout", "not_git_checkout", False),
                         (code, result["status"], result["fetch_error"], result["fetched"]))
        self.assertEqual([], fake.calls)

    def test_errors_after_status_keep_scalar_status_and_verified_versions(self):
        status = {"status": "ok", "pinned_version": "1.1.0", "latest_version": "1.2.0",
                  "advice": [{"package_version": "not-a-version", "advisory": "optional"}],
                  "active_advisories": []}
        fake = self.runner(status=status)
        result, code = fake.run(repo=self.repo, fetch_permitted=False)
        self.assertEqual(2, code)
        self.assertEqual("index_invalid", result["status"])
        self.assertEqual("release_status_invalid_shape", result["fetch_error"])
        self.assertEqual(("1.1.0", None), (result["pinned"], result["latest"]))

    def test_origin_is_checked_before_fetch_and_credential_urls_are_refused(self):
        for remote in ("https://wrong.example/repo.git", "https://user:pass@github.com/amazarache/ai-comm.git"):
            with self.subTest(remote=remote):
                fake = self.runner(remote=remote)
                result, code = fake.run(repo=self.repo, fetch_permitted=True)
                self.assertEqual(2, code)
                self.assertFalse(result["fetched"])
                self.assertEqual("origin_mismatch" if "@" not in remote else "credential_bearing_origin",
                                 result["fetch_error"])
                self.assertEqual(1, len(fake.calls))
                self.assertIn("remote.origin.url", fake.calls[0][0])

    def test_default_origin_comes_from_manifest_bound_head_not_dirty_code(self):
        source = (self.repo / "ai_comm.py").read_text(encoding="utf-8")
        (self.repo / "ai_comm.py").write_text(source.replace(checker.ac.RELEASE_ORIGIN,
                                                               "https://wrong.example/repo"),
                                                  encoding="utf-8")
        fake = self.runner()
        result, code = fake.run(repo=self.repo, fetch_permitted=True)
        self.assertEqual(0, code)
        self.assertTrue(result["fetched"])
        self.assertTrue(any("fetch" in args for args, _ in fake.calls if args[0] == "git"))

    def test_origin_length_is_bounded(self):
        with self.assertRaises(checker.CheckError) as caught:
            checker._normalize_origin("https://example.invalid/" + "x" * checker.MAX_ORIGIN_LENGTH)
        self.assertEqual("invalid_origin", caught.exception.code)

    def test_credential_bearing_expected_origin_is_refused_before_git(self):
        fake = self.runner()
        result, code = fake.run(repo=self.repo, fetch_permitted=True,
                                expected_origin="https://user:secret@example.invalid/repo")
        self.assertEqual(2, code)
        self.assertEqual("credential_bearing_origin", result["fetch_error"])
        self.assertEqual([], fake.calls)

    def test_expected_origin_override_and_url_normalization(self):
        expected = "https://mirror.example/ai-comm.git"
        fake = self.runner(remote="https://mirror.example/ai-comm/")
        result, code = fake.run(repo=self.repo, fetch_permitted=True, expected_origin=expected)
        self.assertEqual(0, code)
        self.assertTrue(result["fetched"])

    def test_fetch_failure_is_error_even_if_index_was_previously_valid(self):
        fake = self.runner(fetch_code=1)
        result, code = fake.run(repo=self.repo, fetch_permitted=True)
        self.assertEqual((2, "error", "fetch_failed", True),
                         (code, result["result"], result["fetch_error"], result["fetched"]))
        self.assertNotIn("must not leak", json.dumps(result))
        self.assertFalse(any(args[-1:] == ["release-status"] for args, _ in fake.calls))


    def test_package_identity_marks_dirty_git_and_verifies_archives(self):
        package = Path(self.temp.name) / "identity package"
        package.mkdir()
        env = checker.ac.git_env({"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                                  "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid"})
        protocol = b"protocol contract\n"
        (package / "PROTOCOL.md").write_bytes(protocol)
        (package / "ai_comm.py").write_text('VERSION = "1.2.0"\n', encoding="utf-8")
        manifest_files = []
        for name in ("ai_comm.py", "PROTOCOL.md"):
            content = (package / name).read_bytes()
            manifest_files.append({"path": name, "bytes": len(content),
                                   "sha256": checker.ac.sha(content)})
        manifest = {"format": "ai-comm-review-manifest/1",
                    "protocol_sha256": checker.ac.sha(protocol), "files": manifest_files}
        (package / "RELEASE-MANIFEST.json").write_bytes(checker.ac.canonical(manifest))
        archive_identity = checker.ac.package_identity(package)
        self.assertEqual(("1.2.0", None, "archive"),
                         (archive_identity["version"], archive_identity["commit"], archive_identity["source"]))
        (package / "ai_comm.py").write_text('VERSION = "1.2.1"\n', encoding="utf-8")
        unknown = checker.ac.package_identity(package)
        self.assertEqual(("unknown", None), (unknown["source"], unknown["version"]))
        self.assertEqual("archive_manifest_mismatch", unknown["reason"])
        (package / "ai_comm.py").write_text('VERSION = "1.2.0"\n', encoding="utf-8")
        subprocess.run(["git", "init", "--initial-branch=main", str(package)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=20)
        subprocess.run(["git", "-C", str(package), "add", "ai_comm.py", "PROTOCOL.md",
                        "RELEASE-MANIFEST.json"], check=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, env=env, timeout=20)
        subprocess.run(["git", "-C", str(package), "commit", "-m", "fixture"], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=20)
        clean = checker.ac.package_identity(package)
        self.assertEqual(("git", False), (clean["source"], clean["dirty"]))
        (package / "ai_comm.py").write_text('VERSION = "9.9.9"\n', encoding="utf-8")
        dirty = checker.ac.package_identity(package)
        self.assertEqual(("1.2.0", clean["commit"], "git", True),
                         (dirty["version"], dirty["commit"], dirty["source"], dirty["dirty"]))

    def test_state_version_note_is_scoped_to_explicit_current_identity(self):
        channel_path = Path(self.temp.name) / "state channel"
        checker.ac.Channel.init(channel_path, "state test", ["alpha", "beta"])
        channel = checker.ac.Channel(channel_path)
        channel.join("alpha", "codex", "alpha-session")
        channel.join("beta", "claude", "beta-session")
        state = channel.snapshot()
        state.current["beta"]["tool_version"] = "1.2.0"
        state._local_identity = {"version": "1.2.0", "commit": "a" * 40,
                                 "protocol_sha256": "b" * 64, "source": "git", "dirty": True}
        state.current["alpha"]["tool_version"] = "1.1.0"
        result = state.state(actor="alpha", session="alpha-session")
        self.assertEqual({"joined_with": "1.1.0", "running": "1.2.0"}, result["version_note"])
        self.assertEqual({"version": "1.2.0", "commit": "a" * 40,
                          "protocol_sha256": "b" * 64, "source": "git", "dirty": True},
                         result["local_package"])
        self.assertTrue(all(p["tool_version_basis"] == "join-time"
                            for p in result["participants"]))
        peer_result = state.state(actor="beta", session="beta-session")
        self.assertNotIn("version_note", peer_result)
        self.assertNotIn("version_note", state.state())
        with self.assertRaises(checker.ac.ProtocolError) as incomplete:
            state.state(actor="alpha")
        self.assertEqual("state_identity_incomplete", incomplete.exception.details["code"])

    def test_cli_argument_errors_emit_one_json_object(self):
        proc = subprocess.run([sys.executable, str(Path(checker.__file__)), "--unknown"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(2, proc.returncode)
        lines = proc.stdout.decode().splitlines()
        self.assertEqual(1, len(lines))
        value = json.loads(lines[0])
        self.assertEqual("ai-comm-release-check/1", value["schema"])
        self.assertEqual("invalid_arguments", value["fetch_error"])


if __name__ == "__main__":
    unittest.main()
