import json

from lumux.colors import HUE_GAMUT_C, WHITE_POINT, ColorAnalyzer, _ray_to_gamut_edge
from lumux.config.settings_manager import SettingsManager
from lumux.utils.rgb_xy_converter import rgb_to_xy


def _chroma(xy):
    """0 = white, 1 = on the edge of Hue's gamut, in that hue."""
    d = (xy[0] - WHITE_POINT[0], xy[1] - WHITE_POINT[1])
    return 1.0 / _ray_to_gamut_edge(d)


def _hue_dir(xy):
    d = (xy[0] - WHITE_POINT[0], xy[1] - WHITE_POINT[1])
    n = (d[0] ** 2 + d[1] ** 2) ** 0.5
    return (d[0] / n, d[1] / n)


WASHED_OUT = (150, 120, 110)  # typical averaged screen edge


def test_averaged_screen_colours_sit_well_inside_hue_gamut():
    """Averaged screen edges are pale, so the boost has to do the work."""
    assert _chroma(rgb_to_xy(*WASHED_OUT)) < 0.3


def test_saturation_zero_keeps_screen_colour():
    xy, _ = ColorAnalyzer(saturation=0.0).analyze_zone(WASHED_OUT)
    assert xy == rgb_to_xy(*WASHED_OUT)


def test_saturation_full_reaches_hue_gamut_edge_in_the_same_hue():
    for rgb in (WASHED_OUT, (0, 255, 0), (40, 60, 200), (255, 0, 0)):
        xy, _ = ColorAnalyzer(saturation=1.0).analyze_zone(rgb)
        assert abs(_chroma(xy) - 1.0) < 1e-6, rgb
        a, b = _hue_dir(xy), _hue_dir(rgb_to_xy(*rgb))
        assert a[0] * b[0] + a[1] * b[1] > 0.9999, rgb  # same hue


def test_saturation_is_monotonic():
    c = [_chroma(ColorAnalyzer(saturation=p).analyze_zone(WASHED_OUT)[0])
         for p in (0.0, 0.5, 1.0)]
    assert c[0] < c[1] < c[2]


def test_neutrals_stay_calm():
    grey, _ = ColorAnalyzer(saturation=1.0).analyze_zone((128, 128, 128))
    assert _chroma(grey) < 0.01
    tinted, _ = ColorAnalyzer(saturation=1.0).analyze_zone((130, 128, 127))
    assert _chroma(tinted) < 0.5


def test_brightness_100_is_the_lights_maximum_whatever_the_screen():
    """The slider is the lights' brightness: the screen only picks the colour."""
    out = ColorAnalyzer(brightness_scale=1.0).analyze_zones_batch(
        {"bright": (255, 200, 0), "dim": (64, 32, 0), "dark": (25, 10, 5)})
    assert out["bright"][1] == out["dim"][1] == out["dark"][1] == 254


def test_brightness_setting_tunes_the_lights_down():
    for scale, expected in ((0.5, 127), (0.25, 63)):
        out = ColorAnalyzer(brightness_scale=scale).analyze_zones_batch({"a": (64, 32, 0)})
        assert out["a"][1] == expected


def test_black_fades_out_instead_of_turning_white():
    out = ColorAnalyzer().analyze_zones_batch(
        {"black": (0, 0, 0), "near": (4, 2, 0), "dark_game": (12, 6, 2)})
    assert out["black"][1] == 1
    assert 100 < out["near"][1] < 160  # ~1.6% level, half of the black threshold
    assert out["dark_game"][1] == 254  # dark but coloured (~5%): full brightness


def test_smoothing_reaches_the_target_brightness():
    """int() truncation used to stall a few points below the target."""
    analyzer = ColorAnalyzer()
    analyzer.previous_colors = {"z": ((0.3, 0.3), 150)}
    for _ in range(40):
        out = analyzer.apply_smoothing({"z": ((0.3, 0.3), 254)}, factor=0.3)
    assert out["z"][1] == 254


def test_old_settings_above_100_percent_are_capped(tmp_path, monkeypatch):
    monkeypatch.setenv("LUMUX_CONFIG_DIR", str(tmp_path))
    (tmp_path / "settings.json").write_text(
        json.dumps({"sync": {"brightness_scale": 2.0, "saturation": 3.0}}))
    SettingsManager._instance = None
    try:
        sync = SettingsManager.get_instance().sync
        assert sync.brightness_scale == 1.0 and sync.saturation == 1.0
    finally:
        SettingsManager._instance = None


def test_gamut_constant_is_hue_gamut_c():
    assert HUE_GAMUT_C[1] == (0.17, 0.7)
