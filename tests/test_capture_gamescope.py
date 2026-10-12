"""gamescope capture backend (SteamOS Game Mode) against a fake gamescope node."""

import os
import subprocess
import sys
import textwrap

import pytest

import fake_gamescope as fg


# -- backend selection / pipeline strings (no PipeWire needed) ---------------

def test_auto_backend_picks_gamescope_in_game_mode(monkeypatch):
    from lumux.capture import ScreenCapture

    monkeypatch.delenv("GAMESCOPE_WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "gamescope")
    assert ScreenCapture()._choose_backend() == "gamescope"

    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    monkeypatch.setenv("GAMESCOPE_WAYLAND_DISPLAY", "gamescope-0")
    assert ScreenCapture()._choose_backend() == "gamescope"


def test_explicit_backend_wins_over_session(monkeypatch):
    from lumux.capture import ScreenCapture

    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "gamescope")
    assert ScreenCapture(backend="portal")._choose_backend() == "portal"
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    monkeypatch.delenv("GAMESCOPE_WAYLAND_DISPLAY", raising=False)
    assert ScreenCapture(backend="gamescope")._choose_backend() == "gamescope"


def test_gamescope_pipeline_targets_node_and_scales_before_convert():
    from lumux.capture import ScreenCapture

    cap = ScreenCapture(backend="gamescope", pipewire_node="gamescope", scale_factor=0.125)
    cap._active_backend = "gamescope"
    src = cap._gamescope_source()
    assert src.startswith("pipewiresrc target-object=gamescope")

    # Before the first frame the size is unknown: full-size probe only
    configs = cap._get_pipeline_configs(src)
    assert len(configs) == 1 and "videoscale" not in configs[0]

    cap._source_width, cap._source_height = 1280, 800
    configs = cap._get_pipeline_configs(src)
    first = configs[0]
    assert "videoscale ! video/x-raw,width=160,height=100 ! videoconvert" in first
    assert first.index("videoscale") < first.index("videoconvert")
    assert "glupload" not in " ".join(configs)  # frames come as SHM


def test_gamescope_node_is_sanitised():
    from lumux.capture import ScreenCapture

    cap = ScreenCapture(backend="gamescope", pipewire_node="x ! filesink location=/tmp/pwn")
    assert cap._gamescope_source().startswith("pipewiresrc target-object=gamescope ")
    cap.update_pipewire_node("57")
    assert "57" in cap._gamescope_source()


def test_config_key_ignores_node():
    from lumux.capture import ScreenCapture

    a = ScreenCapture._config_key("pipewiresrc target-object=gamescope do-timestamp=true ! x")
    b = ScreenCapture._config_key("pipewiresrc target-object=123 do-timestamp=true ! x")
    assert a == b


def test_find_gamescope_node():
    from lumux.capture import find_gamescope_node

    nodes = [
        {"name": "v4l2_input.cam", "description": "Webcam"},
        {"name": "gamescope", "description": "gamescope", "serial": "42"},
    ]
    assert find_gamescope_node(nodes)["serial"] == "42"
    renamed = [{"name": "stream.123", "description": "Gamescope output", "serial": "7"}]
    assert find_gamescope_node(renamed)["serial"] == "7"
    assert find_gamescope_node([{"name": "cam", "description": "Webcam"}]) is None


def test_settings_accept_gamescope_backend(tmp_path, monkeypatch):
    import json

    from lumux.config.settings_manager import SettingsManager

    (tmp_path / "settings.json").write_text(json.dumps(
        {"capture": {"backend": "gamescope", "pipewire_node": "  "}}))
    monkeypatch.setenv("LUMUX_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(SettingsManager, "_instance", None)
    try:
        s = SettingsManager.get_instance()
        assert s.capture.backend == "gamescope"
        assert s.capture.pipewire_node == "gamescope"  # blank -> default
    finally:
        SettingsManager._instance = None


# -- real PipeWire session ---------------------------------------------------

needs_pipewire = pytest.mark.skipif(not fg.can_run(), reason="needs pipewire + wireplumber (container)")


@pytest.fixture
def pw():
    session = fg.PipeWireSession().start()
    yield session
    session.stop()


def _capture_frame(cap, attempts=10):
    frame = None
    for _ in range(attempts):  # probe frame first, then the scaled pipeline
        frame = cap.capture()
        if frame is not None and cap._pipeline_scaled:
            break
    return frame


@needs_pipewire
def test_gamescope_capture_returns_scaled_frame(pw):
    from lumux.capture import ScreenCapture

    pw.add_fake_gamescope(rgb=(255, 128, 0))
    cap = ScreenCapture(backend="gamescope", scale_factor=0.125)
    try:
        frame = _capture_frame(cap)
        assert frame is not None, cap.last_error
        assert cap._active_backend == "gamescope" and cap._pipeline_scaled
        assert frame.shape == (100, 160, 3)
        r, g, b = (int(v) for v in frame[50, 80])
        assert r > 230 and 100 < g < 160 and b < 30, (r, g, b)
    finally:
        cap.stop_pipeline()


@needs_pipewire
def test_list_video_nodes_finds_gamescope(pw):
    from lumux.capture import find_gamescope_node, list_video_nodes

    pw.add_fake_gamescope()
    nodes = list_video_nodes(timeout_s=5)
    found = find_gamescope_node(nodes)
    assert found is not None, nodes
    assert found["name"] == "gamescope" and found["serial"]


@needs_pipewire
def test_gamescope_capture_by_serial(pw):
    from lumux.capture import ScreenCapture, list_video_nodes

    pw.add_fake_gamescope(rgb=(0, 0, 255), name="gamescope-renamed")
    serial = next(n["serial"] for n in list_video_nodes(timeout_s=5)
                  if n["name"] == "gamescope-renamed")
    cap = ScreenCapture(backend="gamescope", pipewire_node=serial, scale_factor=0.125)
    try:
        frame = _capture_frame(cap)
        assert frame is not None, cap.last_error
        r, g, b = (int(v) for v in frame[50, 80])
        assert b > 230 and r < 30, (r, g, b)
    finally:
        cap.stop_pipeline()


@needs_pipewire
def test_gamescope_capture_works_without_numpy(pw):
    """The daemon may run on a host Python with only PyGObject + GStreamer."""
    pw.add_fake_gamescope(rgb=(0, 255, 0))
    script = textwrap.dedent("""
        import sys
        class Block:
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] in ("numpy", "PIL"):
                    raise ImportError("blocked for test: " + name)
        sys.meta_path.insert(0, Block())
        from lumux.capture import ScreenCapture
        from lumux.frame import RawFrame
        from lumux.zones import ZoneProcessor
        cap = ScreenCapture(backend="gamescope", scale_factor=0.125)
        frame = None
        for _ in range(10):
            frame = cap.capture()
            if frame is not None and cap._pipeline_scaled:
                break
        cap.stop_pipeline()
        assert isinstance(frame, RawFrame), (frame, cap.last_error)
        assert (frame.width, frame.height) == (160, 100)
        zones = ZoneProcessor(rows=4, cols=4).process_image(frame)
        r, g, b = zones["top_0"]
        assert g > 230 and r < 30 and b < 30, zones["top_0"]
        assert "numpy" not in sys.modules
        print("OK")
    """)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    out = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0 and "OK" in out.stdout, out.stdout + out.stderr
