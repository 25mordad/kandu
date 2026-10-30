"""Writes the user `systemd` units that wake the supervisor (DESIGN §6).

Rendered from the config, so no setup's paths are in the repo. Installing does
not start anything: `--enable` is a separate, explicit step.
"""
import argparse
import subprocess
import sys
from pathlib import Path

from .config import DEFAULT_LOCAL_DIR, ConfigError, load_config

UNIT_NAME = "kandu"
UNITS_DIR = Path.home() / ".config" / "systemd" / "user"
EXTRA_STOP_MINUTES = 5


def _systemd_path(path):
    return "%h/" + path[2:] if path.startswith("~/") else path


def render_service(config):
    install = _systemd_path(config.settings.brain.install_dir).rstrip("/")
    limit = config.settings.supervisor.max_cycle_minutes + EXTRA_STOP_MINUTES
    return f"""[Unit]
Description=Kandu supervisor cycle

[Service]
Type=oneshot
WorkingDirectory={install}
ExecStart="{install}/.venv/bin/python" -m kandu.supervisor cycle
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
EnvironmentFile=-{install}/local/.env
TimeoutStartSec={limit}min
"""


def render_timer(config):
    return f"""[Unit]
Description=Wake the Kandu supervisor

[Timer]
OnBootSec=5min
OnUnitActiveSec={config.settings.supervisor.wake_every_minutes}min

[Install]
WantedBy=timers.target
"""


def install(config, units_dir=UNITS_DIR):
    units_dir.mkdir(parents=True, exist_ok=True)
    (units_dir / f"{UNIT_NAME}.service").write_text(render_service(config), encoding="utf-8")
    (units_dir / f"{UNIT_NAME}.timer").write_text(render_timer(config), encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)


def main():
    parser = argparse.ArgumentParser(description="Show or install the supervisor's timer.")
    parser.add_argument("--install", action="store_true", help="write the units; starts nothing")
    parser.add_argument("--enable", action="store_true", help="also start the timer")
    parser.add_argument("--local", default=DEFAULT_LOCAL_DIR, help="config directory (default: local/)")
    args = parser.parse_args()
    try:
        config = load_config(args.local)
    except ConfigError as e:
        print(f"timer: {len(e.problems)} problem(s) in config; run: python -m kandu.config", file=sys.stderr)
        return 1
    if not args.install and not args.enable:
        print(render_service(config), render_timer(config), sep="\n")
        return 0
    install(config)
    if args.enable:
        subprocess.run(["systemctl", "--user", "enable", "--now", f"{UNIT_NAME}.timer"], check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
