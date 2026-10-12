"""The headless core must not need GTK/Adw, and must work without numpy."""

import os
import subprocess
import sys
import textwrap

import pytest

np = pytest.importorskip("numpy")


def _run(script):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    return subprocess.run([sys.executable, "-c", textwrap.dedent(script)], env=env,
                          capture_output=True, text=True, timeout=120)


def test_import_lumux_is_lazy():
    out = _run("""
        import sys, lumux
        assert "gi.repository.Gst" not in sys.modules, "import lumux pulled in GStreamer"
        assert "numpy" not in sys.modules
        assert lumux.ZoneProcessor.__name__ == "ZoneProcessor"  # lazy export works
        print("OK")
    """)
    assert "OK" in out.stdout, out.stdout + out.stderr


def test_daemon_never_imports_gtk(tmp_path):
    out = _run(f"""
        import os, sys
        os.environ["LUMUX_CONFIG_DIR"] = {str(tmp_path)!r}
        from lumux.daemon import LumuxDaemon
        d = LumuxDaemon(socket_path={str(tmp_path / "s.sock")!r}, backend="gamescope")
        d.dispatch("status", {{}})
        bad = [m for m in ("gi.repository.Gtk", "gi.repository.Adw", "gi.repository.Gdk",
                           "lumux.gui") if m in sys.modules]
        assert not bad, bad
        print("OK")
    """)
    assert "OK" in out.stdout, out.stdout + out.stderr


def test_raw_frame_zones_match_numpy():
    from lumux.frame import RawFrame
    from lumux.zones import ZoneProcessor

    rng = np.random.default_rng(1)
    img = rng.integers(0, 256, size=(100, 160, 3), dtype=np.uint8)
    raw = RawFrame(160, 100, img.tobytes())
    zp = ZoneProcessor(rows=6, cols=10)
    assert zp.process_image(raw) == zp.process_image(img)


def test_raw_rgb_frame_strips_row_padding():
    from lumux.capture import ScreenCapture

    width, height, stride = 3, 2, 12  # 9 bytes of pixels + 3 padding per row
    data = bytes(range(9)) + b"\xff" * 3 + bytes(range(10, 19)) + b"\xff" * 3
    frame = ScreenCapture._raw_rgb_frame(data, width, height, stride)
    assert frame.data == bytes(range(9)) + bytes(range(10, 19))
    assert frame.pixel(2, 1) == (16, 17, 18)
