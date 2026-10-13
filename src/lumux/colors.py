"""Color analysis and conversion for Hue sync."""

import colorsys
from typing import Dict, Tuple, Optional

from lumux.utils.rgb_xy_converter import rgb_to_xy


class ColorAnalyzer:
    # Averaged screen edges are rarely near full white, so a 1:1 mapping left
    # the lights dim. With this gain, brightness 100% (the slider maximum)
    # drives a light to Hue's maximum (254) for any zone that is at least
    # half-bright; lower settings scale that down proportionally.
    BASE_BRIGHTNESS_GAIN = 2.0
    # Below this HSV saturation a colour counts as neutral (white/grey/black
    # UI); it is not forced towards a hue, which would make lights flicker
    # between random colours on near-grey content.
    NEUTRAL_SATURATION = 0.15

    def __init__(
        self, brightness_scale: float = 1.0, gamma: float = 1.0, saturation: float = 1.0
    ):
        # 0.0-1.0; 1.0 = Hue maximum
        self.brightness_scale = brightness_scale
        self.gamma = gamma
        # 0.0-1.0 vividness: 0 = colours as on screen, 1 = fully saturated
        # (the most vivid colour the lights can show)
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
        corrected_rgb = self._apply_saturation(self._apply_gamma(rgb))
        r, g, b = corrected_rgb
        xy = rgb_to_xy(r, g, b, light_info=light_info)

        brightness = self._calculate_brightness(corrected_rgb)

        return (xy, brightness)

    def _calculate_brightness(self, rgb: Tuple[int, int, int]) -> int:
        """Calculate brightness (0-254) from RGB.

        Uses max RGB component to ensure uniform brightness across all colors.
        This prevents blue and purple from appearing dimmer than other colors,
        since the luminance formula heavily weights green (71.5%) while blue
        only contributes 7.2%.
        """
        r, g, b = rgb
        # Use max component for consistent brightness across all hues
        max_component = max(r, g, b)
        # Content brightness first (capped at Hue's maximum), *then* the
        # user's setting: scaling before the cap meant 50% still gave full
        # brightness on bright scenes, so the slider seemed to do nothing.
        content = min(254.0, (max_component / 255.0) * 254.0 * self.BASE_BRIGHTNESS_GAIN)
        scale = max(0.0, min(1.0, self.brightness_scale))
        return max(1, min(254, int(content * scale)))

    def _apply_saturation(self, rgb: Tuple[int, int, int]) -> Tuple[int, int, int]:
        """Make colours more vivid, keeping their hue and brightness.

        Works in HSV: saturation is moved towards 1.0 by ``self.saturation``
        (0 = unchanged, 1 = fully saturated, i.e. the purest colour the
        lights can show). Value (brightness) is untouched. Near-neutral
        colours get proportionally less push so grey/white stays calm.
        """
        p = max(0.0, min(1.0, self.saturation))
        r, g, b = rgb
        if p <= 0.0 or r == g == b:
            return rgb
        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        weight = min(1.0, s / self.NEUTRAL_SATURATION)
        s = s + (1.0 - s) * p * weight
        r2, g2, b2 = colorsys.hsv_to_rgb(h, s, v)
        return (int(round(r2 * 255)), int(round(g2 * 255)), int(round(b2 * 255)))

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
