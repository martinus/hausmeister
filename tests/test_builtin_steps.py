"""The step check from PLAN.md, for all built-in step folders."""

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

    def test_no_release_upgrade(self):
        for folder in self.folders():
            for path in folder.iterdir():
                with self.subTest(file=str(path.relative_to(ROOT))):
                    text = path.read_text()
                    for word in FORBIDDEN:
                        self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()
