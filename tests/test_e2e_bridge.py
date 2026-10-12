"""End-to-end tests against a fake Hue bridge (see fake_bridge.py)."""

import time

import pytest

import fake_bridge as fb
from fake_bridge import FakeBridge

pytestmark = pytest.mark.skipif(not fb.can_run(), reason="needs root + openssl (run in container)")


@pytest.fixture
def bridge_srv():
    srv = FakeBridge().start()
    time.sleep(0.5)
    yield srv
    srv.stop()


@pytest.fixture
def hue(bridge_srv):
    from lumux.hue_bridge import HueBridge

    return HueBridge("127.0.0.1", fb.APP_KEY)


def _stream():
    from lumux.entertainment import EntertainmentStream

    return EntertainmentStream("127.0.0.1", fb.APP_KEY, fb.CLIENT_KEY, fb.ENT_ID)


def test_stream_keeps_session_alive_without_frames(bridge_srv, hue):
    """The bridge drops sessions after ~10s of silence; we must keep sending
    even when capture hasn't produced a frame yet (e.g. consent dialog open)."""
    stream = _stream()
    assert stream.connect(hue), stream.last_error
    try:
        assert stream.is_connected()
        time.sleep(1.5)
        assert bridge_srv.frames >= 30, bridge_srv.frames  # ~30 Hz keep-alive
    finally:
        stream.disconnect(hue)
    assert bridge_srv.stream_actions() == ["start", "stop"]


def test_stream_reconnects_after_bridge_drops_session(bridge_srv, hue):
    stream = _stream()
    assert stream.connect(hue), stream.last_error
    try:
        time.sleep(0.5)
        bridge_srv.stop_dtls()  # bridge goes away
        time.sleep(1.0)
        assert not stream.is_connected()
        bridge_srv.start_dtls()
        before = bridge_srv.frames
        deadline = time.time() + 10
        while time.time() < deadline and not stream.is_connected():
            time.sleep(0.2)
        assert stream.is_connected(), "did not reconnect"
        time.sleep(0.5)
        assert bridge_srv.frames > before
        # each reconnect re-activates the area on the bridge
        assert bridge_srv.stream_actions().count("start") >= 2
    finally:
        stream.disconnect(hue)


def test_bad_client_key_fails_explicitly(bridge_srv, hue):
    from lumux.entertainment import EntertainmentStream

    stream = EntertainmentStream("127.0.0.1", fb.APP_KEY, "ff" * 16, fb.ENT_ID)
    stream._handshake_timeout = 2
    assert not stream.connect(hue)
    assert "DTLS" in stream.last_error
    # the area is released again so the bridge isn't left in streaming mode
    assert bridge_srv.stream_actions() == ["start", "stop"]


def test_reading_mode_only_touches_entertainment_area_lights(bridge_srv, hue):
    from lumux.reading_mode import ReadingModeController

    reading = ReadingModeController(hue, fb.ENT_ID)
    assert reading.activate(xy=(0.5, 0.4), brightness=120)
    assert sorted(bridge_srv.light_puts()) == ["L1", "L2"]  # never L3/L4
    bridge_srv.puts.clear()
    reading.update_color((0.3, 0.3), 80)
    assert sorted(bridge_srv.light_puts()) == ["L1", "L2"]
    bridge_srv.puts.clear()
    reading.deactivate(turn_off=True)
    assert sorted(bridge_srv.light_puts()) == ["L1", "L2"]


def test_reading_mode_without_area_controls_nothing(bridge_srv, hue):
    from lumux.reading_mode import ReadingModeController

    reading = ReadingModeController(hue, "")
    assert not reading.activate(xy=(0.5, 0.4), brightness=120)
    assert bridge_srv.light_puts() == []


def test_sync_controller_streams_zone_colours(bridge_srv, hue):
    import numpy as np

    from lumux.colors import ColorAnalyzer
    from lumux.config.settings_manager import SyncSettings
    from lumux.sync import SyncController
    from lumux.zones import ZoneProcessor

    class FakeCapture:
        def __init__(self):
            self.calls = 0
            frame = np.zeros((90, 160, 3), dtype=np.uint8)
            frame[:, :80] = (255, 0, 0)  # left half red
            frame[:, 80:] = (0, 0, 255)  # right half blue
            self.frame = frame

        def is_capturing(self):
            return self.calls > 0

        def poll(self):
            return False  # static screen: no new frames after the first

        def capture(self):
            self.calls += 1
            return self.frame

        def cancel_pending(self):
            pass

        def stop_pipeline(self):
            pass

    stream = _stream()
    assert stream.connect(hue), stream.last_error
    cap = FakeCapture()
    sync = SyncController(hue, cap, ZoneProcessor(rows=4, cols=4), ColorAnalyzer(), None,
                          SyncSettings(), entertainment_stream=stream)
    try:
        sync.start()
        time.sleep(1.0)
        assert cap.calls == 1, "static frame should be analysed once, not every tick"
        msg = stream._pending_message
        # channel 0 (left) is red-ish, channel 1 (right) blue-ish, both lit
        import struct
        ch0 = struct.unpack_from(">BHHH", msg, 52)
        ch1 = struct.unpack_from(">BHHH", msg, 59)
        assert ch0[0] == 0 and ch0[3] > 0 and ch0[1] > ch0[2]  # red: x > y
        assert ch1[0] == 1 and ch1[3] > 0 and ch1[2] < 0.2 * 65535  # blue: low y
    finally:
        sync.stop()
        stream.disconnect(hue)
