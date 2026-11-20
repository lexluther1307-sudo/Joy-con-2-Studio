"""Connect to the Steam Deck over SSH and run commands for Decky plugin installs.

What you see: every command the tool runs on the Deck is printed as "deck> ...", its output
streams back to your terminal, and the exit code follows as "deck exit: N". Each action is also
appended to tools/deck-activity.log. Add --trace to see every line the Deck's shell runs (bash -x).

One-time setup:
  1. On the Deck, in Konsole (Desktop Mode): sudo systemctl enable --now sshd
  2. Your PC's SSH key must be in the Deck's ~/.ssh/authorized_keys.
     The key used by default is ~/.ssh/steamdeck (change it with --key).
  3. Copy tools/deck-config.example.json to tools/deck-config.json and set "host"
     to the Deck's address (or a name such as steamdeck.local).

From the project folder:
  py tools/deck_deploy.py check               the Deck's state, plugin_loader and installed plugins
  py tools/deck_deploy.py install             upload decky/joycon2studio and run install.sh
  py tools/deck_deploy.py install --dry-run   list the files that would be uploaded
  py tools/deck_deploy.py logs                last 200 lines of the plugin loader log
  py tools/deck_deploy.py run "command"       run a command on the Deck as the deck user
  py tools/deck_deploy.py run --sudo "cmd"    run a command as root
  py tools/deck_deploy.py shell               open an SSH session
  add --trace before the action to see every line the Deck runs

Steps that use sudo (install, logs, run --sudo) need your own terminal: sudo asks for the
Deck's password there. This script never reads, stores or sends that password.
"""

import argparse
import io
import json
import os
import subprocess
import sys
import tarfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent / "decky" / "joycon2studio"
CONFIG_FILE = HERE / "deck-config.json"
LOG_FILE = HERE / "deck-activity.log"
STAGING = "joycon2studio-staging"  # folder in the deck user's home; replaced on every install
REQUIRED = ("plugin.json", "main.py", "joycon2_link.py", "install.sh", "dist/index.js")
SKIP_DIRS = {"node_modules", "src", "__pycache__", ".git"}
SKIP_NAMES = {"package-lock.json"}
SKIP_SUFFIXES = (".map", ".pyc", ".zip", ".log")

CHECK_SCRIPT = """\
echo "host: $(uname -n)"
grep -m1 PRETTY_NAME /etc/os-release
echo "plugin_loader: $(systemctl is-active plugin_loader 2>/dev/null)"
if [ -d "$HOME/homebrew/plugins" ]; then
  echo "plugins: $(ls "$HOME/homebrew/plugins" | tr '\\n' ' ')"
else
  echo "plugins: no ~/homebrew/plugins folder (is Decky Loader installed?)"
fi
"""

TRACE = False  # set by --trace: the Deck's shell prints each command before running it


def fail(message: str, code: int = 1):
    print(message, file=sys.stderr)
    sys.exit(code)


def log(text: str):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {text}\n")


def load_config(args) -> dict:
    cfg = {"host": "", "user": "deck", "key": str(Path.home() / ".ssh" / "steamdeck")}
    if CONFIG_FILE.exists():
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    if args.host:
        cfg["host"] = args.host
    if args.user:
        cfg["user"] = args.user
    if args.key:
        cfg["key"] = args.key
    if not cfg["host"]:
        fail(f'No Deck address. Set "host" in {CONFIG_FILE.name} or pass --host.')
    return cfg


def ssh_command(cfg: dict, tty: bool = False) -> list:
    cmd = ["ssh", "-i", cfg["key"], "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new"]
    if tty:
        cmd.append("-t")  # a terminal lets sudo ask for the password
    else:
        cmd += ["-o", "BatchMode=yes"]  # never wait for a password inside a script
    return cmd + [f"{cfg['user']}@{cfg['host']}"]


def show(text: str):
    print(f"  deck> {text}", flush=True)


def remote(cfg: dict, command: str = "", *, tty: bool = False, data: bytes = None, action: str = ""):
    """Runs one command on the Deck. Its output streams to the terminal; the call returns the exit code."""
    where = f"{cfg['user']}@{cfg['host']}"
    shown = command if command else "interactive shell"
    show(shown)
    log(f"[{action}] {where}: {shown}")
    args = ssh_command(cfg, tty) + ([command] if command else [])
    result = subprocess.run(args, input=data)
    log(f"[{action}] exit {result.returncode}")
    if result.returncode == 255:
        fail(f"Could not reach {where} over SSH. Check that the Deck is on and awake, "
             "on the same network, and that SSH is enabled on it.")
    print(f"  deck exit: {result.returncode}", flush=True)
    return result


def require_terminal(cfg: dict, action: str):
    """sudo needs a real terminal to ask for the password. Asks the Deck session whether it has one,
    before anything is uploaded or run."""
    if not sys.stdin.isatty():
        fail(f"'{action}' asks for the Deck's sudo password, so run it in your own terminal "
             "(not from a script or an agent shell). Nothing was changed.")
    show("test -t 0 && echo TERMINAL_OK   (checks that sudo can ask for the password here)")
    probe = subprocess.run(ssh_command(cfg, tty=True) + ["test -t 0 && echo TERMINAL_OK"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if probe.returncode == 255:
        fail(f"Could not reach {cfg['user']}@{cfg['host']} over SSH. Nothing was changed.")
    if "TERMINAL_OK" not in probe.stdout:
        fail(f"'{action}' needs a real terminal so sudo can ask for the Deck's password. Run it in your "
             "own terminal (Terminal panel or Konsole), not from a script or agent shell. Nothing was changed.")
    print("  deck exit: 0 (terminal confirmed)", flush=True)


def plugin_files():
    for dirpath, dirnames, filenames in os.walk(PLUGIN_DIR):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if name in SKIP_NAMES or name.endswith(SKIP_SUFFIXES):
                continue
            path = Path(dirpath) / name
            yield path, path.relative_to(PLUGIN_DIR).as_posix()


def build_archive():
    missing = [name for name in REQUIRED if not (PLUGIN_DIR / name).exists()]
    if missing:
        fail(f"Missing in {PLUGIN_DIR}: {', '.join(missing)}. Build the plugin first (npm run build).")
    files = list(plugin_files())
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path, rel in files:
            tar.add(path, arcname=rel)
    return buffer.getvalue(), files


def cmd_check(cfg, args):
    print("Running these checks on the Deck:", flush=True)
    for line in CHECK_SCRIPT.splitlines():
        if line.strip():
            show(line)
    result = remote(cfg, "bash -x -s" if TRACE else "bash -s", data=CHECK_SCRIPT.encode(), action="check")
    return result.returncode


def cmd_install(cfg, args):
    if args.dry_run:
        _, files = build_archive()
        size_kb = sum(path.stat().st_size for path, _ in files) // 1024
        for _, rel in files:
            print(rel)
        print(f"{len(files)} files, {size_kb} KB, would be uploaded to ~/{STAGING} on the Deck.")
        return 0
    require_terminal(cfg, "install")
    sys.path.insert(0, str(HERE))
    from build import build_plugin_bundle, sync_versions  # tools/build.py: one version for all builds
    version = sync_versions()  # bumps the version if anything changed since the last build
    build_plugin_bundle()  # rebuilds the Deck's screen from src/index.tsx
    data, files = build_archive()
    size_kb = sum(path.stat().st_size for path, _ in files) // 1024
    print(f"Deck plugin version {version}.")
    print("Steps:")
    print(f"  1. upload {len(files)} files ({size_kb} KB) to ~/{STAGING} on the Deck")
    print("  2. run install.sh on the Deck (sudo asks for the password in this terminal)")
    print("  3. restart plugin_loader (install.sh does this last)")
    upload = remote(cfg, f'rm -rf "$HOME/{STAGING}" && mkdir -p "$HOME/{STAGING}" && tar -xf - -C "$HOME/{STAGING}"',
                    data=data, action="upload")
    if upload.returncode != 0:
        fail("Upload failed. Nothing was installed.")
    flags = "-x " if TRACE else ""
    run = remote(cfg, f'cd "$HOME/{STAGING}" && bash {flags}install.sh', tty=True, action="install")
    if run.returncode != 0:
        fail(f"install.sh stopped with exit code {run.returncode}. Run 'logs' to see why.")
    print("Installed. On the Deck, open the Quick Access menu and pick Joy-Con 2 Studio.")
    return 0


def cmd_logs(cfg, args):
    require_terminal(cfg, "logs")
    command = "sudo journalctl -u plugin_loader -b --no-pager | tail -n 200"
    return remote(cfg, command, tty=True, action="logs").returncode


def cmd_run(cfg, args):
    command = " ".join(args.command)
    if args.sudo:
        require_terminal(cfg, "run --sudo")
        command = "sudo " + command
    return remote(cfg, command, tty=args.sudo, action="run").returncode


def cmd_shell(cfg, args):
    print("Opening a shell on the Deck. What you type runs there. Type exit to leave.", flush=True)
    log(f"[shell] opened {cfg['user']}@{cfg['host']}")
    code = subprocess.run(ssh_command(cfg, tty=True)).returncode
    log(f"[shell] closed, exit {code}")
    return code


def main() -> int:
    global TRACE
    parser = argparse.ArgumentParser(description="Connect to a Steam Deck and run commands for Decky plugin installs.")
    parser.add_argument("--host", help="the Deck's IP address or name (default: tools/deck-config.json)")
    parser.add_argument("--user", help="SSH user on the Deck (default: deck)")
    parser.add_argument("--key", help="SSH private key (default: ~/.ssh/steamdeck)")
    parser.add_argument("--trace", action="store_true", help="show every line the Deck's shell runs")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("check", help="show the Deck's state and installed plugins")
    install = actions.add_parser("install", help="upload the plugin and run install.sh")
    install.add_argument("--dry-run", action="store_true", help="only list the files")
    actions.add_parser("logs", help="show the plugin loader log (sudo asks for the password)")
    run = actions.add_parser("run", help="run a command on the Deck")
    run.add_argument("--sudo", action="store_true", help="run it as root (sudo asks for the password)")
    run.add_argument("command", nargs="+", help="the command, in quotes")
    actions.add_parser("shell", help="open an SSH session")
    args = parser.parse_args()
    TRACE = args.trace

    if args.action == "install" and args.dry_run:
        return cmd_install(None, args)
    cfg = load_config(args)
    handlers = {"check": cmd_check, "install": cmd_install, "logs": cmd_logs, "run": cmd_run, "shell": cmd_shell}
    return handlers[args.action](cfg, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
