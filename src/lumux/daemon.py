"""Headless Lumux: bridge + entertainment stream + sync + capture, no GTK.

Built for SteamOS Game Mode, where a Decky Loader plugin controls it, but
it runs anywhere. It reads the same settings.json as the desktop app (so
pairing done in Desktop Mode carries over) and is controlled over a Unix
socket speaking JSON lines::

    -> {"id": 1, "cmd": "status"}
    <- {"id": 1, "ok": true, "result": {...}}
    <- {"id": 2, "ok": false, "error": "..."}

Commands: ping, status, start, stop, set_brightness {value}, list_areas,
select_area {area_id}, discover, pair {bridge_ip?, timeout?}, list_nodes,
set_node {node}, shutdown.

Only GStreamer/GLib are imported from gi. Run ``lumux-daemon --help``.
"""

import argparse
import json
import os
import signal
import socket
import socketserver
import sys
import threading
import time
from typing import Any, Callable, Dict, Optional

from gi.repository import GLib

from lumux import __version__
from lumux.utils.logging import timed_print

APP_ID = "io.github.enginkirmaci.lumux"
PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 64 * 1024


def default_socket_path() -> str:
    """$XDG_RUNTIME_DIR/app/<app id>/daemon.sock.

    Flatpak shares exactly that directory between the sandbox and the host,
    so the path is the same whether the daemon runs inside the Lumux Flatpak
    or on the host.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.join(runtime, "app", APP_ID, "daemon.sock")


class CommandError(Exception):
    """A request failed in an expected way; the message goes to the client."""


def request(sock_path: str, cmd: str, timeout: float = 10.0, **args) -> Any:
    """Send one command to a running daemon and return its result."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(sock_path)
        s.sendall((json.dumps({"id": 1, "cmd": cmd, **args}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                raise ConnectionError("daemon closed the connection")
            buf += chunk
    reply = json.loads(buf)
    if not reply.get("ok"):
        raise CommandError(reply.get("error", "unknown error"))
    return reply.get("result")


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        daemon: "LumuxDaemon" = self.server.daemon_ref  # type: ignore[attr-defined]
        while True:
            line = self.rfile.readline(MAX_REQUEST_BYTES + 1)
            if not line:
                return
            if len(line) > MAX_REQUEST_BYTES:
                self._reply({"ok": False, "error": "request too large"})
                return
            line = line.strip()
            if not line:
                continue
            req_id = None
            try:
                req = json.loads(line)
                if not isinstance(req, dict):
                    raise CommandError("request must be a JSON object")
                req_id = req.get("id")
                cmd = req.get("cmd")
                if not isinstance(cmd, str):
                    raise CommandError("missing 'cmd'")
                args = {k: v for k, v in req.items() if k not in ("id", "cmd")}
                result = daemon.dispatch(cmd, args)
                reply = {"id": req_id, "ok": True, "result": result}
            except CommandError as e:
                reply = {"id": req_id, "ok": False, "error": str(e)}
            except json.JSONDecodeError as e:
                reply = {"id": req_id, "ok": False, "error": f"invalid JSON: {e}"}
            except Exception as e:  # never let a bad request kill the server
                timed_print(f"[daemon] {type(e).__name__} handling request: {e}")
                reply = {"id": req_id, "ok": False, "error": f"internal error: {e}"}
            if not self._reply(reply):
                return

    def _reply(self, payload: dict) -> bool:
        try:
            self.wfile.write((json.dumps(payload) + "\n").encode())
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


class LumuxDaemon:
    """Owns the AppContext and serves the control socket.

    Mutating commands run on the GLib main loop (like the GUI's handlers),
    so ModeManager's timeouts/idle callbacks behave exactly as on desktop.
    """

    STATUS_POLL_MS = 500
    BRIDGE_RECHECK_S = 15.0

    def __init__(
        self,
        socket_path: Optional[str] = None,
        backend: Optional[str] = None,
        node: Optional[str] = None,
    ):
        from lumux.app_context import AppContext
        from lumux.config.settings_manager import SettingsManager

        self.socket_path = socket_path or default_socket_path()
        self.settings = SettingsManager.get_instance()
        self.ctx = AppContext(self.settings)
        # CLI overrides apply to this process only; settings.json keeps the
        # desktop app's capture backend.
        self._backend_override = backend
        self._node_override = node
        self._apply_overrides()

        self.loop = GLib.MainLoop()
        self._server: Optional[_Server] = None
        self._state = "idle"
        self._last_error = ""
        self._last_warning = ""
        self._bridge_connected = False
        self._bridge_checked_at = 0.0
        self._pairing = False
        self._stopping = False

    # ------------------------------------------------------------------
    # Lifecycle

    def serve(self, autostart: bool = False) -> int:
        self._bind()
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, self._on_signal)
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, self._on_signal)
        GLib.timeout_add(self.STATUS_POLL_MS, self._poll_sync_status)
        threading.Thread(
            target=self._server.serve_forever, daemon=True, name="ControlSocket"
        ).start()
        timed_print(f"[daemon] Lumux {__version__} listening on {self.socket_path}")
        timed_print(f"[daemon] settings: {self.settings._settings_file}")
        if autostart:
            GLib.idle_add(self._autostart)
        try:
            self.loop.run()
        finally:
            self._teardown()
        return 0

    def _bind(self) -> None:
        directory = os.path.dirname(self.socket_path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        if os.path.exists(self.socket_path):
            try:
                request(self.socket_path, "ping", timeout=1.0)
            except (OSError, CommandError, ValueError):
                os.unlink(self.socket_path)  # stale socket from a crash
            else:
                raise SystemExit(f"lumux-daemon already running on {self.socket_path}")
        old_umask = os.umask(0o177)  # socket is owner-only (0600)
        try:
            self._server = _Server(self.socket_path, _Handler)
        finally:
            os.umask(old_umask)
        self._server.daemon_ref = self  # type: ignore[attr-defined]

    def _on_signal(self) -> bool:
        timed_print("[daemon] signal received, shutting down")
        self.loop.quit()
        return False

    def _teardown(self) -> None:
        self._stopping = True
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        try:
            self.ctx.shutdown()
        except Exception as e:
            timed_print(f"[daemon] error during shutdown: {e}")
        timed_print("[daemon] stopped")

    def _autostart(self) -> bool:
        try:
            self._cmd_start({})
        except CommandError as e:
            timed_print(f"[daemon] autostart failed: {e}")
        return False

    # ------------------------------------------------------------------
    # Dispatch

    def dispatch(self, cmd: str, args: Dict[str, Any]) -> Any:
        handler = {
            "ping": (self._cmd_ping, False),
            "status": (self._cmd_status, False),
            "start": (self._cmd_start, True),
            "stop": (self._cmd_stop, True),
            "set_brightness": (self._cmd_set_brightness, True),
            "list_areas": (self._cmd_list_areas, False),
            "select_area": (self._cmd_select_area, True),
            "discover": (self._cmd_discover, False),
            "pair": (self._cmd_pair, False),
            "list_nodes": (self._cmd_list_nodes, False),
            "set_node": (self._cmd_set_node, True),
            "shutdown": (self._cmd_shutdown, False),
        }.get(cmd)
        if handler is None:
            raise CommandError(f"unknown command: {cmd}")
        fn, on_main = handler
        if on_main:
            return self._run_on_main(fn, args)
        return fn(args)

    def _run_on_main(self, fn: Callable[[dict], Any], args: dict, timeout: float = 30.0) -> Any:
        if not self.loop.is_running():  # tests calling dispatch() directly
            return fn(args)
        done = threading.Event()
        box: Dict[str, Any] = {}

        def call():
            try:
                box["result"] = fn(args)
            except BaseException as e:  # re-raised on the caller's thread
                box["error"] = e
            done.set()
            return False

        GLib.idle_add(call)
        if not done.wait(timeout):
            raise CommandError("daemon busy, try again")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    # ------------------------------------------------------------------
    # Helpers

    def _apply_overrides(self) -> None:
        if self._backend_override:
            self.ctx.capture.update_backend(self._backend_override, self.settings.sync.fps)
        if self._node_override:
            self.ctx.capture.update_pipewire_node(self._node_override)

    def _apply_settings(self) -> None:
        self.ctx.apply_settings()
        self._apply_overrides()

    def _paired(self) -> bool:
        hue = self.settings.hue
        return bool(hue.bridge_ip and hue.app_key and hue.client_key)

    def _check_bridge(self, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and now - self._bridge_checked_at < self.BRIDGE_RECHECK_S:
            return self._bridge_connected
        self._bridge_checked_at = now
        hue = self.settings.hue
        if not (hue.bridge_ip and hue.app_key):
            self._bridge_connected = False
        else:
            self._bridge_connected = self.ctx.bridge.test_connection()
        return self._bridge_connected

    def _area_name(self, config_id: str) -> str:
        if not config_id:
            return ""
        try:
            config = self.ctx.bridge.get_entertainment_configuration(config_id)
        except Exception:
            config = None
        return (config or {}).get("name", "")

    def _poll_sync_status(self) -> bool:
        """Drain SyncController's queue (the GUI does this on a timer too)."""
        if self._stopping:
            return False
        while True:
            item = self.ctx.sync_controller.get_status()
            if item is None:
                break
            kind, message, _data = item
            if kind == "warning":
                self._last_warning = str(message)
                timed_print(f"[daemon] warning: {message}")
            elif kind == "error":
                self._last_error = str(message)
        if self._state == "syncing" and not self.ctx.sync_controller.is_running():
            self._state = "idle"
        return True

    def _on_video_started(self, ok: bool) -> None:
        if ok:
            self._state = "syncing"
            self._last_error = ""
            timed_print("[daemon] sync started")
            return
        stream = self.ctx.entertainment_stream
        reason = getattr(stream, "last_error", "") if stream else ""
        self._state = "error"
        self._last_error = reason or "Could not start entertainment streaming"
        timed_print(f"[daemon] start failed: {self._last_error}")

    def _resolve_gamescope_node(self) -> None:
        """If gamescope's node isn't called 'gamescope', find it by
        description before starting (best effort; needs PipeWire access)."""
        capture = self.ctx.capture
        if capture._choose_backend() != "gamescope" or self._node_override:
            return
        from lumux.capture import DEFAULT_GAMESCOPE_NODE, find_gamescope_node, list_video_nodes

        if capture.pipewire_node != DEFAULT_GAMESCOPE_NODE:
            return
        nodes = list_video_nodes(timeout_s=1.0)
        found = find_gamescope_node(nodes)
        if found and found.get("name") != DEFAULT_GAMESCOPE_NODE:
            target = found.get("serial") or found.get("name") or found.get("id")
            if target:
                timed_print(f"[daemon] using PipeWire node {target} ({found.get('description')})")
                capture.update_pipewire_node(str(target))

    # ------------------------------------------------------------------
    # Commands

    def _cmd_ping(self, args: dict) -> dict:
        return {"pong": True, "version": __version__, "protocol": PROTOCOL_VERSION}

    def _cmd_status(self, args: dict) -> dict:
        hue = self.settings.hue
        capture = self.ctx.capture
        stats = self.ctx.sync_controller.get_stats()
        stream = self.ctx.entertainment_stream
        syncing = self.ctx.sync_controller.is_running()
        state = self._state
        if state == "syncing" and not syncing:
            state = "idle"
        if not self._paired() and state == "idle":
            state = "unpaired"
        try:
            backend = capture._active_backend or capture._choose_backend()
        except Exception:
            backend = capture.backend
        bridge_connected = self._bridge_connected
        if args.get("refresh"):
            bridge_connected = self._check_bridge(force=True)
        return {
            "version": __version__,
            "protocol": PROTOCOL_VERSION,
            "pid": os.getpid(),
            "state": state,
            "syncing": syncing,
            "mode": self.ctx.mode_manager.get_current_mode().name.lower(),
            "paired": self._paired(),
            "pairing": self._pairing,
            "bridge": {
                "ip": hue.bridge_ip,
                "configured": bool(hue.bridge_ip and hue.app_key),
                "connected": bridge_connected,
            },
            "area_id": hue.entertainment_config_id,
            "streaming": bool(stream and stream.is_connected()),
            "brightness": self.settings.sync.brightness_scale,
            "capture": {
                "backend": backend,
                "node": capture.pipewire_node if backend == "gamescope" else "",
                "capturing": capture.is_capturing(),
                "error": capture.last_error,
            },
            "fps": round(float(stats.get("fps", 0) or 0), 1) if syncing else 0.0,
            "frames": stats.get("frame_count", 0),
            "last_error": self._last_error,
            "last_warning": self._last_warning,
            "settings_file": str(self.settings._settings_file),
        }

    def _cmd_start(self, args: dict) -> dict:
        hue = self.settings.hue
        if self.ctx.sync_controller.is_running():
            return {"state": "syncing"}
        if self._state == "starting":
            return {"state": "starting"}
        if not self._paired():
            raise CommandError(
                "Not paired with a Hue bridge. Pair in the Lumux desktop app "
                "(Desktop Mode) or press the bridge's link button and use Pair."
            )
        if not hue.entertainment_config_id:
            raise CommandError("No entertainment area selected")
        if not self._check_bridge(force=True):
            self.ctx.bridge.connect()
            if not self._check_bridge(force=True):
                raise CommandError(f"Hue bridge at {hue.bridge_ip} is not reachable")
        if self.ctx.entertainment_stream is None:
            self._apply_settings()
        self._resolve_gamescope_node()
        self._last_error = ""
        self._last_warning = ""
        self._state = "starting"
        self.ctx.mode_manager.switch_to_video(callback=self._on_video_started)
        return {"state": self._state}

    def _cmd_stop(self, args: dict) -> dict:
        if self._state == "starting":
            # Let the connect finish; stopping mid-handshake races the worker
            raise CommandError("still connecting, try again in a moment")
        reading = self.settings.reading_mode
        if self.ctx.sync_controller.is_running() and reading.auto_activate:
            # Same as the desktop Stop button: hand over to reading mode
            self.ctx.sync_controller.stop()
        else:
            self.ctx.mode_manager.turn_off(turn_off_lights=False)
        self._state = "idle"
        return {"state": "idle"}

    def _cmd_set_brightness(self, args: dict) -> dict:
        try:
            value = float(args["value"])
        except (KeyError, TypeError, ValueError):
            raise CommandError("set_brightness needs a numeric 'value' (0.0-2.0)")
        value = max(0.0, min(2.0, value))
        self.settings.sync.brightness_scale = value
        self.ctx.color_analyzer.brightness_scale = value
        if args.get("save", True):
            self.settings.save()
        return {"brightness": value}

    def _cmd_list_areas(self, args: dict) -> dict:
        if not (self.settings.hue.bridge_ip and self.settings.hue.app_key):
            raise CommandError("Not paired with a Hue bridge")
        configs = self.ctx.bridge.get_entertainment_configurations()
        self._bridge_connected = bool(configs) or self._check_bridge(force=True)
        if not self._bridge_connected:
            raise CommandError(f"Hue bridge at {self.settings.hue.bridge_ip} is not reachable")
        areas = [
            {
                "id": c.get("id"),
                "name": c.get("name", ""),
                "channels": len(c.get("channels") or []),
                "status": c.get("status", ""),
            }
            for c in configs
        ]
        return {"areas": areas, "selected": self.settings.hue.entertainment_config_id}

    def _cmd_select_area(self, args: dict) -> dict:
        area_id = args.get("area_id")
        if not isinstance(area_id, str) or not area_id:
            raise CommandError("select_area needs an 'area_id'")
        hue = self.settings.hue
        if area_id == hue.entertainment_config_id:
            return {"selected": area_id, "restarted": False}
        was_syncing = self.ctx.sync_controller.is_running()
        if was_syncing or self._state == "starting":
            if self._state == "starting":
                raise CommandError("still connecting, try again in a moment")
            self.ctx.mode_manager.turn_off(turn_off_lights=False)
        hue.entertainment_config_id = area_id
        self.settings.save()
        self._apply_settings()
        self._state = "idle"
        if was_syncing:
            self._cmd_start({})
        return {"selected": area_id, "restarted": was_syncing}

    def _cmd_discover(self, args: dict) -> dict:
        from lumux.hue_bridge import HueBridge

        timeout = float(args.get("timeout", 3.0))
        return {"bridges": HueBridge.discover_bridges(timeout=max(0.5, min(10.0, timeout)))}

    def _cmd_pair(self, args: dict) -> dict:
        """Create an app key + client key. The user presses the bridge's
        link button; we poll for up to ``timeout`` seconds."""
        from lumux.hue_bridge import HueBridge

        if self._pairing:
            raise CommandError("pairing already in progress")
        bridge_ip = args.get("bridge_ip") or self.settings.hue.bridge_ip
        timeout = max(1.0, min(60.0, float(args.get("timeout", 30.0))))
        self._pairing = True
        try:
            if not bridge_ip:
                found = HueBridge.discover_bridges(timeout=3.0)
                if not found:
                    raise CommandError("No Hue bridge found on the network")
                bridge_ip = found[0]
            deadline = time.monotonic() + timeout
            result = None
            last_error = ""
            while time.monotonic() < deadline and not self._stopping:
                probe = HueBridge(bridge_ip, "")
                result = probe.create_user(bridge_ip, application_name="lumux#steamdeck")
                if result and result.get("client_key"):
                    break
                last_error = "press the link button on the bridge"
                time.sleep(1.0)
            if not (result and result.get("client_key")):
                raise CommandError(f"Pairing timed out ({last_error})")

            def save():
                hue = self.settings.hue
                hue.bridge_ip = bridge_ip
                hue.app_key = result["app_key"]
                hue.client_key = result["client_key"]
                self.settings.save()
                self._apply_settings()
                self._bridge_checked_at = 0.0
                return True

            self._run_on_main(lambda _a: save(), {})
            areas = []
            try:
                areas = self._cmd_list_areas({})["areas"]
            except CommandError:
                pass
            if len(areas) == 1 and not self.settings.hue.entertainment_config_id:
                self._run_on_main(self._cmd_select_area, {"area_id": areas[0]["id"]})
            return {"paired": True, "bridge_ip": bridge_ip, "areas": areas}
        finally:
            self._pairing = False

    def _cmd_list_nodes(self, args: dict) -> dict:
        from lumux.capture import find_gamescope_node, list_video_nodes

        nodes = list_video_nodes(timeout_s=float(args.get("timeout", 2.0)))
        found = find_gamescope_node(nodes)
        return {"nodes": nodes, "gamescope": found, "selected": self.ctx.capture.pipewire_node}

    def _cmd_set_node(self, args: dict) -> dict:
        node = args.get("node")
        if not isinstance(node, str):
            raise CommandError("set_node needs a 'node' (name, serial or id)")
        node = node.strip() or "gamescope"
        self._node_override = node
        self.ctx.capture.update_pipewire_node(node)
        if args.get("save", True):
            self.settings.capture.pipewire_node = node
            self.settings.save()
        return {"node": node}

    def _cmd_shutdown(self, args: dict) -> dict:
        GLib.idle_add(lambda: self.loop.quit() or False)
        return {"shutting_down": True}


def _parse_args(argv):
    from lumux.capture import BACKENDS

    parser = argparse.ArgumentParser(
        prog="lumux-daemon",
        description="Headless Lumux (Hue screen sync) controlled over a Unix socket.",
    )
    parser.add_argument("--socket", default=None, help="control socket path "
                        f"(default: {default_socket_path()})")
    parser.add_argument("--backend", choices=BACKENDS, default=None,
                        help="capture backend for this run (default: from settings)")
    parser.add_argument("--node", default=None,
                        help="PipeWire node (name, object.serial or id) for the "
                        "gamescope backend (default: 'gamescope')")
    parser.add_argument("--config-dir", default=None,
                        help="directory holding settings.json (default: the "
                        "desktop app's)")
    parser.add_argument("--autostart", action="store_true",
                        help="start syncing right away")
    parser.add_argument("--ctl", metavar="CMD", default=None,
                        help="send CMD to a running daemon and print the reply, "
                        "e.g. --ctl status")
    parser.add_argument("--arg", action="append", default=[], metavar="KEY=VALUE",
                        help="argument for --ctl (JSON values allowed)")
    parser.add_argument("--version", action="version", version=f"lumux-daemon {__version__}")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    sock = args.socket or default_socket_path()

    if args.ctl:
        kwargs = {}
        for item in args.arg:
            key, _, raw = item.partition("=")
            try:
                kwargs[key] = json.loads(raw)
            except json.JSONDecodeError:
                kwargs[key] = raw
        try:
            result = request(sock, args.ctl, timeout=70.0, **kwargs)
        except (OSError, CommandError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        return 0

    if args.config_dir:
        os.environ["LUMUX_CONFIG_DIR"] = args.config_dir
    # Unbuffered-ish logs: the Decky plugin tails them from a file
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    daemon = LumuxDaemon(socket_path=sock, backend=args.backend, node=args.node)
    return daemon.serve(autostart=args.autostart)


if __name__ == "__main__":
    sys.exit(main())
