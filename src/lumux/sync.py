"""Main sync controller with threading."""

import queue
import threading
import time
from typing import Dict, Optional, Tuple, Callable

from lumux.utils.logging import timed_print
from lumux.hue_bridge import HueBridge
from lumux.capture import ScreenCapture
from lumux.zones import ZoneProcessor
from lumux.colors import ColorAnalyzer
from lumux.entertainment import EntertainmentStream
from lumux.config.zone_mapping import ZoneMapping


class SyncController:
    def __init__(
        self,
        bridge: HueBridge,
        capture: ScreenCapture,
        zone_processor: ZoneProcessor,
        color_analyzer: ColorAnalyzer,
        zone_mapping: ZoneMapping,
        settings,
        entertainment_stream: Optional[EntertainmentStream] = None,
    ):
        self.bridge = bridge
        self.capture = capture
        self.zone_processor = zone_processor
        self.color_analyzer = color_analyzer
        self.zone_mapping = zone_mapping
        self.settings = settings
        self.entertainment_stream = entertainment_stream

        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.queue: queue.Queue = queue.Queue(maxsize=100)
        self.lock = threading.Lock()

        # Zone to channel mapping for entertainment streaming
        self._zone_channel_map: Dict[str, int] = {}
        self._mapped_layout: Optional[tuple] = None
        self._last_hue_colors: Optional[Dict[str, Tuple[Tuple[float, float], int]]] = None
        self._last_sent: Optional[Dict[int, Tuple[Tuple[float, float], int]]] = None
        self._reported_capture_error = ""

        self._stats = {
            "fps": 0,
            "frame_count": 0,
            "errors": 0,
            "last_update": time.time(),
        }

        # Callback for when sync stops (used for auto-switching to reading mode)
        self._on_stop_callback: Optional[Callable] = None

    def set_on_stop_callback(self, callback: Callable):
        """Set callback to be called when sync stops."""
        self._on_stop_callback = callback

    def start(self):
        """Start sync thread."""
        if self.running:
            return

        if self.entertainment_stream is None:
            timed_print("Warning: No entertainment stream configured")
        self._zone_channel_map.clear()
        self._mapped_layout = None
        self._last_hue_colors = None
        self._last_sent = None
        self._reported_capture_error = ""
        self.color_analyzer.previous_colors = {}

        self.running = True
        self.thread = threading.Thread(
            target=self._sync_loop, daemon=True, name="SyncLoop"
        )
        self.thread.start()

    def _build_zone_channel_mapping(self):
        """Build mapping from screen zones to entertainment channels based on positions."""
        if not self.entertainment_stream:
            return

        self._zone_channel_map.clear()
        channel_positions = self.entertainment_stream.get_channel_positions()

        if not channel_positions:
            timed_print("Warning: No channel positions available")
            return

        # Get all zone IDs for ambilight layout
        # top_0..top_n, left_0..left_n, right_0..right_n, bottom_0..bottom_n
        # Must match ZoneProcessor: `cols` zones on top/bottom, `rows` zones on
        # left/right. (Using rows // 2 here left the lower half of both side
        # edges unmapped, so it never reached any light.)
        zones = []
        for edge in ["top", "bottom", "left", "right"]:
            if edge in ["top", "bottom"]:
                count = self.zone_processor.cols
            else:
                count = max(1, self.zone_processor.rows)
            for i in range(count):
                zones.append(f"{edge}_{i}")

        # Map each zone to nearest channel based on position
        for zone_id in zones:
            channel_id = self._find_best_channel_for_zone(zone_id, channel_positions)
            if channel_id is not None:
                self._zone_channel_map[zone_id] = channel_id

        timed_print(
            f"Zone-channel mapping: {len(self._zone_channel_map)} zones mapped to {len(set(self._zone_channel_map.values()))} channels"
        )

    def _find_best_channel_for_zone(
        self, zone_id: str, channel_positions: Dict[int, dict]
    ) -> Optional[int]:
        """Find the best matching channel for a screen zone based on position."""
        try:
            parts = zone_id.split("_")
            if len(parts) == 2:
                edge, idx_str = parts
                idx = int(idx_str)
            else:
                # Grid zone like "0_0"
                return list(channel_positions.keys())[0] if channel_positions else None
        except ValueError:
            return list(channel_positions.keys())[0] if channel_positions else None

        # Convert screen edge/index to expected position range
        # Entertainment positions: x is left(-1) to right(+1), z is bottom(-1) to top(+1)
        # Side zones go from top (idx=0) to bottom (idx=rows-1)
        side_z = (
            1.0 - (idx * 2.0 / (self.zone_processor.rows - 1))
            if self.zone_processor.rows > 1
            else 0
        )
        if edge == "left":
            target_x = -1.0
            target_z = side_z
        elif edge == "right":
            target_x = 1.0
            target_z = side_z
        elif edge == "top":
            target_z = 1.0
            target_x = (
                -1.0 + (idx * 2.0 / max(1, self.zone_processor.cols - 1))
                if self.zone_processor.cols > 1
                else 0
            )
        elif edge == "bottom":
            target_z = -1.0
            target_x = (
                -1.0 + (idx * 2.0 / max(1, self.zone_processor.cols - 1))
                if self.zone_processor.cols > 1
                else 0
            )
        else:
            return list(channel_positions.keys())[0] if channel_positions else None

        # Find closest channel
        best_channel = None
        best_distance = float("inf")

        for channel_id, pos in channel_positions.items():
            cx = pos.get("x", 0)
            cz = pos.get("z", 0)
            distance = (cx - target_x) ** 2 + (cz - target_z) ** 2
            if distance < best_distance:
                best_distance = distance
                best_channel = channel_id

        return best_channel

    def stop(self):
        """Stop sync thread."""
        if not self.running:
            return

        self.running = False
        # Unblock a pending screen-share dialog so the thread can exit
        if hasattr(self.capture, "cancel_pending"):
            self.capture.cancel_pending()

        if self.thread:
            self.thread.join(timeout=3)
            if self.thread.is_alive():
                timed_print("Warning: Sync thread did not stop cleanly")

        # Stop the capture pipeline to release portal session
        if hasattr(self.capture, "stop_pipeline"):
            self.capture.stop_pipeline()

        # Call stop callback if set (for auto-switching to reading mode)
        if self._on_stop_callback:
            try:
                self._on_stop_callback()
            except Exception as e:
                timed_print(f"Error in sync stop callback: {e}")

    def is_running(self) -> bool:
        """Check if sync is running."""
        return self.running

    def _sync_loop(self):
        """Main sync loop (runs in background thread)."""
        frame_times = []

        while self.running:
            try:
                start_time = time.time()

                self._process_frame()

                # Time spent processing the frame (capture + analyze + update)
                elapsed = time.time() - start_time

                # Enforce and clamp configured FPS to safe range (1-60)
                try:
                    fps_target = int(getattr(self.settings, "fps", 30))
                except Exception:
                    fps_target = 30

                fps_target = max(1, min(60, fps_target))
                target_delay = 1.0 / fps_target

                # Sleep the remaining time to meet target FPS
                delay = max(0, target_delay - elapsed)
                time.sleep(delay)

                # Measure full loop time including sleep to compute real FPS
                total_time = time.time() - start_time
                frame_times.append(total_time)

                if len(frame_times) > 30:
                    frame_times.pop(0)

                avg_frame_time = sum(frame_times) / len(frame_times)
                self._stats["fps"] = 1.0 / avg_frame_time if avg_frame_time > 0 else 0
                self._stats["frame_count"] += 1

            except KeyboardInterrupt:
                break
            except Exception as e:
                self._stats["errors"] += 1
                timed_print(f"Sync loop error: {e}")
                self._queue_status("error", str(e), None)
                time.sleep(1)

        self._queue_status("status", "stopped", None)

    def _process_frame(self):
        """Process a single frame.

        PipeWire only delivers frames when the screen changes, so on a static
        screen the same frame used to be re-cropped, re-zoned and re-analysed
        30 times a second. Now an unchanged frame reuses the previous zone
        analysis and only the (cheap) smoothing step runs.
        """
        t0 = time.time()

        # poll() pulls the newest frame, if any, without blocking
        reuse = (
            self._last_hue_colors is not None
            and self.capture.is_capturing()
            and not self.capture.poll()
        )

        t_capture = t_zones = t_analyze = 0.0
        zone_colors = None
        if reuse:
            hue_colors = self._last_hue_colors
        else:
            t_capture = time.time()
            screen = self.capture.capture()
            t_capture = time.time() - t_capture
            if screen is None:
                error = getattr(self.capture, "last_error", "")
                if error and error != self._reported_capture_error:
                    self._queue_status("warning", error, None)
                self._reported_capture_error = error
                return
            self._reported_capture_error = ""

            t_zones = time.time()
            zone_colors = self.zone_processor.process_image(screen)
            t_zones = time.time() - t_zones
            if not zone_colors:
                return

            t_analyze = time.time()
            hue_colors = self.color_analyzer.analyze_zones_batch(zone_colors)
            t_analyze = time.time() - t_analyze
            if not hue_colors:
                return
            self._last_hue_colors = hue_colors

        t_smooth = time.time()
        smoothed_colors = self.color_analyzer.apply_smoothing(
            hue_colors, factor=self.settings.smoothing_factor
        )
        t_smooth = time.time() - t_smooth

        t_update = time.time()
        self._update_lights(smoothed_colors)
        t_update = time.time() - t_update

        total = time.time() - t0

        # Record latest per-stage timings
        with self.lock:
            self._stats["last_stage_times"] = {
                "capture": round(t_capture, 4),
                "zones": round(t_zones, 4),
                "analyze": round(t_analyze, 4),
                "smooth": round(t_smooth, 4),
                "update": round(t_update, 4),
                "total": round(total, 4),
            }

        # Send RGB colors to GUI for preview (only when they changed)
        if zone_colors is not None:
            self._queue_status("status", "syncing", zone_colors)

    def _update_lights(self, hue_colors: Dict[str, Tuple[Tuple[float, float], int]]):
        """Hand colours to the entertainment stream's sender thread."""
        stream = self.entertainment_stream
        if not hue_colors or stream is None or not stream.is_active():
            return

        layout = (self.zone_processor.rows, self.zone_processor.cols, len(stream.channels))
        if layout != self._mapped_layout:
            self._build_zone_channel_mapping()
            self._mapped_layout = layout

        # Average every zone that maps to a channel (the old pairwise running
        # average weighted the last zone at 50%).
        sums: Dict[int, list] = {}
        for zone_id, ((x, y), brightness) in hue_colors.items():
            channel_id = self._zone_channel_map.get(zone_id)
            if channel_id is None:
                continue
            acc = sums.get(channel_id)
            if acc is None:
                sums[channel_id] = [x, y, brightness, 1]
            else:
                acc[0] += x
                acc[1] += y
                acc[2] += brightness
                acc[3] += 1

        channel_colors = {
            cid: ((sx / n, sy / n), int(sb / n)) for cid, (sx, sy, sb, n) in sums.items()
        }
        if channel_colors and channel_colors != self._last_sent:
            stream.send_colors_xy(channel_colors)
            self._last_sent = channel_colors

    def get_warning(self) -> str:
        """Current problem to show while syncing ("" if none)."""
        stream = self.entertainment_stream
        if self.running and stream is not None and stream.is_active() and not stream.is_connected():
            return stream.last_error or "Reconnecting to the Hue bridge..."
        return ""

    def _queue_status(self, status_type: str, message, data=None):
        """Queue status update for GUI thread."""
        try:
            self.queue.put_nowait((status_type, message, data))
        except queue.Full:
            pass

    def get_status(self) -> Optional[tuple]:
        """Get queued status update."""
        try:
            return self.queue.get_nowait()
        except queue.Empty:
            return None

    def get_stats(self) -> dict:
        """Get sync statistics."""
        with self.lock:
            return self._stats.copy()

    def reset_stats(self):
        """Reset sync statistics."""
        with self.lock:
            self._stats = {
                "fps": 0,
                "frame_count": 0,
                "errors": 0,
                "last_update": time.time(),
            }
