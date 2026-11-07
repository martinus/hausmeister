# hausmeister

`hausmeister update` updates the system packages and your tools with one command. It works like [topgrade](https://github.com/topgrade-rs/topgrade), but the core is a single Python file without dependencies, and each update step is a small folder with a script that you can read in a minute.

```text
 hausmeister update · Fedora Linux 44 · 04:19 to 04:23 · 4 min 12 s

 ok        self            v0.3.0 → v0.4.0                                    1.2 s
 ok        dnf             System packages (dnf)                              3 min 05 s
 ok        flatpak-user    Flatpak apps (user)                                9 s
 ok        fwupd           Firmware updates available. Run: fwupdmgr update   4.0 s
 FAILED    rustup          Rust toolchains                                    6.2 s

 Not in use: flatpak-system. Not installed: apt, snap, npm.

 Reboot needed (dnf).

 ✘ rustup · exit code 1
   > error: could not download file from 'https://static.rust-lang.org/...'
   Full log: ~/.local/state/hausmeister/last-run/rustup.log

 [r] reboot · [p] power off · [s] shell · [q] quit
```

What it does:

* Updates the system packages with `dnf`, `rpm-ostree` or `apt`, but never to a new distribution release. No Fedora 44 to 45, no Ubuntu 24.04 to 26.04. You do that yourself.
* Updates your tools: Flatpak, Snap, rustup, cargo, pipx, uv, npm, Claude Code, gh extensions, ... See [Steps](#steps).
* Asks for the sudo password one time, and keeps it alive during the run.
* Runs inside tmux. Detach with `Ctrl-b d`, run `hausmeister update` again to see it.
* A failed step does not stop the others. The overview at the end shows what failed, why, and where the log is.
* Updates itself to the newest release before each run.

It is made for Fedora (also Fedora Atomic) and Ubuntu 24.04 or newer. It needs Python 3.12 or newer and git. tmux is optional.

## Install

```bash
git clone https://github.com/martinus/hausmeister ~/.local/share/hausmeister
cd ~/.local/share/hausmeister
git checkout "$(git tag --list 'v*' --sort=-v:refname | head -n 1)"
ln -s ~/.local/share/hausmeister/hausmeister.py ~/.local/bin/hausmeister
```

The checkout of the newest tag is important. On a branch, hausmeister treats the clone as a development checkout and does not update itself.

## Use

```bash
hausmeister update            # update everything you selected
hausmeister update --config   # choose the steps, save them, and stop
```

On the first run, `hausmeister update` shows the list of steps before it starts. The steps that apply to your machine are selected. Space toggles a step, Enter saves. A step that comes later, e.g. with a new release, does not run until you select it with `hausmeister update --config`. The overview tells you about it.

Run it as your normal user, not with sudo. The steps that need root use `sudo` themselves.

## Steps

| Step | Updates | Runs |
| --- | --- | --- |
| `apt` | System packages (apt) | `sudo apt-get update`, `sudo apt-get full-upgrade -y` |
| `dnf` | System packages (dnf) | `sudo dnf upgrade --refresh -y` |
| `rpm-ostree` | System image (rpm-ostree), Fedora Atomic | `sudo rpm-ostree upgrade` |
| `snap` | Snap packages | `sudo snap refresh` |
| `flatpak-system` | Flatpak apps (system) | `sudo flatpak update --system -y` |
| `flatpak-user` | Flatpak apps (user) | `flatpak update --user -y` |
| `fwupd` | Firmware, only reports | `fwupdmgr get-updates`. Install firmware yourself with `fwupdmgr update`, it can need a reboot. |
| `rustup` | Rust toolchains | `rustup update` |
| `cargo` | Cargo programs | `cargo install-update -a --git`, needs [cargo-update](https://github.com/nabijaczleweli/cargo-update) |
| `pipx` | pipx apps | `pipx upgrade-all --include-injected` |
| `uv` | uv tools | `uv tool upgrade --all` |
| `npm` | npm global packages | `npm update -g`, only when the global folder belongs to you |
| `claude` | Claude Code | `claude update` |
| `claude-plugins` | Claude Code plugins | `claude plugin marketplace update` |
| `gh-extensions` | gh extensions | `gh extension upgrade --all` |
| `gh-skills` | gh skills | `gh skill update --all` |
| `micro` | micro plugins | `micro -plugin update` |
| `distrobox` | Distrobox containers | `distrobox upgrade --all` |
| `tldr` | tldr pages | `tldr --update` |

A step only runs when its program is installed and it applies, e.g. `dnf` does not run on Fedora Atomic. The steps run in this order.

## Own steps

A step is a folder in `steps/`. You can add your own in `~/.config/hausmeister/steps/<id>/`, in any language:

```text
~/.config/hausmeister/steps/my-backup/
├── info.toml     # required
├── check         # optional executable: exit code 0 means the step applies here
└── update        # required executable: exit code 0 means success
```

```toml
name = "Backup dotfiles"
description = "Copies the dotfiles to the NAS"
needs = "rsync"     # programs that must be installed. "a b" means both, "a | b" one of them
sudo = false        # true if update uses $SUDO
order = 95          # lower runs first
```

A folder with the same id as a built-in step replaces the built-in folder as a whole. `update` runs in your home folder and gets these variables:

| Variable | Value |
| --- | --- |
| `SUDO` | `sudo -n`. Use it for root commands: `$SUDO dnf upgrade -y` |
| `HAUSMEISTER_DISTRO` | `fedora`, `fedora-atomic`, `ubuntu`, or the `ID` from `/etc/os-release` |
| `HAUSMEISTER_VERSION_ID` | `VERSION_ID` from `/etc/os-release` |
| `HAUSMEISTER_REASON` | A file. Write one line into it to explain a failure. |
| `HAUSMEISTER_NOTE` | A file. One line in it shows up next to the step in the overview. |
| `HAUSMEISTER_REBOOT` | A file. Create it when the update needs a reboot. |
| `HAUSMEISTER_STEP_DIR` | The step folder, for helper files. |

You can test a step without hausmeister:

```bash
export SUDO=sudo HAUSMEISTER_DISTRO=fedora HAUSMEISTER_REASON=/tmp/reason
export HAUSMEISTER_NOTE=/tmp/note HAUSMEISTER_REBOOT=/tmp/reboot
./steps/dnf/check && ./steps/dnf/update
```

## Config

`~/.config/hausmeister/config.toml`:

```toml
[general]
tmux = true          # false: run in the current terminal
self_update = true   # false: stay at the version you checked out

[select]             # written by hausmeister update --config
dnf = true
flatpak-user = false
rustup = true
```

`hausmeister update --config` rewrites only the `[select]` table. The rest of the file, with your comments, stays as it is.

The output of each step of the last run is in `~/.local/state/hausmeister/last-run/<step>.log`.

## Updates and going back

Before each run, hausmeister fetches the tags from GitHub, checks out the newest `v*` tag and restarts with it. The core and the built-in steps always come from the same release. It does not update itself when the clone has local changes, is on a branch, or has no network.

If a new version is broken, go back and stop the self-update until a fix is out:

```bash
git -C ~/.local/share/hausmeister checkout v0.1.0
```

and set `self_update = false` in the config.

## Development

```bash
python3 -m unittest discover -s tests
shellcheck steps/*/check steps/*/update
```

The tests run sample steps from `tests/fixtures/`, never a real package manager. [PLAN.md](PLAN.md) describes the design and the decisions.

## License

[MIT](LICENSE)
