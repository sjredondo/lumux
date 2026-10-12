"""Lumux — Philips Hue Sync for Linux on Wayland.

Public API:
    LumuxApp — main application class (import from lumux.__main__)
    AppContext — dependency injection container
    HueBridge — bridge discovery and connection
    BridgeClient — Hue v2 REST API client
    ScreenCapture — screen capture via PipeWire portal, X11 or gamescope
    ZoneProcessor — ambilight zone processing
    SyncController — main sync loop controller
    ModeManager — Video/Reading mode switching
    EntertainmentStream — DTLS streaming to Hue Entertainment
    ReadingModeController — Reading mode static lighting
    BlackBarDetector — letterbox/pillarbox detection

The classes are imported lazily: ``import lumux`` (or the headless
``lumux.daemon``) must not drag in GStreamer, numpy or GTK until they are
actually used.
"""

import importlib

__version__ = "0.7.1"
__author__ = "Engin Kırmacı"

_EXPORTS = {
    "AppContext": "lumux.app_context",
    "HueBridge": "lumux.hue_bridge",
    "BridgeClient": "lumux.bridge_client",
    "ScreenCapture": "lumux.capture",
    "ZoneProcessor": "lumux.zones",
    "SyncController": "lumux.sync",
    "ModeManager": "lumux.mode_manager",
    "EntertainmentStream": "lumux.entertainment",
    "ReadingModeController": "lumux.reading_mode",
    "BlackBarDetector": "lumux.black_bar_detector",
    "ColorAnalyzer": "lumux.colors",
}

__all__ = list(_EXPORTS)


def __getattr__(name):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module 'lumux' has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value
