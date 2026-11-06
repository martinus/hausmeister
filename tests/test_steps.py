import tempfile
import time
import unittest
from pathlib import Path

from support import env_with_path, load, make_step, write

hm = load()


def process_alive(pid: int) -> bool:
    """True if pid runs for one more second. A killed child can be a zombie for a moment."""
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            with open(f"/proc/{pid}/stat") as f:
                if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                    return False
        except FileNotFoundError:
            return False
        time.sleep(0.05)
    return True


class LoadStepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def load(self, **kwargs):
        return hm.load_step(make_step(self.tmp, "s", **kwargs))

    def test_defaults(self):
        step = self.load()
        self.assertEqual(step.broken, "")
        self.assertEqual((step.name, step.sudo, step.order, step.default, step.contract), ("Test", False, 50, True, 1))

    def test_all_keys(self):
        info = 'name = "N"\ndescription = "D"\nneeds = "a b"\nsudo = true\norder = 10\ndefault = false\ncontract = 1\n'
        step = self.load(info=info)
        self.assertEqual(step.broken, "")
        self.assertEqual(step.warnings, [])
        self.assertEqual(
            (step.description, step.needs, step.sudo, step.order, step.default), ("D", "a b", True, 10, False)
        )

    def test_no_info(self):
        self.assertEqual(self.load(info=None).broken, "no info.toml")

    def test_wrong_toml(self):
        self.assertTrue(self.load(info="name = \n").broken.startswith("info.toml: "))

    def test_no_name(self):
        self.assertEqual(self.load(info='description = "x"\n').broken, "info.toml: name is missing")

    def test_wrong_type(self):
        self.assertEqual(self.load(info='name = "N"\nsudo = "yes"\n').broken, "info.toml: sudo must be true or false")

    def test_bool_is_not_an_integer(self):
        self.assertEqual(self.load(info='name = "N"\norder = true\n').broken, "info.toml: order must be an integer")

    def test_unknown_contract(self):
        self.assertIn("unknown contract 2", self.load(info='name = "N"\ncontract = 2\n').broken)

    def test_no_update(self):
        self.assertEqual(self.load(update=None).broken, "no update file")

    def test_update_not_executable(self):
        folder = make_step(self.tmp, "s")
        (folder / "update").chmod(0o644)
        self.assertEqual(hm.load_step(folder).broken, "update is not executable")

    def test_check_not_executable(self):
        folder = make_step(self.tmp, "s", check="exit 0\n")
        (folder / "check").chmod(0o644)
        self.assertEqual(hm.load_step(folder).broken, "check is not executable")

    def test_unknown_key_warns(self):
        step = self.load(info='name = "N"\nsudoo = true\n')
        self.assertEqual(step.broken, "")
        self.assertEqual(step.warnings, ["unknown key 'sudoo' in info.toml"])


class LoadStepsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_sorted_by_order_then_id(self):
        builtin = self.tmp / "builtin"
        make_step(builtin, "b", info='name = "B"\norder = 10\n')
        make_step(builtin, "a", info='name = "A"\norder = 20\n')
        make_step(builtin, "c", info='name = "C"\norder = 10\n')
        write(builtin / "README", "not a step")
        make_step(builtin, ".hidden")
        self.assertEqual([s.id for s in hm.load_steps([builtin])], ["b", "c", "a"])

    def test_own_folder_replaces_builtin_as_a_whole(self):
        builtin, own = self.tmp / "builtin", self.tmp / "own"
        make_step(builtin, "x", info='name = "Built-in"\n', check="exit 0\n")
        make_step(own, "x", info='name = "Own"\n')
        make_step(builtin, "y")
        steps = {s.id: s for s in hm.load_steps([builtin, own, self.tmp / "missing"])}
        self.assertEqual(steps["x"].name, "Own")
        self.assertEqual(steps["x"].path, own / "x")
        self.assertFalse(steps["x"].check.exists())  # the built-in check does not come along
        self.assertEqual(steps["y"].path, builtin / "y")


class NeedsTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(hm.parse_needs("cargo cargo-install-update"), [["cargo"], ["cargo-install-update"]])
        self.assertEqual(hm.parse_needs(" hx | helix  git "), [["hx", "helix"], ["git"]])
        self.assertEqual(hm.parse_needs("a|b|c"), [["a", "b", "c"]])
        self.assertEqual(hm.parse_needs(""), [])

    def test_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp) / "helix", "", executable=True)
            write(Path(tmp) / "cargo", "", executable=True)
            write(Path(tmp) / "not-executable", "")
            self.assertEqual(hm.missing_needs("hx | helix cargo", tmp), [])
            self.assertEqual(hm.missing_needs("cargo cargo-install-update", tmp), ["cargo-install-update"])
            self.assertEqual(hm.missing_needs("hx | vim", tmp), ["hx | vim"])
            self.assertEqual(hm.missing_needs("not-executable", tmp), ["not-executable"])


class EvaluateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.bin = self.tmp / "bin"
        write(self.bin / "tool", "", executable=True)
        self.steps_dir = self.tmp / "steps"

    def evaluate(self, timeout=hm.CHECK_TIMEOUT):
        steps = hm.load_steps([self.steps_dir])
        hm.evaluate(steps, env_with_path(f"{self.bin}:/usr/bin:/bin"), timeout)
        return {s.id: s for s in steps}

    def test_states(self):
        make_step(self.steps_dir, "no-check", info='name = "N"\nneeds = "tool"\n')
        make_step(self.steps_dir, "check-0", check="exit 0\n")
        make_step(self.steps_dir, "check-1", check="exit 1\n")
        make_step(
            self.steps_dir, "missing", info='name = "N"\nneeds = "tool hausmeister-test-missing"\n', check="exit 0\n"
        )
        make_step(self.steps_dir, "broken", info=None)
        steps = self.evaluate()
        self.assertEqual(steps["no-check"].state, hm.INSTALLED)
        self.assertEqual(steps["check-0"].state, hm.INSTALLED)
        self.assertEqual(steps["check-1"].state, hm.NOT_IN_USE)
        self.assertEqual(
            (steps["missing"].state, steps["missing"].detail), (hm.NOT_INSTALLED, "missing hausmeister-test-missing")
        )
        self.assertEqual((steps["broken"].state, steps["broken"].detail), (hm.BROKEN, "no info.toml"))

    def test_check_gets_environment(self):
        make_step(
            self.steps_dir, "s", check='[ "$HAUSMEISTER_STEP_DIR" = "$(dirname "$0")" ] && [ "$PWD" = "$HOME" ]\n'
        )
        self.assertEqual(self.evaluate()["s"].state, hm.INSTALLED)

    def test_check_that_runs_too_long(self):
        # The timeout must also stop the children of the check, not only bash.
        pid_file = self.tmp / "pid"
        make_step(self.steps_dir, "slow", check=f'sleep 30 &\necho $! > "{pid_file}"\nwait\n')
        start = time.monotonic()
        steps = self.evaluate(timeout=0.5)
        self.assertEqual(steps["slow"].state, hm.NOT_IN_USE)
        self.assertLess(time.monotonic() - start, 5)
        self.assertFalse(process_alive(int(pid_file.read_text())))

    def test_checks_run_in_parallel(self):
        for i in range(4):
            make_step(self.steps_dir, f"s{i}", check="sleep 0.5\n")
        start = time.monotonic()
        steps = self.evaluate()
        self.assertTrue(all(s.state == hm.INSTALLED for s in steps.values()))
        self.assertLess(time.monotonic() - start, 1.5)


if __name__ == "__main__":
    unittest.main()
