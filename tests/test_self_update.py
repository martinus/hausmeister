"""Milestone 6: the self-update. All git repositories are temporary, and the
git config of the user is switched off."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import ROOT, load, write

hm = load()

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.org",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.org",
}


class Exec(Exception):
    pass


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def describe(repo):
    return subprocess.run(
        ["git", "-C", str(repo), "describe", "--tags", "--exact-match"], capture_output=True, text=True
    ).stdout.strip()


class GitTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("HAUSMEISTER_", "GIT_"))}
        self.enterContext(mock.patch.dict(os.environ, clean | GIT_ENV, clear=True))

    def make_origin(self, versions, core=None):
        """A bare repository with one tag per version. core(version) gives the
        text of hausmeister.py, the step prints the version."""
        work = self.tmp / "work"
        git(self.tmp, "init", "-q", "-b", "main", str(work))
        for version in versions:
            write(work / "hausmeister.py", core(version) if core else f"# {version}\n")
            write(work / "steps" / "marker" / "info.toml", 'name = "Marker"\n')
            write(work / "steps" / "marker" / "update", f'#!/bin/sh\necho "step {version}"\n', executable=True)
            git(work, "add", "-A")
            git(work, "commit", "-q", "-m", version)
            git(work, "tag", version)
        origin = self.tmp / "origin.git"
        git(self.tmp, "clone", "-q", "--bare", str(work), str(origin))
        return origin

    def make_clone(self, origin, tag):
        clone = self.tmp / "clone"
        git(self.tmp, "clone", "-q", str(origin), str(clone))
        if tag:
            git(clone, "checkout", "-q", "--detach", tag)
        return clone

    def self_update(self, clone):
        self.calls = []

        def exec_fn(file, argv):
            self.calls.append((file, argv))
            raise Exec

        return hm.self_update(clone / "hausmeister.py", ["update"], exec_fn)


class SelfUpdateTest(GitTestCase):
    def test_updates_to_the_newest_tag_and_restarts(self):
        clone = self.make_clone(self.make_origin(["v0.1.0", "v0.2.0"]), "v0.1.0")
        script = clone / "hausmeister.py"
        with self.assertRaises(Exec):
            self.self_update(clone)
        self.assertEqual(describe(clone), "v0.2.0")
        self.assertEqual(script.read_text(), "# v0.2.0\n")
        self.assertIn("step v0.2.0", (clone / "steps" / "marker" / "update").read_text())
        self.assertEqual(self.calls, [(sys.executable, [sys.executable, str(script), "update"])])
        self.assertEqual(os.environ["HAUSMEISTER_UPDATED"], "1")
        result = hm.SelfUpdate.decode(os.environ["HAUSMEISTER_SELF"])
        self.assertEqual((result.status, result.text), ("ok", "v0.1.0 → v0.2.0"))

    def test_version_order(self):
        clone = self.make_clone(self.make_origin(["v0.9.0", "v0.10.0"]), "v0.9.0")
        with self.assertRaises(Exec):
            self.self_update(clone)
        self.assertEqual(describe(clone), "v0.10.0")

    def test_already_the_newest(self):
        clone = self.make_clone(self.make_origin(["v0.1.0", "v0.2.0"]), "v0.2.0")
        result = self.self_update(clone)
        self.assertEqual((result.status, result.text, self.calls), ("ok", "v0.2.0, the newest", []))

    def test_new_tag_after_the_clone(self):
        origin = self.make_origin(["v0.1.0"])
        clone = self.make_clone(origin, "v0.1.0")
        work = self.tmp / "work"
        write(work / "hausmeister.py", "# v0.2.0\n")
        git(work, "commit", "-q", "-am", "v0.2.0")
        git(work, "tag", "v0.2.0")
        git(work, "push", "-q", str(origin), "main", "v0.2.0")
        with self.assertRaises(Exec):
            self.self_update(clone)
        self.assertEqual(describe(clone), "v0.2.0")

    def test_local_changes(self):
        clone = self.make_clone(self.make_origin(["v0.1.0", "v0.2.0"]), "v0.1.0")
        write(clone / "steps" / "mine" / "update", "x")
        result = self.self_update(clone)
        self.assertEqual(result.status, "skipped")
        self.assertIn("local changes", result.text)
        self.assertEqual((describe(clone), self.calls), ("v0.1.0", []))

    def test_branch_is_a_development_checkout(self):
        clone = self.make_clone(self.make_origin(["v0.1.0", "v0.2.0"]), None)
        git(clone, "checkout", "-q", "-b", "work", "v0.1.0")
        result = self.self_update(clone)
        self.assertEqual((result.status, result.text, self.calls), ("skipped", "development checkout", []))

    def test_unreachable_remote(self):
        clone = self.make_clone(self.make_origin(["v0.1.0"]), "v0.1.0")
        git(clone, "remote", "set-url", "origin", str(self.tmp / "missing.git"))
        result = self.self_update(clone)
        self.assertEqual((result.status, result.text), ("skipped", "v0.1.0, git fetch failed (no network?)"))
        self.assertEqual(describe(clone), "v0.1.0")

    def test_not_a_git_clone(self):
        folder = self.tmp / "plain"
        write(folder / "hausmeister.py", "")
        result = self.self_update(folder)
        self.assertEqual(result.status, "skipped")
        self.assertTrue(result.text.startswith("not a git clone"))


class RestartEndToEndTest(GitTestCase):
    """A real hausmeister.py at v0.1.0 updates itself to v0.2.0 with the real
    os.execv and finishes the run with the new code and the new step."""

    def test_run_finishes_with_new_code_and_new_step(self):
        source = (ROOT / "hausmeister.py").read_text()
        header = " hausmeister update · "
        self.assertIn(header, source)

        def core(version):
            return source.replace(header, f" hausmeister update {version} · ")

        clone = self.make_clone(self.make_origin(["v0.1.0", "v0.2.0"], core), "v0.1.0")
        env = dict(os.environ, XDG_CONFIG_HOME=str(self.tmp / "config"), XDG_STATE_HOME=str(self.tmp / "state"))
        write(self.tmp / "config" / "hausmeister" / "config.toml", "[general]\ntmux = false\n")
        proc = subprocess.run(
            [sys.executable, str(clone / "hausmeister.py"), "update"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        self.assertEqual(describe(clone), "v0.2.0")
        self.assertIn(" hausmeister update v0.2.0 · ", proc.stdout, proc.stderr)
        self.assertIn("step v0.2.0", proc.stdout)
        self.assertNotIn("step v0.1.0", proc.stdout)
        self.assertRegex(proc.stdout, r" ok +self +v0\.1\.0 → v0\.2\.0 ")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class EncodeTest(unittest.TestCase):
    def test_round_trip(self):
        result = hm.SelfUpdate("ok", "v0.1.0 → v0.2.0", 1.25)
        self.assertEqual(hm.SelfUpdate.decode(result.encode()), result)

    def test_invalid(self):
        for value in ("", "ok", "maybe\tx\t1", "ok\tx\tnot a number"):
            self.assertIsNone(hm.SelfUpdate.decode(value))


class RunSelfUpdateTest(unittest.TestCase):
    def setUp(self):
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("HAUSMEISTER_", "TMUX"))}
        self.enterContext(mock.patch.dict(os.environ, clean, clear=True))
        self.self_update = self.enterContext(
            mock.patch.object(hm, "self_update", return_value=hm.SelfUpdate("ok", "v1, the newest"))
        )
        self.session = self.enterContext(mock.patch.object(hm, "tmux_session_running", return_value=False))

    def test_after_restart(self):
        os.environ["HAUSMEISTER_UPDATED"] = "1"
        os.environ["HAUSMEISTER_SELF"] = "ok\tv1 → v2\t2.000"
        self.assertEqual(hm.run_self_update(True, ["update"]), hm.SelfUpdate("ok", "v1 → v2", 2.0))
        self.self_update.assert_not_called()

    def test_off_in_config(self):
        self.assertEqual(hm.run_self_update(False, ["update"]).text, "self_update = false in the config")
        self.self_update.assert_not_called()

    def test_run_in_progress(self):
        self.session.return_value = True
        self.assertEqual(hm.run_self_update(True, ["update"]).text, "a run is in progress")
        self.self_update.assert_not_called()

    def test_runs_and_remembers_the_result(self):
        self.assertEqual(hm.run_self_update(True, ["update"]).text, "v1, the newest")
        self.self_update.assert_called_once_with(hm.SCRIPT, ["update"], os.execv)
        self.assertEqual(os.environ["HAUSMEISTER_SELF"], hm.SelfUpdate("ok", "v1, the newest").encode())


class OverviewSelfTest(unittest.TestCase):
    def run_update(self, self_result):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryFile() as out:
            os_release = write(Path(tmp) / "os-release", "ID=fedora\nVERSION_ID=44\n")
            code = hm.update([Path(tmp) / "none"], os_release, Path(tmp) / "x", out.fileno(), self_result=self_result)
            out.seek(0)
            return code, out.read().decode()

    def test_ok(self):
        code, text = self.run_update(hm.SelfUpdate("ok", "v0.3.0 → v0.4.0", 1.2))
        self.assertEqual(code, 0)
        self.assertRegex(text, r"\n ok +self +v0\.3\.0 → v0\.4\.0 +1\.2 s\n")

    def test_skipped_is_no_failure(self):
        code, text = self.run_update(hm.SelfUpdate("skipped", "development checkout"))
        self.assertEqual(code, 0)
        self.assertRegex(text, r"\n skipped +self +development checkout")
        self.assertNotIn("✘", text)

    def test_failed(self):
        code, text = self.run_update(hm.SelfUpdate("FAILED", "git checkout v0.4.0: error"))
        self.assertEqual(code, 1)
        self.assertIn(" ✘ self · exit code 0\n   Reason: git checkout v0.4.0: error", text)


class TmuxForwardTest(unittest.TestCase):
    def test_self_result_goes_into_the_tmux_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmux = write(Path(tmp) / "tmux", "#!/bin/sh\nexit 1\n", executable=True)
            calls = []
            env = {"PATH": tmp, "HAUSMEISTER_SELF": "ok\tv1 → v2\t1.000"}
            hm.start_tmux(env, True, True, lambda file, argv: calls.append(argv))
        argv = calls[0]
        self.assertEqual(argv[0], str(tmux))
        index = argv.index("HAUSMEISTER_SELF=ok\tv1 → v2\t1.000")
        self.assertEqual(argv[index - 1], "-e")


if __name__ == "__main__":
    unittest.main()
