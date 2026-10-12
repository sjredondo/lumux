"""Zone processing for screen division."""

from __future__ import annotations

from typing import Dict, Optional, TYPE_CHECKING

try:
    import numpy as np
except ImportError:  # headless daemon on a minimal Python: see frame.py
    np = None

from lumux.frame import RawFrame

if TYPE_CHECKING:
    from lumux.config.settings_manager import ZoneSettings


class ZoneProcessor:
    def __init__(
        self, rows: int = 16, cols: int = 16, settings: Optional["ZoneSettings"] = None
    ):
        if settings is not None:
            self.rows = settings.rows
            self.cols = settings.cols
        else:
            self.rows = rows
            self.cols = cols
        self.zones: Dict[str, tuple[int, int, int]] = {}

    def _rebuild_zone_ids(self):
        self.zones = {}

    def process_image(self, image: np.ndarray) -> Dict[str, tuple[int, int, int]]:
        """Process image and return zone colors.

        Args:
            image: numpy array (H, W, 3), dtype uint8, or a RawFrame

        Returns:
            Dictionary mapping zone IDs to RGB tuples
        """
        if isinstance(image, RawFrame):
            return self._process_ambilight_raw(image)
        return self._process_ambilight(image)

    def _edge_layout(self, width: int, height: int):
        """Yield (zone_id, x1, y1, x2, y2) for every edge zone."""
        edge_width = max(min(width // self.cols, height // 8), 5)
        top_w = width // self.cols
        side_h = height // self.rows
        for i in range(self.cols):
            x1, x2 = i * top_w, min((i + 1) * top_w, width)
            yield f"top_{i}", x1, 0, x2, edge_width
        for i in range(self.cols):
            x1, x2 = i * top_w, min((i + 1) * top_w, width)
            yield f"bottom_{i}", x1, max(0, height - edge_width), x2, height
        for i in range(self.rows):
            y1, y2 = i * side_h, min((i + 1) * side_h, height)
            yield f"left_{i}", 0, y1, edge_width, y2
        for i in range(self.rows):
            y1, y2 = i * side_h, min((i + 1) * side_h, height)
            yield f"right_{i}", max(0, width - edge_width), y1, width, y2

    def _process_ambilight_raw(self, frame: RawFrame) -> Dict[str, tuple[int, int, int]]:
        """Same zones as _process_ambilight, in plain Python. Meant for the
        tiny frames GStreamer produces when numpy isn't installed."""
        if frame.width < 2 or frame.height < 2:
            return {}
        return {
            zone_id: frame.mean(x1, y1, x2, y2)
            for zone_id, x1, y1, x2, y2 in self._edge_layout(frame.width, frame.height)
        }

    def _process_ambilight(
        self, img_array: np.ndarray
    ) -> Dict[str, tuple[int, int, int]]:
        """Process only edge zones (top, bottom, left, right)."""
        try:
            if img_array is None or img_array.size == 0:
                return {}

            height, width = img_array.shape[0], img_array.shape[1]

            if len(img_array.shape) == 2:
                img_array = np.stack([img_array] * 3, axis=-1)
            elif img_array.shape[2] == 4:
                img_array = img_array[:, :, :3]

            if height < 2 or width < 2:
                return {}

            edge_width = min(width // self.cols, height // 8)
            edge_width = max(edge_width, 5)

            top_count = self.cols
            bottom_count = self.cols
            left_count = self.rows
            right_count = self.rows

            top_zone_width = width // top_count
            bottom_zone_width = width // bottom_count
            left_zone_height = height // left_count
            right_zone_height = height // right_count

            zones = {}

            for i in range(top_count):
                x1 = i * top_zone_width
                x2 = min((i + 1) * top_zone_width, width)
                avg_color = np.mean(img_array[0:edge_width, x1:x2], axis=(0, 1))
                zones[f"top_{i}"] = (
                    int(avg_color[0]),
                    int(avg_color[1]),
                    int(avg_color[2]),
                )

            for i in range(bottom_count):
                x1 = i * bottom_zone_width
                x2 = min((i + 1) * bottom_zone_width, width)
                y1 = max(0, height - edge_width)
                avg_color = np.mean(img_array[y1:height, x1:x2], axis=(0, 1))
                zones[f"bottom_{i}"] = (
                    int(avg_color[0]),
                    int(avg_color[1]),
                    int(avg_color[2]),
                )

            for i in range(left_count):
                y1 = i * left_zone_height
                y2 = min((i + 1) * left_zone_height, height)
                avg_color = np.mean(img_array[y1:y2, 0:edge_width], axis=(0, 1))
                zones[f"left_{i}"] = (
                    int(avg_color[0]),
                    int(avg_color[1]),
                    int(avg_color[2]),
                )

            for i in range(right_count):
                y1 = i * right_zone_height
                y2 = min((i + 1) * right_zone_height, height)
                x1 = max(0, width - edge_width)
                avg_color = np.mean(img_array[y1:y2, x1:width], axis=(0, 1))
                zones[f"right_{i}"] = (
                    int(avg_color[0]),
                    int(avg_color[1]),
                    int(avg_color[2]),
                )

            return zones
        except Exception as e:
            print(f"Error processing ambilight: {e}")
            return {}
