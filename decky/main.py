"""Lumux Decky plugin backend.

Launches and supervises ``lumux-daemon`` (from the Lumux Flatpak by
default) and relays the Quick Access panel's requests to its control
socket. See py_modules/lumux_decky/supervisor.py for why the daemon is a
separate process.
"""

import asyncio
import os

# The decky plugin module is provided by Decky Loader at runtime
import decky

from lumux_decky import client
from lumux_decky.supervisor import Supervisor

PAIRING_HELP_URL = "https://github.com/sjredondo/lumux/tree/main/decky#first-time-setup"


def _ok(result=None) -> dict:
    return {"ok": True, "result": result, "error": ""}


def _err(error) -> dict:
    return {"ok": False, "result": None, "error": str(error) or type(error).__name__}


class Plugin:
    supervisor: Supervisor = None

    async def _call(self, cmd: str, timeout: float = 10.0, **args) -> dict:
        try:
            return _ok(await self.supervisor.call(cmd, timeout=timeout, **args))
        except client.DaemonError as e:
            return _err(e)
        except (OSError, asyncio.TimeoutError) as e:
            return _err(f"lumux-daemon is not responding ({type(e).__name__})")

    # -- frontend API -------------------------------------------------------

    async def get_status(self) -> dict:
        """Service state plus the daemon's own status. Starts the daemon in
        the background the first time the panel asks."""
        sup = self.supervisor
        # Also retry while "missing": the Flatpak may have just been installed
        if sup.state in ("stopped", "missing") and not sup._start_lock.locked():
            asyncio.ensure_future(sup.ensure_running())
        daemon = None
        if sup.state in ("running", "starting"):
            try:
                daemon = await client.request(sup.sock, "status", timeout=3.0)
                sup.state = "running"
                rev = daemon.get("settings_revision")
                if (rev is not None and sup._saved_revision is not None
                        and rev != sup._saved_revision):
                    asyncio.ensure_future(sup.persist_settings(rev))
            except Exception:
                daemon = None
        return {
            "service": sup.state,
            "service_error": sup.error,
            "runner": sup.runner,
            "runners": await sup.available_runners(),
            "settings": dict(sup.settings),
            "daemon": daemon,
            "help_url": PAIRING_HELP_URL,
            "version": decky.DECKY_PLUGIN_VERSION,
        }

    async def set_sync(self, enabled: bool) -> dict:
        return await self._call("start" if enabled else "stop", timeout=20.0)

    async def list_areas(self) -> dict:
        return await self._call("list_areas", timeout=15.0)

    async def select_area(self, area_id: str) -> dict:
        return await self._call("select_area", timeout=20.0, area_id=area_id)

    async def set_brightness(self, value: float) -> dict:
        return await self._call("set_brightness", value=float(value))

    async def set_saturation(self, value: float) -> dict:
        return await self._call("set_saturation", value=float(value))

    async def pair(self, bridge_ip: str = "") -> dict:
        # link_timeout, not timeout: that name is the request timeout below,
        # and passing both made every pairing attempt raise a TypeError.
        args = {"link_timeout": 30}
        if bridge_ip:
            args["bridge_ip"] = bridge_ip
        return await self._call("pair", timeout=45.0, **args)

    async def list_nodes(self) -> dict:
        return await self._call("list_nodes", timeout=10.0)

    async def set_node(self, node: str) -> dict:
        # Remember it for future launches too (passed as --node)
        self.supervisor.set_setting("node", node or "")
        return await self._call("set_node", node=node or "gamescope")

    async def set_setting(self, key: str, value) -> dict:
        try:
            settings = self.supervisor.set_setting(key, value)
        except ValueError as e:
            return _err(e)
        if key in ("runner", "custom_command"):
            await self.supervisor.restart()
        return _ok(settings)

    async def restart_service(self) -> dict:
        ok = await self.supervisor.restart()
        return _ok(True) if ok else _err(self.supervisor.error or "could not start lumux-daemon")

    async def get_log(self, lines: int = 40) -> dict:
        return _ok(self.supervisor.log_tail(int(lines)))

    # -- Decky lifecycle ----------------------------------------------------

    async def _main(self):
        self.supervisor = Supervisor(
            settings_dir=decky.DECKY_PLUGIN_SETTINGS_DIR,
            log_dir=decky.DECKY_PLUGIN_LOG_DIR,
            home=decky.DECKY_USER_HOME,
            logger=decky.logger,
        )
        decky.logger.info(f"Lumux plugin {decky.DECKY_PLUGIN_VERSION} loaded")
        if self.supervisor.settings.get("auto_start"):
            await self.supervisor.ensure_running(autostart=True)

    async def _unload(self):
        # Stop syncing and release the entertainment area on the bridge
        if self.supervisor is not None:
            await self.supervisor.stop()
        decky.logger.info("Lumux plugin unloaded")

    async def _uninstall(self):
        pass
