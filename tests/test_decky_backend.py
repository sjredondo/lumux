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
        "--socket", sup.sock, "--backend", "gamescope", "--config-dir", sup.config_dir,
        "--external-settings"]
    # Decky and the desktop window must share the Flatpak's settings.json
    assert sup.config_dir == os.path.join(
        sup.home, ".var", "app", "io.github.enginkirmaci.lumux", "config", "lumux")
    sup.set_setting("node", "57")
    cmd = sup.build_command("host", autostart=True)
    assert cmd[-2:] == ["--node", "57"]
    # autostart is sent by the plugin after pushing settings, not as a flag
    assert "--autostart" not in cmd


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
        res = await plugin.set_saturation(0.5)
        assert res["ok"] and res["result"]["saturation"] == 0.5
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


def test_persist_never_overwrites_before_settings_were_pushed(tmp_path):
    sup = _sup(tmp_path)
    os.makedirs(sup.config_dir)
    with open(sup.lumux_settings_file, "w") as f:
        f.write('{"hue": {"app_key": "keep-me"}}')
    asyncio.run(sup.persist_settings(5))  # no daemon pushed yet: must be a no-op
    with open(sup.lumux_settings_file) as f:
        assert "keep-me" in f.read()


def test_plugin_owns_settings_json(tmp_path, monkeypatch):
    """The daemon (in Decky's Flatpak sandbox) may not see the host's
    settings.json at all; the plugin pushes it in and writes changes back."""
    import json

    sock = str(tmp_path / "run" / "daemon.sock")
    monkeypatch.setattr(client, "socket_path", lambda uid=None: sock)
    unreachable = tmp_path / "sandbox-view"  # what the daemon would see
    main = _load_plugin(tmp_path, monkeypatch)
    plugin = main.Plugin()
    host_cfg = tmp_path / "home" / ".var" / "app" / "io.github.enginkirmaci.lumux" / "config" / "lumux"
    host_cfg.mkdir(parents=True)
    desktop = {
        "hue": {"bridge_ip": "192.0.2.10", "app_key": "desk-key", "client_key": "00" * 16,
                "entertainment_config_id": "area-1"},
        "sync": {"brightness_scale": 1.0, "saturation": 1.0},
        "ui": {"minimize_at_startup": True},  # desktop-only: must survive
    }
    (host_cfg / "settings.json").write_text(json.dumps(desktop))

    async def go():
        await plugin._main()
        plugin.supervisor.set_setting(
            "custom_command",
            f"env PYTHONPATH={os.pathsep.join(sys.path)} {sys.executable} -m lumux.daemon",
        )
        # the daemon gets a config dir it can't use; it must not need it
        plugin.supervisor.config_dir_for_daemon = str(unreachable)
        status = await plugin.get_status()
        deadline = time.monotonic() + 30
        # the panel may see the daemon a moment before the push completes
        while (status["daemon"] is None or not status["daemon"]["paired"]) \
                and time.monotonic() < deadline:
            await asyncio.sleep(0.3)
            status = await plugin.get_status()
        daemon = status["daemon"]
        assert daemon, plugin.supervisor.log_tail()
        assert daemon["paired"] and daemon["area_id"] == "area-1"  # read from the host file
        assert daemon["bridge"]["ip"] == "192.0.2.10"

        res = await plugin.set_brightness(0.7)
        assert res["ok"], res
        res = await plugin.set_saturation(0.4)
        assert res["ok"], res
        saved = json.loads((host_cfg / "settings.json").read_text())
        assert saved["sync"]["brightness_scale"] == 0.7
        assert saved["sync"]["saturation"] == 0.4
        assert saved["hue"]["app_key"] == "desk-key"
        assert saved["ui"]["minimize_at_startup"] is True
        assert not unreachable.exists()  # the daemon never wrote anything itself

        await plugin._unload()

    asyncio.run(go())


@pytest.mark.skipif(not __import__("fake_bridge").can_run(), reason="needs root + openssl (container)")
def test_first_setup_in_game_mode_creates_settings_json(tmp_path, monkeypatch):
    """No desktop setup yet: pairing from the Decky panel must create the
    shared settings.json (pairing + auto-selected area) for both apps."""
    import json

    import fake_bridge as fb

    srv = fb.FakeBridge().start()
    srv.link_pressed = True
    sock = str(tmp_path / "run" / "daemon.sock")
    monkeypatch.setattr(client, "socket_path", lambda uid=None: sock)
    main = _load_plugin(tmp_path, monkeypatch)
    plugin = main.Plugin()
    host_file = (tmp_path / "home" / ".var" / "app" / "io.github.enginkirmaci.lumux"
                 / "config" / "lumux" / "settings.json")

    async def go():
        await plugin._main()
        plugin.supervisor.set_setting(
            "custom_command",
            f"env PYTHONPATH={os.pathsep.join(sys.path)} {sys.executable} -m lumux.daemon",
        )
        status = await plugin.get_status()
        deadline = time.monotonic() + 30
        while status["daemon"] is None and time.monotonic() < deadline:
            await asyncio.sleep(0.3)
            status = await plugin.get_status()
        assert status["daemon"]["state"] == "unpaired"
        assert not host_file.exists()

        res = await plugin.pair("127.0.0.1")
        assert res["ok"], res
        saved = json.loads(host_file.read_text())
        assert saved["hue"]["app_key"] == fb.APP_KEY
        assert saved["hue"]["client_key"] == fb.CLIENT_KEY
        assert saved["hue"]["entertainment_config_id"] == fb.ENT_ID  # single area
        status = await plugin.get_status()
        assert status["daemon"]["paired"]

        # a restarted service (e.g. next boot) picks the pairing up again
        assert (await plugin.restart_service())["ok"]
        status = await plugin.get_status()
        deadline = time.monotonic() + 15
        while not (status["daemon"] and status["daemon"]["paired"]) and time.monotonic() < deadline:
            await asyncio.sleep(0.3)
            status = await plugin.get_status()
        assert status["daemon"]["paired"]
        await plugin._unload()

    try:
        asyncio.run(go())
    finally:
        srv.stop()


def test_flatpak_not_found_is_not_cached_forever(tmp_path, monkeypatch):
    """A failed check (e.g. while the Flatpak was being reinstalled) used to
    stick until Decky restarted, hiding the pairing UI behind "install Lumux"."""
    import lumux_decky.supervisor as supmod

    installed = {"yes": False}

    class _Proc:
        def __init__(self):
            self.returncode = 0 if installed["yes"] else 1

        async def communicate(self):
            return (b"", b"" if installed["yes"] else b"error: io.github.enginkirmaci.lumux not installed")

    async def fake_exec(*argv, **kw):
        return _Proc()

    monkeypatch.setattr(supmod.asyncio, "create_subprocess_exec", fake_exec)
    sup = _sup(tmp_path, which=lambda name, path=None: "/usr/bin/flatpak")

    async def go():
        assert await sup.flatpak_installed() is False
        installed["yes"] = True
        assert await sup.flatpak_installed() is False  # within the recheck window
        sup._flatpak_checked_at -= sup.FLATPAK_RECHECK_S + 1
        assert await sup.flatpak_installed() is True
        installed["yes"] = False
        assert await sup.flatpak_installed() is True  # positive answers are kept

    asyncio.run(go())


def test_child_env_drops_foreign_xdg_dirs(tmp_path):
    env = child_env("/home/deck", 1000, base={
        "XDG_DATA_HOME": "/root/.local/share", "XDG_CONFIG_HOME": "/home/deck/.config",
        "PATH": "/usr/bin"})
    assert "XDG_DATA_HOME" not in env  # would hide the --user Flatpak install
    assert env["XDG_CONFIG_HOME"] == "/home/deck/.config"
