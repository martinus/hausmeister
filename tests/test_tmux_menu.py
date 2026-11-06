"""tmux re-exec, [general] config and the end menu. The tests use a fake tmux
script and fake exec and run functions: nothing starts the real tmux,
systemctl or a shell."""

import os
import pty
import sys
import tempfile
import termios
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from support import load, write

hm = load()


class Exec(Exception):
    pass


def fake_exec(calls):
    def exec_fn(file, argv):
        calls.append((file, argv))
        raise Exec

    return exec_fn


class StartTmuxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.log = self.tmp / "tmux.log"
        self.bin = self.tmp / "bin"
        self.tmux = write(
            self.bin / "tmux",
            f'#!/bin/sh\necho "$*" >> {self.log}\n[ "$1" = has-session ] && exit "$HAS_SESSION_RC"\nexit 0\n',
            executable=True,
        )
        self.env = {"PATH": f"{self.bin}:/usr/bin:/bin"}
        os.environ["HAS_SESSION_RC"] = "1"  # has-session runs with the environment of hausmeister
        self.addCleanup(os.environ.pop, "HAS_SESSION_RC", None)
        self.calls = []

    def start(self, use_tmux=True, stdin_is_tty=True):
        return hm.start_tmux(self.env, use_tmux, stdin_is_tty, fake_exec(self.calls))

    def tmux_calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_already_in_tmux(self):
        self.env["TMUX"] = "/tmp/tmux-1000/default,1,0"
        self.assertEqual(self.start(), "")
        self.assertEqual((self.calls, self.tmux_calls()), ([], []))

    def test_disabled_in_config(self):
        self.assertEqual(self.start(use_tmux=False), "tmux = false in the config, running in this terminal.")
        self.assertEqual((self.calls, self.tmux_calls()), ([], []))

    def test_not_a_terminal(self):
        self.assertIn("not a terminal", self.start(stdin_is_tty=False))
        self.assertEqual(self.calls, [])

    def test_not_installed(self):
        self.env["PATH"] = str(self.tmp / "empty")
        self.assertEqual(self.start(), "tmux is not installed, running in this terminal.")
        self.assertEqual(self.calls, [])

    def test_new_session(self):
        with self.assertRaises(Exec):
            self.start()
        self.assertEqual(self.tmux_calls(), ["has-session -t =hausmeister"])
        file, argv = self.calls[0]
        self.assertEqual(file, str(self.tmux))
        self.assertEqual(
            argv,
            [
                str(self.tmux),
                "new-session",
                "-s",
                "hausmeister",
                "-e",
                f"PATH={self.env['PATH']}",
                "-e",
                "HAUSMEISTER_UPDATED=1",
                sys.executable,
                str(hm.SCRIPT),
                "update",
            ],
        )

    def test_attach_to_running_session(self):
        os.environ["HAS_SESSION_RC"] = "0"
        with self.assertRaises(Exec):
            self.start()
        self.assertEqual(self.calls, [(str(self.tmux), [str(self.tmux), "attach-session", "-t", "=hausmeister"])])

    def test_exec_that_returns(self):
        note = hm.start_tmux(self.env, True, True, lambda file, argv: None)
        self.assertEqual(note, "could not start tmux, running in this terminal.")


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_missing_file(self):
        self.assertEqual(hm.read_config(self.tmp / "missing.toml"), ({}, ""))

    def test_broken_file(self):
        config, message = hm.read_config(write(self.tmp / "c.toml", "[general\n"))
        self.assertEqual(config, {})
        self.assertIn("Using the defaults.", message)

    def test_general(self):
        config, _ = hm.read_config(write(self.tmp / "c.toml", "[general]\ntmux = false\n"))
        self.assertEqual(hm.general_setting(config, "tmux"), (False, ""))
        self.assertEqual(hm.general_setting(config, "self_update"), (True, ""))
        self.assertEqual(hm.general_setting({}, "tmux"), (True, ""))

    def test_wrong_values(self):
        self.assertEqual(
            hm.general_setting({"general": {"tmux": "no"}}, "tmux"),
            (True, "config: tmux must be true or false. Using true."),
        )
        self.assertEqual(hm.general_setting({"general": "x"}, "tmux"), (True, ""))

    def test_location(self):
        old = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp)
        try:
            self.assertEqual(hm.config_path(), self.tmp / "hausmeister" / "config.toml")
        finally:
            if old is None:
                del os.environ["XDG_CONFIG_HOME"]
            else:
                os.environ["XDG_CONFIG_HOME"] = old


class EndMenuTest(unittest.TestCase):
    def menu(self, keys, run_result=0):
        keys = iter(keys)
        self.runs, self.execs = [], []
        out = self.enterContext(tempfile.TemporaryFile())

        def run(command):
            self.runs.append(command)
            return run_result

        def exec_fn(file, argv):
            self.execs.append((file, argv))

        hm.end_menu(lambda: next(keys), run, exec_fn, "/bin/fake-shell", out.fileno())
        out.seek(0)
        return out.read().decode()

    def test_quit(self):
        for key in ("q", "\n", "\r", "", "\x03", "\x04", "\x1b"):
            with self.subTest(key=key):
                self.assertEqual(self.menu([key]), hm.MENU + "\n")
                self.assertEqual((self.runs, self.execs), ([], []))

    def test_other_keys_are_ignored(self):
        self.menu(["x", "R", "q"])
        self.assertEqual((self.runs, self.execs), ([], []))

    def test_reboot(self):
        self.menu(["r"])
        self.assertEqual(self.runs, [["systemctl", "reboot"]])

    def test_power_off_that_fails(self):
        out = self.menu(["p", "q"], run_result=1)
        self.assertEqual(self.runs, [["systemctl", "poweroff"]])
        self.assertIn(" systemctl poweroff failed.\n", out)

    def test_shell(self):
        self.menu(["s"])
        self.assertEqual(self.execs, [("/bin/fake-shell", ["/bin/fake-shell"])])
        self.assertEqual(self.runs, [])


class MainTest(unittest.TestCase):
    """main() with update, start_tmux and end_menu replaced, so nothing real runs."""

    def run_main(self, config_text, stdin_is_tty):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        if config_text is not None:
            write(tmp / "hausmeister" / "config.toml", config_text)
        self.enterContext(mock.patch.dict(os.environ, XDG_CONFIG_HOME=str(tmp)))
        self.enterContext(mock.patch.object(hm.os, "geteuid", return_value=1000))
        self.enterContext(mock.patch.object(hm.os, "isatty", return_value=stdin_is_tty))
        self.enterContext(mock.patch("sys.stderr", new=self.enterContext(tempfile.TemporaryFile(mode="w+"))))
        start_tmux = self.enterContext(mock.patch.object(hm, "start_tmux", return_value=""))
        update = self.enterContext(mock.patch.object(hm, "update", return_value=1))
        end_menu = self.enterContext(mock.patch.object(hm, "end_menu"))
        self.assertEqual(hm.main(["update"]), 1)
        update.assert_called_once()
        return start_tmux, end_menu

    def test_tmux_false_in_config(self):
        start_tmux, _ = self.run_main("[general]\ntmux = false\n", stdin_is_tty=False)
        env, use_tmux, stdin_is_tty, exec_fn = start_tmux.call_args.args
        self.assertEqual((use_tmux, stdin_is_tty, exec_fn), (False, False, os.execv))

    def test_menu_only_on_a_terminal(self):
        start_tmux, end_menu = self.run_main(None, stdin_is_tty=True)
        self.assertTrue(start_tmux.call_args.args[1])
        end_menu.assert_called_once()
        _, end_menu = self.run_main(None, stdin_is_tty=False)
        end_menu.assert_not_called()


class ReadKeyTest(unittest.TestCase):
    def test_one_key_without_enter(self):
        master, slave = pty.openpty()
        try:
            before = termios.tcgetattr(slave)
            result = []
            reader = threading.Thread(target=lambda: result.append(hm.read_key(slave)), daemon=True)
            reader.start()
            time.sleep(0.2)  # read_key has switched off the line mode and waits
            os.write(master, b"r")  # one key, no Enter
            reader.join(timeout=2)
            still_waiting = reader.is_alive()
            after = None if still_waiting else termios.tcgetattr(slave)
        finally:
            os.close(master)  # ends a read that still waits, so the test cannot hang
            reader.join(timeout=2)
            os.close(slave)
        self.assertFalse(still_waiting)
        self.assertEqual(result, ["r"])
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
