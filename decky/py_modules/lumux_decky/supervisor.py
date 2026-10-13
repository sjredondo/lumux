"""Find, launch and babysit lumux-daemon for the Decky plugin.

Why a separate process: Decky's Python is a self-contained PyInstaller
build that cannot import the system's PyGObject/GStreamer, and SteamOS's
read-only rootfs doesn't ship the GStreamer PipeWire plugin anyway. The
Lumux Flatpak already bundles everything (Python, PyGObject, GStreamer with
pipewiresrc, numpy, requests), lives in /home so it survives SteamOS
updates, and is where the user pairs the bridge in Desktop Mode. So the
default runner is ``flatpak run --command=lumux-daemon``.

Runners, in "auto" order:
  flatpak  the Lumux Flatpak (user or system install)
  host     a ``lumux-daemon`` on PATH or in ~/.local/bin
  custom   any command line from the plugin settings
"""

import asyncio
import json
import os
import shlex
import shutil
import signal
import time
from typing import Callable, Dict, List, Optional

from . import client

FLATPAK_APP_ID = client.APP_ID
RUNNERS = ("auto", "flatpak", "host", "custom")
# Daemon commands that may change settings.json contents
SETTINGS_COMMANDS = {"pair", "set_brightness", "set_saturation", "select_area", "set_node"}

DEFAULT_SETTINGS = {
    "runner": "auto",
    "custom_command": "",
    "node": "",  # PipeWire node override; empty = 'gamescope'
    "auto_start": False,  # start syncing when the plugin loads
}
STARTUP_TIMEOUT = 25.0  # first `flatpak run` + GStreamer init can be slow
MAX_CRASHES = 4  # within CRASH_WINDOW seconds, then give up
CRASH_WINDOW = 120.0
LOG_MAX_BYTES = 1 << 20


def child_env(home: str, uid: int, base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Environment for the daemon.

    Decky runs plugins from a PyInstaller bundle that points LD_LIBRARY_PATH
    (and possibly PYTHONHOME/PYTHONPATH) at its own copies, which breaks
    system binaries such as flatpak or /usr/bin/python3. Restore the
    original values and add the session variables that a Game Mode plugin
    process may lack.
    """
    env = dict(os.environ if base is None else base)
    orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
    if orig:
        env["LD_LIBRARY_PATH"] = orig
    else:
        env.pop("LD_LIBRARY_PATH", None)
    for key in ("PYTHONHOME", "PYTHONPATH", "_MEIPASS2", "_PYI_APPLICATION_HOME_DIR",
                "_PYI_ARCHIVE_FILE", "_PYI_PARENT_PROCESS_LEVEL"):
        env.pop(key, None)
    runtime = f"/run/user/{uid}"
    env["HOME"] = home
    env.setdefault("XDG_RUNTIME_DIR", runtime)
    bus = os.path.join(env["XDG_RUNTIME_DIR"], "bus")
    if "DBUS_SESSION_BUS_ADDRESS" not in env and os.path.exists(bus):
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    path = env.get("PATH") or "/usr/bin:/bin"
    extra = [os.path.join(home, ".local", "bin"), "/usr/local/bin", "/usr/bin", "/bin"]
    parts = path.split(":")
    env["PATH"] = ":".join(parts + [p for p in extra if p not in parts])
    env["PYTHONUNBUFFERED"] = "1"
    return env


class Supervisor:
    """Owns the daemon process. All methods run on Decky's asyncio loop."""

    def __init__(
        self,
        settings_dir: str,
        log_dir: str,
        home: str,
        uid: Optional[int] = None,
        logger=None,
        sock_path: Optional[str] = None,
        which: Callable[..., Optional[str]] = shutil.which,
    ):
        self.uid = os.getuid() if uid is None else uid
        self.home = home
        # The desktop app's settings (pairing, area, sliders). Passed to the
        # daemon explicitly so Decky and the desktop window always share one
        # settings.json, whatever environment Decky starts us with. Flatpak
        # mounts this directory at the same path inside the sandbox.
        self.config_dir = os.path.join(home, ".var", "app", FLATPAK_APP_ID, "config", "lumux")
        self.lumux_settings_file = os.path.join(self.config_dir, "settings.json")
        # Path handed to the daemon (only used if it can reach it; tests
        # point it somewhere unreachable to mimic Decky's sandbox)
        self.config_dir_for_daemon = self.config_dir
        # The plugin owns settings.json: the daemon may run in a Flatpak
        # sandbox that can't see (or write) the host's copy, so we push the
        # file in at start and write it back whenever the daemon's settings
        # revision changes.
        self._saved_revision: Optional[int] = None
        self._persist_lock = asyncio.Lock()
        self.settings_file = os.path.join(settings_dir, "settings.json")
        self.log_file = os.path.join(log_dir, "daemon.log")
        self.sock = sock_path or client.socket_path(self.uid)
        self.log = logger
        self._which = which
        self.settings = self._load_settings()

        self.proc: Optional[asyncio.subprocess.Process] = None
        self.state = "stopped"  # stopped | starting | running | crashed | missing
        self.error = ""
        self.runner = ""
        self._want_running = False
        self._crashes: List[float] = []
        self._start_lock = asyncio.Lock()
        self._watch_task: Optional[asyncio.Task] = None
        self._flatpak_cache: Optional[bool] = None

    # -- settings ---------------------------------------------------------

    def _load_settings(self) -> dict:
        data = dict(DEFAULT_SETTINGS)
        try:
            with open(self.settings_file) as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                data.update({k: v for k, v in stored.items() if k in DEFAULT_SETTINGS})
        except (OSError, ValueError):
            pass
        if data["runner"] not in RUNNERS:
            data["runner"] = "auto"
        return data

    def set_setting(self, key: str, value) -> dict:
        if key not in DEFAULT_SETTINGS:
            raise ValueError(f"unknown setting: {key}")
        if key == "runner" and value not in RUNNERS:
            raise ValueError(f"runner must be one of {', '.join(RUNNERS)}")
        if isinstance(DEFAULT_SETTINGS[key], bool):
            value = bool(value)
        elif not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
        self.settings[key] = value
        os.makedirs(os.path.dirname(self.settings_file), exist_ok=True)
        tmp = self.settings_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.settings, f, indent=2)
        os.replace(tmp, self.settings_file)
        return dict(self.settings)

    # -- runner detection ---------------------------------------------------

    def _env(self) -> Dict[str, str]:
        return child_env(self.home, self.uid)

    async def flatpak_installed(self, refresh: bool = False) -> bool:
        if self._flatpak_cache is not None and not refresh:
            return self._flatpak_cache
        flatpak = self._which("flatpak", path=self._env()["PATH"])
        ok = False
        if flatpak:
            try:
                proc = await asyncio.create_subprocess_exec(
                    flatpak, "info", "--show-ref", FLATPAK_APP_ID,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    env=self._env(),
                )
                ok = await asyncio.wait_for(proc.wait(), 15) == 0
            except (OSError, asyncio.TimeoutError):
                ok = False
        self._flatpak_cache = ok
        return ok

    def host_daemon(self) -> Optional[str]:
        return self._which("lumux-daemon", path=self._env()["PATH"])

    async def available_runners(self) -> Dict[str, bool]:
        return {
            "flatpak": await self.flatpak_installed(),
            "host": self.host_daemon() is not None,
            "custom": bool(self.settings.get("custom_command", "").strip()),
        }

    async def pick_runner(self) -> Optional[str]:
        wanted = self.settings["runner"]
        avail = await self.available_runners()
        if wanted != "auto":
            return wanted if avail.get(wanted) else None
        for name in ("custom", "flatpak", "host"):
            if avail[name]:
                return name
        return None

    def build_command(self, runner: str, autostart: bool = False) -> List[str]:
        daemon_args = [
            "--socket", self.sock,
            "--backend", "gamescope",
            "--config-dir", self.config_dir_for_daemon,
            "--external-settings",
        ]
        node = (self.settings.get("node") or "").strip()
        if node:
            daemon_args += ["--node", node]
        if runner == "flatpak":
            flatpak = self._which("flatpak", path=self._env()["PATH"]) or "flatpak"
            return [flatpak, "run", "--command=lumux-daemon", FLATPAK_APP_ID, *daemon_args]
        if runner == "host":
            return [self.host_daemon() or "lumux-daemon", *daemon_args]
        if runner == "custom":
            cmd = shlex.split(self.settings.get("custom_command", ""))
            if not cmd:
                raise ValueError("custom runner selected but no command set")
            return [*cmd, *daemon_args]
        raise ValueError(f"unknown runner: {runner}")

    # -- process management -------------------------------------------------

    async def alive(self) -> bool:
        try:
            await client.request(self.sock, "ping", timeout=2.0)
            return True
        except Exception:
            return False

    def _rotate_log(self) -> None:
        try:
            if os.path.getsize(self.log_file) > LOG_MAX_BYTES:
                os.replace(self.log_file, self.log_file + ".1")
        except OSError:
            pass

    async def ensure_running(self, autostart: bool = False) -> bool:
        """Start the daemon unless it is already up. Returns True when it
        answers on its socket."""
        self._want_running = True
        async with self._start_lock:
            if self.proc is not None and self.proc.returncode is None and await self.alive():
                self.state = "running"
                return True
            if self.proc is None and await self.alive():
                # Started by someone else (e.g. manually for debugging): use it
                self.state, self.runner, self.error = "running", "external", ""
                return True
            return await self._spawn(autostart)

    async def _spawn(self, autostart: bool) -> bool:
        runner = await self.pick_runner()
        if runner is None:
            self.state = "missing"
            self.runner = ""
            self.error = (
                "Lumux is not installed. Install the Lumux Flatpak in Desktop Mode "
                "(see the plugin README), pair your bridge there, then come back."
            )
            return False
        try:
            argv = self.build_command(runner, autostart=autostart)
        except ValueError as e:
            self.state, self.error = "crashed", str(e)
            return False

        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
        self._saved_revision = None  # new daemon: nothing pushed yet
        try:
            os.makedirs(self.config_dir, exist_ok=True)
        except OSError as e:
            self._log("warning", f"could not create {self.config_dir}: {e}")
        self._rotate_log()
        self.state, self.runner, self.error = "starting", runner, ""
        self._log("info", f"starting lumux-daemon via {runner}: {shlex.join(argv)}")
        logf = open(self.log_file, "ab")
        try:
            logf.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} {shlex.join(argv)}\n".encode())
            logf.flush()
            self.proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.DEVNULL, stdout=logf,
                stderr=asyncio.subprocess.STDOUT, env=self._env(),
                start_new_session=True,  # our signals don't hit it by accident
            )
        except OSError as e:
            self.state, self.error = "crashed", f"could not launch lumux-daemon: {e}"
            return False
        finally:
            logf.close()

        proc = self.proc
        self._watch_task = asyncio.ensure_future(self._watch(proc))
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            if proc.returncode is not None:
                self.error = self._explain_exit(proc.returncode, self.log_tail(8))
                return False  # _watch decides whether to retry
            if await self.alive():
                self.state = "running"
                await self.push_settings()
                if autostart:
                    # After the push: autostarting before it would find the
                    # daemon unpaired.
                    try:
                        await client.request(self.sock, "start", timeout=20.0)
                    except Exception as e:
                        self._log("warning", f"autostart failed: {e}")
                return True
            await asyncio.sleep(0.3)
        self.state = "crashed"
        self.error = "lumux-daemon did not open its control socket in time; see daemon.log"
        await self._terminate(proc)
        return False

    async def _watch(self, proc) -> None:
        code = await proc.wait()
        if proc is not self.proc:
            return
        self.proc = None
        if not self._want_running:
            self.state = "stopped"
            return
        tail = self.log_tail(8)
        self.error = self._explain_exit(code, tail)
        self._log("warning", f"lumux-daemon exited with {code}: {self.error}")
        now = time.monotonic()
        self._crashes = [t for t in self._crashes if now - t < CRASH_WINDOW] + [now]
        if len(self._crashes) > MAX_CRASHES or self._fatal(self.error):
            self.state = "crashed"
            self._want_running = False
            return
        self.state = "starting"
        await asyncio.sleep(min(30, 2 ** len(self._crashes)))
        if self._want_running and self.proc is None:
            await self.ensure_running()

    @staticmethod
    def _fatal(error: str) -> bool:
        """Errors a restart can't fix."""
        return "has no lumux-daemon" in error or "already using" in error

    def _explain_exit(self, code: int, tail: str) -> str:
        if "lumux-daemon" in tail and ("not found" in tail or "No such file" in tail):
            return (
                "This Lumux Flatpak has no lumux-daemon. Update it to the latest "
                "sjredondo/lumux release (scripts/install-steamos.sh)."
            )
        if "already running" in tail:
            return "another lumux-daemon is already using the control socket"
        last = [line for line in tail.splitlines() if line.strip()]
        return f"lumux-daemon exited ({code})" + (f": {last[-1][:200]}" if last else "")

    async def stop(self) -> None:
        self._want_running = False
        proc = self.proc
        if proc is None or proc.returncode is not None:
            if self.runner == "external":
                self.state = "stopped"
            return
        try:
            await client.request(self.sock, "shutdown", timeout=3.0)
            await asyncio.wait_for(proc.wait(), 8)
        except Exception:
            await self._terminate(proc)
        self.proc = None
        self.state = "stopped"

    async def _terminate(self, proc) -> None:
        if proc.returncode is not None:
            return
        for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                return
            except PermissionError:
                proc.send_signal(sig)
            try:
                await asyncio.wait_for(proc.wait(), wait)
                return
            except asyncio.TimeoutError:
                continue

    async def restart(self) -> bool:
        await self.stop()
        self._crashes.clear()
        self._flatpak_cache = None
        return await self.ensure_running()

    # -- helpers ----------------------------------------------------------

    def log_tail(self, lines: int = 40) -> str:
        try:
            with open(self.log_file, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 16384))
                data = f.read().decode("utf-8", "replace")
        except OSError:
            return ""
        return "\n".join(data.splitlines()[-lines:])

    def _log(self, level: str, msg: str) -> None:
        if self.log is not None:
            getattr(self.log, level)(msg)

    async def call(self, cmd: str, timeout: float = 10.0, **args):
        if not await self.ensure_running():
            raise client.DaemonError(self.error or "lumux-daemon is not running")
        result = await client.request(self.sock, cmd, timeout=timeout, **args)
        if cmd in SETTINGS_COMMANDS:
            await self.persist_settings()
        return result

    # -- settings.json ownership ---------------------------------------------

    def read_settings_file(self) -> Optional[dict]:
        try:
            with open(self.lumux_settings_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            self._log("warning", f"could not read {self.lumux_settings_file}: {e}")
            return None

    async def push_settings(self) -> None:
        """Hand the desktop app's settings.json to the daemon."""
        data = self.read_settings_file()
        if data is None:
            self._log("info", f"no {self.lumux_settings_file} yet; daemon starts unpaired")
            self._saved_revision = 0
            return
        try:
            res = await client.request(self.sock, "load_settings", timeout=10.0, settings=data)
            self._saved_revision = res.get("revision", 0)
            self._log("info", f"loaded {self.lumux_settings_file} (paired={res.get('paired')})")
        except Exception as e:
            self._log("error", f"could not load settings into lumux-daemon: {e}")

    async def persist_settings(self, revision: Optional[int] = None) -> None:
        """Write the daemon's settings to settings.json if they changed."""
        if self._saved_revision is None:
            # Settings haven't been pushed into this daemon yet; its values
            # are defaults and must never overwrite the user's file.
            return
        if revision is not None and revision == self._saved_revision:
            return
        async with self._persist_lock:
            try:
                res = await client.request(self.sock, "get_settings", timeout=5.0)
            except Exception as e:
                self._log("warning", f"could not fetch settings from lumux-daemon: {e}")
                return
            rev = res.get("revision")
            if self._saved_revision is None or rev == self._saved_revision:
                return
            try:
                os.makedirs(self.config_dir, exist_ok=True)
                tmp = self.lumux_settings_file + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(res["settings"], f, indent=2)
                os.replace(tmp, self.lumux_settings_file)
                self._saved_revision = rev
            except (OSError, KeyError, TypeError) as e:
                self._log("error", f"could not save {self.lumux_settings_file}: {e}")
