# hausmeister — Plan for Version 1

## Goal and scope

Version 1 of `hausmeister` updates the system and the selected tools with one command: `hausmeister update`. It works like `topgrade`, but the core is one pure Python file, and each update step is a small folder with executable scripts.

Version 1 does these things:

- Update the system packages on Fedora and Ubuntu, but never to a new distribution release.
- Update a set of user tools (for example Flatpak, rustup, pipx).
- Let you add or replace steps with your own scripts, in any language.
- Let you select the steps in a terminal UI (`hausmeister update --config`), and save the selection in a config file.
- Update itself with git before each run: the core file and the built-in steps together.
- Run inside tmux by default.
- Ask for the sudo password one time only.
- Continue when a step fails. At the end, show an overview of what failed and why.

Version 1 does not do these things. They come later:

- Cleanup (caches, old kernels, unused runtimes).
- Health checks (disk space, failed units). Version 1 only shows "Reboot needed".
- Snapshots before the update.
- Desktop notifications.
- Parallel steps.
- Runs on more than one machine over SSH.

## Requirements and constraints

The core uses only the Python standard library. The steps use only the tools that they update, plus bash and coreutils.

- **No dependencies.** The core uses only the standard library: `argparse`, `curses`, `tomllib`, `subprocess`, `pty`, `shutil`, `threading`, `concurrent.futures`, `pathlib`.
- **git is required.** You install hausmeister as a git clone, and the self-update uses git.
- **Ubuntu 24.04 is the lowest supported version.** It has Python 3.12, so the core needs Python 3.12 or later. All supported Fedora releases have 3.12 or newer.
- **Never upgrade to a new distribution release.** For example, no Fedora 44 to 45, and no Ubuntu 24.04 to 26.04. A release upgrade can break things, so you do it yourself. No step calls `dnf system-upgrade`, `dnf offline-upgrade`, `rpm-ostree rebase` or `do-release-upgrade`, and no step sets `--releasever`. `dnf upgrade`, `apt-get full-upgrade` and `rpm-ostree upgrade` stay in the current release.
- **Fedora and Ubuntu first.** Another distribution needs only new step folders for its package manager, for example `pacman/`.
- **Run as the normal user.** Do not run hausmeister as root. A step that needs root uses `$SUDO`. User tools such as rustup or pipx must run as you, not as root.
- **Never hang.** `$SUDO` is `sudo -n`: if the sudo cache is gone, the command fails at once. A `check` script has a timeout of 5 seconds.

## Architecture: Python core plus step folders

The Python core knows nothing about specific tools. Each update step is a folder with one TOML file and one or two executable files. The core finds the folders, decides which steps apply, runs them and reports the result.

### The step folder

```text
steps/flatpak-user/
├── info.toml     # required: metadata
├── check         # optional executable: does the step apply here?
└── update        # required executable: do the update
```

The folder name is the step id. Steps have the names of the tools that they update (`dnf`, `apt`, `rpm-ostree`, `flatpak-user`), not of the distribution. `check` and `update` can be in any language: bash, Python or anything else with a shebang line and the executable bit set.

### info.toml

```toml
# Updates Flatpak apps that you installed with --user.
name = "Flatpak apps (user)"
description = "Runs flatpak update --user"
needs = "flatpak"
```

| Key | Type | Meaning |
| --- | --- | --- |
| `name` | string, required | Title in the selection UI and in the overview. |
| `description` | string | One line, shown in the selection UI under the cursor. |
| `needs` | string | Programs that must be in `PATH`. A space means "all of them": `"cargo cargo-install-update"`. A `\|` means "one of them": `"hx \| helix"`. If one is missing, the step is "not installed". |
| `sudo` | boolean, default `false` | The step uses `$SUDO`. hausmeister asks for the password only if a selected step that applies has `sudo = true`. |
| `order` | integer, default `50` | Run order. Lower runs first. Ties run in id order. |
| `default` | boolean, default `true` | If `true`, the first-run selection selects the step when it applies. |
| `contract` | integer, default `1` | Version of this contract. The core skips a step with an unknown version and says so. |

The core shows a warning for an unknown key, so that a typo does not stay hidden.

### The check file

- Optional. Without it, the step applies when `needs` is met.
- Exit code 0: the step applies on this machine. Any other exit code: the step is "not in use" here.
- It must not change anything and must not use sudo.
- It must finish in 5 seconds. Else the core stops it and treats the step as "not in use".
- stdin is `/dev/null`. The core ignores its output.

### The update file

- Required. It must be executable.
- Exit code 0 means success. Any other exit code means failure.
- Its stdout and stderr are a pseudo-terminal, so progress bars work. stdin is the terminal of hausmeister, so prompts work.
- On failure, it can write one line with the reason to the file `$HAUSMEISTER_REASON`. The overview shows it.
- It can write one line to the file `$HAUSMEISTER_NOTE`, also on success. The overview shows it next to the step, for example "Firmware updates available. Run: fwupdmgr update".
- If the update needs a reboot, it creates the file `$HAUSMEISTER_REBOOT`. The overview then shows "Reboot needed". Example: `dnf/update` runs `dnf needs-restarting -r` at the end. `apt/update` checks for `/var/run/reboot-required`.
- It handles its own special cases. Example: `fwupdmgr` returns 2 when there is nothing to do, so `fwupd/update` turns 2 into 0.

### Environment for check and update

| Variable | Value |
| --- | --- |
| `SUDO` | `sudo -n`. Use it for all root commands: `$SUDO dnf upgrade -y`. |
| `HAUSMEISTER_DISTRO` | `fedora`, `fedora-atomic`, `ubuntu`, or the `ID` from `/etc/os-release`. |
| `HAUSMEISTER_VERSION_ID` | `VERSION_ID` from `/etc/os-release`. |
| `HAUSMEISTER_REASON` | Path of the reason file. Only for `update`. |
| `HAUSMEISTER_NOTE` | Path of the note file. Only for `update`. |
| `HAUSMEISTER_REBOOT` | Path of the reboot file. Only for `update`. |
| `HAUSMEISTER_STEP_DIR` | Path of the step folder, for helper files. |

The working directory is `$HOME`.

### Built-in steps and your own steps

- Built-in steps are in `steps/` in the hausmeister repository.
- Your own steps are in `~/.config/hausmeister/steps/`.
- A folder of yours with the same id replaces the built-in folder as a whole, not file by file. Thus your `check` never runs with a built-in `update` that expects something different.
- A folder with a missing or wrong `info.toml`, or with an `update` that is not executable, is a "broken" step. The UI and the overview show it with the reason.

### What the core does

- Detect the distribution from `/etc/os-release`. If `/run/ostree-booted` exists, the distribution is `fedora-atomic`. The core uses this only for the environment, the overview header and the release check. On an unknown distribution, it shows a warning and still runs the steps that apply.
- Load all step folders, check `needs`, run the `check` files.
- Run the self-update, tmux, sudo cache, steps and overview (see Run flow).
- Show the selection UI and write the config.

## Built-in steps for version 1

Version 1 ships 19 step folders. The `self` step is not a folder: it is part of the core and always runs first.

| Step | `needs` | Applies when (`check`) | `update` runs | `sudo` | `order` |
| --- | --- | --- | --- | --- | --- |
| `dnf` | `dnf` | `/run/ostree-booted` does not exist | `$SUDO dnf upgrade --refresh -y` | yes | 10 |
| `rpm-ostree` | `rpm-ostree` | `/run/ostree-booted` exists | `$SUDO rpm-ostree upgrade` | yes | 10 |
| `apt` | `apt-get` | always | `$SUDO apt-get update`, then `$SUDO apt-get full-upgrade -y` | yes | 10 |
| `snap` | `snap` | always | `$SUDO snap refresh` | yes | 20 |
| `flatpak-user` | `flatpak` | `flatpak remotes --user` is not empty | `flatpak update --user -y --noninteractive` | no | 30 |
| `flatpak-system` | `flatpak` | `flatpak remotes --system` is not empty | `$SUDO flatpak update --system -y --noninteractive` | yes | 30 |
| `fwupd` | `fwupdmgr` | always | `$SUDO fwupdmgr refresh --force`, then `fwupdmgr get-updates`. Only reports, installs nothing. | yes | 30 |
| `rustup` | `rustup` | always | `rustup update` | no | 40 |
| `cargo` | `cargo cargo-install-update` | always | `cargo install-update -a --git` | no | 45 |
| `pipx` | `pipx` | always | `pipx upgrade-all --include-injected` | no | 50 |
| `uv` | `uv` | always | `uv tool upgrade --all` | no | 50 |
| `npm` | `npm` | the global npm folder is writable | `npm update -g` | no | 50 |
| `claude` | `claude` | always | `claude update` | no | 50 |
| `claude-plugins` | `claude` | always | `claude plugin marketplace update` | no | 55 |
| `gh-extensions` | `gh` | always | `gh extension upgrade --all` | no | 50 |
| `gh-skills` | `gh` | `gh skill --help` works (newer gh only) | `gh skill update --all` | no | 50 |
| `micro` | `micro` | always | `micro -plugin update` | no | 60 |
| `distrobox` | `distrobox` | always | `distrobox upgrade --all` | no | 60 |
| `tldr` | `tldr` | always | `tldr --update` | no | 70 |

All steps have `default = true`. `fwupd` only reports firmware updates and installs nothing, so it is safe to select by default. Firmware updates can need a reboot, so you install them yourself with `fwupdmgr update` when you have time.

Two examples show how small a step is. `dnf/check`:

```bash
#!/usr/bin/env bash
# dnf does not work on Fedora Atomic. The rpm-ostree step does the update there.
[ ! -e /run/ostree-booted ]
```

`fwupd/update`, with its special exit code and a note:

```bash
#!/usr/bin/env bash
set -uo pipefail
# Only report firmware updates. You install them yourself, because they can need a reboot.
# fwupdmgr returns 2 when there is nothing to do. That is not a failure.
$SUDO fwupdmgr refresh --force || [ $? -eq 2 ] || exit 1
fwupdmgr get-updates --no-unreported-check
rc=$?
[ $rc -eq 2 ] && exit 0
[ $rc -ne 0 ] && exit $rc
echo "Firmware updates available. Run: fwupdmgr update" > "$HAUSMEISTER_NOTE"
```

More steps can come later as folders, with no change to the core. Examples: Helix grammars, tmux plugins (tpm), toolbox containers, and `git pull` in a list of repositories. There is no `pip` step: on Ubuntu 24.04, `pip install --user` fails because of PEP 668, and pipx and uv cover your own Python tools.

## Run flow: self-update, tmux, steps, sudo, overview

`hausmeister update` first updates hausmeister itself and restarts with the new code. Then it starts tmux, finds the steps that apply, asks for the sudo password one time, runs the steps in sequence and shows an overview.

### 1. Update itself

hausmeister is a git clone. It updates itself to the newest release tag and then restarts with the new code.

One checkout updates everything in the clone: `hausmeister.py` and all built-in step folders in `steps/`. Thus the core and the steps always come from the same release, and a new step contract version never meets old steps. Your own steps in `~/.config/hausmeister/steps/` are not part of the clone, so the self-update never changes them.

The restart is possible because Python reads and compiles `hausmeister.py` at start: the running process does not need the file on disk after that. The step files are read only when they run, so the self-update must come before the first step. After the restart, the new core runs the new steps.

1. If `self_update = false` is in the config, or `HAUSMEISTER_UPDATED=1` is in the environment, skip this part.
2. Find the repository: the folder of `Path(__file__).resolve()`. `resolve()` follows the symlink in `~/.local/bin`.
3. If `git status --porcelain` shows local changes, skip with a note. The self-update must never remove your changes. Your own steps belong in `~/.config/hausmeister/steps/`.
4. If HEAD is not exactly at a `v*` tag (`git describe --tags --exact-match HEAD`), skip with the note "development checkout". Thus you can develop on a branch in the same clone.
5. Run `git fetch --tags --quiet origin` with a timeout of 30 seconds. If there is no network, continue with the current version.
6. Find the newest tag: the first line of `git tag --list 'v*' --sort=-v:refname`. If it is the current tag, continue with part 2.
7. Run `git checkout --quiet --detach <tag>`.
8. Set `HAUSMEISTER_UPDATED=1` and run `os.execv(sys.executable, [sys.executable, path, *sys.argv[1:]])`. The process keeps its PID and its arguments, but now runs the new code. The environment variable prevents a loop.

The overview shows the change, for example "self: v0.3.0 → v0.4.0". If the new version is broken, go back with `git -C ~/.local/share/hausmeister checkout v0.3.0`. If the self-update fails, the run continues with the current version, and the overview shows the failure.

### 2. Start tmux

1. If `$TMUX` is set, you are already in tmux. Continue in the current pane. The re-exec in item 3 uses this check too, so it needs no extra option.
2. If a session with the name `hausmeister` exists, attach to it (`tmux attach -t hausmeister`). A run is already in progress, so do not start a second one.
3. Otherwise replace the process with `os.execvp`: `tmux new-session -s hausmeister -e PATH=$PATH -e HAUSMEISTER_UPDATED=1 <python> <hausmeister.py> update`. The second `-e` prevents a second self-update check inside tmux.
4. If tmux is not installed, or `tmux = false` is in the config, run in the current terminal and show a short note.

Why `-e PATH=$PATH`: if a tmux server is already running, the new session gets the environment of the server, not of your shell. Then tools in `~/.cargo/bin` or `~/.local/bin` can be missing, and their steps show as "not installed".

You can detach with `Ctrl-b d`. Type `hausmeister update` again to attach to the running update.

### 3. Find the steps that apply

1. Load the built-in step folders and your own step folders.
2. Keep the steps that are selected in the `[select]` table. If a step is not in the table, it is new: it does not run, and the overview says "2 new steps: cargo, uv. Run `hausmeister update --config`."
3. Check `needs`. If a program is missing, the step is "not installed".
4. Run the `check` files, in parallel threads, each with a timeout of 5 seconds. If a check fails, the step is "not in use".

This part comes before the sudo part, so that hausmeister asks for the password only when a step needs it.

### 4. Cache sudo

1. If no step that applies has `sudo = true`, skip this part.
2. Run `sudo -v` one time. This shows the password prompt. If it fails, all steps with `sudo = true` fail with the reason "No sudo password". The other steps still run.
3. Start a daemon thread. It runs `sudo -n -v` every 60 seconds to keep the cache alive. A `threading.Event` stops it at the end.
4. The steps run root commands with `$SUDO`, which is `sudo -n`.

This part runs inside tmux. On Fedora and Ubuntu, sudo stores the cache per terminal (`timestamp_type=tty`), and a tmux pane is a new terminal. A password typed before the tmux start does not apply in the pane. For the same reason, a step must keep the controlling terminal of hausmeister: in its own terminal, `sudo -n` fails with "a password is required".

### 5. Run the steps

1. Read `VERSION_ID` from `/etc/os-release` and remember it.
2. Run the steps that apply, sorted by `order`, then by id.
3. Show a header line with the clock time and the step name, for example `── 04:19:26 · rustup · Rust toolchains ──`. When you attach to tmux later, you see when each step started and which one is still running.
4. Run `update` with stdout and stderr on a new pseudo-terminal (`pty.openpty`), so progress bars work. stdin and the controlling terminal stay the ones of hausmeister, so prompts and `Ctrl-C` reach the step directly. Do not use `pty.spawn`: it makes the new pseudo-terminal the controlling terminal of the step, and then `sudo -n` does not find the cached password (see part 4). The output goes directly to the screen. hausmeister also copies it to `~/.local/state/hausmeister/last-run/<step>.log` and keeps the last 40 lines in memory.
5. Record the exit code (`os.waitstatus_to_exitcode`), the duration, the first line of the reason file, the first line of the note file, and if the reboot file exists.
6. If the step fails, continue with the next step. Do not ask.
7. `Ctrl-C` stops the current step and marks it as "cancelled". A second `Ctrl-C` in 2 seconds stops the full run. The overview comes in both cases.
8. After each step, read `VERSION_ID` again. If it changed, mark the step as failed with the reason "The distribution release changed." This must never happen, so this check finds a wrong step at once.

### 6. Show the overview

The overview shows one line per step that ran, then one short line for the steps that did not apply, then a block for each failure with its reason, and at the end a menu.

```text
 hausmeister update · Fedora Linux 44 · 04:19 to 04:23 · 4 min 12 s

 ok      self            v0.3.0 → v0.4.0                                    1.2 s
 ok      dnf             System packages (dnf)                              3 min 05 s
 ok      flatpak-user    Flatpak apps (user)                                9 s
 ok      fwupd           Firmware updates available. Run: fwupdmgr update   4 s
 FAILED  rustup          Rust toolchains                                    6 s
 ok      pipx            pipx apps                                          12 s

 Not in use: flatpak-system. Not installed: apt, snap, npm.

 Reboot needed (dnf).

 ✘ rustup · exit code 1
   Reason: A download failed. Check the network and run again.
   > error: could not download file from 'https://static.rust-lang.org/...'
   Full log: ~/.local/state/hausmeister/last-run/rustup.log

 [r] reboot · [p] power off · [s] shell · [q] quit
```

A step line shows the note of the step if there is one, else the step name.

The reason comes from the first of these sources that gives one:

1. The reason file of the step (`$HAUSMEISTER_REASON`).
2. Rules in the core for all steps: "sudo: a password is required" in the output means the sudo cache expired. Exit code 127 means "command not found". 126 means "not executable". A signal means "killed by signal N". `Ctrl-C` means "cancelled".
3. If there is no reason, show no "Reason" line.

Under the reason, show up to 5 lines from the output: lines with `error`, `failed` or `E:` first, else the last 5 lines. Use green and red when the terminal has colours, else plain text.

The line "Reboot needed" comes when a step created its reboot file. It names the steps that did so.

The menu at the end comes when stdin is a terminal:

- `r` runs `systemctl reboot`.
- `p` runs `systemctl poweroff`.
- `s` opens your `$SHELL` in the same pane. When you leave the shell, hausmeister ends.
- `q` or `Enter` ends hausmeister. In tmux mode, the pane then closes.

Without a terminal, hausmeister ends at once. The exit code is 1 if a step failed, else 0.

## Selection UI and config file

`hausmeister update --config` opens a checkbox list in `curses`, saves your selection in `~/.config/hausmeister/config.toml` and stops. It does not start an update.

### The selection UI

```text
 hausmeister · select steps · Fedora Linux 44

 > [x] dnf             System packages (dnf)      installed
   [x] flatpak-system  Flatpak apps (system)      installed
   [ ] flatpak-user    Flatpak apps (user)        not in use
   [x] rustup          Rust toolchains            installed
   [x] fwupd           Firmware                   installed
   [ ] apt             System packages (apt)      not installed
   [ ] my-backup       Backup dotfiles            broken: update is not executable

   Runs dnf upgrade --refresh -y
 ↑↓/jk move · space toggle · a all · n none · enter save · q cancel
```

- Show all steps, sorted by `order`, then by id. The line above the keys shows the `description` of the step under the cursor.
- Each step has one of four states: "installed", "not installed" (from `needs`), "not in use" (from `check`) or "broken" (with the reason). Steps that do not apply are dim. You can still select them, so they run when they apply later.
- The UI runs the `check` files in parallel threads, so it opens fast.
- Keys: `↑` `↓` or `j` `k` to move, `Space` to toggle, `a` to select all steps that apply, `n` to select none, `Enter` to save, `q` or `Esc` to cancel.
- Use `curses.wrapper`, so the terminal is always restored, also after an error. Handle `KEY_RESIZE`. If the terminal has no colours, show the state as text only.
- Fallback: if `curses` cannot start, or stdin is not a terminal, show a numbered list. You type numbers to toggle and an empty line to save.
- First run: if no config exists and stdin is a terminal, open the UI before the first update. The steps that apply and have `default = true` are selected.

### The config file

```toml
# hausmeister config. "hausmeister update --config" rewrites only the [select] table.

[general]
tmux = true
self_update = true

[select]
dnf = true
flatpak-system = true
flatpak-user = false
rustup = true
fwupd = true
```

- Location: `$XDG_CONFIG_HOME/hausmeister/config.toml`. If `$XDG_CONFIG_HOME` is not set, use `~/.config`.
- Read with `tomllib`.
- `[select]` has one line per step: the step id and `true` or `false`.
- `update --config` rewrites only the text of the `[select]` table: from the `[select]` line to the next table header or the end of the file. All other lines, with your comments, stay byte for byte. If the table is missing, add it at the end.
- Write to a temporary file, then `os.replace`. A crash cannot leave a half-written config.
- A step that is not in `[select]` is new, for example from a new release or a new folder of yours. It does not run until you select it.

## CLI

Version 1 has one command with one option. All other settings are in the config file. `hausmeister` without a command shows the help.

| Command | What it does |
| --- | --- |
| `hausmeister update` | Update hausmeister itself, then run all selected steps that apply. Continue on failures, then show the overview. |
| `hausmeister update --config` | Open the selection UI, save the selection, and stop. |

There are no other options. The tmux re-exec needs no hidden option, because it checks `$TMUX`. Without a dry run, the tests must cover the logic of the core, and you can test each step by hand (see Tests).

## Repository layout, install and tests

The repository holds the core file, the step folders and the tests.

```text
hausmeister/
├── hausmeister.py          # the core, one file
├── steps/
│   ├── dnf/
│   │   ├── info.toml
│   │   ├── check
│   │   └── update
│   ├── apt/
│   │   ├── info.toml
│   │   └── update
│   └── ...                 # 19 folders in version 1
├── tests/
│   ├── test_*.py
│   └── fixtures/steps/     # small sample steps for the core tests
├── README.md
└── LICENSE
```

`hausmeister.py` has clear sections. Each section uses only the sections above it:

1. Header: `#!/usr/bin/env python3`, docstring, imports.
2. Distribution detection.
3. Step loader: `info.toml`, `needs`, your own steps, broken steps.
4. Self-update with git.
5. tmux re-exec.
6. sudo cache.
7. Config: read, rewrite the `[select]` table.
8. Runner: `check` threads, `pty`, step logs, reasons, notes, reboot file, release check.
9. Overview and end menu.
10. Selection UI and the text fallback.
11. CLI: `argparse`, `main()`.

**Install.** Clone the repository, check out the newest release tag and add a symlink:

```bash
git clone https://github.com/martinus/hausmeister ~/.local/share/hausmeister
cd ~/.local/share/hausmeister
git checkout "$(git tag --list 'v*' --sort=-v:refname | head -n 1)"
ln -s ~/.local/share/hausmeister/hausmeister.py ~/.local/bin/hausmeister
```

The checkout of a tag is necessary: on a branch, the self-update treats the clone as a development checkout and skips.

**Release.** A release is a `vX.Y.Z` tag on `main`. There are no release files. CI runs the tests, `shellcheck` on all bash step files and the step check below.

**Tests of the core.** Use `unittest` from the standard library. Tests load `hausmeister.py` with `importlib.util.spec_from_file_location`.

- Distribution detection: sample `/etc/os-release` files for Fedora, Fedora Atomic (with a fake `ostree-booted` path), Ubuntu 24.04, Linux Mint and an unknown system.
- Step loader: `needs` with spaces and `|`, a folder of yours replaces a built-in folder as a whole, broken folders (no `info.toml`, wrong TOML, no `name`, `update` not executable, unknown `contract`), the warning for an unknown key.
- `check`: exit 0, exit 1, and a check that runs too long (with a short timeout in the test).
- Runner, with the sample steps in `tests/fixtures/steps/`: success, a failure that does not stop the run, the reason file, the note file, the reboot file, exit code 127, a changed `VERSION_ID` (with a fake `os-release` path).
- Config: the rewrite of `[select]` keeps all other bytes. A missing table is added at the end. New steps are found.
- Self-update: a local bare repository with the tags `v0.1.0` and `v0.2.0`. A clone at `v0.1.0` updates to `v0.2.0`, and both `hausmeister.py` and the step folders change. A fake replaces `os.execv`. The skip rules work: local changes, a branch, a remote that is not reachable.
- The UI logic (cursor, toggle, all, none) is in a pure class without `curses`.

**Step check.** A test for all built-in step folders: each has a valid `info.toml` with `name`, an executable `update`, and an executable `check` if the file exists. No file contains `system-upgrade`, `offline-upgrade`, `rpm-ostree rebase`, `do-release-upgrade` or `--releasever`.

**Test a step by hand.** Each step runs without the core:

```bash
export SUDO=sudo HAUSMEISTER_DISTRO=fedora HAUSMEISTER_REASON=/tmp/reason
export HAUSMEISTER_NOTE=/tmp/note HAUSMEISTER_REBOOT=/tmp/reboot
./steps/dnf/check && ./steps/dnf/update
```

## Milestones

Build version 1 in seven steps. Each step gives a tool that you can use.

- [ ] **1. Core and system steps.** Distribution detection, step loader, `needs`, `check`, runner with `pty`, a simple overview, the `update` subcommand, the release check, and the steps `dnf`, `rpm-ostree` and `apt`. Done when a real run updates a Fedora and an Ubuntu 24.04 VM.
- [ ] **2. Sudo cache.** `sudo -v`, the keep-alive thread, `$SUDO`. Done when a run with a long system update asks for the password one time only.
- [ ] **3. tmux.** Re-exec, attach to a running session, `-e PATH`, `tmux = false`, the end menu. Done when you can detach, attach again with `hausmeister update`, and see the overview.
- [ ] **4. All built-in steps and the full overview.** The other 16 step folders, reasons, notes, the reboot line, clock times in the header lines, step logs. Done when a failed step does not stop the other steps, the overview gives the reason, and the step check and `shellcheck` pass.
- [ ] **5. Config, selection UI, own steps.** `update --config`, the four step states, the first-run UI, the `[select]` rewrite, new-step note, your folders in `~/.config/hausmeister/steps/`. Done when the selection survives a restart and your own step replaces a built-in step.
- [ ] **6. Self-update.** git fetch, newest tag, the skip rules, `os.execv`. Done when a clone at an old tag updates to a newer tag from a local test repository and finishes the run with the new code and the new steps.
- [ ] **7. Release.** README in plain language, CI, the first tag `v0.1.0`. Done when a fresh Fedora and a fresh Ubuntu 24.04 VM install with the four commands above and run without extra installs (except git and tmux).

## Decisions

All decisions are made.

| Question | Decision | Reason |
| --- | --- | --- |
| Format of a step? | A folder per step: `info.toml`, optional `check`, required `update`. Any language. | You can test a step by hand. Contributors need no Python. |
| Names of steps? | After the tool (`dnf`, `apt`), not after the distribution | A new distribution needs only new folders. No distribution variants in the files. |
| Format of `info.toml` and the config? | TOML | In the standard library, has comments, one format for both. YAML is not in the standard library. |
| Install and self-update? | git clone. Self-update to the newest `v*` tag. | Simpler than a download with a hash check. A rollback is a checkout of the old tag. |
| What does the self-update change? | The whole clone: `hausmeister.py` and the built-in steps | The core and the steps always come from the same release. Your own steps are outside the clone. |
| Self-update from tags or from `main`? | Tags | A broken commit on `main` cannot break all your machines. |
| Own steps? | `~/.config/hausmeister/steps/<id>/` replaces the built-in folder as a whole | Your `check` never runs with a built-in `update`. |
| After `update --config`, start the update? | No. Save and stop. | You run `hausmeister update` when you want it. |
| `apt-get full-upgrade` or `upgrade`? | `full-upgrade` | Both stay in the current release. You watch the run in tmux and can stop it. |
| Install firmware updates? | No. `fwupd` only reports them, and is selected by default. | Firmware updates can need a reboot, so you install them yourself. |
| A `pip` step? | No | `pip install --user` fails on Ubuntu 24.04 (PEP 668). pipx and uv cover your own Python tools. |
| End of a run? | A menu: reboot, power off, shell, quit | Useful after a kernel update, like in topgrade. |
| Config file? | One file. `update --config` rewrites only `[select]`. | Your comments and settings stay untouched. |
| License? | MIT | Short and permissive, the same as the other projects. |
