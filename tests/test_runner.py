import datetime
import os
import pty
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import FEDORA_44, FIXTURE_STEPS, load, write

hm = load()

DISTRO = hm.Distro("fedora", "44", "Fedora Linux 44")
PRINT_CTTY = "print('ctty', open('/proc/self/stat').read().rsplit(')', 1)[1].split()[4], flush=True)"


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.os_release = write(self.tmp / "os-release", FEDORA_44)
        self.enterContext(mock.patch.dict(os.environ, FAKE_OS_RELEASE=str(self.os_release)))
        self.out = self.enterContext(tempfile.TemporaryFile())
        self.all_steps = {s.id: s for s in hm.load_steps([FIXTURE_STEPS])}

    def run_steps(self, *ids):
        steps = [self.all_steps[i] for i in ids]
        results, stopped = hm.run_steps(steps, hm.base_env(DISTRO), self.os_release, self.out.fileno())
        self.assertFalse(stopped)
        return {r.step.id: r for r in results}

    def output(self):
        self.out.seek(0)
        return self.out.read().decode()

    def test_success(self):
        r = self.run_steps("ok")["ok"]
        self.assertEqual((r.status, r.exit_code, r.reason, r.note, r.reboot), ("ok", 0, "", "", False))
        self.assertEqual(r.tail, ["all fine"])
        self.assertRegex(self.output(), r"── \d\d:\d\d:\d\d · ok · Prints and succeeds ──\n")
        self.assertIn("all fine", self.output())

    def test_failure_does_not_stop_the_run(self):
        results = self.run_steps("fail", "ok")
        self.assertEqual((results["fail"].status, results["fail"].exit_code), ("FAILED", 1))
        self.assertEqual(results["fail"].tail, ["first line", "something broke"])
        self.assertEqual(results["ok"].status, "ok")

    def test_reason_file(self):
        r = self.run_steps("reason")["reason"]
        self.assertEqual((r.status, r.exit_code, r.reason), ("FAILED", 3, "Network is down"))

    def test_note_file(self):
        r = self.run_steps("note")["note"]
        self.assertEqual((r.status, r.note), ("ok", "Firmware updates available"))

    def test_reboot_file(self):
        self.assertTrue(self.run_steps("reboot")["reboot"].reboot)

    def test_command_not_found(self):
        r = self.run_steps("missing-command")["missing-command"]
        self.assertEqual((r.status, r.exit_code), ("FAILED", 127))

    def test_release_change_fails_the_step(self):
        results = self.run_steps("release-change", "ok")
        r = results["release-change"]
        self.assertEqual((r.status, r.exit_code), ("FAILED", 0))
        self.assertEqual(r.reason, "The distribution release changed (44 → 99).")
        self.assertEqual(results["ok"].status, "ok")

    def test_environment(self):
        self.run_steps("env")
        out = self.output().replace("\r\n", "\n")
        self.assertIn("SUDO=sudo -n\n", out)
        self.assertIn("DISTRO=fedora\nVERSION_ID=44\n", out)
        self.assertIn(f"STEP_DIR={FIXTURE_STEPS / 'env'}\n", out)
        self.assertIn(f"PWD={Path.home()}\n", out)
        self.assertIn("stdout is a terminal\n", out)

    def test_step_keeps_the_controlling_terminal(self):
        """sudo caches the password per terminal. The step must have the controlling
        terminal of hausmeister, else `sudo -n` does not find the password of `sudo -v`."""
        pid, fd = pty.fork()  # gives the child a controlling terminal, like a real shell
        if pid == 0:
            try:
                exec(PRINT_CTTY)
                hm.spawn_update([sys.executable, "-c", PRINT_CTTY], dict(os.environ), Path.home(), 1)
            finally:
                os._exit(0)
        output = b""
        while True:
            try:
                data = os.read(fd, 4096)
            except OSError:
                break
            if not data:
                break
            output += data
        os.close(fd)
        os.waitpid(pid, 0)
        ttys = re.findall(r"ctty (\d+)", output.decode())
        self.assertEqual(len(ttys), 2, output)
        self.assertNotEqual(ttys[0], "0")
        self.assertEqual(ttys[1], ttys[0])

    def test_update_that_cannot_start(self):
        step = hm.Step(id="gone", path=self.tmp / "gone", name="Gone")
        results, _ = hm.run_steps([step], hm.base_env(DISTRO), self.os_release, self.out.fileno())
        self.assertEqual((results[0].status, results[0].exit_code), ("FAILED", 127))

    def test_ctrl_c_cancels_the_step(self):
        with mock.patch.object(hm, "spawn_update", return_value=(-2, b"", [100.0])):
            result, stop = hm.run_step(self.all_steps["ok"], {}, "44", self.os_release, self.out.fileno())
        self.assertEqual((result.status, result.reason, stop), ("cancelled", "cancelled", False))
        with mock.patch.object(hm, "spawn_update", return_value=(-2, b"", [100.0, 101.5])):
            _, stop = hm.run_step(self.all_steps["ok"], {}, "44", self.os_release, self.out.fileno())
        self.assertTrue(stop)
        with mock.patch.object(hm, "spawn_update", return_value=(-2, b"", [100.0, 103.0])):
            _, stop = hm.run_step(self.all_steps["ok"], {}, "44", self.os_release, self.out.fileno())
        self.assertFalse(stop)

    def test_ctrl_c_ignored_by_a_step_that_succeeds(self):
        with mock.patch.object(hm, "spawn_update", return_value=(0, b"", [100.0])):
            result, stop = hm.run_step(self.all_steps["ok"], {}, "44", self.os_release, self.out.fileno())
        self.assertEqual((result.status, stop), ("ok", False))


class LastLinesTest(unittest.TestCase):
    def test_progress_bar_and_colours(self):
        output = b"\x1b[32mone\x1b[0m\r\n\r\n 10%\r 50%\r100%\r\ntwo  \r\n"
        self.assertEqual(hm.last_lines(output), ["one", "100%", "two"])

    def test_only_the_last_lines(self):
        output = "".join(f"line {i}\r\n" for i in range(10)).encode()
        self.assertEqual(hm.last_lines(output, 5), [f"line {i}" for i in range(5, 10)])
        many = "".join(f"line {i}\r\n" for i in range(100)).encode()
        self.assertEqual(len(hm.last_lines(many)), 40)


class OverviewTest(unittest.TestCase):
    def test_overview(self):
        def step(step_id, state, name="", detail=""):
            return hm.Step(id=step_id, path=Path("/x"), name=name or step_id, state=state, detail=detail)

        dnf = step("dnf", hm.INSTALLED, "System packages (dnf)")
        rustup = step("rustup", hm.INSTALLED, "Rust toolchains")
        fwupd = step("fwupd", hm.INSTALLED, "Firmware")
        steps = [
            dnf,
            rustup,
            fwupd,
            step("flatpak-system", hm.NOT_IN_USE),
            step("apt", hm.NOT_INSTALLED, detail="missing apt-get"),
            step("snap", hm.NOT_INSTALLED),
            step("mine", hm.BROKEN, detail="no info.toml"),
        ]
        results = [
            hm.Result(dnf, "ok", 0, 185.4, reboot=True),
            hm.Result(fwupd, "ok", 0, 4.04, note="Firmware updates available. Run: fwupdmgr update"),
            hm.Result(rustup, "FAILED", 1, 6.2, reason="A download failed.", tail=["error: could not download"]),
        ]
        started = datetime.datetime(2026, 11, 6, 4, 19, 0)
        text = hm.render_overview(DISTRO, steps, results, started, started + datetime.timedelta(seconds=252), False)
        self.assertEqual(
            text,
            "\n".join(
                [
                    " hausmeister update · Fedora Linux 44 · 04:19 to 04:23 · 4 min 12 s",
                    "",
                    " ok        dnf             System packages (dnf)                              3 min 05 s",
                    " ok        fwupd           Firmware updates available. Run: fwupdmgr update   4.0 s",
                    " FAILED    rustup          Rust toolchains                                    6.2 s",
                    "",
                    " Not in use: flatpak-system. Not installed: apt, snap. Broken: mine (no info.toml).",
                    "",
                    " Reboot needed (dnf).",
                    "",
                    " ✘ rustup · exit code 1",
                    "   Reason: A download failed.",
                    "   > error: could not download",
                    "",
                ]
            ),
        )

    def test_nothing_ran(self):
        started = datetime.datetime(2026, 11, 6, 4, 19, 0)
        text = hm.render_overview(DISTRO, [], [], started, started, True)
        self.assertIn(" No step applies on this machine.", text)
        self.assertIn(" Stopped with Ctrl-C.", text)

    def test_signal(self):
        self.assertEqual(hm.describe_exit(-15), "killed by signal 15")

    def test_format_duration(self):
        self.assertEqual(
            [hm.format_duration(s) for s in (0.04, 9.94, 12.9, 60, 3725)],
            ["0.0 s", "9.9 s", "12 s", "1 min 00 s", "62 min 05 s"],
        )


class UpdateCommandTest(unittest.TestCase):
    """The update command end to end, with the fixture steps only."""

    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.os_release = write(self.tmp / "os-release", FEDORA_44)
        self.enterContext(mock.patch.dict(os.environ, FAKE_OS_RELEASE=str(self.os_release)))
        self.out = self.enterContext(tempfile.TemporaryFile())
        self.enterContext(mock.patch("sys.stderr", new=self.enterContext(tempfile.TemporaryFile(mode="w+"))))

    def update(self, *dirs):
        code = hm.update(list(dirs), self.os_release, self.tmp / "no-ostree-booted", self.out.fileno())
        self.out.seek(0)
        return code, self.out.read().decode()

    def test_all_ok(self):
        own = self.tmp / "own"
        (own / "ok").mkdir(parents=True)
        write(own / "ok" / "info.toml", 'name = "Off by default"\ndefault = false\n')
        write(own / "ok" / "update", "#!/bin/sh\nexit 1\n", executable=True)
        code, out = self.update(self.tmp / "builtin", own)
        self.assertEqual(code, 0)
        self.assertIn(" No step applies on this machine.", out)

    def test_failures_give_exit_code_1(self):
        code, out = self.update(FIXTURE_STEPS)
        self.assertEqual(code, 1)
        self.assertIn(" hausmeister update · Fedora Linux 44 · ", out)
        self.assertIn(" ✘ reason · exit code 3\n   Reason: Network is down", out)
        self.assertIn(" Reboot needed (reboot).", out)

    def test_refuses_root(self):
        with mock.patch.object(hm.os, "geteuid", return_value=0), mock.patch.object(hm, "update") as update:
            self.assertEqual(hm.main(["update"]), 2)
        update.assert_not_called()

    def test_no_command_shows_help(self):
        with (
            mock.patch("sys.stdout", new=self.enterContext(tempfile.TemporaryFile(mode="w+"))) as out,
            mock.patch.object(hm, "update") as update,
        ):
            self.assertEqual(hm.main([]), 0)
            out.seek(0)
            self.assertIn("usage: hausmeister", out.read())
        update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
