"""Milestone 5: the [select] table, the selection UI and the first run."""

import fcntl
import io
import json
import os
import pty
import re
import select
import signal
import struct
import tempfile
import termios
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from support import FEDORA_44, FIXTURE_STEPS, load, write

hm = load()

CONFIG = """# hausmeister config. "hausmeister update --config" rewrites only the [select] table.

[general]
tmux = true   # keep this comment
self_update = true

[select]  # my selection
dnf = true
flatpak-user = false
# an old comment inside the table
gone = true

# tmux plugins come later
[other]
key = "value"
"""


def step(step_id, state=hm.INSTALLED, default=True, name=""):
    return hm.Step(id=step_id, path=Path("/x") / step_id, name=name or step_id, state=state, default=default)


class ReadSelectionTest(unittest.TestCase):
    def test_no_table(self):
        self.assertEqual(hm.read_selection({}), (None, []))
        self.assertEqual(hm.read_selection({"select": 1})[0], None)

    def test_values(self):
        selection, warnings = hm.read_selection({"select": {"dnf": True, "apt": False, "x": "yes"}})
        self.assertEqual(selection, {"dnf": True, "apt": False, "x": False})
        self.assertEqual(warnings, ["config: select.x must be true or false. The step does not run."])


class RewriteSelectTest(unittest.TestCase):
    def test_other_bytes_stay(self):
        new = hm.rewrite_select(CONFIG, {"dnf": False, "rustup": True, "my step": True})
        before, after = CONFIG.split("[select]")[0], CONFIG[CONFIG.index("# tmux plugins") :]
        self.assertTrue(new.startswith(before))
        self.assertTrue(new.endswith("\n" + after))
        self.assertIn('[select]\ndnf = false\nrustup = true\n"my step" = true\n\n# tmux plugins', new)
        parsed = tomllib.loads(new)
        self.assertEqual(parsed["select"], {"dnf": False, "rustup": True, "my step": True})
        self.assertEqual(parsed["other"], {"key": "value"})

    def test_same_selection_twice(self):
        once = hm.rewrite_select(CONFIG, {"dnf": True})
        self.assertEqual(hm.rewrite_select(once, {"dnf": True}), once)

    def test_table_at_the_end(self):
        text = "[general]\ntmux = false\n\n[select]\ndnf = true\n"
        self.assertEqual(hm.rewrite_select(text, {"apt": True}), "[general]\ntmux = false\n\n[select]\napt = true\n")

    def test_missing_table_is_added_at_the_end(self):
        self.assertEqual(
            hm.rewrite_select("[general]\ntmux = false\n", {"a": True}),
            "[general]\ntmux = false\n\n[select]\na = true\n",
        )
        self.assertEqual(hm.rewrite_select("x = 1", {"a": True}), "x = 1\n\n[select]\na = true\n")
        self.assertEqual(hm.rewrite_select("", {"a": False}), "[select]\na = false\n")

    def test_similar_headers_are_not_the_table(self):
        text = "[selection]\na = 1\n"
        self.assertEqual(hm.rewrite_select(text, {"b": True}), "[selection]\na = 1\n\n[select]\nb = true\n")

    def test_keys_that_need_quotes(self):
        selection = {"a.b": True, 'q"uote': False, "ü": True, "tab\there": True, "\U0001f600": False}
        self.assertEqual(tomllib.loads(hm.render_select(selection))["select"], selection)


class SaveSelectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.tmp / "hausmeister" / "config.toml"

    def test_new_file(self):
        hm.save_selection(self.path, {"dnf": True})
        self.assertEqual(self.path.read_text(), "[select]\ndnf = true\n")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)

    def test_keeps_mode_and_leaves_no_temporary_file(self):
        write(self.path, CONFIG)
        self.path.chmod(0o600)
        hm.save_selection(self.path, {"dnf": False})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["config.toml"])

    def test_failure_keeps_the_old_file(self):
        write(self.path, CONFIG)
        with mock.patch.object(hm.os, "replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            hm.save_selection(self.path, {"dnf": False})
        self.assertEqual(self.path.read_text(), CONFIG)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["config.toml"])


class SelectionTest(unittest.TestCase):
    def setUp(self):
        self.steps = [step("dnf"), step("apt", hm.NOT_INSTALLED), step("rustup"), step("mine", hm.BROKEN)]

    def test_move_is_clamped(self):
        s = hm.Selection(self.steps, set())
        s.move(-1)
        self.assertEqual(s.cursor, 0)
        s.move(10)
        self.assertEqual((s.cursor, s.current.id), (3, "mine"))

    def test_toggle_all_none(self):
        s = hm.Selection(self.steps, {"apt", "unknown"})
        self.assertEqual(s.selected, {"apt"})
        s.toggle()
        s.toggle(2)
        self.assertEqual(s.selected, {"apt", "dnf", "rustup"})
        s.toggle()
        self.assertEqual(s.selected, {"apt", "rustup"})
        s.select_none()
        s.select_all()  # only the steps that apply
        self.assertEqual(s.selected, {"dnf", "rustup"})
        self.assertEqual(s.result(), {"dnf": True, "apt": False, "rustup": True, "mine": False})

    def test_lines(self):
        self.steps[3].detail = "no info.toml"
        lines = hm.Selection(self.steps, {"dnf"}).lines()
        self.assertTrue(lines[0].startswith("[x] dnf "))
        self.assertTrue(lines[1].startswith("[ ] apt "))
        self.assertTrue(lines[1].endswith("not installed"))
        self.assertTrue(lines[3].endswith("broken: no info.toml"))

    def test_no_steps(self):
        s = hm.Selection([], set())
        s.move(1)
        s.toggle()
        self.assertIsNone(s.current)
        self.assertEqual(s.result(), {})


class TextSelectTest(unittest.TestCase):
    def run_text(self, answers):
        answers = iter(answers)

        def read_line():
            try:
                return next(answers)
            except StopIteration:
                raise EOFError from None

        out = io.StringIO()
        s = hm.Selection([step("dnf"), step("apt", hm.NOT_INSTALLED), step("rustup")], {"dnf"})
        return hm.text_select(s, "title", read_line, out), s, out.getvalue()

    def test_toggle_and_save(self):
        saved, s, out = self.run_text(["1 3", "2,9 x", ""])
        self.assertTrue(saved)
        self.assertEqual(s.selected, {"apt", "rustup"})
        self.assertIn("Not a step number: 9", out)
        self.assertIn("Not a step number: x", out)
        self.assertIn("   1 [x] dnf", out)

    def test_all_none(self):
        saved, s, _ = self.run_text(["n", "a", ""])
        self.assertEqual((saved, s.selected), (True, {"dnf", "rustup"}))

    def test_cancel(self):
        self.assertFalse(self.run_text(["q"])[0])
        self.assertFalse(self.run_text([])[0])  # end of input


class StepsToRunTest(unittest.TestCase):
    def setUp(self):
        self.steps = [step("dnf"), step("off", default=False), step("cargo"), step("uv"), step("apt", hm.NOT_INSTALLED)]

    def test_without_table_the_defaults_run(self):
        to_run, notes = hm.steps_to_run(self.steps, None)
        self.assertEqual([s.id for s in to_run], ["dnf", "cargo", "uv"])
        self.assertIn("No selection saved", notes[0])

    def test_selected_steps_and_new_steps(self):
        to_run, notes = hm.steps_to_run(self.steps, {"dnf": True, "off": True, "cargo": False, "apt": True})
        self.assertEqual([s.id for s in to_run], ["dnf", "off"])
        self.assertEqual(notes, ["1 new step: uv. Run hausmeister update --config."])
        _, notes = hm.steps_to_run(self.steps, {"dnf": True})
        self.assertEqual(notes, ["3 new steps: off, cargo, uv. Run hausmeister update --config."])

    def test_update_runs_only_the_selection(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryFile() as out:
            os_release = write(Path(tmp) / "os-release", FEDORA_44)
            with mock.patch.dict(os.environ, FAKE_OS_RELEASE=str(os_release)):
                selection = {s.id: s.id == "ok" for s in hm.load_steps([FIXTURE_STEPS])}
                del selection["note"]
                code = hm.update([FIXTURE_STEPS], os_release, Path(tmp) / "x", out.fileno(), selection=selection)
            out.seek(0)
            text = out.read().decode()
        self.assertEqual(code, 0)
        self.assertIn(" ok        ok ", text)
        self.assertNotIn("· fail ·", text)
        self.assertIn(" 1 new step: note. Run hausmeister update --config.", text)


class MainConfigTest(unittest.TestCase):
    """main() with discover, the UI, tmux, update and the menu replaced."""

    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.tmp / "hausmeister" / "config.toml"
        self.enterContext(mock.patch.dict(os.environ, XDG_CONFIG_HOME=str(self.tmp), XDG_STATE_HOME=str(self.tmp)))
        self.enterContext(mock.patch.object(hm.os, "geteuid", return_value=1000))
        self.enterContext(mock.patch("sys.stderr", new=io.StringIO()))
        self.stdout = self.enterContext(mock.patch("sys.stdout", new=io.StringIO()))
        steps = [step("dnf"), step("apt", hm.NOT_INSTALLED), step("off", default=False)]
        distro = hm.Distro("fedora", "44", "Fedora Linux 44")
        self.enterContext(mock.patch.object(hm, "discover", return_value=(distro, steps, {})))
        self.choose = self.enterContext(mock.patch.object(hm, "choose_steps", return_value={"dnf": False, "apt": True}))
        self.start_tmux = self.enterContext(mock.patch.object(hm, "start_tmux", return_value=""))
        self.self_update = self.enterContext(mock.patch.object(hm, "run_self_update", return_value=None))
        self.update = self.enterContext(mock.patch.object(hm, "update", return_value=0))
        self.enterContext(mock.patch.object(hm, "end_menu"))

    def main(self, args, tty):
        with mock.patch.object(hm.os, "isatty", return_value=tty):
            return hm.main(args)

    def test_config_saves_and_stops(self):
        write(self.path, "[general]\ntmux = false\n\n[select]\ndnf = true\nmoved-away = true\n")
        self.assertEqual(self.main(["update", "--config"], tty=True), 0)
        self.assertEqual(self.choose.call_args.args[1], {"dnf", "moved-away"})
        self.assertEqual(
            self.path.read_text(), "[general]\ntmux = false\n\n[select]\ndnf = false\napt = true\nmoved-away = true\n"
        )
        self.update.assert_not_called()
        self.start_tmux.assert_not_called()

    def test_config_cancelled(self):
        self.choose.return_value = None
        self.assertEqual(self.main(["update", "--config"], tty=True), 0)
        self.assertFalse(self.path.exists())
        self.assertIn("Nothing saved.", self.stdout.getvalue())

    def test_first_run_selects_the_defaults(self):
        self.main(["update"], tty=True)
        self.assertEqual(self.choose.call_args.args[1], {"dnf"})
        self.assertEqual(tomllib.loads(self.path.read_text())["select"], {"dnf": False, "apt": True})
        self.assertEqual(self.update.call_args.kwargs["selection"], {"dnf": False, "apt": True})

    def test_first_run_cancelled(self):
        self.choose.return_value = None
        self.assertEqual(self.main(["update"], tty=True), 0)
        self.update.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_no_terminal_without_table_runs_the_defaults(self):
        self.main(["update"], tty=False)
        self.choose.assert_not_called()
        self.assertIsNone(self.update.call_args.kwargs["selection"])

    def test_saved_selection(self):
        write(self.path, "[select]\ndnf = true\n")
        self.main(["update"], tty=True)
        self.choose.assert_not_called()
        self.assertEqual(self.update.call_args.kwargs["selection"], {"dnf": True})


class CursesTest(unittest.TestCase):
    """The real curses UI in a pseudo-terminal, with keys from the test."""

    def run_ui(self, keys: bytes):
        pid, fd = pty.fork()
        if pid == 0:
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
                os.environ["TERM"] = "xterm"
                steps = [step("dnf"), step("apt", hm.NOT_INSTALLED), step("rustup")]
                s = hm.Selection(steps, {"dnf"})
                saved = hm.curses_select(s, "test")
                print("RESULT " + json.dumps([saved, sorted(s.selected)]), flush=True)
            finally:
                os._exit(0)
        output = b""
        deadline = time.monotonic() + 10
        sent = False
        try:
            while time.monotonic() < deadline:
                readable, _, _ = select.select([fd], [], [], 0.1)
                if readable:
                    try:
                        data = os.read(fd, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    output += data
                    if not sent and b"enter save" in output:
                        for key in keys:
                            os.write(fd, bytes([key]))
                            time.sleep(0.05)
                        sent = True
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
            os.close(fd)
        match = re.search(r"RESULT (.*)\r\n", output.decode(errors="replace"))
        self.assertIsNotNone(match, output[-500:])
        return json.loads(match.group(1))

    def test_move_toggle_save(self):
        self.assertEqual(self.run_ui(b"jj \r"), [True, ["dnf", "rustup"]])

    def test_cancel(self):
        self.assertEqual(self.run_ui(b" q"), [False, []])


if __name__ == "__main__":
    unittest.main()
