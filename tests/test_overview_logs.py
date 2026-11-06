"""Milestone 4: step logs, reasons, the lines in the overview, colours."""

import datetime
import os
import pty
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import FEDORA_44, FIXTURE_STEPS, load, write

hm = load()

DISTRO = hm.Distro("fedora", "44", "Fedora Linux 44")


class ReasonTest(unittest.TestCase):
    def test_rules(self):
        self.assertEqual(hm.core_reason(1, b"", cancelled=True), "cancelled")
        self.assertIn("sudo cache expired", hm.core_reason(1, b"x\r\nsudo: a password is required\r\n", False))
        self.assertEqual(hm.core_reason(127, b"", False), "command not found")
        self.assertEqual(hm.core_reason(126, b"", False), "not executable")
        self.assertEqual(hm.core_reason(-9, b"", False), "killed by signal 9")
        self.assertEqual(hm.core_reason(1, b"", False), "")


class ImportantLinesTest(unittest.TestCase):
    def test_error_lines_first(self):
        lines = ["Downloading", "Error: no network", "x", "E: Unable to fetch", "unpack FAILED", "done", "the end"]
        self.assertEqual(hm.important_lines(lines), ["Error: no network", "E: Unable to fetch", "unpack FAILED"])

    def test_last_lines_without_error(self):
        lines = [f"line {i}" for i in range(8)] + ["see E: in the middle is no match"]
        self.assertEqual(hm.important_lines(lines), ["line 4", "line 5", "line 6", "line 7", lines[-1]])

    def test_at_most_five(self):
        lines = [f"error {i}" for i in range(9)]
        self.assertEqual(hm.important_lines(lines), [f"error {i}" for i in range(4, 9)])


class RunnerLogsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.os_release = write(self.tmp / "os-release", FEDORA_44)
        self.enterContext(mock.patch.dict(os.environ, FAKE_OS_RELEASE=str(self.os_release)))
        self.out = self.enterContext(tempfile.TemporaryFile())
        self.steps = {s.id: s for s in hm.load_steps([FIXTURE_STEPS])}
        self.logs = self.tmp / "last-run"
        self.logs.mkdir()

    def run_steps(self, *ids):
        steps = [self.steps[i] for i in ids]
        results, _ = hm.run_steps(steps, hm.base_env(DISTRO), self.os_release, self.out.fileno(), True, self.logs)
        return {r.step.id: r for r in results}

    def test_log_per_step(self):
        results = self.run_steps("ok", "fail")
        self.assertEqual(results["ok"].log, self.logs / "ok.log")
        self.assertEqual((self.logs / "ok.log").read_bytes(), b"all fine\r\n")
        self.assertIn(b"something broke", (self.logs / "fail.log").read_bytes())

    def test_no_log_dir(self):
        steps = [self.steps["ok"]]
        results, _ = hm.run_steps(steps, hm.base_env(DISTRO), self.os_release, self.out.fileno())
        self.assertIsNone(results[0].log)

    def test_core_reasons(self):
        results = self.run_steps("killed", "sudo-expired", "missing-command", "reason")
        self.assertEqual(results["killed"].reason, "killed by signal 15")
        self.assertIn("sudo cache expired", results["sudo-expired"].reason)
        self.assertEqual(results["missing-command"].reason, "command not found")
        self.assertEqual(results["reason"].reason, "Network is down")  # the reason file comes first

    def test_ok_step_has_no_reason(self):
        self.assertEqual(self.run_steps("ok")["ok"].reason, "")


class PrepareLogDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch("sys.stderr", new=self.enterContext(tempfile.TemporaryFile(mode="w+"))))

    def test_removes_only_old_logs(self):
        logs = self.tmp / "state" / "hausmeister" / "last-run"
        write(logs / "dnf.log", "old")
        write(logs / "notes.txt", "keep me")
        (logs / "folder.log").mkdir()
        self.assertEqual(hm.prepare_log_dir(logs), logs)
        self.assertEqual(sorted(p.name for p in logs.iterdir()), ["folder.log", "notes.txt"])

    def test_creates_the_folder(self):
        logs = self.tmp / "a" / "b"
        self.assertEqual(hm.prepare_log_dir(logs), logs)
        self.assertTrue(logs.is_dir())

    def test_unusable_folder(self):
        blocker = write(self.tmp / "file", "")
        self.assertIsNone(hm.prepare_log_dir(blocker / "last-run"))

    def test_location(self):
        with mock.patch.dict(os.environ, XDG_STATE_HOME=str(self.tmp)):
            self.assertEqual(hm.log_dir_path(), self.tmp / "hausmeister" / "last-run")

    def test_update_writes_logs(self):
        os_release = write(self.tmp / "os-release", FEDORA_44)
        logs = self.tmp / "logs"
        with mock.patch.dict(os.environ, FAKE_OS_RELEASE=str(os_release)), tempfile.TemporaryFile() as out:
            hm.update([FIXTURE_STEPS], os_release, self.tmp / "no-ostree", out.fileno(), log_dir=logs)
            out.seek(0)
            text = out.read().decode()
        self.assertTrue((logs / "ok.log").is_file())
        self.assertIn(f"   Full log: {logs / 'fail.log'}\n", text)


class OverviewDetailsTest(unittest.TestCase):
    def render(self, result, colours=False):
        started = datetime.datetime(2026, 11, 6, 4, 19, 0)
        return hm.render_overview(DISTRO, [result.step], [result], started, started, False, colours)

    def failed(self, **kwargs):
        step = hm.Step(id="rustup", path=Path("/x"), name="Rust toolchains", state=hm.INSTALLED)
        return hm.Result(step, "FAILED", 1, 6.2, **kwargs)

    def test_important_lines_and_log(self):
        log = Path.home() / ".local" / "state" / "hausmeister" / "last-run" / "rustup.log"
        tail = ["info: syncing", "error: could not download", "info: rollback", "done"]
        text = self.render(self.failed(tail=tail, log=log))
        self.assertIn(
            "   > error: could not download\n   Full log: ~/.local/state/hausmeister/last-run/rustup.log", text
        )
        self.assertNotIn("info: syncing", text)

    def test_log_outside_home(self):
        self.assertEqual(hm.short_path(Path("/var/log/x.log")), "/var/log/x.log")

    def test_colours(self):
        plain = self.render(self.failed())
        self.assertNotIn("\x1b[", plain)
        coloured = self.render(self.failed(), colours=True)
        self.assertIn(f" {hm.RED}FAILED    {hm.RESET}rustup", coloured)
        self.assertIn(f"{hm.RED} ✘ rustup · exit code 1{hm.RESET}", coloured)

    def test_use_colours(self):
        master, slave = pty.openpty()
        try:
            self.assertTrue(hm.use_colours(slave, {"TERM": "xterm-256color"}))
            self.assertFalse(hm.use_colours(slave, {"TERM": "dumb"}))
            self.assertFalse(hm.use_colours(slave, {}))
            self.assertFalse(hm.use_colours(slave, {"TERM": "xterm", "NO_COLOR": "1"}))
        finally:
            os.close(master)
            os.close(slave)
        with tempfile.TemporaryFile() as f:
            self.assertFalse(hm.use_colours(f.fileno(), {"TERM": "xterm"}))


if __name__ == "__main__":
    unittest.main()
