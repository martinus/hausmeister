"""The step check from PLAN.md, for all built-in step folders."""

import re
import tomllib
import unittest

from support import ROOT, load

hm = load()

STEPS = ROOT / "steps"
FORBIDDEN = ("system-upgrade", "offline-upgrade", "rpm-ostree rebase", "do-release-upgrade", "--releasever")


class BuiltinStepsTest(unittest.TestCase):
    def folders(self):
        folders = sorted(p for p in STEPS.iterdir() if p.is_dir())
        self.assertTrue(folders)
        return folders

    def test_each_step_is_valid(self):
        for folder in self.folders():
            with self.subTest(step=folder.name):
                with (folder / "info.toml").open("rb") as f:
                    self.assertIsInstance(tomllib.load(f).get("name"), str)
                step = hm.load_step(folder)
                self.assertEqual(step.broken, "")
                self.assertEqual(step.warnings, [])
                self.assertTrue(hm.is_executable_file(folder / "update"))
                if (folder / "check").exists():
                    self.assertTrue(hm.is_executable_file(folder / "check"))

    def test_order(self):
        """A step that needs another step runs right after it. Only the
        system package managers share an order, one of them applies."""
        ids = [s.id for s in hm.load_steps([STEPS])]
        for first, second in (("rustup", "cargo"), ("claude", "claude-plugins"), ("gh-extensions", "gh-skills")):
            with self.subTest(first=first):
                self.assertEqual(ids.index(second), ids.index(first) + 1)
        orders = [s.order for s in hm.load_steps([STEPS]) if s.id not in ("apt", "dnf", "rpm-ostree")]
        self.assertEqual(len(orders), len(set(orders)))

    def test_readme_lists_all_steps_in_run_order(self):
        rows = re.findall(r"^\| `([a-z0-9-]+)` \|", (ROOT / "README.md").read_text(), re.M)
        steps_table = [r for r in rows if r in {p.name for p in self.folders()}]
        self.assertEqual(steps_table, [s.id for s in hm.load_steps([STEPS])])

    def test_no_release_upgrade(self):
        for folder in self.folders():
            for path in folder.iterdir():
                with self.subTest(file=str(path.relative_to(ROOT))):
                    text = path.read_text()
                    for word in FORBIDDEN:
                        self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()
