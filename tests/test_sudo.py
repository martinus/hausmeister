"""The sudo cache. All tests replace sudo with a script that only writes a log."""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from support import FEDORA_44, FIXTURE_STEPS, ROOT, load, make_step, write

hm = load()

SUDO_STEPS = ROOT / "tests" / "fixtures" / "sudo-steps"


class CacheSudoTest(unittest.TestCase):
    def test_result(self):
        self.assertTrue(hm.cache_sudo(["true"]))
        self.assertFalse(hm.cache_sudo(["false"]))
        self.assertFalse(hm.cache_sudo(["/nonexistent/hausmeister-test-sudo"]))


class SudoKeepAliveTest(unittest.TestCase):
    def test_refreshes_until_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "log"
            keep_alive = hm.SudoKeepAlive(["sh", "-c", f"echo refresh >> {log}"], interval=0.05)
            keep_alive.start()
            threading.Event().wait(0.5)
            keep_alive.stop()
            count = len(log.read_text().splitlines())
            self.assertGreaterEqual(count, 2)
            threading.Event().wait(0.2)
            self.assertEqual(len(log.read_text().splitlines()), count)


class UpdateWithSudoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.os_release = write(self.tmp / "os-release", FEDORA_44)
        self.log = self.tmp / "sudo.log"
        self.marker = self.tmp / "root-step-ran"
        self.enterContext(mock.patch.dict(os.environ, FAKE_OS_RELEASE=str(self.os_release), MARKER=str(self.marker)))
        self.out = self.enterContext(tempfile.TemporaryFile())
        self.enterContext(mock.patch("sys.stderr", new=self.enterContext(tempfile.TemporaryFile(mode="w+"))))
        self.sudo(validate_ok=True)
        self.enterContext(mock.patch.object(hm, "SUDO_REFRESH", self.fake("refresh", True)))
        self.enterContext(mock.patch.object(hm, "SUDO_REFRESH_INTERVAL", 0.05))

    def fake(self, word, ok):
        return ["sh", "-c", f"echo {word} >> {self.log}; exit {0 if ok else 1}"]

    def sudo(self, validate_ok):
        patcher = mock.patch.object(hm, "SUDO_VALIDATE", self.fake("validate", validate_ok))
        patcher.start()
        self.addCleanup(patcher.stop)

    def sudo_calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def update(self, *dirs):
        code = hm.update(list(dirs), self.os_release, self.tmp / "no-ostree-booted", self.out.fileno())
        self.out.seek(0)
        return code, self.out.read().decode()

    def test_password_asked_one_time_and_kept_alive(self):
        code, _ = self.update(SUDO_STEPS)
        self.assertEqual(code, 0)
        self.assertTrue(self.marker.exists())
        calls = self.sudo_calls()
        self.assertEqual(calls[0], "validate")
        self.assertEqual(calls.count("validate"), 1)
        self.assertGreaterEqual(calls.count("refresh"), 2)
        self.assertFalse(any(t.name == "sudo-keep-alive" for t in threading.enumerate()))

    def test_no_password(self):
        self.sudo(validate_ok=False)
        code, out = self.update(SUDO_STEPS)
        self.assertEqual(code, 1)
        self.assertFalse(self.marker.exists())
        self.assertIn("user step", out)
        self.assertIn(" ✘ root · not run\n   Reason: No sudo password\n", out)
        self.assertEqual(self.sudo_calls(), ["validate"])

    def test_no_prompt_without_sudo_step(self):
        self.update(FIXTURE_STEPS)
        self.assertEqual(self.sudo_calls(), [])

    def test_no_prompt_for_sudo_step_that_does_not_apply(self):
        steps = self.tmp / "steps"
        make_step(steps, "absent", info='name = "N"\nsudo = true\nneeds = "hausmeister-test-missing"\n')
        make_step(steps, "unused", info='name = "N"\nsudo = true\n', check="exit 1\n")
        make_step(steps, "off", info='name = "N"\nsudo = true\ndefault = false\n')
        make_step(steps, "user")
        code, _ = self.update(steps)
        self.assertEqual(code, 0)
        self.assertEqual(self.sudo_calls(), [])


if __name__ == "__main__":
    unittest.main()
