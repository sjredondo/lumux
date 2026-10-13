"""Portal capture must use the PipeWire fd from OpenPipeWireRemote.

Inside Flatpak, a plain pipewiresrc connection can't see the screen-cast
node, so capture silently produced no frames (lights stayed dark).
"""

import time


def test_pipewiresrc_uses_portal_fd():
    from lumux.capture import ScreenCapture

    cap = ScreenCapture()
    cap._active_backend = "portal"
    cap._portal_node_id = 42
    cap._pipewire_fd = 7
    assert cap._source_element().startswith("pipewiresrc fd=7 path=42 ")
    cap._pipewire_fd = None
    assert cap._source_element().startswith("pipewiresrc path=42 ")


def test_running_pipeline_without_frames_is_reported():
    from lumux.capture import ScreenCapture

    cap = ScreenCapture()
    cap._active_backend = "portal"
    cap._pipeline_running = True
    cap._pipeline_started_at = time.monotonic() - 10
    cap._appsink = None  # nothing ever arrives
    assert cap.capture() is None
    assert "no frames" in cap.last_error
    cap._pipeline_running = False


def test_portal_prefers_shared_memory_pipelines():
    """GPU (DMA-BUF/GL) import can silently give black frames, so plain
    shared-memory caps come first and GL is only a fallback."""
    from lumux.capture import ScreenCapture

    cap = ScreenCapture(scale_factor=0.125)
    cap._active_backend = "portal"
    src = "pipewiresrc path=42"
    first = cap._get_pipeline_configs(src)[0]
    assert first.startswith(f"{src} ! video/x-raw ! videoconvert ")
    cap._source_width, cap._source_height = 1280, 800
    first = cap._get_pipeline_configs(src)[0]
    assert first.startswith(f"{src} ! video/x-raw ! videoscale ! video/x-raw,width=160,height=100")
    assert "glupload" not in first


# -- real PipeWire session (same harness as the gamescope tests) ------------

import pytest  # noqa: E402

import fake_gamescope as fg  # noqa: E402

needs_pipewire = pytest.mark.skipif(not fg.can_run(), reason="needs pipewire + wireplumber (container)")


@pytest.fixture
def pw():
    session = fg.PipeWireSession().start()
    yield session
    session.stop()


def _portal_capture_on(node_name):
    from lumux.capture import ScreenCapture, list_video_nodes

    node = next(n for n in list_video_nodes() if n["name"] == node_name)
    cap = ScreenCapture(backend="portal", scale_factor=0.125)
    cap._portal_node_id = int(node["id"])  # what the portal's Start returns
    return cap


@needs_pipewire
def test_portal_capture_uses_shared_memory_and_gets_colours(pw):
    pw.add_fake_gamescope(rgb=(0, 128, 255), name="screen")
    cap = _portal_capture_on("screen")
    try:
        frame = None
        for _ in range(10):
            frame = cap.capture()
            if frame is not None and cap._pipeline_scaled:
                break
        assert frame is not None, cap.last_error
        assert "! video/x-raw ! videoscale" in cap._active_config_key
        r, g, b = (int(v) for v in frame[frame.shape[0] // 2, frame.shape[1] // 2])
        assert r < 30 and 100 < g < 160 and b > 230, (r, g, b)
    finally:
        cap.stop_pipeline()


@needs_pipewire
def test_black_only_pipeline_falls_back_to_next_method(pw, monkeypatch):
    from lumux.capture import ScreenCapture

    monkeypatch.setattr(ScreenCapture, "BLACK_FRAME_GRACE_S", 0.5)
    pw.add_fake_gamescope(rgb=(0, 0, 0), name="screen")
    cap = _portal_capture_on("screen")
    try:
        first_key = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            cap.poll()
            cap.capture()
            if first_key is None and cap._pipeline_scaled:
                first_key = cap._active_config_key
            if first_key and cap._runtime_failed_configs:
                break
            time.sleep(0.1)
        assert first_key in cap._runtime_failed_configs
        cap.capture()  # restart happens on the next capture
        assert cap._pipeline_running and cap._active_config_key != first_key
    finally:
        cap.stop_pipeline()
