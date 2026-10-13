"""Color analysis and conversion for Hue sync."""

from typing import Dict, Tuple, Optional

from lumux.utils.rgb_xy_converter import rgb_to_xy


# Hue "gamut C" (all current colour bulbs and strips): the most saturated
# colours the lights can make. Screen RGB only spans the much smaller sRGB
# triangle inside it, which is why "fully saturated" screen colours still
# looked pale next to the Hue app.
HUE_GAMUT_C = ((0.6915, 0.3083), (0.17, 0.7), (0.1532, 0.0475))
WHITE_POINT = (0.3127, 0.3290)  # D65


def _ray_to_gamut_edge(d: Tuple[float, float], gamut=HUE_GAMUT_C) -> float:
    """t such that WHITE_POINT + t*d lies on the gamut triangle's edge."""
    wx, wy = WHITE_POINT
    best = float("inf")
    for i in range(3):
        (ax, ay), (bx, by) = gamut[i], gamut[(i + 1) % 3]
        ex, ey = bx - ax, by - ay
        denom = d[0] * ey - d[1] * ex
        if abs(denom) < 1e-12:
            continue
        t = ((ax - wx) * ey - (ay - wy) * ex) / denom
        u = ((ax - wx) * d[1] - (ay - wy) * d[0]) / denom
        if t > 0 and -1e-9 <= u <= 1 + 1e-9:
            best = min(best, t)
    return best


class ColorAnalyzer:
    # Below this fraction of the way from white to the gamut edge a colour
    # counts as neutral (white/grey UI). It is pushed proportionally less,
    # so near-grey content doesn't make the lights flicker between hues.
    NEUTRAL_CHROMA = 0.15
    # Zones darker than this (black bars, loading screens) fade towards off
    # instead of turning into full-brightness white.
    BLACK_LEVEL = 0.08

    def __init__(
        self, brightness_scale: float = 1.0, gamma: float = 1.0, saturation: float = 1.0
    ):
        # 0.0-1.0 of the lights' own brightness: 1.0 = Hue's maximum. The
        # screen picks the colour; this sets how bright the lights are.
        self.brightness_scale = brightness_scale
        self.gamma = gamma
        # 0.0-1.0: 0 = colours as on screen, 1 = the most saturated colour
        # Hue lights can make in that hue (the edge of their gamut)
        self.saturation = saturation
        self.previous_colors: Dict[str, Tuple[Tuple[float, float], int]] = {}

    def analyze_zone(
        self, rgb: Tuple[int, int, int], light_info: Optional[dict] = None
    ) -> Tuple[Tuple[float, float], int]:
        """Calculate Hue color for a zone.

        Args:
            rgb: RGB tuple (0-255)
            light_info: Optional light metadata for gamut correction

        Returns:
            Tuple of ((x, y), brightness)
        """
        corrected_rgb = self._apply_gamma(rgb)
        r, g, b = corrected_rgb
        xy = self._saturate_xy(rgb_to_xy(r, g, b, light_info=light_info))
        brightness = self._calculate_brightness(max(corrected_rgb) / 255.0)
        return (xy, brightness)

    def _calculate_brightness(self, level: float) -> int:
        """Brightness (0-254) for a zone at ``level`` (max RGB component, 0-1).

        The brightness setting is the lights' brightness: 100% = Hue's
        maximum for every zone with any colour, and lower settings tune it
        down. Only near-black zones (below BLACK_LEVEL) fade towards off, so
        black screens don't become bright white light.
        """
        scale = max(0.0, min(1.0, self.brightness_scale))
        fade = min(1.0, level / self.BLACK_LEVEL)
        return max(1, min(254, int(254.0 * scale * fade)))

    def _saturate_xy(self, xy: Tuple[float, float]) -> Tuple[float, float]:
        """Push a colour from white towards Hue's gamut edge, keeping its hue.

        Works in CIE xy, i.e. in the lights' own colour space: 100% is the
        most saturated colour Hue can make in that hue (what the edge of the
        Hue app's colour wheel gives), not just a saturated screen colour.
        """
        p = max(0.0, min(1.0, self.saturation))
        d = (xy[0] - WHITE_POINT[0], xy[1] - WHITE_POINT[1])
        if p <= 0.0 or (abs(d[0]) < 1e-6 and abs(d[1]) < 1e-6):
            return xy
        t_edge = _ray_to_gamut_edge(d)
        if t_edge == float("inf"):
            return xy
        chroma = min(1.0, 1.0 / t_edge)  # 1.0 = on the gamut edge
        weight = min(1.0, chroma / self.NEUTRAL_CHROMA)
        target = chroma + (1.0 - chroma) * p * weight
        f = target * t_edge
        return (WHITE_POINT[0] + d[0] * f, WHITE_POINT[1] + d[1] * f)

    def _apply_gamma(self, rgb: Tuple[int, int, int]) -> Tuple[int, int, int]:
        """Apply gamma correction to RGB values."""
        gamma = self.gamma if self.gamma > 0 else 1.0
        corrected = []
        for channel in rgb:
            normalized = max(0.0, min(1.0, channel / 255.0))
            adjusted = normalized**gamma
            corrected.append(int(round(adjusted * 255.0)))
        return tuple(corrected)  # type: ignore[return-value]

    def apply_smoothing(
        self, current: Dict[str, Tuple[Tuple[float, float], int]], factor: float = 0.3
    ) -> Dict[str, Tuple[Tuple[float, float], int]]:
        """Smooth color transitions between updates.

        Uses exponential moving average:
        smoothed = previous + factor * (current - previous)

        Args:
            current: Current zone colors
            factor: Smoothing factor (0-1), higher = faster changes

        Returns:
            Smoothed zone colors
        """
        smoothed = {}

        # Return early if current is empty to avoid overwriting previous_colors
        if not current:
            return smoothed

        for zone_id, curr_value in current.items():
            if zone_id in self.previous_colors:
                curr_xy, curr_bri = curr_value
                prev_xy, prev_bri = self.previous_colors[zone_id]

                smooth_xy = (
                    prev_xy[0] + factor * (curr_xy[0] - prev_xy[0]),
                    prev_xy[1] + factor * (curr_xy[1] - prev_xy[1]),
                )
                smooth_bri = int(prev_bri + factor * (curr_bri - prev_bri))

                smoothed[zone_id] = (smooth_xy, smooth_bri)
            else:
                smoothed[zone_id] = curr_value

        self.previous_colors = smoothed.copy()
        return smoothed

    def analyze_zones_batch(
        self,
        zone_colors: Dict[str, Tuple[int, int, int]],
        light_info_map: Optional[Dict[str, dict]] = None,
    ) -> Dict[str, Tuple[Tuple[float, float], int]]:
        """Analyze multiple zones at once.

        Args:
            zone_colors: Dictionary mapping zone IDs to RGB tuples
            light_info_map: Optional mapping of zones to light info

        Returns:
            Dictionary mapping zone IDs to ((x, y), brightness)
        """
        hue_colors = {}

        for zone_id, rgb in zone_colors.items():
            light_info = None
            if light_info_map and zone_id in light_info_map:
                light_info = light_info_map[zone_id]

            hue_colors[zone_id] = self.analyze_zone(rgb, light_info)

        return hue_colors
