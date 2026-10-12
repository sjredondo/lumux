"""Async client for lumux-daemon's control socket (JSON lines).

Kept dependency-free: it runs inside Decky Loader's bundled Python, which
can't load the system's PyGObject. The daemon itself is a separate process.
"""

import asyncio
import itertools
import json
import os
from typing import Any

APP_ID = "io.github.enginkirmaci.lumux"

_ids = itertools.count(1)


class DaemonError(Exception):
    """The daemon answered with ok=false (message is user-presentable)."""


def socket_path(uid: int = None) -> str:
    """Same path the daemon uses by default, both inside the Flatpak and on
    the host: Flatpak shares $XDG_RUNTIME_DIR/app/<app id> with the host."""
    uid = os.getuid() if uid is None else uid
    return f"/run/user/{uid}/app/{APP_ID}/daemon.sock"


async def request(path: str, cmd: str, timeout: float = 10.0, **args) -> Any:
    """Send one command and return its result; raises DaemonError for
    ok=false replies and OSError/asyncio.TimeoutError if unreachable."""

    async def _do():
        reader, writer = await asyncio.open_unix_connection(path, limit=1 << 20)
        try:
            req_id = next(_ids)
            writer.write((json.dumps({"id": req_id, "cmd": cmd, **args}) + "\n").encode())
            await writer.drain()
            line = await reader.readline()
            if not line:
                raise ConnectionError("lumux-daemon closed the connection")
            return json.loads(line)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    reply = await asyncio.wait_for(_do(), timeout)
    if not reply.get("ok"):
        raise DaemonError(reply.get("error") or "unknown error")
    return reply.get("result")
