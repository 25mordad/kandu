"""The supervisor's timer on Windows: three Task Scheduler tasks in the folder `\\Kandu\\` (DESIGN §6, §7).

They do what the `systemd` units of `timer.py` do on Linux:
- `supervisor` wakes a cycle every `wake_every_minutes`,
- `sync` reads GitHub and sends the queue every `sync_every_minutes`,
- `dashboard` starts at logon and comes back if it fails.

Each runs `kandu.launch` from the venv, which reads `local/.env` and logs to `logs/<task>.log`.
The tasks run as the user while the user is logged on, with no admin rights. Installing writes them
switched off, as `--install` on Linux starts nothing; `--enable` switches them on.
"""
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from . import compat

FOLDER = "\\Kandu\\"
SUPERVISOR, SYNC, DASHBOARD = "supervisor", "sync", "dashboard"
TASKS = (SUPERVISOR, SYNC, DASHBOARD)
EXTRA_STOP_MINUTES = 5
SYNC_LIMIT_MINUTES = 5
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def install_dir(config):
    return Path(os.path.expanduser(config.settings.brain.install_dir)).resolve()


def current_user():
    """`DOMAIN\\name` of the user this runs as, read from the system, never from config."""
    import getpass
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{getpass.getuser()}" if domain else getpass.getuser()


def pythonw(install):
    """The venv's Python that opens no console window."""
    return install / ".venv" / "Scripts" / "pythonw.exe"


def render_task(config, name, enabled=False, now=None):
    """The task's XML, as Task Scheduler reads it."""
    install = install_dir(config)
    start = (now or datetime.now()).replace(microsecond=0).isoformat()
    settings = config.settings
    if name == DASHBOARD:
        # A logon trigger for any user needs admin rights; one for this user does not.
        trigger = f"<LogonTrigger><Enabled>true</Enabled><UserId>{escape(current_user())}</UserId></LogonTrigger>"
        limit, restart = "PT0S", "<RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>"
        description, args = "Kandu dashboard", "dashboard"
    else:
        every = settings.supervisor.wake_every_minutes if name == SUPERVISOR else settings.github.sync_every_minutes
        trigger = (f"<TimeTrigger><Repetition><Interval>PT{every}M</Interval><StopAtDurationEnd>false</StopAtDurationEnd>"
                   f"</Repetition><StartBoundary>{start}</StartBoundary><Enabled>true</Enabled></TimeTrigger>")
        minutes = settings.supervisor.max_cycle_minutes + EXTRA_STOP_MINUTES if name == SUPERVISOR else SYNC_LIMIT_MINUTES
        limit, restart = f"PT{minutes}M", ""
        description = "Wake the Kandu supervisor" if name == SUPERVISOR else "Sync Kandu's copy of GitHub"
        args = "supervisor cycle" if name == SUPERVISOR else "sync"
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>{escape(description)}</Description></RegistrationInfo>
  <Triggers>{trigger}</Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>{limit}</ExecutionTimeLimit>
    <Enabled>{str(enabled).lower()}</Enabled>{restart}
  </Settings>
  <Actions Context="Author"><Exec>
    <Command>{escape(str(pythonw(install)))}</Command>
    <Arguments>-X utf8 -m kandu.launch {args}</Arguments>
    <WorkingDirectory>{escape(str(install))}</WorkingDirectory>
  </Exec></Actions>
</Task>
"""


def _schtasks(*args):
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, errors="replace")


def state(name):
    """`Ready`, `Running`, `Disabled`, or None if there is no such task. Not translated, unlike `schtasks`."""
    command = f"(Get-ScheduledTask -TaskPath '{FOLDER}' -TaskName '{name}' -ErrorAction SilentlyContinue).State"
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, text=True, errors="replace")
    return result.stdout.strip() or None


def install(config):
    """Writes the three tasks. A task that is on stays on, so applying a new config keeps the timer running."""
    for name in TASKS:
        enabled = state(name) in ("Ready", "Running")
        with tempfile.NamedTemporaryFile("w", suffix=".xml", encoding="utf-16", delete=False) as xml:
            xml.write(render_task(config, name, enabled))
        try:
            result = _schtasks("/Create", "/F", "/TN", FOLDER + name, "/XML", xml.name)
        finally:
            Path(xml.name).unlink(missing_ok=True)
        if result.returncode != 0:
            raise OSError(f"Task Scheduler refused {name}: {result.stderr.strip() or result.stdout.strip()}")


def enable(names):
    for name in names:
        result = _schtasks("/Change", "/ENABLE", "/TN", FOLDER + name)
        if result.returncode != 0:
            raise OSError(f"Task Scheduler could not switch on {name}: {result.stderr.strip()}")
    if DASHBOARD in names and state(DASHBOARD) != "Running":
        _schtasks("/Run", "/TN", FOLDER + DASHBOARD)


def timer_on():
    """Whether Task Scheduler will wake the supervisor by itself."""
    return state(SUPERVISOR) in ("Ready", "Running")


def start_cycle(lock_path):
    """Starts a cycle now, apart from the timer. None if one is already running, False if it could not start.

    The supervisor's own lock tells whether a cycle runs, so a cycle started by hand counts too.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a") as lock:
        try:
            compat.lock(lock, blocking=False)
        except BlockingIOError:
            return None
    install = Path(__file__).resolve().parent.parent
    try:
        subprocess.Popen([str(pythonw(install)), "-X", "utf8", "-m", "kandu.launch", "supervisor", "cycle"],
                         cwd=install, creationflags=DETACHED, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
    except OSError:
        return False
    return True


def restart(config):
    """Applies the config: writes the tasks again, then restarts the dashboard a second later, from outside it."""
    install(config)
    if state(DASHBOARD) != "Running":
        return
    task = FOLDER + DASHBOARD
    line = f'ping -n 2 127.0.0.1 >nul & schtasks /End /TN "{task}" & schtasks /Run /TN "{task}"'
    subprocess.Popen(["cmd", "/c", line], creationflags=DETACHED, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
