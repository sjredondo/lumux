"""Minimal frame type for running without numpy.

The headless daemon may run on a stock Python with only PyGObject and
GStreamer. There GStreamer shrinks the capture to a tiny RGB frame (about
160x100) and the zone code averages it in plain Python.
"""


class RawFrame:
    """Packed RGB24 pixels, row-major, no padding."""

    __slots__ = ("width", "height", "data")

    def __init__(self, width: int, height: int, data: bytes):
        if len(data) != width * height * 3:
            raise ValueError(
                f"RawFrame needs {width * height * 3} bytes, got {len(data)}"
            )
        self.width = width
        self.height = height
        self.data = data

    @property
    def shape(self):
        return (self.height, self.width, 3)

    @property
    def size(self) -> int:
        return len(self.data)

    def pixel(self, x: int, y: int):
        i = (y * self.width + x) * 3
        return self.data[i], self.data[i + 1], self.data[i + 2]

    def mean(self, x1: int, y1: int, x2: int, y2: int):
        """Average RGB of the rectangle [x1, x2) x [y1, y2)."""
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(self.width, x2), min(self.height, y2)
        if x2 <= x1 or y2 <= y1:
            return (0, 0, 0)
        data = self.data
        stride = self.width * 3
        r = g = b = 0
        for y in range(y1, y2):
            row = data[y * stride + x1 * 3 : y * stride + x2 * 3]
            r += sum(row[0::3])
            g += sum(row[1::3])
            b += sum(row[2::3])
        n = (x2 - x1) * (y2 - y1)
        return (r // n, g // n, b // n)
