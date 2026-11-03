"""Tests for the per-release bootstrap (design 023 v3 with transport amendments A1/A2).

No network: a local HTTPS fixture with a throwaway CA, trusted only through SSL_CERT_FILE in
the bootstrap's environment. Tests run a separately rendered copy of the bootstrap whose only
differences are render-time values (the loopback base URL and small time limits); the
transport code itself is the production code. The fixture release contains a stub
install.sh that records its arguments instead of installing anything.
"""
import hashlib
import http.server
import io
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import render_bootstrap as rb  # noqa: E402

VERSION = "9.8.7"
OPENSSL = shutil.which("openssl")
STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_LOG"
case "$*" in
  *"--user install"*)
    case "${STUB_MODE:-ok}" in
      ok) echo '{"status": "installed"}' ;;
      installed) echo '{"code": "release_installed", "status": "stopped"}'; exit 2 ;;
      fail) echo '{"code": "suite_failed", "status": "stopped"}'; exit 2 ;;
    esac ;;
  *"--user register"*) [ "${STUB_REGISTER:-ok}" = ok ] || { echo register-failed; exit 2; } ;;
esac
exit 0
"""


def make_archive(extra=None):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = STUB.encode()
        info = tarfile.TarInfo("install.sh")
        info.size, info.mode = len(data), 0o755
        tar.addfile(info, io.BytesIO(data))
        if extra:
            extra(tar)
    return buf.getvalue()


def sums_for(archive, name=f"ai-comm-{VERSION}.tar.gz"):
    return f"{hashlib.sha256(archive).hexdigest()}  {name}\n".encode()


class Routes:
    def __init__(self):
        self.table, self.log, self.lock = {}, [], threading.Lock()


def make_handler(routes):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            with routes.lock:
                routes.log.append(self.path)
            action = routes.table.get(self.path, ("status", 404))
            kind = action[0]
            try:
                if kind == "status":
                    self.send_response(action[1])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif kind == "file":
                    body = action[1]
                    self.send_response(200)
                    self.send_header("Content-Length", str(action[2] if len(action) > 2 else len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif kind == "redirect":
                    self.send_response(302)
                    self.send_header("Location", action[1])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif kind == "redirect_drip":
                    self.send_response(302)
                    self.send_header("Location", action[1])
                    self.send_header("Content-Length", "100000")
                    self.end_headers()
                    for _ in range(200):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(0.3)
                elif kind == "drip":
                    self.send_response(200)
                    self.send_header("Content-Length", "100000")
                    self.end_headers()
                    for _ in range(200):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(0.3)
                elif kind == "stall":
                    time.sleep(30)
                elif kind == "reset":
                    self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
                    self.connection.close()
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
                pass
    return Handler


@unittest.skipUnless(OPENSSL, "openssl is needed to build the HTTPS fixture")
class BootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pki = tempfile.TemporaryDirectory(prefix="bootstrap-pki-")
        d = Path(cls.pki.name)
        run = lambda *a: subprocess.run(a, cwd=d, check=True, capture_output=True, timeout=60)
        run(OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem",
            "-days", "2", "-subj", "/CN=bootstrap test CA")
        run(OPENSSL, "req", "-newkey", "rsa:2048", "-nodes", "-keyout", "srv.key", "-out", "srv.csr",
            "-subj", "/CN=127.0.0.1")
        (d / "ext").write_text("subjectAltName=IP:127.0.0.1\nbasicConstraints=CA:FALSE\n")
        run(OPENSSL, "x509", "-req", "-in", "srv.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
            "-out", "srv.pem", "-days", "2", "-extfile", "ext")
        run(OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "other.key", "-out", "other.pem",
            "-days", "2", "-subj", "/CN=untrusted")
        cls.ca = d / "ca.pem"
        cls.other_ca = d / "other.pem"
        cls.routes = Routes()
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.routes))
        cls.server.daemon_threads = True
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(d / "srv.pem", d / "srv.key")
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.plain_routes = Routes()
        cls.plain = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.plain_routes))
        cls.plain.daemon_threads = True
        threading.Thread(target=cls.plain.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.plain.shutdown()
        cls.pki.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bootstrap-test-")
        self.dir = Path(self.tmp.name)
        for sub in ("home", "data", "tmp", "bin"):
            (self.dir / sub).mkdir()
        self.log = self.dir / "stub.log"
        self.routes.table.clear()
        self.routes.log.clear()
        self.plain_routes.table.clear()
        self.plain_routes.log.clear()
        self.base = f"https://127.0.0.1:{self.port}/v{VERSION}"
        self.prefix = f"/v{VERSION}"

    def tearDown(self):
        self.tmp.cleanup()

    def serve_release(self, archive=None, sums=None):
        archive = make_archive() if archive is None else archive
        self.routes.table[f"{self.prefix}/ai-comm-{VERSION}.tar.gz"] = ("file", archive)
        self.routes.table[f"{self.prefix}/SHA256SUMS"] = ("file", sums_for(archive) if sums is None else sums)
        return archive

    def script(self, base=None, **limits):
        limits = dict({"TOTAL_SECONDS": 3, "SOCKET_SECONDS": 2}, **limits)
        path = self.dir / "install-ai-comm.sh"
        path.write_bytes(rb.render(VERSION, base=base or self.base, **limits))
        return path

    def env(self, ca=True, **extra):
        env = {"HOME": str(self.dir / "home"), "XDG_DATA_HOME": str(self.dir / "data"),
               "TMPDIR": str(self.dir / "tmp"), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "STUB_LOG": str(self.log)}
        if ca:
            env["SSL_CERT_FILE"] = str(self.ca if ca is True else ca)
        env.update(extra)
        return env

    def run_script(self, *args, base=None, env=None, timeout=60, **limits):
        path = self.script(base=base, **limits)
        return subprocess.run(["sh", str(path), *args], env=env or self.env(), capture_output=True,
                              text=True, timeout=timeout)

    def stub_calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def assert_clean(self):
        self.assertEqual([], os.listdir(self.dir / "tmp"), "the private temporary directory must be removed")

    def assert_stopped_before_install(self, result):
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual([], self.stub_calls(), "no downloaded code may run after a failure")
        self.assert_clean()

    # Success and the installer call
    def test_success_runs_the_release_installer_with_consent(self):
        self.serve_release()
        result = self.run_script()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual([f"--user install --version {VERSION} --fetch-permitted"], self.stub_calls())
        self.assertIn("checksum ok", result.stdout)
        self.assert_clean()

    def test_five_redirect_hops_are_followed(self):
        archive = self.serve_release()
        name = f"ai-comm-{VERSION}.tar.gz"
        for i in range(5):
            target = f"/hop{i + 1}" if i < 4 else f"{self.prefix}/real-{name}"
            self.routes.table[f"/hop{i}" if i else f"{self.prefix}/{name}"] = ("redirect", f"https://127.0.0.1:{self.port}{target}")
        self.routes.table[f"{self.prefix}/real-{name}"] = ("file", archive)
        result = self.run_script()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    # Transport refusals (A1/A2)
    def test_redirect_to_http_is_refused_without_contacting_the_target(self):
        self.serve_release()
        name = f"ai-comm-{VERSION}.tar.gz"
        self.plain_routes.table[f"/{name}"] = ("file", b"never")
        self.routes.table[f"{self.prefix}/{name}"] = ("redirect", f"http://127.0.0.1:{self.plain.server_address[1]}/{name}")
        result = self.run_script()
        self.assert_stopped_before_install(result)
        self.assertEqual([], self.plain_routes.log)
        self.assertIn("refusing a redirect away from HTTPS", result.stderr)

    def test_transport_refuses_a_plain_http_url(self):
        source = rb.render(VERSION).decode()
        start = source.index("transport='") + len("transport='")
        transport = source[start:source.index("\n'\n", start)]
        out = self.dir / "out"
        done = subprocess.run([sys.executable, "-I", "-c", transport,
                               f"http://127.0.0.1:{self.plain.server_address[1]}/x", str(out)],
                              capture_output=True, text=True, timeout=30)
        self.assertNotEqual(0, done.returncode)
        self.assertIn("non-HTTPS", done.stderr)
        self.assertEqual([], self.plain_routes.log)
        self.assertFalse(out.exists())

    def test_untrusted_certificate_and_wrong_hostname_are_refused(self):
        self.serve_release()
        untrusted = self.run_script(env=self.env(ca=self.other_ca))
        self.assert_stopped_before_install(untrusted)
        self.assertIn("CERTIFICATE_VERIFY_FAILED", untrusted.stderr)
        mismatch = self.run_script(base=f"https://localhost:{self.port}/v{VERSION}")
        self.assert_stopped_before_install(mismatch)
        self.assertRegex(mismatch.stderr, r"CERTIFICATE_VERIFY_FAILED|[Hh]ostname mismatch|doesn't match")

    def test_sixth_redirect_hop_is_refused_before_its_request(self):
        self.serve_release()
        name = f"ai-comm-{VERSION}.tar.gz"
        self.routes.table[f"{self.prefix}/{name}"] = ("redirect", f"https://127.0.0.1:{self.port}/a")
        self.routes.table["/a"] = ("redirect", f"https://127.0.0.1:{self.port}/b")
        self.routes.table["/b"] = ("redirect", f"https://127.0.0.1:{self.port}/a")
        result = self.run_script()
        self.assert_stopped_before_install(result)
        self.assertEqual(6, len(self.routes.log), self.routes.log)
        self.assertIn("too many redirects", result.stderr)

    def test_http_errors_reset_and_short_body_stop(self):
        name = f"ai-comm-{VERSION}.tar.gz"
        archive = make_archive()
        for action in (("status", 404), ("status", 500), ("reset",), ("file", archive, len(archive) + 50)):
            with self.subTest(action=action[0] if action[0] != "status" else action[1]):
                self.log.unlink(missing_ok=True)
                self.serve_release(archive)
                self.routes.table[f"{self.prefix}/{name}"] = action
                result = self.run_script()
                self.assert_stopped_before_install(result)
                self.assertIn(f"download of ai-comm-{VERSION}.tar.gz failed", result.stderr)

    def test_stalled_server_hits_the_socket_timeout(self):
        self.serve_release()
        self.routes.table[f"{self.prefix}/ai-comm-{VERSION}.tar.gz"] = ("stall",)
        began = time.monotonic()
        result = self.run_script(TOTAL_SECONDS=10, SOCKET_SECONDS=1)
        self.assert_stopped_before_install(result)
        self.assertLess(time.monotonic() - began, 9)
        self.assertRegex(result.stderr, r"[Tt]imed? ?out")

    def test_slow_drip_hits_the_total_deadline(self):
        self.serve_release()
        self.routes.table[f"{self.prefix}/ai-comm-{VERSION}.tar.gz"] = ("drip",)
        began = time.monotonic()
        result = self.run_script(TOTAL_SECONDS=3, SOCKET_SECONDS=2)
        self.assert_stopped_before_install(result)
        self.assertLess(time.monotonic() - began, 15)
        self.assertIn("total download time exceeded", result.stderr)

    def test_endless_redirect_body_hits_the_deadline_and_target_is_not_requested(self):
        self.serve_release()
        name = f"ai-comm-{VERSION}.tar.gz"
        self.routes.table[f"{self.prefix}/{name}"] = ("redirect_drip", f"https://127.0.0.1:{self.port}/target")
        self.routes.table["/target"] = ("file", b"never")
        result = self.run_script(TOTAL_SECONDS=3, SOCKET_SECONDS=2)
        self.assert_stopped_before_install(result)
        self.assertNotIn("/target", self.routes.log)
        self.assertIn("total download time exceeded", result.stderr)

    # Checksum and archive (v3)
    def test_checksum_record_must_be_exactly_one_and_match(self):
        archive = make_archive()
        name = f"ai-comm-{VERSION}.tar.gz"
        good = hashlib.sha256(archive).hexdigest()
        cases = {"mismatch": f"{'0' * 64}  {name}\n", "missing": f"{good}  other.tar.gz\n",
                 "duplicate": f"{good}  {name}\n{good}  {name}\n", "malformed": f"{good[:63]}  {name}\n",
                 "path trick": f"{good}  ../{name}\n"}
        for label, sums in cases.items():
            with self.subTest(label):
                self.serve_release(archive, sums.encode())
                self.assert_stopped_before_install(self.run_script())
        self.serve_release(archive, f"{good} *{name}\n{'f' * 64}  unrelated\n".encode())
        self.assertEqual(0, self.run_script().returncode)

    def test_unsafe_archive_members_are_refused(self):
        def symlink(tar):
            info = tarfile.TarInfo("evil")
            info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
            tar.addfile(info)

        def dotdot(tar):
            info = tarfile.TarInfo("../escape")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        for label, extra in (("symlink", symlink), ("dotdot", dotdot)):
            with self.subTest(label):
                self.serve_release(make_archive(extra))
                self.assert_stopped_before_install(self.run_script())

    # Environment and options
    def test_root_is_refused(self):
        fake = self.dir / "bin" / "id"
        fake.write_text("#!/bin/sh\necho 0\n")
        fake.chmod(0o755)
        self.serve_release()
        result = self.run_script(env=self.env(PATH=f"{self.dir / 'bin'}:{os.environ['PATH']}"))
        self.assert_stopped_before_install(result)
        self.assertIn("root", result.stderr)
        self.assertEqual([], self.routes.log)

    def test_missing_git_is_reported(self):
        bin_dir = self.dir / "bin"
        for tool in ("sh", "python3", "id", "mktemp", "chmod", "rm", "mkdir", "cat"):
            found = shutil.which(tool)
            if found:
                (bin_dir / tool).symlink_to(found)
        self.serve_release()
        result = self.run_script(env=self.env(PATH=str(bin_dir)))
        self.assert_stopped_before_install(result)
        self.assertIn("git 2.25", result.stderr)

    def test_release_already_present_is_a_refusal_and_skips_optional_steps(self):
        self.serve_release()
        result = self.run_script("--register", str(self.dir / "skills"), "--launcher", str(self.dir / "bin"),
                                 env=self.env(STUB_MODE="installed"))
        self.assertNotEqual(0, result.returncode)
        self.assertIn("already present", result.stdout)
        self.assertIn("were not performed", result.stdout)
        self.assertEqual(1, len(self.stub_calls()))
        self.assert_clean()

    def test_installer_failure_is_reported(self):
        self.serve_release()
        result = self.run_script(env=self.env(STUB_MODE="fail"))
        self.assertNotEqual(0, result.returncode)
        self.assertIn("installer stopped", result.stdout)
        self.assert_clean()

    def test_optional_steps_only_when_asked(self):
        self.serve_release()
        self.assertEqual(0, self.run_script().returncode)
        self.assertEqual(1, len(self.stub_calls()))
        self.log.unlink()
        skills_a, skills_b, bin_dir = self.dir / "s a", self.dir / "s2", self.dir / "bin"
        result = self.run_script("--register", str(skills_a), "--register", str(skills_b), "--launcher", str(bin_dir))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual([f"--user install --version {VERSION} --fetch-permitted",
                          f"--user register --skills-dir {skills_a}", f"--user register --skills-dir {skills_b}",
                          f"--user launcher --bin-dir {bin_dir}"], self.stub_calls())

    def test_failed_registration_is_reported_after_a_successful_install(self):
        self.serve_release()
        result = self.run_script("--register", str(self.dir / "s"), env=self.env(STUB_REGISTER="fail"))
        self.assertEqual(1, result.returncode)
        self.assertIn("cache install above succeeded", result.stdout)

    def test_bad_options_are_refused_before_any_download(self):
        for args in (("--register",), ("--launcher", "a", "--launcher", "b"), ("--bogus",)):
            with self.subTest(args=args):
                result = self.run_script(*args)
                self.assertNotEqual(0, result.returncode)
        self.assertEqual([], self.routes.log)

    def test_a_decoy_project_is_untouched(self):
        project = self.dir / "home" / "project"
        (project / ".tools" / "ai-comm").mkdir(parents=True)
        (project / ".ai-comm").mkdir()
        (project / ".tools" / "ai-comm" / "ai_comm.py").write_text("pin\n")
        (project / ".ai-comm" / "x").write_text("channel\n")
        before = {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}
        self.serve_release()
        self.assertEqual(0, self.run_script(env=self.env(PWD=str(project))).returncode)
        self.assertEqual(before, {p: p.read_bytes() for p in project.rglob("*") if p.is_file()})

    def test_body_runs_only_from_the_final_call(self):
        source = rb.render(VERSION, base=self.base).decode()
        cut = source[:source.index("  say \"checksum ok\"")]
        path = self.dir / "cut.sh"
        path.write_text(cut)
        self.serve_release()
        result = subprocess.run(["sh", str(path)], env=self.env(), capture_output=True, text=True, timeout=30)
        self.assertEqual([], self.routes.log, "a script cut inside the function body must not run it")
        self.assertEqual([], self.stub_calls())
        del result


class RenderTests(unittest.TestCase):
    def test_render_is_deterministic_and_has_no_runtime_override(self):
        first, second = rb.render("1.2.3"), rb.render("1.2.3")
        self.assertEqual(first, second)
        text = first.decode()
        self.assertNotIn("\r", text)
        self.assertIn("base='https://github.com/amazarache/ai-comm/releases/download/v1.2.3'", text)
        self.assertIn("TOTAL, SOCKET, HOPS, LIMIT = 300, 20, 5, 67108864", text)
        for hook in ("environ", "getenv", "AI_COMM_", "BOOTSTRAP_", "SSL_CERT", "verify_mode", "CERT_NONE",
                     "check_hostname", "_create_unverified"):
            self.assertNotIn(hook, text, hook)
        self.assertNotIn("sudo", text)
        self.assertNotRegex(text, r"@[A-Z_]+@")

    def test_render_rejects_bad_input(self):
        for bad in ("1.2", "v1.2.3", "1.2.3; rm"):
            with self.assertRaises(ValueError):
                rb.render(bad)
        with self.assertRaises(ValueError):
            rb.render("1.2.3", base="http://example.invalid")
        with self.assertRaises(ValueError):
            rb.render("1.2.3", TOTAL_SECONDS=0)

    def test_install_doc_table_matches_this_trees_bootstrap(self):
        done = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "scripts" / "render_bootstrap.py"), "--check"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(0, done.returncode, done.stderr)

    def test_posix_syntax(self):
        data = rb.render("1.2.3")
        for shell in ("sh", "dash", "bash"):
            path = shutil.which(shell)
            if path:
                with self.subTest(shell=shell):
                    done = subprocess.run([path, "-n"], input=data, capture_output=True, timeout=30)
                    self.assertEqual(0, done.returncode, done.stderr)


class VerifyFirstTests(unittest.TestCase):
    """The documented verify-first command, from a persistent parent shell with its own traps."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="verify-first-")
        self.dir = Path(self.tmp.name)
        (self.dir / "bin").mkdir()
        (self.dir / "tmp").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def command(self):
        doc = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
        start = doc.index("( f=$(mktemp)")
        return doc[start:doc.index(" )\n", start) + 2].replace("\\\n", " ")

    def run_parent(self, payload, digest=None, curl_fails=False, kill=False):
        fixture = self.dir / "payload.sh"
        fixture.write_text(payload)
        digest = digest or hashlib.sha256(fixture.read_bytes()).hexdigest()
        curl = self.dir / "bin" / "curl"
        curl.write_text("#!/bin/sh\n" + ("exit 22\n" if curl_fails else
                        f'while [ $# -gt 0 ]; do [ "$1" = -o ] && cp "{fixture}" "$2"; shift; done\n'))
        curl.chmod(0o755)
        cmd = self.command().replace("<sha256>", digest)
        parent = ("trap 'echo parent-usr1' USR1\n"
                  + (f"{cmd} &\npid=$!\nsleep 1\nkill -TERM $pid\nwait $pid\n" if kill else f"{cmd}\n")
                  + "echo rc=$?\ntrap -p USR1 2>/dev/null || trap\nls \"$TMPDIR\"\n")
        env = {"PATH": f"{self.dir / 'bin'}:{os.environ['PATH']}", "TMPDIR": str(self.dir / "tmp"),
               "HOME": str(self.dir)}
        return subprocess.run(["bash", "-c", parent], env=env, capture_output=True, text=True, timeout=60)

    def assert_cleaned_and_parent_trap_kept(self, done):
        self.assertIn("parent-usr1", done.stdout)
        self.assertEqual([], os.listdir(self.dir / "tmp"))

    @unittest.skipUnless(shutil.which("bash") and shutil.which("sha256sum"), "bash and sha256sum needed")
    def test_success_runs_and_cleans(self):
        marker = self.dir / "ran"
        done = self.run_parent(f"touch {marker}\n")
        self.assertTrue(marker.exists(), done.stdout + done.stderr)
        self.assertIn("rc=0", done.stdout)
        self.assert_cleaned_and_parent_trap_kept(done)

    @unittest.skipUnless(shutil.which("bash") and shutil.which("sha256sum"), "bash and sha256sum needed")
    def test_checksum_failure_does_not_run_and_cleans(self):
        marker = self.dir / "ran"
        done = self.run_parent(f"touch {marker}\n", digest="0" * 64)
        self.assertFalse(marker.exists())
        self.assertNotIn("rc=0", done.stdout)
        self.assert_cleaned_and_parent_trap_kept(done)

    @unittest.skipUnless(shutil.which("bash") and shutil.which("sha256sum"), "bash and sha256sum needed")
    def test_download_failure_does_not_run_and_cleans(self):
        done = self.run_parent("touch /nonexistent-never\n", curl_fails=True)
        self.assertNotIn("rc=0", done.stdout)
        self.assert_cleaned_and_parent_trap_kept(done)

    @unittest.skipUnless(shutil.which("bash") and shutil.which("sha256sum"), "bash and sha256sum needed")
    def test_signal_cleans(self):
        done = self.run_parent("sleep 20\n", kill=True)
        self.assert_cleaned_and_parent_trap_kept(done)


if __name__ == "__main__":
    unittest.main()
