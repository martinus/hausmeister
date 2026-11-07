#!/usr/bin/env python3
"""hausmeister updates the system and your tools with one command.

Each update step is a folder with an `info.toml`, an optional `check` and an
`update` file. The core finds the folders, decides which steps apply, runs them
one after the other and shows an overview. PLAN.md describes the contract.
"""

import argparse
import concurrent.futures
import curses
import dataclasses
import datetime
import errno
import fcntl
import itertools
import os
import pty
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import threading
import time
import tomllib
import tty
from pathlib import Path

# ---------------------------------------------------------------------------
# Distribution detection
# ---------------------------------------------------------------------------

OS_RELEASE = Path("/etc/os-release")
OSTREE_BOOTED = Path("/run/ostree-booted")
KNOWN_DISTROS = ("fedora", "fedora-atomic", "ubuntu")


@dataclasses.dataclass
class Distro:
    id: str
    version_id: str
    name: str


def read_os_release(path: Path) -> dict[str, str]:
    """Parse an os-release file. Returns an empty dict if it cannot be read."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return {}
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        try:
            result[key.strip()] = " ".join(shlex.split(value))
        except ValueError:
            continue
    return result


def detect_distro(os_release: Path = OS_RELEASE, ostree_booted: Path = OSTREE_BOOTED) -> Distro:
    info = read_os_release(os_release)
    distro_id = info.get("ID") or "linux"
    if ostree_booted.exists():
        distro_id = "fedora-atomic"
    version_id = info.get("VERSION_ID", "")
    name = f"{info.get('NAME') or 'Linux'} {version_id}".strip()
    return Distro(distro_id, version_id, name)


# ---------------------------------------------------------------------------
# Step loader
# ---------------------------------------------------------------------------

STEPS_DIR = Path(__file__).resolve().parent / "steps"
CONTRACT = 1

INFO_KEYS = {
    "name": str,
    "description": str,
    "needs": str,
    "sudo": bool,
    "order": int,
    "default": bool,
    "contract": int,
}
TYPE_NAMES = {str: "a string", bool: "true or false", int: "an integer"}

INSTALLED = "installed"
NOT_INSTALLED = "not installed"
NOT_IN_USE = "not in use"
BROKEN = "broken"


@dataclasses.dataclass
class Step:
    id: str
    path: Path
    name: str = ""
    description: str = ""
    needs: str = ""
    sudo: bool = False
    order: int = 50
    default: bool = True
    contract: int = 1
    broken: str = ""
    warnings: list[str] = dataclasses.field(default_factory=list)
    state: str = ""
    detail: str = ""

    @property
    def update(self) -> Path:
        return self.path / "update"

    @property
    def check(self) -> Path:
        return self.path / "check"

    @property
    def title(self) -> str:
        return self.name or self.id


def user_steps_dir() -> Path:
    config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config) / "hausmeister" / "steps"


def is_executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def load_step(path: Path) -> Step:
    """Load one step folder. Problems go into step.broken, never raise."""
    step = Step(id=path.name, path=path)
    try:
        with (path / "info.toml").open("rb") as f:
            info = tomllib.load(f)
    except FileNotFoundError:
        step.broken = "no info.toml"
        return step
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        step.broken = f"info.toml: {e}"
        return step

    for key, value in info.items():
        expected = INFO_KEYS.get(key)
        if expected is None:
            step.warnings.append(f"unknown key {key!r} in info.toml")
            continue
        # type() and not isinstance(): in Python, True is also an int.
        if type(value) is not expected:
            step.broken = f"info.toml: {key} must be {TYPE_NAMES[expected]}"
            return step
        setattr(step, key, value)

    if not step.name:
        step.broken = "info.toml: name is missing"
    elif step.contract != CONTRACT:
        step.broken = f"unknown contract {step.contract}, this hausmeister knows contract {CONTRACT}"
    elif not step.update.exists():
        step.broken = "no update file"
    elif not is_executable_file(step.update):
        step.broken = "update is not executable"
    elif step.check.exists() and not is_executable_file(step.check):
        step.broken = "check is not executable"
    return step


def load_steps(dirs: list[Path]) -> list[Step]:
    """Load the step folders of all dirs. A folder in a later dir replaces a
    folder with the same id in an earlier dir as a whole."""
    steps: dict[str, Step] = {}
    for d in dirs:
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(".") or not entry.is_dir():
                continue
            steps[entry.name] = load_step(entry)
    return sorted(steps.values(), key=lambda s: (s.order, s.id))


def parse_needs(needs: str) -> list[list[str]]:
    """'cargo cargo-install-update' -> [['cargo'], ['cargo-install-update']]
    'hx | helix' -> [['hx', 'helix']]"""
    needs = re.sub(r"\s*\|\s*", "|", needs.strip())
    groups = [[alt for alt in group.split("|") if alt] for group in needs.split()]
    return [g for g in groups if g]


def missing_needs(needs: str, path: str | None) -> list[str]:
    return [" | ".join(group) for group in parse_needs(needs) if not any(shutil.which(alt, path=path) for alt in group)]


# ---------------------------------------------------------------------------
# Self-update with git
# ---------------------------------------------------------------------------

SCRIPT = Path(__file__).resolve()
FETCH_TIMEOUT = 30.0
GIT_TIMEOUT = 10.0


@dataclasses.dataclass
class SelfUpdate:
    status: str  # "ok", "skipped" or "FAILED"
    text: str
    duration: float = 0.0

    def encode(self) -> str:
        """For HAUSMEISTER_SELF, so that the restarted process can show it."""
        return f"{self.status}\t{self.text}\t{self.duration:.3f}"

    @staticmethod
    def decode(value: str) -> "SelfUpdate | None":
        parts = value.split("\t")
        if len(parts) != 3 or parts[0] not in ("ok", "skipped", "FAILED"):
            return None
        try:
            return SelfUpdate(parts[0], parts[1], float(parts[2]))
        except ValueError:
            return None


def git(repo: Path, *args: str, timeout: float = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def first_error(proc: subprocess.CompletedProcess) -> str:
    lines = (proc.stderr or proc.stdout or "").strip().splitlines()
    return lines[0] if lines else f"exit code {proc.returncode}"


def version(repo: Path | None = None) -> str:
    """The version of the clone: "v0.1.0" at a release tag, "v0.1.0-3-gdae12fd"
    after it, with "-dirty" for local changes, "unknown" without git."""
    try:
        proc = git(repo or SCRIPT.parent, "describe", "--tags", "--match", "v*", "--always", "--dirty")
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else "unknown"


def self_update(script: Path, argv: list[str], exec_fn) -> SelfUpdate:
    """Update the clone of hausmeister to the newest v* tag and restart with
    exec_fn(file, argv). Returns only if there is no restart."""
    start = time.monotonic()
    repo = script.parent

    def result(status: str, text: str) -> SelfUpdate:
        return SelfUpdate(status, text, time.monotonic() - start)

    try:
        status = git(repo, "status", "--porcelain")
        if status.returncode != 0:
            return result("skipped", f"not a git clone: {first_error(status)}")
        if status.stdout.strip():
            return result("skipped", "local changes in the clone. Own steps belong in ~/.config/hausmeister/steps/")
        # On a branch, also at a tagged commit, or not exactly at a v* tag: you develop here.
        on_branch = git(repo, "symbolic-ref", "-q", "HEAD").returncode == 0
        describe = git(repo, "describe", "--tags", "--exact-match", "--match", "v*", "HEAD")
        if on_branch or describe.returncode != 0:
            return result("skipped", "development checkout")
        current = describe.stdout.strip()

        try:
            fetch = git(repo, "fetch", "--tags", "--quiet", "origin", timeout=FETCH_TIMEOUT)
            fetched = fetch.returncode == 0
        except subprocess.TimeoutExpired:
            fetched = False
        if not fetched:
            return result("skipped", f"{current}, git fetch failed (no network?)")

        tags = git(repo, "tag", "--list", "v*", "--sort=-v:refname").stdout.split()
        newest = tags[0] if tags else current
        if newest == current:
            return result("ok", f"{current}, the newest")

        checkout = git(repo, "checkout", "--quiet", "--detach", newest)
        if checkout.returncode != 0:
            return result("FAILED", f"git checkout {newest}: {first_error(checkout)}")
    except (OSError, subprocess.TimeoutExpired) as e:
        return result("FAILED", f"git: {e}")

    done = result("ok", f"{current} → {newest}")
    os.environ["HAUSMEISTER_UPDATED"] = "1"  # the new process does not update again
    os.environ["HAUSMEISTER_SELF"] = done.encode()
    exec_fn(sys.executable, [sys.executable, str(script), *argv])
    return result("FAILED", f"could not restart after the update to {newest}")


# ---------------------------------------------------------------------------
# tmux re-exec
# ---------------------------------------------------------------------------
TMUX_SESSION = "hausmeister"


def tmux_session_running(env: dict[str, str]) -> bool:
    tmux = shutil.which("tmux", path=env.get("PATH"))
    if not tmux:
        return False
    try:
        proc = subprocess.run(
            [tmux, "has-session", "-t", f"={TMUX_SESSION}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def start_tmux(env: dict[str, str], use_tmux: bool, stdin_is_tty: bool, exec_fn) -> str:
    """Replace this process with tmux: attach to a running hausmeister session,
    or start a new one that runs `hausmeister update`. Returns only if the run
    stays in the current terminal, with a note why (empty if already in tmux)."""
    if env.get("TMUX"):
        return ""
    if not use_tmux:
        return "tmux = false in the config, running in this terminal."
    if not stdin_is_tty:
        return "stdin is not a terminal, running without tmux."
    tmux = shutil.which("tmux", path=env.get("PATH"))
    if not tmux:
        return "tmux is not installed, running in this terminal."
    has_session = subprocess.run(
        [tmux, "has-session", "-t", f"={TMUX_SESSION}"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if has_session.returncode == 0:
        # A run is already in progress. Show it instead of starting a second one.
        exec_fn(tmux, [tmux, "attach-session", "-t", f"={TMUX_SESSION}"])
    else:
        # A running tmux server gives a new session its own environment, not the
        # one of this shell. -e PATH keeps tools in ~/.cargo/bin and ~/.local/bin.
        exec_fn(
            tmux,
            [
                tmux,
                "new-session",
                "-s",
                TMUX_SESSION,
                "-e",
                f"PATH={env.get('PATH', '')}",
                "-e",
                "HAUSMEISTER_UPDATED=1",
                *(["-e", f"HAUSMEISTER_SELF={env['HAUSMEISTER_SELF']}"] if env.get("HAUSMEISTER_SELF") else []),
                sys.executable,
                str(SCRIPT),
                "update",
            ],
        )
    return "could not start tmux, running in this terminal."


# ---------------------------------------------------------------------------
# sudo cache
# ---------------------------------------------------------------------------

SUDO_VALIDATE = ["sudo", "-v"]
SUDO_REFRESH = ["sudo", "-n", "-v"]
SUDO_REFRESH_INTERVAL = 60.0
NO_SUDO = "No sudo password"


def cache_sudo(command: list[str] | None = None) -> bool:
    """Ask for the sudo password one time. True if sudo has the password now."""
    try:
        return subprocess.run(command or SUDO_VALIDATE).returncode == 0
    except (OSError, KeyboardInterrupt):
        return False


class SudoKeepAlive:
    """Refreshes the sudo cache in the background, so that a long step does not
    let it expire for the steps after it. The refresh keeps the controlling
    terminal of hausmeister, so it refreshes the same cache that the steps use."""

    def __init__(self, command: list[str] | None = None, interval: float | None = None):
        self.command = command or SUDO_REFRESH
        self.interval = interval or SUDO_REFRESH_INTERVAL
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sudo-keep-alive", daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                subprocess.run(
                    self.command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=15)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GENERAL_DEFAULTS = {"tmux": True, "self_update": True}


def config_path() -> Path:
    config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config) / "hausmeister" / "config.toml"


def read_config(path: Path) -> tuple[dict, str]:
    """Returns the config and a warning. A missing file is an empty config."""
    try:
        with path.open("rb") as f:
            return tomllib.load(f), ""
    except FileNotFoundError:
        return {}, ""
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        return {}, f"{path}: {e}. Using the defaults."


def general_setting(config: dict, key: str) -> tuple[bool, str]:
    """Returns a [general] setting and a warning."""
    default = GENERAL_DEFAULTS[key]
    general = config.get("general", {})
    value = general.get(key, default) if isinstance(general, dict) else default
    if type(value) is not bool:
        return default, f"config: {key} must be true or false. Using {str(default).lower()}."
    return value, ""


def read_selection(config: dict) -> tuple[dict[str, bool] | None, list[str]]:
    """The [select] table, or None if there is none. Returns warnings too."""
    table = config.get("select")
    if table is None:
        return None, []
    if not isinstance(table, dict):
        return None, ["config: select must be a table. Ignoring it."]
    selection, warnings = {}, []
    for step_id, value in table.items():
        if type(value) is bool:
            selection[step_id] = value
        else:
            warnings.append(f"config: select.{step_id} must be true or false. The step does not run.")
            selection[step_id] = False
    return selection, warnings


BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
TABLE_HEADER = re.compile(r"^[ \t]*\[")
SELECT_HEADER = re.compile(r"^[ \t]*\[[ \t]*select[ \t]*\][ \t]*(#.*)?$")


def toml_key(key: str) -> str:
    if BARE_KEY.fullmatch(key):
        return key
    escaped = "".join(
        c if c.isprintable() and c not in '"\\' else (f"\\u{ord(c):04x}" if ord(c) <= 0xFFFF else f"\\U{ord(c):08x}")
        for c in key
    )
    return f'"{escaped}"'


def render_select(selection: dict[str, bool]) -> str:
    lines = ["[select]"] + [f"{toml_key(k)} = {str(v).lower()}" for k, v in selection.items()]
    return "\n".join(lines) + "\n"


def rewrite_select(text: str, selection: dict[str, bool]) -> str:
    """Replace the text of the [select] table: from its header to the next
    table header or the end. All other lines stay byte for byte. Without a
    table, add it at the end."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if SELECT_HEADER.match(line.rstrip("\r\n"))), None)
    table = render_select(selection)
    if start is None:
        if text and not text.endswith("\n"):
            text += "\n"
        return text + ("\n" if text else "") + table
    end = next((i for i in range(start + 1, len(lines)) if TABLE_HEADER.match(lines[i])), len(lines))
    # Empty lines and comments at the end belong to what comes next, e.g.
    # "# tmux settings" above [general]. Keep them.
    keep = end
    while keep > start + 1 and (not lines[keep - 1].strip() or lines[keep - 1].lstrip().startswith("#")):
        keep -= 1
    rest = "".join(lines[keep:])
    if end < len(lines) and not lines[keep:end]:
        rest = "\n" + rest  # an empty line before the next table
    return "".join(lines[:start]) + table + rest


def save_selection(path: Path, selection: dict[str, bool]) -> None:
    """Rewrite [select] in the config file. Write a temporary file, then
    os.replace: a crash cannot leave a half-written config."""
    try:
        text = path.read_text()
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        text, mode = "", 0o644
    new = rewrite_select(text, selection)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(new)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

CHECK_TIMEOUT = 5.0
TAIL_BYTES = 64 * 1024
TAIL_LINES = 5
KEEP_LINES = 40
IMPORTANT_LINE = re.compile(r"error|failed|^E:", re.IGNORECASE)
SUDO_EXPIRED = "sudo: a password is required"
SECOND_CTRL_C = 2.0


def base_env(distro: Distro) -> dict[str, str]:
    env = dict(os.environ)
    env["SUDO"] = "sudo -n"
    env["HAUSMEISTER_DISTRO"] = distro.id
    env["HAUSMEISTER_VERSION_ID"] = distro.version_id
    return env


def run_check(step: Step, env: dict[str, str], timeout: float = CHECK_TIMEOUT) -> bool:
    """True if the check exits with 0 within the timeout."""
    env = dict(env, HAUSMEISTER_STEP_DIR=str(step.path))
    try:
        proc = subprocess.Popen(
            [str(step.check)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=Path.home(),
            start_new_session=True,
        )
    except OSError:
        return False
    try:
        return proc.wait(timeout=timeout) == 0
    except subprocess.TimeoutExpired:
        # The check runs in its own session, so this also stops its children.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        return False


def evaluate(steps: list[Step], env: dict[str, str], timeout: float = CHECK_TIMEOUT) -> None:
    """Set step.state: installed, not installed, not in use or broken."""
    pending = []
    for step in steps:
        if step.broken:
            step.state, step.detail = BROKEN, step.broken
        elif missing := missing_needs(step.needs, env.get("PATH")):
            step.state, step.detail = NOT_INSTALLED, "missing " + ", ".join(missing)
        elif step.check.exists():
            pending.append(step)
        else:
            step.state = INSTALLED
    if not pending:
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(pending))) as pool:
        results = list(pool.map(lambda s: run_check(s, env, timeout), pending))
    for step, applies in zip(pending, results, strict=True):
        step.state = INSTALLED if applies else NOT_IN_USE


@dataclasses.dataclass
class Result:
    step: Step
    status: str  # "ok", "FAILED" or "cancelled"
    exit_code: int
    duration: float
    reason: str = ""
    note: str = ""
    reboot: bool = False
    tail: list[str] = dataclasses.field(default_factory=list)
    ran: bool = True
    log: Path | None = None


def write_all(fd: int, data: bytes) -> None:
    try:
        while data:
            data = data[os.write(fd, data) :]
    except OSError:
        pass  # e.g. stdout is a closed pipe. Keep the step running.


def copy_window_size(src_fd: int, dst_fd: int) -> None:
    try:
        size = fcntl.ioctl(src_fd, termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(dst_fd, termios.TIOCSWINSZ, size)
    except OSError:
        pass


def spawn_update(
    argv: list[str], env: dict[str, str], cwd: Path, out_fd: int, log=None
) -> tuple[int, bytes, list[float]]:
    """Run argv with stdout and stderr on a new pseudo-terminal, and copy its
    output to out_fd and to the binary file log. Returns the exit code, the end of the output and the times
    of Ctrl-C presses.

    stdin and the controlling terminal stay the ones of hausmeister. sudo caches
    the password per terminal (timestamp_type=tty), so `sudo -n` in the step
    finds the password from `sudo -v` in the same terminal. Ctrl-C reaches the
    step directly, because it is in the foreground process group.
    """
    master, slave = pty.openpty()
    copy_window_size(out_fd, slave)
    try:
        proc = subprocess.Popen(argv, stdout=slave, stderr=slave, env=env, cwd=cwd)
    except OSError as e:
        os.close(master)
        os.close(slave)
        message = f"hausmeister: cannot run {argv[0]}: {e.strerror}\n".encode()
        write_all(out_fd, message)
        if log:
            log.write(message)
        return (127 if e.errno == errno.ENOENT else 126), message, []
    os.close(slave)

    tail = b""
    interrupts: list[float] = []
    try:
        while True:
            try:
                readable, _, _ = select.select([master], [], [], 0.2)
                if readable:
                    try:
                        data = os.read(master, 65536)
                    except OSError:  # EIO: no process has the terminal open any more
                        data = b""
                    if not data:
                        break
                    write_all(out_fd, data)
                    if log:
                        try:
                            log.write(data)
                        except OSError:
                            pass  # e.g. a full disk. The step goes on.
                    tail = (tail + data)[-TAIL_BYTES:]
                elif proc.poll() is not None:
                    # The step ended and nothing came for 0.2 s. A background
                    # child can keep the terminal open forever, so stop here.
                    break
            except KeyboardInterrupt:
                interrupts.append(time.monotonic())
        while True:
            try:
                exit_code = proc.wait()
                break
            except KeyboardInterrupt:
                interrupts.append(time.monotonic())
    finally:
        os.close(master)
    return exit_code, tail, interrupts


ANSI_ESCAPE = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")


def last_lines(output: bytes, count: int = KEEP_LINES) -> list[str]:
    text = ANSI_ESCAPE.sub(b"", output).decode(errors="replace")
    lines = []
    for line in text.split("\n"):
        line = line.rstrip("\r").rsplit("\r", 1)[-1].rstrip()  # keep the final state of a progress bar
        if line:
            lines.append(line)
    return lines[-count:]


def first_line(path: Path) -> str:
    try:
        with path.open(errors="replace") as f:
            return f.readline().strip()
    except OSError:
        return ""


def important_lines(lines: list[str], count: int = TAIL_LINES) -> list[str]:
    """The lines with error, failed or E: first, else the last lines."""
    important = [line for line in lines if IMPORTANT_LINE.search(line)]
    return (important or lines)[-count:]


def core_reason(exit_code: int, output: bytes, cancelled: bool) -> str:
    """The reason from the rules for all steps, when the step gave none."""
    if cancelled:
        return "cancelled"
    if SUDO_EXPIRED.encode() in output:
        return "The sudo cache expired, sudo asked for the password again."
    if exit_code == 127:
        return "command not found"
    if exit_code == 126:
        return "not executable"
    if exit_code < 0:
        return f"killed by signal {-exit_code}"
    return ""


def describe_exit(exit_code: int) -> str:
    if exit_code < 0:
        return f"killed by signal {-exit_code}"
    return f"exit code {exit_code}"


def run_step(
    step: Step, env: dict[str, str], version_id: str, os_release: Path, out_fd: int, log_dir: Path | None = None
) -> tuple[Result, bool]:
    """Run one update. Returns the result and True if the user wants to stop the run."""
    clock = datetime.datetime.now().strftime("%H:%M:%S")
    write_all(out_fd, f"\n── {clock} · {step.id} · {step.title} ──\n".encode())
    with tempfile.TemporaryDirectory(prefix=f"hausmeister-{step.id}-") as tmp:
        files = Path(tmp)
        step_env = dict(
            env,
            HAUSMEISTER_STEP_DIR=str(step.path),
            HAUSMEISTER_REASON=str(files / "reason"),
            HAUSMEISTER_NOTE=str(files / "note"),
            HAUSMEISTER_REBOOT=str(files / "reboot"),
        )
        log_path = log_dir / f"{step.id}.log" if log_dir else None
        log = None
        if log_path:
            try:
                log = log_path.open("wb")
            except OSError:
                log_path = None
        start = time.monotonic()
        try:
            exit_code, output, interrupts = spawn_update([str(step.update)], step_env, Path.home(), out_fd, log)
        finally:
            if log:
                log.close()
        result = Result(
            step=step,
            status="ok" if exit_code == 0 else "FAILED",
            exit_code=exit_code,
            duration=time.monotonic() - start,
            reason=first_line(files / "reason"),
            note=first_line(files / "note"),
            reboot=(files / "reboot").exists(),
            tail=last_lines(output),
            log=log_path,
        )
    cancelled = bool(interrupts) and exit_code != 0
    if cancelled:
        result.status = "cancelled"
    if result.status != "ok" and not result.reason:
        result.reason = core_reason(exit_code, output, cancelled)
    new_version_id = read_os_release(os_release).get("VERSION_ID", "")
    if new_version_id != version_id:
        result.status = "FAILED"
        result.reason = f"The distribution release changed ({version_id} → {new_version_id})."
    stop = any(b - a <= SECOND_CTRL_C for a, b in itertools.pairwise(interrupts))
    return result, stop


def run_steps(
    steps: list[Step],
    env: dict[str, str],
    os_release: Path,
    out_fd: int,
    have_sudo: bool = True,
    log_dir: Path | None = None,
) -> tuple[list[Result], bool]:
    """Run the steps in sequence. A failure does not stop the run, two Ctrl-C
    within 2 seconds do. Without have_sudo, the steps with sudo = true fail
    without running. Returns the results and True if the run was stopped."""
    results = []
    for step in steps:
        if step.sudo and not have_sudo:
            results.append(Result(step, "FAILED", 0, 0.0, reason=NO_SUDO, ran=False))
            continue
        # Read it before each step, so that only the step that changed it fails.
        version_id = read_os_release(os_release).get("VERSION_ID", "")
        result, stop = run_step(step, env, version_id, os_release, out_fd, log_dir)
        results.append(result)
        if stop:
            return results, True
    return results, False


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


def format_duration(seconds: float) -> str:
    if seconds < 10:
        return f"{seconds:.1f} s"
    if seconds < 60:
        return f"{int(seconds)} s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes} min {secs:02d} s"


GREEN, RED, RESET = "\x1b[32m", "\x1b[31m", "\x1b[0m"


def use_colours(fd: int, env: dict[str, str]) -> bool:
    return os.isatty(fd) and env.get("TERM", "dumb") != "dumb" and "NO_COLOR" not in env


def short_path(path: Path) -> str:
    """~/.local/state/... instead of /home/me/.local/state/..."""
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def render_overview(
    distro: Distro,
    steps: list[Step],
    results: list[Result],
    started: datetime.datetime,
    ended: datetime.datetime,
    stopped: bool,
    colours: bool = False,
    notes: list[str] | None = None,
    running_version: str = "",
) -> str:
    def paint(text: str, colour: str) -> str:
        return f"{colour}{text}{RESET}" if colours else text

    total = (ended - started).total_seconds()
    out = [
        f" {' '.join(filter(None, ['hausmeister', running_version]))} · {distro.name} · "
        f"{started:%H:%M} to {ended:%H:%M} · {format_duration(total)}",
        "",
    ]

    if results:
        id_width = max(14, *(len(r.step.id) for r in results)) + 2
        text_width = max(len(r.note or r.step.title) for r in results) + 3
        for r in results:
            text = r.note or r.step.title
            colour = {"ok": GREEN, "skipped": ""}.get(r.status, RED)
            status = paint(f"{r.status:<10}", colour) if colour else f"{r.status:<10}"
            out.append(f" {status}{r.step.id:<{id_width}}{text:<{text_width}}{format_duration(r.duration)}")
    else:
        out.append(" No step applies on this machine.")
    if stopped:
        out.append(" Stopped with Ctrl-C. The other steps did not run.")
    out.append("")

    groups = []
    for state, label in ((NOT_IN_USE, "Not in use"), (NOT_INSTALLED, "Not installed")):
        ids = [s.id for s in steps if s.state == state]
        if ids:
            groups.append(f"{label}: {', '.join(ids)}.")
    broken = [f"{s.id} ({s.detail})" for s in steps if s.state == BROKEN]
    if broken:
        groups.append(f"Broken: {', '.join(broken)}.")
    if groups:
        out += [" " + " ".join(groups), ""]
    for note in notes or []:
        out += [f" {note}", ""]

    reboot = [r.step.id for r in results if r.reboot]
    if reboot:
        out += [f" Reboot needed ({', '.join(reboot)}).", ""]

    for r in results:
        if r.status in ("ok", "skipped"):
            continue
        out.append(paint(f" ✘ {r.step.id} · {describe_exit(r.exit_code) if r.ran else 'not run'}", RED))
        if r.reason:
            out.append(f"   Reason: {r.reason}")
        out += [f"   > {line}" for line in important_lines(r.tail)]
        if r.log:
            out.append(f"   Full log: {short_path(r.log)}")
        out.append("")
    return "\n".join(out)


REBOOT = ["systemctl", "reboot"]
POWER_OFF = ["systemctl", "poweroff"]
MENU = " [r] reboot · [p] power off · [s] shell · [q] quit"
MENU_ITEMS = (("r", "reboot"), ("p", "power off"), ("s", "shell"), ("q", "quit"))
KEY_CAP, KEY_CAP_END = "\x1b[1;7m", "\x1b[0m"


def menu_text(colours: bool) -> str:
    """With colours, each key looks like a key cap: reverse video."""
    if not colours:
        return MENU
    return " " + "   ".join(f"{KEY_CAP} {key} {KEY_CAP_END} {label}" for key, label in MENU_ITEMS)


QUIT_KEYS = ("q", "Q", "\r", "\n", "", "\x03", "\x04", "\x1b")


def read_key(fd: int) -> str:
    """Read one key without Enter. Returns "" at the end of the input."""
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        return os.read(fd, 1).decode(errors="replace")
    except (OSError, KeyboardInterrupt):
        return ""
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def end_menu(read, run, exec_fn, shell: str, out_fd: int, colours: bool = False) -> None:
    """The menu after the overview. read() returns a key, run(argv) an exit
    code, exec_fn(file, argv) replaces the process with the shell."""
    menu = menu_text(colours)
    write_all(out_fd, (menu + "\n").encode())
    while True:
        key = read()
        if key in ("r", "p"):
            command = REBOOT if key == "r" else POWER_OFF
            if run(command) == 0:
                return
            write_all(out_fd, f" {' '.join(command)} failed.\n{menu}\n".encode())
        elif key == "s":
            exec_fn(shell, [shell])
            return
        elif key in QUIT_KEYS:
            return


# ---------------------------------------------------------------------------
# Selection UI and the text fallback
# ---------------------------------------------------------------------------

UI_KEYS = " ↑↓/jk move · space toggle · a all · n none · enter save · q cancel"


def state_text(step: Step) -> str:
    return f"broken: {step.detail}" if step.state == BROKEN else step.state


class Selection:
    """Cursor and selection of the UI, without curses."""

    def __init__(self, steps: list[Step], selected: set[str]):
        self.steps = steps
        self.selected = {s.id for s in steps if s.id in selected}
        self.cursor = 0

    @property
    def current(self) -> Step | None:
        return self.steps[self.cursor] if self.steps else None

    def move(self, delta: int) -> None:
        if self.steps:
            self.cursor = max(0, min(len(self.steps) - 1, self.cursor + delta))

    def toggle(self, index: int | None = None) -> None:
        index = self.cursor if index is None else index
        if 0 <= index < len(self.steps):
            self.selected ^= {self.steps[index].id}

    def select_all(self) -> None:
        self.selected |= {s.id for s in self.steps if s.state == INSTALLED}

    def select_none(self) -> None:
        self.selected.clear()

    def lines(self) -> list[str]:
        id_width = max([14] + [len(s.id) for s in self.steps]) + 2
        name_width = max([20] + [len(s.title) for s in self.steps]) + 3
        return [
            f"[{'x' if s.id in self.selected else ' '}] {s.id:<{id_width}}{s.title:<{name_width}}{state_text(s)}"
            for s in self.steps
        ]

    def result(self) -> dict[str, bool]:
        return {s.id: s.id in self.selected for s in self.steps}


def curses_select(selection: Selection, title: str) -> bool:
    """The checkbox list. True if you saved, False if you cancelled."""
    os.environ.setdefault("ESCDELAY", "25")  # Esc cancels without a 1 s delay

    def ui(screen) -> bool:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        offset = 0
        while True:
            height, width = screen.getmaxyx()
            visible = max(1, height - 5)
            offset = min(max(offset, selection.cursor - visible + 1), selection.cursor)
            screen.erase()
            rows = [(0, f" {title}", curses.A_BOLD)]
            for i, line in enumerate(selection.lines()[offset : offset + visible]):
                index = offset + i
                step = selection.steps[index]
                attr = curses.A_NORMAL if step.state == INSTALLED else curses.A_DIM
                if index == selection.cursor:
                    attr |= curses.A_BOLD
                rows.append((2 + i, f" {'>' if index == selection.cursor else ' '} {line}", attr))
            current = selection.current
            rows.append((height - 2, f"   {current.description if current else ''}", curses.A_NORMAL))
            rows.append((height - 1, UI_KEYS, curses.A_DIM))
            for y, text, attr in rows:
                if 0 <= y < height:
                    try:
                        screen.addnstr(y, 0, text, max(1, width - 1), attr)
                    except curses.error:
                        pass
            screen.refresh()

            key = screen.getch()
            if key in (curses.KEY_UP, ord("k")):
                selection.move(-1)
            elif key in (curses.KEY_DOWN, ord("j")):
                selection.move(1)
            elif key == ord(" "):
                selection.toggle()
            elif key == ord("a"):
                selection.select_all()
            elif key == ord("n"):
                selection.select_none()
            elif key in (curses.KEY_ENTER, 10, 13):
                return True
            elif key in (ord("q"), 27):
                return False
            # KEY_RESIZE and other keys: draw again.

    return curses.wrapper(ui)


def text_select(selection: Selection, title: str, read_line, out) -> bool:
    """The numbered list, when curses cannot start or stdin is no terminal."""
    while True:
        print(f" {title}\n", file=out)
        for i, line in enumerate(selection.lines(), 1):
            print(f" {i:>3} {line}", file=out)
        print(" Numbers to toggle (e.g. 1 3), a all, n none, empty line saves, q cancels.", file=out)
        try:
            answer = read_line().strip()
        except (EOFError, KeyboardInterrupt):
            return False
        if not answer:
            return True
        if answer == "q":
            return False
        if answer == "a":
            selection.select_all()
            continue
        if answer == "n":
            selection.select_none()
            continue
        for word in answer.replace(",", " ").split():
            if word.isdigit() and 1 <= int(word) <= len(selection.steps):
                selection.toggle(int(word) - 1)
            else:
                print(f" Not a step number: {word}", file=out)


def choose_steps(steps: list[Step], selected: set[str], title: str, stdin_is_tty: bool) -> dict[str, bool] | None:
    """Show the UI. Returns the selection of all steps, or None if cancelled."""
    selection = Selection(steps, selected)
    saved = None
    if stdin_is_tty:
        try:
            saved = curses_select(selection, title)
        except curses.error:
            saved = None  # e.g. TERM is unknown. Use the text list.
    if saved is None:
        saved = text_select(selection, title, input, sys.stdout)
    return selection.result() if saved else None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def log_dir_path() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "hausmeister" / "last-run"


def prepare_log_dir(log_dir: Path) -> Path | None:
    """Create the log folder and remove the logs of the last run. Only *.log
    files, nothing else in the folder. Returns None if it cannot be used."""
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        for old in log_dir.glob("*.log"):
            if old.is_file():
                old.unlink()
    except OSError as e:
        warn(f"cannot use {log_dir}: {e.strerror}. Running without step logs.")
        return None
    return log_dir


def warn(message: str) -> None:
    print(f"hausmeister: {message}", file=sys.stderr)


def discover(
    steps_dirs: list[Path], os_release: Path = OS_RELEASE, ostree_booted: Path = OSTREE_BOOTED
) -> tuple[Distro, list[Step], dict[str, str]]:
    """Detect the distribution, load the steps and find their state."""
    distro = detect_distro(os_release, ostree_booted)
    if distro.id not in KNOWN_DISTROS:
        warn(f"unknown distribution {distro.id!r}. Only the steps that apply run.")
    steps = load_steps(steps_dirs)
    for step in steps:
        for message in step.warnings:
            warn(f"{step.id}: {message}")
    env = base_env(distro)
    evaluate(steps, env)
    return distro, steps, env


def steps_to_run(steps: list[Step], selection: dict[str, bool] | None) -> tuple[list[Step], list[str]]:
    """The selected steps that apply, and notes for the overview. Without a
    [select] table, the steps with default = true run."""
    applies = [s for s in steps if s.state == INSTALLED]
    if selection is None:
        note = "No selection saved, so the default steps ran. Run hausmeister update --config to choose."
        return [s for s in applies if s.default], [note]
    new = [s.id for s in applies if s.id not in selection]
    notes = []
    if new:
        noun = "step" if len(new) == 1 else "steps"
        notes.append(f"{len(new)} new {noun}: {', '.join(new)}. Run hausmeister update --config.")
    return [s for s in applies if selection.get(s.id) is True], notes


def update(
    steps_dirs: list[Path],
    os_release: Path = OS_RELEASE,
    ostree_booted: Path = OSTREE_BOOTED,
    out_fd: int = 1,
    log_dir: Path | None = None,
    selection: dict[str, bool] | None = None,
    self_result: SelfUpdate | None = None,
    running_version: str = "",
) -> int:
    """Run the selected steps that apply. With log_dir, the output of each step
    also goes to <log_dir>/<step>.log, and the logs of the last run are removed."""
    distro, steps, env = discover(steps_dirs, os_release, ostree_booted)
    write_all(out_fd, f"{' '.join(filter(None, ['hausmeister', running_version]))} · {distro.name}\n".encode())

    started = datetime.datetime.now()
    to_run, notes = steps_to_run(steps, selection)
    if log_dir:
        log_dir = prepare_log_dir(log_dir)

    keep_alive = None
    have_sudo = True
    if any(s.sudo for s in to_run):
        have_sudo = cache_sudo()
        if have_sudo:
            keep_alive = SudoKeepAlive()
            keep_alive.start()
    try:
        results, stopped = run_steps(to_run, env, os_release, out_fd, have_sudo, log_dir)
    finally:
        if keep_alive:
            keep_alive.stop()
    if self_result:
        self_step = Step(id="self", path=SCRIPT.parent, name="hausmeister")
        reason = self_result.text if self_result.status == "FAILED" else ""
        results.insert(0, Result(self_step, self_result.status, 0, self_result.duration, reason, self_result.text))
    now = datetime.datetime.now()
    overview = render_overview(
        distro, steps, results, started, now, stopped, use_colours(out_fd, env), notes, running_version
    )
    write_all(out_fd, ("\n" + overview + "\n").encode())
    return 0 if all(r.status in ("ok", "skipped") for r in results) and not stopped else 1


def initial_selection(steps: list[Step], selection: dict[str, bool] | None) -> set[str]:
    """What the UI shows as selected: the saved selection, or on the first run
    the steps that apply and have default = true."""
    if selection is None:
        return {s.id for s in steps if s.state == INSTALLED and s.default}
    return {step_id for step_id, value in selection.items() if value}


def select_and_save(path: Path, selection: dict[str, bool] | None, title_prefix: str) -> dict[str, bool] | None:
    """Show the selection UI and save the result. None if cancelled."""
    distro, steps, _ = discover([STEPS_DIR, user_steps_dir()])
    chosen = choose_steps(steps, initial_selection(steps, selection), f"{title_prefix} · {distro.name}", os.isatty(0))
    if chosen is None:
        return None
    # Keep the entries of steps that are not here now, e.g. an own folder you moved away.
    merged = chosen | {k: v for k, v in (selection or {}).items() if k not in chosen}
    save_selection(path, merged)
    print(f"Saved the selection in {short_path(path)}.")
    return merged


def run_self_update(use_self_update: bool, argv: list[str]) -> SelfUpdate | None:
    """The self-update, or why it does not run. May restart the process."""
    if os.environ.get("HAUSMEISTER_UPDATED") == "1":
        # Restarted after the self-update or by tmux: show what the first process did.
        return SelfUpdate.decode(os.environ.get("HAUSMEISTER_SELF", ""))
    if not use_self_update:
        result = SelfUpdate("skipped", "self_update = false in the config")
    elif not os.environ.get("TMUX") and tmux_session_running(dict(os.environ)):
        # Never change the step files under a run that is in progress.
        result = SelfUpdate("skipped", "a run is in progress")
    else:
        result = self_update(SCRIPT, argv, os.execv)
    os.environ["HAUSMEISTER_SELF"] = result.encode()  # for the restart in tmux
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hausmeister", description="Update the system and your tools.")
    parser.add_argument("-v", "--version", action="store_true", help="show the version and stop")
    commands = parser.add_subparsers(dest="command")
    update_parser = commands.add_parser("update", help="run all selected steps that apply, then show an overview")
    update_parser.add_argument("--config", action="store_true", help="choose the steps, save them and stop")
    argv = sys.argv[1:] if argv is None else argv
    args = parser.parse_args(argv)
    if args.version:
        print(f"hausmeister {version()}")
        return 0

    if args.command != "update":
        parser.print_help()
        return 0
    if os.geteuid() == 0:
        warn("do not run hausmeister as root. Steps that need root use sudo.")
        return 2

    path = config_path()
    config, message = read_config(path)
    if message:
        warn(message)
    selection, messages = read_selection(config)
    for message in messages:
        warn(message)
    if args.config:
        if select_and_save(path, selection, "hausmeister · select steps") is None:
            print("Nothing saved.")
        return 0

    use_self_update, message = general_setting(config, "self_update")
    if message:
        warn(message)
    self_result = run_self_update(use_self_update, argv)

    use_tmux, message = general_setting(config, "tmux")
    if message:
        warn(message)
    note = start_tmux(dict(os.environ), use_tmux, os.isatty(0), os.execv)
    if note:
        warn(note)

    code = 0
    if selection is None and os.isatty(0):
        # The first run: choose the steps before the first update.
        selection = select_and_save(path, None, "hausmeister · first run · select steps")
        if selection is None:
            print("Nothing saved, nothing updated. Run hausmeister update again to choose the steps.")
    if selection is not None or not os.isatty(0):
        code = update(
            [STEPS_DIR, user_steps_dir()],
            log_dir=log_dir_path(),
            selection=selection,
            self_result=self_result,
            running_version=version(),
        )
    if os.isatty(0):
        end_menu(
            lambda: read_key(0),
            lambda command: subprocess.run(command).returncode,
            os.execvp,
            os.environ.get("SHELL") or "/bin/sh",
            1,
            use_colours(1, dict(os.environ)),
        )
    return code


if __name__ == "__main__":
    sys.exit(main())
