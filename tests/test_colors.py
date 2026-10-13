import colorsys

from lumux.colors import ColorAnalyzer
from lumux.config.settings_manager import SettingsManager


def _hsv(rgb):
    return colorsys.rgb_to_hsv(*(c / 255.0 for c in rgb))


def test_saturation_zero_leaves_colours_as_captured():
    assert ColorAnalyzer(saturation=0.0)._apply_saturation((150, 120, 110)) == (150, 120, 110)


def test_saturation_full_is_the_most_vivid_colour():
    """100% = fully saturated: same hue and brightness, no white mixed in."""
    washed_out = (150, 120, 110)  # typical averaged screen edge
    vivid = ColorAnalyzer(saturation=1.0)._apply_saturation(washed_out)
    h0, s0, v0 = _hsv(washed_out)
    h1, s1, v1 = _hsv(vivid)
    assert s1 > 0.99 and min(vivid) == 0
    assert abs(h1 - h0) < 0.01 and abs(v1 - v0) < 0.01  # hue + brightness kept


def test_saturation_scales_between_as_captured_and_full():
    s = [_hsv(ColorAnalyzer(saturation=p)._apply_saturation((150, 120, 110)))[1]
         for p in (0.0, 0.5, 1.0)]
    assert s[0] < s[1] < s[2]


def test_saturation_keeps_neutrals_calm():
    analyzer = ColorAnalyzer(saturation=1.0)
    assert analyzer._apply_saturation((128, 128, 128)) == (128, 128, 128)
    # a barely tinted grey isn't forced into a strong colour
    assert _hsv(analyzer._apply_saturation((130, 128, 127)))[1] < 0.5


def test_brightness_full_reaches_hue_maximum():
    """100% (the maximum) drives the light to Hue's maximum 254 for any
    zone at least half-bright; lower settings scale down proportionally."""
    analyzer = ColorAnalyzer(brightness_scale=1.0)
    assert analyzer._calculate_brightness((128, 64, 0)) == 254
    assert analyzer._calculate_brightness((255, 255, 255)) == 254
    assert analyzer._calculate_brightness((64, 32, 0)) == 127
    assert ColorAnalyzer(brightness_scale=0.5)._calculate_brightness((255, 0, 0)) == 127
    # values above 100% from older settings don't go beyond the maximum curve
    assert ColorAnalyzer(brightness_scale=2.0)._calculate_brightness((64, 32, 0)) == 127


def test_old_settings_above_100_percent_are_capped(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("LUMUX_CONFIG_DIR", str(tmp_path))
    (tmp_path / "settings.json").write_text(
        json.dumps({"sync": {"brightness_scale": 2.0, "saturation": 3.0}}))
    SettingsManager._instance = None
    try:
        sync = SettingsManager.get_instance().sync
        assert sync.brightness_scale == 1.0 and sync.saturation == 1.0
    finally:
        SettingsManager._instance = None
