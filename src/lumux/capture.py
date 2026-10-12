"""Screen capture via the xdg-desktop-portal (Wayland) or X11, using GStreamer.

Backends:

* **portal** - org.freedesktop.portal.ScreenCast + pipewiresrc. Works on
  Wayland compositors (GNOME, Plasma Wayland, ...).
* **x11** - ximagesrc. Needed on X11 sessions such as SteamOS Desktop Mode,
  where the KDE portal does not offer ScreenCast at all, so the portal path
  never produced a single frame.

Frames are *pulled* from the appsink by the sync thread at the sync rate
rather than pushed through a Python callback on every compositor frame
(60-144 Hz), and the appsink only keeps the newest frame.
"""

import itertools
import os
import re
import threading
import time
from typing import Optional, List, Set, TYPE_CHECKING

import numpy as np

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gio, GLib, Gst, GstApp, GstVideo

from lumux.black_bar_detector import BlackBarDetector, CropRegion

if TYPE_CHECKING:
    from lumux.config.settings_manager import BlackBarSettings

Gst.init(None)

_request_counter = itertools.count(1)


class _PortalCancelled(Exception):
    pass


class ScreenCapture:
    # After a failed/denied portal request, wait before asking again so a
    # cancelled dialog isn't immediately re-opened every frame.
    PORTAL_RETRY_DELAY = 10.0

    def __init__(
        self,
        scale_factor: float = 0.125,
        black_bar_settings: Optional["BlackBarSettings"] = None,
        source_type: str = "screen",
        restore_token: str = "",
        backend: str = "auto",
        max_fps: int = 30,
    ):
        self.scale_factor = scale_factor
        self.source_type = source_type
        self.backend = backend
        self.max_fps = max_fps
        self.last_error: str = ""

        self._portal_node_id: Optional[int] = None
        self._portal_session_handle: Optional[str] = None
        self._portal_bus = None
        # PipeWire connection handed out by the portal (OpenPipeWireRemote).
        # Sandboxed (Flatpak) apps can only see the granted screen-cast node
        # through this fd, not through the plain PipeWire socket.
        self._pipewire_fd: Optional[int] = None
        self._pipeline_started_at = 0.0
        self._no_frames_reported = False
        self._portal_cancel = threading.Event()
        self._portal_retry_at = 0.0
        self._portal_unsupported = False
        # Portal restore token for persistent screen-cast consent. When set,
        # the portal can skip the permission dialog on subsequent sessions.
        self._restore_token: str = restore_token or ""
        self._on_restore_token_changed = None
        if self._restore_token:
            print(
                f"[capture] Loaded cached screen-share consent token "
                f"({len(self._restore_token)} chars) — should skip prompt"
            )

        self._pipeline: Optional[Gst.Pipeline] = None
        self._appsink: Optional[GstApp.AppSink] = None
        self._latest_frame: Optional[np.ndarray] = None
        self._frame_seq = 0
        self._pipeline_running = False
        self._pipeline_error_logged = False
        self._runtime_failed_configs: Set[str] = set()
        self._active_config_key: Optional[str] = None
        self._active_backend: Optional[str] = None

        self._source_width: Optional[int] = None
        self._source_height: Optional[int] = None
        self._pipeline_scaled = False
        self._needs_pipeline_restart = False

        self._black_bar_detector: Optional[BlackBarDetector] = None
        if black_bar_settings is not None:
            self._init_black_bar_detector(black_bar_settings)

    # ------------------------------------------------------------------
    # Public state

    @property
    def frame_seq(self) -> int:
        """Incremented each time a new frame is received."""
        return self._frame_seq

    def is_capturing(self) -> bool:
        return self._pipeline_running

    @property
    def restore_token(self) -> str:
        """Current portal restore token (for diagnostics)."""
        return self._restore_token

    def set_on_restore_token_changed(self, callback) -> None:
        """Register a callback invoked when the portal issues/refreshes a
        restore token. Signature: callback(token: str). Use it to persist the
        token so subsequent runs can skip the permission dialog.
        """
        self._on_restore_token_changed = callback

    # ------------------------------------------------------------------
    # Settings

    def _compute_scaled_dimensions(self):
        if self._source_width and self._source_height and self.scale_factor < 1.0:
            # Round down to a multiple of 4 so RGB row strides stay 4-byte
            # aligned (avoids padded strides) and odd-width caps that some
            # converters refuse to link.
            w = max(4, int(self._source_width * self.scale_factor) & ~3)
            h = max(4, int(self._source_height * self.scale_factor) & ~3)
            return w, h
        return None, None

    def update_scale_factor(self, new_scale: float) -> None:
        new_scale = max(0.01, min(1.0, new_scale))
        changed = abs(new_scale - self.scale_factor) > 0.001
        self.scale_factor = new_scale
        if changed and self._pipeline_running:
            target_w, target_h = self._compute_scaled_dimensions()
            scaled_changed = target_w != getattr(
                self, "_scaled_w", None
            ) or target_h != getattr(self, "_scaled_h", None)
            if scaled_changed:
                # Applied by the sync thread on its next capture() call
                self._needs_pipeline_restart = True

    def update_backend(self, backend: str, max_fps: int) -> None:
        changed = backend != self.backend or (
            self._active_backend == "x11" and max_fps != self.max_fps
        )
        self.backend = backend
        self.max_fps = max_fps
        if changed and self._pipeline_running:
            self._needs_pipeline_restart = True

    def _init_black_bar_detector(self, settings: "BlackBarSettings") -> None:
        self._black_bar_detector = BlackBarDetector(
            enabled=settings.enabled,
            threshold=settings.threshold,
            detection_rate=settings.detection_rate,
            smooth_factor=settings.smooth_factor,
        )

    def update_black_bar_settings(self, settings: "BlackBarSettings") -> None:
        if self._black_bar_detector is None:
            self._init_black_bar_detector(settings)
        else:
            self._black_bar_detector.set_enabled(settings.enabled)
            self._black_bar_detector.set_threshold(settings.threshold)
            self._black_bar_detector.set_detection_rate(settings.detection_rate)
            self._black_bar_detector.smooth_factor = settings.smooth_factor

    def get_black_bar_crop_region(self):
        if self._black_bar_detector is None:
            return None
        return self._black_bar_detector.get_crop_region()

    # ------------------------------------------------------------------
    # Backend selection

    @staticmethod
    def _is_x11_session() -> bool:
        session = os.environ.get("XDG_SESSION_TYPE", "").lower()
        if session == "x11":
            return True
        if session == "wayland":
            return False
        return bool(os.environ.get("DISPLAY")) and not os.environ.get("WAYLAND_DISPLAY")

    @staticmethod
    def _x11_available() -> bool:
        return bool(os.environ.get("DISPLAY")) and (
            Gst.ElementFactory.find("ximagesrc") is not None
        )

    def _choose_backend(self) -> str:
        if self.backend in ("portal", "x11"):
            return self.backend
        if (self._is_x11_session() or self._portal_unsupported) and self._x11_available():
            return "x11"
        return "portal"

    # ------------------------------------------------------------------
    # Frame acquisition (called from the sync thread)

    def poll(self) -> bool:
        """Fetch the newest frame if one arrived. Returns True if new."""
        if not self._pipeline_running:
            return False
        self._drain_bus()
        if not self._pipeline_running:
            return False
        return self._pull_sample(0)

    def capture(self) -> Optional[np.ndarray]:
        if self._needs_pipeline_restart and self._pipeline_running:
            self._restart_pipeline()
            if not self._pipeline_running:
                return None

        if self._pipeline_running:
            self.poll()
            if self._latest_frame is not None:
                return self._process_image(self._latest_frame)

        if not self._pipeline_running and not self._ensure_source():
            return None

        if not self._pipeline_running:
            if not self._start_pipeline():
                return None

        # Wait for the first frame (a static screen may only send one)
        deadline = time.monotonic() + 2.0
        while self._pipeline_running and time.monotonic() < deadline:
            self._drain_bus()
            if self._pull_sample(0.1) or self._latest_frame is not None:
                self._no_frames_reported = False
                return self._process_image(self._latest_frame)

        if (
            self._pipeline_running
            and self._latest_frame is None
            and time.monotonic() - self._pipeline_started_at > 5.0
        ):
            # Used to fail silently: lights stayed dark with no explanation
            self.last_error = (
                "Screen capture started but no frames are arriving "
                f"({self._active_backend} backend)"
            )
            if not self._no_frames_reported:
                self._no_frames_reported = True
                print(f"[capture] {self.last_error}")
                self._log_pipeline_details()
        return None

    def _ensure_source(self) -> bool:
        """Make sure we have something to capture from."""
        backend = self._choose_backend()
        if backend == "x11":
            if not os.environ.get("DISPLAY"):
                self.last_error = (
                    "X11 capture needs an X11 session; this is Wayland, so set "
                    "Capture Method to Automatic"
                )
                return False
            if not self._x11_available():
                self.last_error = "X11 capture unavailable: GStreamer ximagesrc plugin missing"
                return False
            self._active_backend = "x11"
            return True

        self._active_backend = "portal"
        if self._portal_node_id:
            return True
        if time.monotonic() < self._portal_retry_at:
            return False
        ok = self._setup_portal_session()
        if not ok and self._restore_token and not self._portal_cancel.is_set():
            # A stale restore token (e.g. after a monitor/source change)
            # can make session setup fail silently. Retry once without it
            # so the portal shows a fresh permission dialog.
            print(
                "[capture] Cached consent token was rejected by portal "
                "(stale or revoked) — clearing cache and re-prompting"
            )
            self._set_restore_token("")
            ok = self._setup_portal_session()
        if not ok:
            self._portal_retry_at = time.monotonic() + self.PORTAL_RETRY_DELAY
            # Portal has no ScreenCast (e.g. KDE on X11): fall back to X11
            if self._portal_unsupported and self.backend == "auto" and self._x11_available():
                print("[capture] Portal capture unavailable; falling back to X11 capture")
                self._active_backend = "x11"
                return True
        return ok

    def _pull_sample(self, timeout_s: float) -> bool:
        appsink = self._appsink
        if appsink is None:
            return False
        sample = appsink.try_pull_sample(int(timeout_s * Gst.SECOND))
        if sample is None:
            return False
        frame = self._sample_to_frame(sample)
        if frame is None:
            return False
        self._latest_frame = frame
        self._frame_seq += 1
        return True

    def _process_image(self, screen: np.ndarray) -> np.ndarray:
        if screen is None or screen.size == 0:
            return screen

        if self._black_bar_detector is not None:
            try:
                crop_region = self._black_bar_detector.process(screen)
                if crop_region is not None and crop_region.is_valid(
                    screen.shape[1], screen.shape[0]
                ):
                    screen = screen[
                        crop_region.top : crop_region.bottom,
                        crop_region.left : crop_region.right,
                        :,
                    ]
            except Exception as e:
                print(f"Black bar detection error: {e}")

        if screen is None or screen.size == 0:
            return screen

        if not self._pipeline_scaled and self.scale_factor < 1.0:
            import PIL.Image as Image

            new_h = max(1, int(screen.shape[0] * self.scale_factor))
            new_w = max(1, int(screen.shape[1] * self.scale_factor))
            if not screen.flags["C_CONTIGUOUS"]:
                screen = np.ascontiguousarray(screen)
            screen = np.array(
                Image.fromarray(screen).resize(
                    (new_w, new_h), Image.Resampling.BILINEAR
                )
            )
        return screen

    # ------------------------------------------------------------------
    # Portal session

    def _set_restore_token(self, token: str) -> None:
        if token == self._restore_token:
            return
        self._restore_token = token
        if self._on_restore_token_changed:
            try:
                self._on_restore_token_changed(token)
            except Exception as e:
                print(f"Error persisting restore token: {e}")

    def _portal_request(self, ctx, bus, call, options: dict, timeout_s: float) -> dict:
        """Invoke a portal method and wait for its Request::Response.

        Subscribes *before* calling (using a predictable handle_token) so a
        fast response can't be missed, and waits on a private main context
        so this worker thread never iterates GTK's default context.
        """
        token = f"lumux{os.getpid()}_{next(_request_counter)}"
        options["handle_token"] = GLib.Variant("s", token)
        sender = bus.con.get_unique_name().lstrip(":").replace(".", "_")
        path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"

        loop = GLib.MainLoop(ctx)
        state = {"code": None, "results": {}}

        def on_response(_conn, _sender, _obj, _iface, _signal, params):
            code, results = params.unpack()
            state["code"] = code
            state["results"] = results
            loop.quit()

        sub = bus.con.signal_subscribe(
            None,
            "org.freedesktop.portal.Request",
            "Response",
            path,
            None,
            0,
            on_response,
        )
        deadline = time.monotonic() + timeout_s

        def tick():
            if self._portal_cancel.is_set() or time.monotonic() > deadline:
                loop.quit()
                return False
            return True

        timer = GLib.timeout_source_new(100)
        timer.set_callback(tick)
        timer.attach(ctx)
        try:
            call(options)
            if state["code"] is None:
                loop.run()
        finally:
            timer.destroy()
            bus.con.signal_unsubscribe(sub)

        if self._portal_cancel.is_set():
            raise _PortalCancelled()
        if state["code"] is None:
            raise TimeoutError("portal did not respond")
        if state["code"] != 0:
            raise PermissionError(
                "screen sharing was cancelled" if state["code"] == 1 else
                f"portal request failed (code {state['code']})"
            )
        return state["results"]

    def _setup_portal_session(self) -> bool:
        self._portal_cancel.clear()
        ctx = GLib.MainContext.new()
        ctx.push_thread_default()
        try:
            import pydbus

            kind = "window" if self.source_type == "window" else "screen"
            print(f"Requesting {kind} capture permission via portal...")
            bus = pydbus.SessionBus()
            self._portal_bus = bus
            portal = bus.get(
                "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop"
            )
            try:
                screencast = portal["org.freedesktop.portal.ScreenCast"]
            except KeyError:
                self._portal_unsupported = True
                self.last_error = (
                    "Desktop portal has no ScreenCast support "
                    "(X11 session?) — use the X11 capture method"
                )
                print(f"[capture] {self.last_error}")
                return False

            token = str(int(time.time()))
            create_options = {
                "session_handle_token": GLib.Variant("s", "s" + token),
                # persist_mode = 2: persist consent until explicitly revoked,
                # so the portal can issue a restore_token for future sessions.
                "persist_mode": GLib.Variant("u", 2),
            }
            # Only send a restore_token when we actually have one; sending an
            # empty string can confuse some portal backends.
            if self._restore_token:
                create_options["restore_token"] = GLib.Variant(
                    "s", self._restore_token
                )

            results = self._portal_request(
                ctx, bus, lambda o: screencast.CreateSession(o), create_options, 30
            )
            session_handle = results.get("session_handle")
            if not session_handle:
                return False
            self._portal_session_handle = session_handle

            portal_types = 1 if self.source_type == "screen" else 2
            select_options = {
                "types": GLib.Variant("u", portal_types),
                "multiple": GLib.Variant("b", False),
                "persist_mode": GLib.Variant("u", 2),
            }
            if self._restore_token:
                select_options["restore_token"] = GLib.Variant("s", self._restore_token)
            self._portal_request(
                ctx,
                bus,
                lambda o: screencast.SelectSources(session_handle, o),
                select_options,
                30,
            )

            # Start shows the consent dialog; give the user time to answer
            results = self._portal_request(
                ctx, bus, lambda o: screencast.Start(session_handle, "", o), {}, 180
            )
            streams = results.get("streams") or []
            if not streams:
                return False

            self._portal_node_id = streams[0][0]
            self._pipewire_fd = self._open_pipewire_remote(bus, session_handle)
            self.last_error = ""
            print(
                f"Portal session started. PipeWire node: {self._portal_node_id}"
                + (f", remote fd {self._pipewire_fd}" if self._pipewire_fd is not None else "")
            )
            new_token = results.get("restore_token") or ""
            if not new_token:
                print(
                    "[capture] Portal did NOT issue a consent token — "
                    "the prompt will recur each sync"
                )
            self._set_restore_token(new_token)
            return True

        except _PortalCancelled:
            print("[capture] Portal request cancelled")
        except Exception as e:
            self.last_error = f"Screen capture setup failed: {e}"
            print(f"Failed to setup portal session: {e}")
        finally:
            ctx.pop_thread_default()

        self._close_portal_session()
        return False

    @staticmethod
    def _open_pipewire_remote(bus, session_handle: str) -> Optional[int]:
        """ScreenCast.OpenPipeWireRemote: returns a PipeWire socket fd that
        can see the granted stream. pydbus can't receive fds, so use Gio."""
        try:
            result, fd_list = bus.con.call_with_unix_fd_list_sync(
                "org.freedesktop.portal.Desktop",
                "/org/freedesktop/portal/desktop",
                "org.freedesktop.portal.ScreenCast",
                "OpenPipeWireRemote",
                GLib.Variant("(oa{sv})", (session_handle, {})),
                GLib.VariantType.new("(h)"),
                Gio.DBusCallFlags.NONE,
                -1,
                None,
                None,
            )
            (index,) = result.unpack()
            return fd_list.get(index)
        except Exception as e:
            print(f"[capture] OpenPipeWireRemote failed, using default PipeWire socket: {e}")
            return None

    def _close_portal_session(self):
        if self._portal_session_handle and self._portal_bus:
            try:
                session = self._portal_bus.get(
                    "org.freedesktop.portal.Desktop",
                    self._portal_session_handle,
                )
                session.Close()
                print("Portal session closed")
            except Exception as e:
                print(f"Error closing portal session (may already be closed): {e}")
        self._portal_session_handle = None
        self._portal_bus = None
        if self._pipewire_fd is not None:
            try:
                os.close(self._pipewire_fd)
            except OSError:
                pass
            self._pipewire_fd = None

    # ------------------------------------------------------------------
    # GStreamer pipeline

    def _source_element(self) -> Optional[str]:
        if self._active_backend == "x11":
            fps = max(1, min(60, int(self.max_fps or 30)))
            # framerate caps make ximagesrc grab only as often as we sync
            return (
                "ximagesrc use-damage=false show-pointer=false ! "
                f"video/x-raw,framerate={fps}/1"
            )
        if self._portal_node_id:
            # pipewiresrc dups the fd, so it stays valid across pipeline restarts
            fd = f"fd={self._pipewire_fd} " if self._pipewire_fd is not None else ""
            return f"pipewiresrc {fd}path={self._portal_node_id} do-timestamp=true"
        return None

    def _get_pipeline_configs(self, src: str) -> List[str]:
        configs = []

        target_w, target_h = self._compute_scaled_dimensions()

        has_glupload = Gst.ElementFactory.find("glupload") is not None
        has_glcolorconvert = Gst.ElementFactory.find("glcolorconvert") is not None
        has_gldownload = Gst.ElementFactory.find("gldownload") is not None
        has_glcolorscale = Gst.ElementFactory.find("glcolorscale") is not None
        has_videoscale = Gst.ElementFactory.find("videoscale") is not None
        has_v4l2convert = Gst.ElementFactory.find("v4l2convert") is not None
        has_videoconvert = Gst.ElementFactory.find("videoconvert") is not None

        # No signals: the sync thread pulls the newest frame itself
        sink_props = "name=sink emit-signals=false drop=true max-buffers=1 sync=false"
        use_gl = self._active_backend != "x11"  # X11 frames are already in RAM

        if target_w and target_h:
            scaled_caps = f"video/x-raw,format=RGB,width={target_w},height={target_h}"

            if self._active_backend == "x11" and has_videoscale and has_videoconvert:
                # Scale first so colour conversion runs on the small frame
                configs.append(
                    f"{src} ! videoscale ! videoconvert ! {scaled_caps} ! "
                    f"appsink {sink_props}"
                )

            if use_gl and has_glupload and has_glcolorconvert and has_gldownload:
                if has_glcolorscale:
                    configs.append(
                        f"{src} ! "
                        f"glupload ! glcolorconvert ! glcolorscale ! "
                        f"gldownload ! videoscale ! {scaled_caps} ! "
                        f"appsink {sink_props}"
                    )
                if has_videoscale:
                    configs.append(
                        f"{src} ! "
                        f"glupload ! glcolorconvert ! gldownload ! videoscale ! "
                        f"{scaled_caps} ! appsink {sink_props}"
                    )

            if use_gl and has_v4l2convert and has_videoscale:
                configs.append(
                    f"{src} ! v4l2convert ! videoscale ! {scaled_caps} ! "
                    f"appsink {sink_props}"
                )

            if has_videoconvert and has_videoscale:
                configs.append(
                    f"{src} ! videoconvert ! videoscale ! {scaled_caps} ! "
                    f"appsink {sink_props}"
                )

        if use_gl and has_glupload and has_glcolorconvert and has_gldownload:
            configs.append(
                f"{src} ! "
                f"glupload ! glcolorconvert ! gldownload ! "
                f"video/x-raw,format=RGB ! "
                f"appsink {sink_props}"
            )

        if use_gl and has_glupload and has_glcolorscale and has_gldownload:
            configs.append(
                f"{src} ! "
                f"glupload ! glcolorscale ! gldownload ! "
                f"video/x-raw,format=RGB ! "
                f"appsink {sink_props}"
            )

        if use_gl and has_v4l2convert:
            configs.append(
                f"{src} ! v4l2convert ! video/x-raw,format=RGB ! appsink {sink_props}"
            )

        if has_videoconvert:
            configs.append(
                f"{src} ! videoconvert ! video/x-raw,format=RGB ! appsink {sink_props}"
            )

        return configs

    @staticmethod
    def _config_key(pipeline_str: str) -> str:
        """Identify a pipeline config independent of node id and dimensions,
        so runtime failures can be remembered across restarts."""
        key = re.sub(r"path=\d+", "path=*", pipeline_str)
        key = re.sub(r"width=\d+", "width=*", key)
        key = re.sub(r"height=\d+", "height=*", key)
        key = re.sub(r"framerate=\d+/1", "framerate=*", key)
        return key

    @staticmethod
    def _describe(pipeline_str: str) -> str:
        parts = [p.strip().split(" ")[0] for p in pipeline_str.split(" ! ")]
        return " ! ".join(p for p in parts if not p.startswith("video/"))

    def _start_pipeline(self) -> bool:
        src = self._source_element()
        if not src:
            return False

        if self._pipeline:
            self._stop_gst_pipeline()

        configs = self._get_pipeline_configs(src)
        if not configs:
            print("No suitable GStreamer pipeline configuration found")
            print("Need one of: glupload+glcolorconvert, v4l2convert, or videoconvert")
            self._log_pipeline_details()
            return False

        target_w, target_h = self._compute_scaled_dimensions()

        for i, pipeline_str in enumerate(configs):
            config_key = self._config_key(pipeline_str)
            if config_key in self._runtime_failed_configs:
                continue
            try:
                self._pipeline = Gst.parse_launch(pipeline_str)
                self._appsink = self._pipeline.get_by_name("sink")

                ret = self._pipeline.set_state(Gst.State.PLAYING)
                if ret == Gst.StateChangeReturn.FAILURE:
                    print(f"Pipeline config {i + 1} failed ({self._describe(pipeline_str)})")
                    self._pipeline.set_state(Gst.State.NULL)
                    self._pipeline = None
                    self._appsink = None
                    continue

                uses_scaled_caps = (
                    target_w is not None and f"width={target_w}" in pipeline_str
                )
                self._pipeline_scaled = uses_scaled_caps
                self._scaled_w = target_w
                self._scaled_h = target_h

                self._pipeline_running = True
                self._pipeline_started_at = time.monotonic()
                self._pipeline_error_logged = False
                self._active_config_key = config_key
                scale_info = (
                    f", scaled to {target_w}x{target_h}" if uses_scaled_caps else ""
                )
                print(
                    f"GStreamer capture pipeline started "
                    f"({self._describe(pipeline_str)}{scale_info})"
                )
                return True

            except Exception as e:
                print(f"Pipeline config {i + 1} exception: {e}")
                if self._pipeline:
                    self._pipeline.set_state(Gst.State.NULL)
                    self._pipeline = None
                    self._appsink = None
                continue

        self._pipeline_scaled = False
        self.last_error = "All capture pipeline configurations failed"
        print("All pipeline configurations failed")
        self._log_pipeline_details()
        return False

    def _drain_bus(self) -> None:
        """Handle pipeline errors/warnings on the sync thread (no main-loop
        signal watch, so GStreamer state is only touched from one thread)."""
        pipeline = self._pipeline
        if pipeline is None:
            return
        bus = pipeline.get_bus()
        mask = Gst.MessageType.ERROR | Gst.MessageType.WARNING | Gst.MessageType.ELEMENT
        while True:
            message = bus.pop_filtered(mask)
            if message is None:
                return
            if message.type == Gst.MessageType.ERROR:
                err, debug = message.parse_error()
                if not self._pipeline_error_logged:
                    print(f"GStreamer pipeline error: {err.message}")
                    print(f"GStreamer debug info: {debug}")
                    self._pipeline_error_logged = True
                if self._active_config_key:
                    self._runtime_failed_configs.add(self._active_config_key)
                    print(
                        "Marking pipeline config as failed; "
                        "will fall back to next converter on restart"
                    )
                self._stop_gst_pipeline()
                return
            if message.type == Gst.MessageType.WARNING:
                warn, _debug = message.parse_warning()
                print(f"GStreamer pipeline warning: {warn.message}")
            else:
                structure = message.get_structure()
                if structure and structure.get_name() == "missing-plugin":
                    print(f"Missing GStreamer plugin: {structure.to_string()}")

    def _stop_gst_pipeline(self) -> None:
        if self._pipeline:
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
            self._appsink = None
        self._active_config_key = None
        self._pipeline_running = False
        self._latest_frame = None

    def _restart_pipeline(self) -> None:
        self._needs_pipeline_restart = False
        self._stop_gst_pipeline()
        if self._active_backend != self._choose_backend():
            # Capture method changed in settings: re-acquire the source
            self.stop_pipeline()
            return
        self._start_pipeline()

    def _log_pipeline_details(self):
        try:
            print(f"Capture backend: {self._active_backend}, PipeWire node: {self._portal_node_id}")
            plugins = {
                name: Gst.ElementFactory.find(name) is not None
                for name in (
                    "pipewiresrc",
                    "ximagesrc",
                    "videoconvert",
                    "videoscale",
                    "v4l2convert",
                    "glupload",
                    "glcolorconvert",
                    "gldownload",
                    "glcolorscale",
                )
            }
            print(f"GStreamer plugins: {plugins}")
        except Exception as e:
            print(f"Error logging pipeline details: {e}")

    # ------------------------------------------------------------------
    # Sample conversion

    @staticmethod
    def _extract_pixel_rows(
        data, width: int, height: int, bpp: int, stride: Optional[int]
    ) -> np.ndarray:
        """Return a (height, width*bpp) uint8 array, stripping row padding.

        GStreamer may pad each row to an alignment boundary (e.g. 4 bytes),
        so the buffer stride can exceed width*bpp. The real stride comes from
        GstVideoMeta when available, otherwise it is inferred from the buffer
        size. The result may be a view of ``data``; callers copy it.
        """
        arr = np.frombuffer(data, dtype=np.uint8)
        row_bytes = width * bpp
        if not stride or stride < row_bytes:
            if arr.size == height * row_bytes:
                stride = row_bytes
            else:
                stride = arr.size // height if height else row_bytes
        if stride < row_bytes or arr.size < stride * (height - 1) + row_bytes:
            raise ValueError(
                f"Buffer too small: {arr.size} bytes for "
                f"{width}x{height} with bpp={bpp} (stride={stride})"
            )

        if stride == row_bytes:
            return arr[: height * row_bytes].reshape(height, row_bytes)

        # Padded rows: strided view (handles a possibly unpadded final row)
        return np.lib.stride_tricks.as_strided(
            arr, shape=(height, row_bytes), strides=(stride, 1)
        )

    def _sample_to_frame(self, sample) -> Optional[np.ndarray]:
        try:
            buffer = sample.get_buffer()
            caps = sample.get_caps()

            struct = caps.get_structure(0)
            width = struct.get_value("width")
            height = struct.get_value("height")
            fmt = struct.get_value("format") if struct.has_field("format") else None

            if not self._pipeline_scaled and (
                self._source_width != width or self._source_height != height
            ):
                self._source_width = width
                self._source_height = height
                if self.scale_factor < 1.0:
                    self._needs_pipeline_restart = True

            stride = None
            try:
                meta = GstVideo.buffer_get_video_meta(buffer)
                if meta and meta.n_planes >= 1:
                    stride = meta.stride[0]
            except Exception:
                stride = None

            success, map_info = buffer.map(Gst.MapFlags.READ)
            if not success:
                print(f"Failed to map buffer (format={fmt}, {width}x{height})")
                print("This likely means DMA-BUF buffers are being received.")
                print("Ensure GStreamer GL plugins (gst-plugins-gl) are installed.")
                return None

            try:
                if fmt in ("RGBA", "RGBx", "BGRA", "BGRx"):
                    bpp = 4
                elif fmt in ("BGR15", "RGB15"):
                    bpp = 2
                else:
                    bpp = 3

                rows = self._extract_pixel_rows(map_info.data, width, height, bpp, stride)

                # Every branch produces a fresh array: the mapped memory is
                # released right after this block.
                if fmt == "BGR":
                    return rows.reshape(height, width, 3)[:, :, ::-1].copy()
                if fmt in ("RGBA", "RGBx"):
                    return rows.reshape(height, width, 4)[:, :, :3].copy()
                if fmt in ("BGRA", "BGRx"):
                    return rows.reshape(height, width, 4)[:, :, 2::-1].copy()
                if fmt in ("BGR15", "RGB15"):
                    arr = np.ascontiguousarray(rows).view(np.uint16).reshape(
                        (height, width)
                    )
                    r = ((arr >> 10) & 0x1F).astype(np.uint8) * 255 // 31
                    g = ((arr >> 5) & 0x1F).astype(np.uint8) * 255 // 31
                    b = (arr & 0x1F).astype(np.uint8) * 255 // 31
                    return np.stack([r, g, b], axis=2)
                # RGB and unknown formats assumed 3 bytes per pixel
                return rows.reshape(height, width, 3).copy()
            finally:
                buffer.unmap(map_info)

        except Exception as e:
            print(f"Error processing frame: {e}")
            return None

    # ------------------------------------------------------------------
    # Teardown

    def cancel_pending(self) -> None:
        """Abort a portal dialog wait (thread-safe)."""
        self._portal_cancel.set()

    def stop_pipeline(self):
        self._stop_gst_pipeline()
        self._source_width = None
        self._source_height = None
        self._pipeline_scaled = False
        self._needs_pipeline_restart = False
        self._portal_node_id = None
        self._portal_retry_at = 0.0
        self._active_backend = None
        self._portal_unsupported = False
        self._runtime_failed_configs.clear()
        print("GStreamer pipeline stopped")
        self._close_portal_session()

    def __del__(self):
        try:
            self.stop_pipeline()
        except Exception:
            pass
