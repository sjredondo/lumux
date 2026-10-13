"""Shared logging utilities for Lumux."""

import os
import sys
from datetime import datetime
from pathlib import Path


def timed_print(*args, **kwargs) -> None:
    """Print with timestamp prefix.
    
    Args:
        *args: Values to print
        **kwargs: Keyword arguments passed to print()
    """
    prefix = datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    print(prefix, *args, **kwargs)


def log_dir() -> Path:
    """~/.cache/lumux, or ~/.var/app/<id>/cache/lumux inside Flatpak."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(Path.home(), ".cache")
    return Path(base) / "lumux"


class _Tee:
    def __init__(self, *streams):
        self._streams = streams

    def write(self, data):
        for stream in self._streams:
            try:
                stream.write(data)
                stream.flush()
            except Exception:
                pass
        return len(data)

    def flush(self):
        for stream in self._streams:
            try:
                stream.flush()
            except Exception:
                pass

    def __getattr__(self, name):
        return getattr(self._streams[0], name)


def setup_log_file(name: str = "lumux") -> Path:
    """Copy everything printed to <log_dir>/<name>.log (previous run kept as
    <name>.log.1), and send GStreamer warnings/errors to <name>-gst.log.

    Call before GStreamer is initialised so GST_DEBUG_FILE takes effect.
    """
    directory = log_dir()
    path = directory / f"{name}.log"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.replace(directory / f"{name}.log.1")
        log = open(path, "w", buffering=1, encoding="utf-8")
    except OSError as e:
        print(f"Could not open log file {path}: {e}", file=sys.stderr)
        return path
    sys.stdout = _Tee(sys.__stdout__, log)
    sys.stderr = _Tee(sys.__stderr__, log)
    os.environ.setdefault("GST_DEBUG", "2")
    os.environ.setdefault("GST_DEBUG_FILE", str(directory / f"{name}-gst.log"))
    os.environ.setdefault("GST_DEBUG_NO_COLOR", "1")
    timed_print(f"Lumux log: {path}")
    return path
