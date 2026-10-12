from lumux.colors import ColorAnalyzer
from lumux.config.settings_manager import SyncSettings
from lumux.sync import SyncController
from lumux.zones import ZoneProcessor


class _Stream:
    def __init__(self, positions):
        self._positions = positions
        self.sent = None
        self.channels = positions

    def is_active(self):
        return True

    def get_channel_positions(self):
        return self._positions

    def send_colors_xy(self, colors):
        self.sent = colors


def _controller(stream, rows=8, cols=8):
    return SyncController(None, None, ZoneProcessor(rows=rows, cols=cols), ColorAnalyzer(),
                          None, SyncSettings(), entertainment_stream=stream)


def test_every_side_zone_is_mapped():
    stream = _Stream({0: {"x": -1, "y": 0, "z": 1}, 1: {"x": -1, "y": 0, "z": -1}})
    sync = _controller(stream, rows=8)
    sync._build_zone_channel_mapping()
    for i in range(8):
        assert f"left_{i}" in sync._zone_channel_map
    # top of the left edge -> upper light, bottom -> lower light
    assert sync._zone_channel_map["left_0"] == 0
    assert sync._zone_channel_map["left_7"] == 1


def test_zones_sharing_a_channel_are_averaged_evenly():
    stream = _Stream({0: {"x": 0, "y": 0, "z": 1}})
    sync = _controller(stream, rows=2, cols=3)
    colors = {f"top_{i}": ((0.1 * (i + 1), 0.3), 30 * (i + 1)) for i in range(3)}
    sync._mapped_layout = None
    sync._last_sent = None
    sync._update_lights(colors)
    (x, y), bri = stream.sent[0]
    assert abs(x - 0.2) < 1e-9 and bri == 60
