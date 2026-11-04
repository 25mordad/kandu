"""What differs between Linux and Windows, in one place.

Kandu's brain runs on Linux with `systemd`, or on Windows with Task Scheduler. Everything else reads
from here instead of asking which system it is on.
"""
import os
import shutil
import time
from pathlib import Path

WINDOWS = os.name == "nt"

if WINDOWS:
    import msvcrt
else:
    import fcntl

LOCK_POLL_SECONDS = 0.05
O_BINARY = getattr(os, "O_BINARY", 0)  # Without it, Windows writes "\r\n" for each "\n".


def lock(file, blocking=True):
    """Takes an exclusive lock on an open file, held until the file is closed.

    Without `blocking`, raises BlockingIOError when another process holds it.
    """
    if not WINDOWS:
        fcntl.flock(file, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return
    # Windows locks a byte range, not the file. Byte 0 stands for the whole file.
    file.seek(0)
    while True:
        try:
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            if not blocking:
                raise BlockingIOError("the file is locked by another process") from None
            time.sleep(LOCK_POLL_SECONDS)


def read_at(fd, offset):
    """Reads one byte at `offset`. Windows has no `os.pread`; a file opened with O_APPEND still writes at its end."""
    if hasattr(os, "pread"):
        return os.pread(fd, 1, offset)
    os.lseek(fd, offset, os.SEEK_SET)
    return os.read(fd, 1)


def start(argv, **kwargs):
    """Starts a child like `subprocess.Popen`, in a way that `stop` can end with all it starts.

    On Windows a shell's `exec` starts a new process instead of becoming it, and that process may not
    show as the shell's child. A Windows job holds every process started inside it, so the child goes
    into one at once.
    """
    import subprocess
    process = subprocess.Popen(argv, **kwargs)
    if WINDOWS:
        try:
            process.kandu_job = _job_for(process)
        except OSError:
            process.kandu_job = None
    return process


def _job_for(process):
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.AssignProcessToJobObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    job = kernel32.CreateJobObjectW(None, None)
    if not job or not kernel32.AssignProcessToJobObject(job, int(process._handle)):
        raise ctypes.WinError(ctypes.get_last_error())
    return job


def stop(process, force=False):
    """Ends a child and everything it started. On Linux, `force` sends SIGKILL instead of SIGTERM."""
    if not WINDOWS:
        process.kill() if force else process.terminate()
        return
    job = getattr(process, "kandu_job", None)
    if job:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateJobObject.argtypes = (ctypes.c_void_p, ctypes.c_uint)
        kernel32.TerminateJobObject(job, 1)
    if process.poll() is None:
        import subprocess
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True)


def done(process):
    """Lets go of what `start` holds for a child that has ended."""
    job = getattr(process, "kandu_job", None)
    if job:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle(job)
        process.kandu_job = None


def pid_alive(pid):
    """Whether a process with this id runs. Windows only; Linux sessions are checked through tmux."""
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def kill_tree(pid):
    """Ends a process and all it started, by id. Windows only."""
    import subprocess
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)


def home():
    """The user's home folder, as `~` means it to Kandu. On Windows, with forward slashes, which `sh` reads too."""
    found = os.environ.get("HOME") or str(Path.home())
    return found.replace("\\", "/") if WINDOWS else found


def venv_python(install_dir):
    """The path of Kandu's own Python inside an install, as a string with forward slashes."""
    install = str(install_dir).rstrip("/\\").replace("\\", "/")
    return f"{install}/.venv/Scripts/python.exe" if WINDOWS else f"{install}/.venv/bin/python"


def shell():
    """The POSIX `sh` that runs Kandu's shell lines. On Windows it is the one that comes with Git."""
    if not WINDOWS:
        return "sh"
    found = shutil.which("sh")
    if found:
        return found
    git = shutil.which("git")
    if git:
        root = Path(git).resolve().parent.parent  # <Git>/cmd/git.exe or <Git>/bin/git.exe
        for candidate in (root / "bin" / "sh.exe", root / "usr" / "bin" / "sh.exe"):
            if candidate.exists():
                return str(candidate)
    return "sh"


def posix_path(path):
    """A local path as `sh` reads it: `C:\\Users\\me` becomes `/c/Users/me` on Windows."""
    text = str(path)
    if not WINDOWS:
        return text
    text = text.replace("\\", "/")
    if len(text) > 1 and text[1] == ":":
        text = f"/{text[0].lower()}{text[2:]}"
    return text


def sh(script, *args):
    """An argv that runs one shell line, with optional positional arguments."""
    return [shell(), "-c", script, *args]
