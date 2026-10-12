"""Hue Entertainment API streaming via DTLS-PSK.

This module implements the Hue Entertainment streaming protocol which allows
low-latency color updates to lights in an entertainment zone using DTLS over UDP.
"""

import struct
import threading
import time
from typing import Dict, Optional, Tuple, TYPE_CHECKING

from lumux.dtls import DTLSClient, DTLSError
from lumux.utils.logging import timed_print

if TYPE_CHECKING:
    from lumux.hue_bridge import HueBridge


# HueStream protocol constants
class HueStreamProtocol:
    """HueStream protocol constants."""

    HEADER = b"HueStream"
    VERSION_MAJOR = 0x02  # API version 2.0
    VERSION_MINOR = 0x00
    PORT = 2100

    # Color space constants
    COLORSPACE_RGB = 0x00
    COLORSPACE_XY = 0x01

    # Message structure sizes (bytes)
    HEADER_SIZE = 9
    VERSION_SIZE = 2
    SEQUENCE_SIZE = 1
    RESERVED_SIZE = 2
    COLORSPACE_SIZE = 1
    RESERVED2_SIZE = 1
    CONFIG_ID_SIZE = 36  # ASCII UUID string
    CHANNEL_DATA_SIZE = 7  # 1 byte ID + 3×2 bytes color data
    MESSAGE_HEADER_SIZE = 52

    # Color value scaling
    MAX_16BIT = 65535
    BRIGHTNESS_SCALE = 257  # 254 * 257 ≈ 65278


class ChannelInfo:
    """Information about an entertainment channel."""

    def __init__(self, channel_id: int, position: Dict, members: list):
        self.channel_id = channel_id
        self.position = position
        self.members = members


class EntertainmentStream:
    """Manages the DTLS session and streaming to a Hue Entertainment zone.

    Colours are pushed with ``send_colors_xy()`` from the sync loop, but the
    actual transmission happens on a dedicated sender thread at a fixed rate.
    This matters because the bridge silently ends a streaming session after
    ~10 seconds without packets: previously Lumux only sent a packet when a
    screen frame arrived, so a slow screen-share consent dialog, a static
    screen, or a capture backend that never produced frames (e.g. no portal
    ScreenCast on SteamOS Desktop Mode) left the lights dead. The sender
    thread also detects a dropped session and reconnects with backoff.
    """

    # Bridge forwards to lights at 25 Hz; streaming a bit faster than that
    # covers occasional UDP loss without wasting CPU.
    STREAM_HZ = 30
    RECONNECT_BACKOFF_MAX = 10.0

    def __init__(
        self,
        bridge_ip: str,
        app_key: str,
        client_key: str,
        entertainment_config_id: str,
        handshake_timeout: float = 5.0,
    ):
        """Initialize entertainment stream.

        Args:
            bridge_ip: IP address of the Hue bridge
            app_key: Application key (username) - used to get hue-application-id
            client_key: Client key - used as PSK (32-byte hex string)
            entertainment_config_id: ID of the entertainment configuration to stream to
            handshake_timeout: Max time to wait for the DTLS handshake (seconds)
        """
        self.bridge_ip = bridge_ip
        self.app_key = app_key
        self.client_key = client_key
        self.entertainment_config_id = entertainment_config_id
        self._handshake_timeout = handshake_timeout

        self._application_id: Optional[str] = None
        self._transport: Optional[DTLSClient] = None
        self._bridge: Optional["HueBridge"] = None
        self._active = False
        self._sequence = 0
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._sender: Optional[threading.Thread] = None
        self.last_error: str = ""

        # Channel mappings
        self._channels: Dict[int, ChannelInfo] = {}
        self._light_to_channel: Dict[str, int] = {}

        # Pre-computed message construction caches
        self._sorted_channel_ids: list[int] = []
        self._encoded_config_id: bytes = self.entertainment_config_id.encode("ascii")
        self._message_buffer: bytearray = bytearray()
        # Latest frame to transmit (rebuilt by send_colors*, read by sender)
        self._pending_message: Optional[bytes] = None

    @property
    def channels(self) -> Dict[int, ChannelInfo]:
        """Channel mapping: channel_id -> ChannelInfo."""
        return self._channels

    @property
    def light_to_channel(self) -> Dict[str, int]:
        """Reverse mapping: entertainment service rid -> channel_id."""
        return self._light_to_channel

    def matches(self, bridge_ip: str, app_key: str, client_key: str, config_id: str) -> bool:
        """True if this stream was built for exactly these credentials."""
        return (self.bridge_ip, self.app_key, self.client_key, self.entertainment_config_id) == (
            bridge_ip,
            app_key,
            client_key,
            config_id,
        )

    def connect(self, bridge: "HueBridge") -> bool:
        """Activate streaming on the bridge, handshake, and start the sender.

        Blocks for up to a few seconds (REST + DTLS handshake); call it off
        the GTK main thread.

        Returns:
            True if connection successful, False otherwise (see last_error)
        """
        if self._active:
            return True
        self.last_error = ""
        if not self.client_key:
            self.last_error = "No client key; re-pair the bridge in Settings"
            print(self.last_error)
            return False
        try:
            config = self._fetch_entertainment_config(bridge)
            if not config:
                self.last_error = "Entertainment area not found on bridge"
                return False

            self._parse_channels(config)
            self._init_message_buffer()
            self._application_id = self._fetch_application_id(bridge)
            self._bridge = bridge

            if not self._open_session(bridge):
                return False

            self._active = True
            self._stop_event.clear()
            # Lights go dark until the first captured frame arrives
            self.send_colors_xy({})
            self._sender = threading.Thread(
                target=self._sender_loop, daemon=True, name="HueStreamSender"
            )
            self._sender.start()
            timed_print(
                f"Entertainment stream connected with {len(self._channels)} channels"
            )
            return True

        except Exception as e:
            self.last_error = str(e)
            print(f"Error connecting entertainment stream: {e}")
            return False

    def _open_session(self, bridge: "HueBridge") -> bool:
        """Activate the entertainment config and complete the DTLS handshake."""
        if not bridge.activate_entertainment_streaming(self.entertainment_config_id):
            self.last_error = (
                "Bridge refused to start streaming (is another app such as "
                "Hue Sync already streaming to this area?)"
            )
            print(self.last_error)
            return False

        identity = self._application_id or self.app_key
        transport = DTLSClient(
            self.bridge_ip, HueStreamProtocol.PORT, identity, self.client_key
        )
        try:
            transport.connect(timeout=self._handshake_timeout)
        except DTLSError as e:
            self.last_error = str(e)
            print(f"DTLS connection to {self.bridge_ip} failed: {e}")
            self._deactivate_streaming(bridge)
            return False

        with self._lock:
            self._transport = transport
        timed_print(
            f"DTLS connection established to {self.bridge_ip}:{HueStreamProtocol.PORT}"
        )
        return True

    def _close_transport(self, notify: bool = True) -> None:
        with self._lock:
            transport, self._transport = self._transport, None
        if transport:
            try:
                transport.close(notify=notify)
            except Exception:
                pass

    def _sender_loop(self) -> None:
        """Transmit the latest frame at STREAM_HZ; reconnect if the bridge drops us."""
        interval = 1.0 / self.STREAM_HZ
        backoff = 1.0
        next_tick = time.monotonic()
        poll_counter = 0

        while not self._stop_event.is_set():
            transport = self._transport
            if transport is None or transport.closed:
                self._close_transport(notify=False)
                timed_print(
                    f"Entertainment stream lost; reconnecting in {backoff:.0f}s..."
                )
                if self._stop_event.wait(backoff):
                    break
                bridge = self._bridge
                if bridge is not None:
                    self._deactivate_streaming(bridge)
                    if self._open_session(bridge):
                        backoff = 1.0
                        next_tick = time.monotonic()
                        continue
                backoff = min(backoff * 2, self.RECONNECT_BACKOFF_MAX)
                continue

            message = self._pending_message
            if message is not None:
                try:
                    transport.send(message)
                except DTLSError as e:
                    print(f"DTLS connection lost: {e}")
                    continue

            # Check for close_notify / ICMP errors about twice a second
            poll_counter += 1
            if poll_counter >= self.STREAM_HZ // 2:
                poll_counter = 0
                if transport.poll_closed():
                    print("Bridge closed the entertainment session")
                    continue

            next_tick += interval
            delay = next_tick - time.monotonic()
            if delay < -interval:
                next_tick = time.monotonic()  # fell behind (suspend etc.)
                delay = 0
            if delay > 0:
                self._stop_event.wait(delay)

    def _fetch_entertainment_config(self, bridge: "HueBridge") -> Optional[dict]:
        """Fetch entertainment configuration from bridge."""
        config = bridge.get_entertainment_configuration(self.entertainment_config_id)
        if not config:
            print(
                f"Entertainment configuration {self.entertainment_config_id} not found"
            )
        return config

    def _fetch_application_id(self, bridge: "HueBridge") -> str:
        """Get hue-application-id from /auth/v1 endpoint.

        This is the correct PSK identity for DTLS connection per official API docs.
        Falls back to app_key if unavailable.
        """
        try:
            app_id = bridge.get_application_id()
            if app_id:
                timed_print(f"Got hue-application-id: {app_id}")
                return app_id
        except Exception as e:
            timed_print(f"Error getting application ID: {e}")

        timed_print(
            "Warning: Could not get hue-application-id, falling back to app_key"
        )
        return self.app_key

    def _deactivate_streaming(self, bridge: "HueBridge") -> None:
        """Deactivate streaming via REST API."""
        try:
            bridge.deactivate_entertainment_streaming(self.entertainment_config_id)
        except Exception as e:
            print(f"Error deactivating streaming: {e}")

    def _parse_channels(self, config: dict) -> None:
        """Parse channel information from entertainment configuration."""
        self._channels.clear()
        self._light_to_channel.clear()

        channels = config.get("channels", [])
        timed_print(f"Entertainment config has {len(channels)} channel entries")

        for channel in channels:
            self._parse_single_channel(channel)

        timed_print(f"Parsed {len(self._channels)} channels from entertainment config")

    def _init_message_buffer(self) -> None:
        self._sorted_channel_ids = sorted(self._channels.keys())
        num_channels = len(self._sorted_channel_ids)
        total_size = (
            HueStreamProtocol.MESSAGE_HEADER_SIZE
            + HueStreamProtocol.CHANNEL_DATA_SIZE * num_channels
        )
        self._message_buffer = bytearray(total_size)
        buf = self._message_buffer
        buf[0:9] = HueStreamProtocol.HEADER
        buf[9] = HueStreamProtocol.VERSION_MAJOR
        buf[10] = HueStreamProtocol.VERSION_MINOR
        buf[12] = 0x00
        buf[13] = 0x00
        buf[15] = 0x00
        buf[16:52] = self._encoded_config_id

    def _parse_single_channel(self, channel: dict) -> None:
        """Parse a single channel from config."""
        channel_id = channel.get("channel_id")
        if channel_id is None:
            timed_print(f"  Skipping channel without ID: {channel}")
            return

        position = channel.get("position", {})
        members = channel.get("members", [])

        self._channels[channel_id] = ChannelInfo(
            channel_id=channel_id, position=position, members=members
        )

        pos_x = position.get("x", 0)
        pos_y = position.get("y", 0)
        pos_z = position.get("z", 0)
        timed_print(
            f"  Channel {channel_id}: "
            f"pos=({pos_x:.2f}, {pos_y:.2f}, {pos_z:.2f}), "
            f"{len(members)} members"
        )

        for member in members:
            self._map_member_to_channel(member, channel_id)

    def _map_member_to_channel(self, member: dict, channel_id: int) -> None:
        """Map a member's entertainment service to its channel."""
        service = member.get("service", {})
        rid = service.get("rid")
        if rid:
            self._light_to_channel[rid] = channel_id

    def disconnect(self, bridge: "HueBridge") -> None:
        """Stop the sender, close DTLS, and deactivate streaming.

        Deactivating hands the lights back to the bridge, which restores
        their pre-stream state.
        """
        was_active = self._active
        self._active = False
        self._stop_event.set()
        if self._sender and self._sender is not threading.current_thread():
            self._sender.join(timeout=2)
        self._sender = None
        self._close_transport(notify=True)
        self._pending_message = None
        if was_active:
            self._deactivate_streaming(bridge)
        timed_print("Entertainment stream disconnected")

    def is_active(self) -> bool:
        """True between connect() and disconnect(), even while reconnecting."""
        return self._active

    def is_connected(self) -> bool:
        """True if the DTLS session to the bridge is currently up."""
        transport = self._transport
        return self._active and transport is not None and not transport.closed

    def send_colors(self, colors: Dict[int, Tuple[float, float, float, float]]) -> None:
        """Queue a colour update for all channels (RGB format).

        Args:
            colors: Dict mapping channel_id to (r, g, b, _) tuple
                   where r, g, b are 0.0-1.0
        """
        if not self._message_buffer:
            return
        with self._lock:
            message = self._build_rgb_message(colors)
            self._sequence = (self._sequence + 1) % 256
        self._pending_message = message

    def send_colors_xy(
        self, channel_colors: Dict[int, Tuple[Tuple[float, float], int]]
    ) -> None:
        """Queue a colour update using XY + brightness format.

        Channels missing from ``channel_colors`` are sent as off. The sender
        thread transmits the most recent update continuously.

        Args:
            channel_colors: Dict mapping channel_id to ((x, y), brightness) tuple
                           where x, y are CIE color coordinates and brightness is 0-254
        """
        if not self._message_buffer:
            return
        with self._lock:
            message = self._build_xy_message(channel_colors)
            self._sequence = (self._sequence + 1) % 256
        self._pending_message = message

    def _build_rgb_message(
        self, colors: Dict[int, Tuple[float, float, float, float]]
    ) -> bytes:
        """Build HueStream v2 message with RGB color space."""
        buf = self._message_buffer
        buf[11] = self._sequence
        buf[14] = HueStreamProtocol.COLORSPACE_RGB
        offset = HueStreamProtocol.MESSAGE_HEADER_SIZE
        for channel_id in self._sorted_channel_ids:
            r, g, b = self._extract_rgb(colors, channel_id)
            struct.pack_into(
                ">BHHH",
                buf,
                offset,
                channel_id,
                int(max(0, min(1, r)) * HueStreamProtocol.MAX_16BIT),
                int(max(0, min(1, g)) * HueStreamProtocol.MAX_16BIT),
                int(max(0, min(1, b)) * HueStreamProtocol.MAX_16BIT),
            )
            offset += HueStreamProtocol.CHANNEL_DATA_SIZE
        return bytes(buf)

    def _build_xy_message(
        self, colors: Dict[int, Tuple[Tuple[float, float], int]]
    ) -> bytes:
        """Build HueStream v2 message with XY+Brightness color space."""
        buf = self._message_buffer
        buf[11] = self._sequence
        buf[14] = HueStreamProtocol.COLORSPACE_XY
        offset = HueStreamProtocol.MESSAGE_HEADER_SIZE
        for channel_id in self._sorted_channel_ids:
            (x, y), brightness = self._extract_xy_brightness(colors, channel_id)
            struct.pack_into(
                ">BHHH",
                buf,
                offset,
                channel_id,
                int(max(0, min(1, x)) * HueStreamProtocol.MAX_16BIT),
                int(max(0, min(1, y)) * HueStreamProtocol.MAX_16BIT),
                int(max(0, min(254, brightness)) * HueStreamProtocol.BRIGHTNESS_SCALE),
            )
            offset += HueStreamProtocol.CHANNEL_DATA_SIZE
        return bytes(buf)

    def _extract_rgb(
        self, colors: Dict[int, Tuple[float, float, float, float]], channel_id: int
    ) -> Tuple[float, float, float]:
        """Extract RGB values from colors dict."""
        if channel_id in colors:
            r, g, b, _ = colors[channel_id]
            return r, g, b
        return 0.0, 0.0, 0.0

    def _extract_xy_brightness(
        self, colors: Dict[int, Tuple[Tuple[float, float], int]], channel_id: int
    ) -> Tuple[Tuple[float, float], int]:
        """Extract XY and brightness values from colors dict."""
        if channel_id in colors:
            return colors[channel_id]
        return (0.0, 0.0), 0

    def get_channel_positions(self) -> Dict[int, dict]:
        """Get mapping of channel IDs to their 3D positions.

        Returns dict of channel_id -> {'x': float, 'y': float, 'z': float}
        Positions are normalized: x=-1 to 1 (left to right),
        y=-1 to 1 (front to back), z=0 to 1 (bottom to top)
        """
        return {
            channel_id: {
                "x": info.position.get("x", 0),
                "y": info.position.get("y", 0),
                "z": info.position.get("z", 0),
            }
            for channel_id, info in self._channels.items()
        }

    def map_zone_to_channel(self, zone_id: str) -> Optional[int]:
        """Map a screen zone ID to an entertainment channel ID based on position.

        Args:
            zone_id: Zone identifier like 'top_0', 'left_1', 'right_2', 'bottom_3'

        Returns:
            Best matching channel_id or None
        """
        if not self._channels:
            return None

        try:
            edge, idx_str = zone_id.split("_")
            idx = int(idx_str)
        except ValueError:
            return None

        edge_positions = self._get_edge_position_ranges()
        if edge not in edge_positions:
            return None

        matching_channels = self._find_channels_for_edge(edge, edge_positions[edge])

        if not matching_channels:
            return next(iter(self._channels.keys()), None)

        # Sort and pick by index
        matching_channels.sort(key=lambda c: c[1])
        idx = min(idx, len(matching_channels) - 1)
        return matching_channels[idx][0]

    def _get_edge_position_ranges(self) -> Dict[str, dict]:
        """Get position ranges for each screen edge."""
        return {
            "top": {"z_min": 0.5, "z_max": 1.0},
            "bottom": {"z_min": -1.0, "z_max": -0.5},
            "left": {"x_min": -1.0, "x_max": -0.5},
            "right": {"x_min": 0.5, "x_max": 1.0},
        }

    def _find_channels_for_edge(
        self, edge: str, edge_range: dict
    ) -> list[tuple[int, float]]:
        """Find channels matching the given edge."""
        matching = []

        for channel_id, info in self._channels.items():
            pos = info.position
            x = pos.get("x", 0)
            z = pos.get("z", 0)

            matches, sort_key = self._channel_matches_edge(edge, edge_range, x, z)
            if matches:
                matching.append((channel_id, sort_key))

        return matching

    def _channel_matches_edge(
        self, edge: str, edge_range: dict, x: float, z: float
    ) -> Tuple[bool, float]:
        """Check if channel position matches edge, return (matches, sort_key)."""
        if edge in ("left", "right"):
            if edge_range["x_min"] <= x <= edge_range["x_max"]:
                return True, z  # Sort by height for left/right
        else:  # top/bottom
            if edge_range["z_min"] <= z <= edge_range["z_max"]:
                return True, x  # Sort by x position for top/bottom
        return False, 0.0
