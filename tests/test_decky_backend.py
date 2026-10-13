"""Decky plugin backend (decky/main.py + py_modules/lumux_decky).

Decky Loader isn't available here, so a stub ``decky`` module stands in for
it; the daemon is real (run through the plugin's "custom" runner).
"""

import asyncio
import importlib.util
import logging
import os
import sys
import time
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DECKY = os.path.join(ROOT, "decky")
sys.path.insert(0, os.path.join(DECKY, "py_modules"))

from lumux_decky import client  # noqa: E402
from lumux_decky.supervisor import Supervisor, child_env  # noqa: E402


def _load_plugin(tmp_path, monkeypatch):
    stub = types.ModuleType("decky")
    stub.DECKY_PLUGIN_SETTINGS_DIR = str(tmp_path / "settings")
    stub.DECKY_PLUGIN_LOG_DIR = str(tmp_path / "logs")
    stub.DECKY_USER_HOME = str(tmp_path / "home")
    stub.DECKY_PLUGIN_VERSION = "0.0.0-test"
    stub.logger = logging.getLogger("decky-test")
    monkeypatch.setitem(sys.modules, "decky", stub)
    spec = importlib.util.spec_from_file_location("lumux_decky_main", os.path.join(DECKY, "main.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sup(tmp_path, which=lambda *a, **k: None, **kw):
    return Supervisor(settings_dir=str(tmp_path / "s"), log_dir=str(tmp_path / "l"),
                      home=str(tmp_path), uid=1000, sock_path=str(tmp_path / "d.sock"),
                      which=which, **kw)


# -- unit ---------------------------------------------------------------------

def test_child_env_undoes_pyinstaller_and_adds_session(tmp_path):
    (tmp_path / "bus").touch()
    base = {"LD_LIBRARY_PATH": "/tmp/_MEI123", "LD_LIBRARY_PATH_ORIG": "/usr/lib/extra",
            "PYTHONHOME": "/tmp/_MEI123", "PYTHONPATH": "/tmp/_MEI123", "PATH": "/tmp/_MEI123",
            "XDG_RUNTIME_DIR": str(tmp_path)}
    env = child_env("/home/deck", 1000, base)
    assert env["LD_LIBRARY_PATH"] == "/usr/lib/extra"
    assert "PYTHONHOME" not in env and "PYTHONPATH" not in env
    assert env["HOME"] == "/home/deck"
    assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={tmp_path}/bus"
    assert "/usr/bin" in env["PATH"].split(":") and "/home/deck/.local/bin" in env["PATH"]

    env = child_env("/home/deck", 1000, {"LD_LIBRARY_PATH": "/tmp/_MEI1"})
    assert "LD_LIBRARY_PATH" not in env
    assert env["XDG_RUNTIME_DIR"] == "/run/user/1000"


def test_socket_path_matches_daemon_default(monkeypatch):
    from lumux.daemon import default_socket_path

    monkeypatch.setenv("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    assert client.socket_path() == default_socket_path()


def test_flatpak_command(tmp_path):
    sup = _sup(tmp_path, which=lambda name, path=None: f"/usr/bin/{name}")
    assert sup.build_command("flatpak") == [
        "/usr/bin/flatpak", "run", "--command=lumux-daemon", "io.github.enginkirmaci.lumux",
        "--socket", sup.sock, "--backend", "gamescope", "--config-dir", sup.config_dir]
    # Decky and the desktop window must share the Flatpak's settings.json
    assert sup.config_dir == os.path.join(
        sup.home, ".var", "app", "io.github.enginkirmaci.lumux", "config", "lumux")
    sup.set_setting("node", "57")
    assert sup.build_command("host", autostart=True)[-3:] == ["--node", "57", "--autostart"]


def test_settings_validation_and_persistence(tmp_path):
    sup = _sup(tmp_path)
    with pytest.raises(ValueError):
        sup.set_setting("runner", "docker")
    with pytest.raises(ValueError):
        sup.set_setting("evil", 1)
    sup.set_setting("auto_start", 1)
    assert _sup(tmp_path).settings["auto_start"] is True


def test_runner_choice(tmp_path):
    host = {"lumux-daemon": "/home/deck/.local/bin/lumux-daemon"}
    sup = _sup(tmp_path, which=lambda name, path=None: host.get(name))
    assert asyncio.run(sup.pick_runner()) == "host"  # no flatpak binary here
    sup.set_setting("runner", "flatpak")
    assert asyncio.run(sup.pick_runner()) is None
    sup.set_setting("runner", "auto")
    sup.set_setting("custom_command", "my-daemon --x")
    assert asyncio.run(sup.pick_runner()) == "custom"


def test_nothing_installed_reports_missing(tmp_path):
    sup = _sup(tmp_path)
    assert asyncio.run(sup.ensure_running()) is False
    assert sup.state == "missing" and "Flatpak" in sup.error


def test_old_flatpak_without_daemon_is_not_retried(tmp_path):
    sup = _sup(tmp_path)
    sup.set_setting("custom_command",
                    "sh -c 'echo \"bwrap: execvp lumux-daemon: No such file or directory\"; exit 1' sh")

    async def go():
        assert await sup.ensure_running() is False
        await asyncio.sleep(0.5)  # let the watcher record the exit
        return sup.state, sup.error

    state, error = asyncio.run(go())
    assert state == "crashed", (state, error)
    assert "has no lumux-daemon" in error


# -- end to end with the real daemon ------------------------------------------

def test_plugin_drives_daemon(tmp_path, monkeypatch):
    sock = str(tmp_path / "run" / "daemon.sock")
    monkeypatch.setattr(client, "socket_path", lambda uid=None: sock)
    cfg = tmp_path / "lumux-config"
    cfg.mkdir()
    main = _load_plugin(tmp_path, monkeypatch)
    plugin = main.Plugin()

    async def go():
        await plugin._main()
        plugin.supervisor.set_setting(
            "custom_command",
            f"env PYTHONPATH={os.pathsep.join(sys.path)} {sys.executable} -m lumux.daemon "
            f"--config-dir {cfg}",
        )
        status = await plugin.get_status()  # kicks off the daemon
        deadline = time.monotonic() + 30
        while (status["daemon"] is None) and time.monotonic() < deadline:
            await asyncio.sleep(0.3)
            status = await plugin.get_status()
        assert status["daemon"], (status, plugin.supervisor.log_tail())
        assert status["service"] == "running" and status["runner"] == "custom"
        assert status["daemon"]["state"] == "unpaired"
        assert status["daemon"]["capture"]["backend"] == "gamescope"

        res = await plugin.set_sync(True)
        assert not res["ok"] and "Not paired" in res["error"]
        res = await plugin.set_brightness(0.6)
        assert res["ok"] and res["result"]["brightness"] == 0.6
        res = await plugin.set_saturation(1.5)
        assert res["ok"] and res["result"]["saturation"] == 1.5
        res = await plugin.list_areas()
        assert not res["ok"]
        res = await plugin.set_node("gamescope-1")
        assert res["ok"] and plugin.supervisor.settings["node"] == "gamescope-1"

        pid = status["daemon"]["pid"]
        assert (await plugin.restart_service())["ok"]
        status = await plugin.get_status()
        assert status["daemon"]["pid"] != pid
        assert status["daemon"]["capture"]["node"] == "gamescope-1"  # --node on relaunch

        await plugin._unload()
        assert plugin.supervisor.state == "stopped"
        assert not os.path.exists(sock)

    asyncio.run(go())
