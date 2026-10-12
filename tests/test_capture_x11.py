"""X11 capture path (SteamOS Desktop Mode has no portal ScreenCast)."""

import os
import shutil
import subprocess
import time

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DISPLAY") or not shutil.which("gst-launch-1.0"),
    reason="needs an X server (e.g. xvfb-run)",
)


def test_auto_backend_uses_x11_on_x11_session(monkeypatch):
    from lumux.capture import ScreenCapture

    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    cap = ScreenCapture(scale_factor=0.25)
    assert cap._choose_backend() == "x11"
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    assert cap._choose_backend() == "portal"


def test_x11_capture_returns_scaled_screen_colours(monkeypatch):
    from lumux.capture import ScreenCapture

    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    # Paint the screen orange with a real X window
    painter = subprocess.Popen(
        ["gst-launch-1.0", "-q", "videotestsrc", "pattern=solid-color",
         "foreground-color=0xffff8000", "!",
         "video/x-raw,width=1280,height=800,framerate=10/1", "!", "ximagesink"],
    )
    time.sleep(2)
    cap = ScreenCapture(scale_factor=0.25, backend="auto", max_fps=10)
    try:
        frame = None
        for _ in range(5):  # first frame is full size, then pipeline rescales
            frame = cap.capture()
            if frame is not None and cap._pipeline_scaled:
                break
        assert frame is not None, cap.last_error
        assert cap._active_backend == "x11"
        assert cap._pipeline_scaled
        h, w, c = frame.shape
        assert c == 3 and w <= 400 and h <= 300  # 1280x800 * 0.25 = 320x200
        r, g, b = frame[h // 2, w // 2]
        assert r > 200 and 100 < g < 160 and b < 40, (r, g, b)
    finally:
        cap.stop_pipeline()
        painter.kill()
