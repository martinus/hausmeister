"""Loads hausmeister.py as a module, as PLAN.md describes."""

import importlib.util
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_STEPS = Path(__file__).resolve().parent / "fixtures" / "steps"

FEDORA_44 = 'NAME="Fedora Linux"\nVERSION="44 (Workstation Edition)"\nID=fedora\nVERSION_ID=44\n'


def load():
    if "hausmeister" not in sys.modules:
        spec = importlib.util.spec_from_file_location("hausmeister", ROOT / "hausmeister.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["hausmeister"] = module
        spec.loader.exec_module(module)
    return sys.modules["hausmeister"]


def write(path: Path, text: str, executable: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def make_step(
    root: Path, step_id: str, info: str = 'name = "Test"\n', update: str | None = "exit 0\n", check: str | None = None
) -> Path:
    folder = root / step_id
    folder.mkdir(parents=True)
    if info is not None:
        write(folder / "info.toml", info)
    if update is not None:
        write(folder / "update", "#!/usr/bin/env bash\n" + update, executable=True)
    if check is not None:
        write(folder / "check", "#!/usr/bin/env bash\n" + check, executable=True)
    return folder


def env_with_path(path: str) -> dict[str, str]:
    return dict(os.environ, PATH=path)
