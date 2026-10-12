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
