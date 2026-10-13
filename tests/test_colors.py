from lumux.colors import ColorAnalyzer
from lumux.utils.rgb_xy_converter import rgb_to_xy

WHITE = rgb_to_xy(255, 255, 255)


def _dist(xy):
    return ((xy[0] - WHITE[0]) ** 2 + (xy[1] - WHITE[1]) ** 2) ** 0.5


def test_saturation_base_is_doubled():
    """100% saturation now means what 200% used to; 50% is "as captured"."""
    assert ColorAnalyzer(saturation=0.5)._apply_saturation((120, 80, 60)) == (120, 80, 60)
    base = ColorAnalyzer(saturation=1.0)
    old_200 = ColorAnalyzer(saturation=1.0)
    old_200.BASE_SATURATION_GAIN = 1.0
    old_200.saturation = 2.0
    assert base._apply_saturation((120, 80, 60)) == old_200._apply_saturation((120, 80, 60))


def test_saturation_boost_makes_colours_more_vivid():
    washed_out = (150, 120, 110)  # typical averaged screen edge
    plain, _ = ColorAnalyzer(saturation=0.5).analyze_zone(washed_out)  # as captured
    vivid, _ = ColorAnalyzer(saturation=1.0).analyze_zone(washed_out)
    assert _dist(vivid) > _dist(plain) * 1.5


def test_saturation_keeps_greys_grey_and_clamps():
    analyzer = ColorAnalyzer(saturation=1.5)
    assert analyzer._apply_saturation((128, 128, 128)) == (128, 128, 128)
    assert analyzer._apply_saturation((250, 10, 10)) == (255, 0, 0)


def test_zero_saturation_is_greyscale():
    r, g, b = ColorAnalyzer(saturation=0.0)._apply_saturation((200, 50, 50))
    assert r == g == b


def test_brightness_base_is_doubled():
    """100% brightness now means what 200% used to: half-intensity screen
    colour already drives the light fully."""
    analyzer = ColorAnalyzer(brightness_scale=1.0)
    assert analyzer._calculate_brightness((128, 64, 0)) == 254
    assert analyzer._calculate_brightness((64, 32, 0)) == 127
    assert ColorAnalyzer(brightness_scale=0.5)._calculate_brightness((64, 32, 0)) == 63
