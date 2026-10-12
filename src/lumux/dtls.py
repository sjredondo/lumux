"""Minimal DTLS 1.2 PSK client for the Hue Entertainment API.

Python's ``ssl`` module has no DTLS support, so this binds the handful of
libssl functions needed via ctypes. libssl is always present wherever Python's
own ``ssl`` module works (Arch/SteamOS, the GNOME Flatpak runtime, Fedora...),
so this avoids spawning an ``openssl s_client`` subprocess and piping binary
frames through its stdin, which:

* gave no real signal that the handshake succeeded (it just slept and checked
  the process was still alive),
* could coalesce several HueStream frames into one datagram, and
* never noticed when the bridge dropped the session.

Each ``send()`` here is exactly one DTLS record / one UDP datagram, the
handshake result is explicit, and ``poll_closed()`` detects a close_notify or
ICMP error from the bridge so the caller can reconnect.
"""

import ctypes
import ctypes.util
import select
import socket
import struct
import sys
import time
from typing import Optional

# libssl constants (stable ABI values from ssl.h / bio.h / dtls1.h)
_SSL_CTRL_SET_MIN_PROTO_VERSION = 123
_SSL_CTRL_SET_MAX_PROTO_VERSION = 124
_DTLS1_2_VERSION = 0xFEFD
_DTLS_CTRL_GET_TIMEOUT = 73
_DTLS_CTRL_HANDLE_TIMEOUT = 74
_BIO_CTRL_DGRAM_SET_CONNECTED = 32
_BIO_NOCLOSE = 0

_SSL_ERROR_WANT_READ = 2
_SSL_ERROR_WANT_WRITE = 3
_SSL_ERROR_ZERO_RETURN = 6

# Hue bridges only accept TLS_PSK_WITH_AES_128_GCM_SHA256
_HUE_CIPHER = b"PSK-AES128-GCM-SHA256"

_PSK_CLIENT_CB = ctypes.CFUNCTYPE(
    ctypes.c_uint,
    ctypes.c_void_p,  # SSL *ssl
    ctypes.c_char_p,  # const char *hint
    ctypes.c_void_p,  # char *identity
    ctypes.c_uint,  # unsigned int max_identity_len
    ctypes.c_void_p,  # unsigned char *psk
    ctypes.c_uint,  # unsigned int max_psk_len
)


class DTLSError(Exception):
    """Raised when the DTLS session cannot be established or is lost."""


class _Timeval(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


_lib = None


def _load_libssl():
    global _lib
    if _lib is not None:
        return _lib

    candidates = ["libssl.so.3", "libssl.so", "libssl.so.1.1"]
    found = ctypes.util.find_library("ssl")
    if found:
        candidates.insert(0, found)
    if sys.platform == "darwin":
        candidates += [
            "/opt/homebrew/opt/openssl@3/lib/libssl.3.dylib",
            "/usr/local/opt/openssl@3/lib/libssl.3.dylib",
        ]

    last_error = None
    for name in candidates:
        try:
            lib = ctypes.CDLL(name)
            lib.DTLS_client_method  # OpenSSL >= 1.1 only
            break
        except (OSError, AttributeError) as e:
            last_error = e
    else:
        raise DTLSError(f"libssl with DTLS support not found: {last_error}")

    vp = ctypes.c_void_p
    lib.DTLS_client_method.restype = vp
    lib.SSL_CTX_new.argtypes = [vp]
    lib.SSL_CTX_new.restype = vp
    lib.SSL_CTX_free.argtypes = [vp]
    lib.SSL_CTX_ctrl.argtypes = [vp, ctypes.c_int, ctypes.c_long, vp]
    lib.SSL_CTX_ctrl.restype = ctypes.c_long
    lib.SSL_CTX_set_cipher_list.argtypes = [vp, ctypes.c_char_p]
    lib.SSL_CTX_set_cipher_list.restype = ctypes.c_int
    lib.SSL_CTX_set_psk_client_callback.argtypes = [vp, _PSK_CLIENT_CB]
    lib.SSL_new.argtypes = [vp]
    lib.SSL_new.restype = vp
    lib.SSL_free.argtypes = [vp]
    lib.SSL_set_bio.argtypes = [vp, vp, vp]
    lib.SSL_connect.argtypes = [vp]
    lib.SSL_connect.restype = ctypes.c_int
    lib.SSL_write.argtypes = [vp, ctypes.c_char_p, ctypes.c_int]
    lib.SSL_write.restype = ctypes.c_int
    lib.SSL_read.argtypes = [vp, ctypes.c_char_p, ctypes.c_int]
    lib.SSL_read.restype = ctypes.c_int
    lib.SSL_get_error.argtypes = [vp, ctypes.c_int]
    lib.SSL_get_error.restype = ctypes.c_int
    lib.SSL_shutdown.argtypes = [vp]
    lib.SSL_shutdown.restype = ctypes.c_int
    lib.SSL_ctrl.argtypes = [vp, ctypes.c_int, ctypes.c_long, vp]
    lib.SSL_ctrl.restype = ctypes.c_long
    lib.BIO_new_dgram.argtypes = [ctypes.c_int, ctypes.c_int]
    lib.BIO_new_dgram.restype = vp
    lib.BIO_ctrl.argtypes = [vp, ctypes.c_int, ctypes.c_long, vp]
    lib.BIO_ctrl.restype = ctypes.c_long
    lib.ERR_get_error.restype = ctypes.c_ulong
    lib.ERR_error_string_n.argtypes = [ctypes.c_ulong, ctypes.c_char_p, ctypes.c_size_t]
    lib.ERR_clear_error.restype = None

    _lib = lib
    return lib


def is_available() -> bool:
    """True if a DTLS-capable libssl can be loaded."""
    try:
        _load_libssl()
        return True
    except DTLSError:
        return False


def _sockaddr_in(ip: str, port: int) -> bytes:
    """Build a raw struct sockaddr_in (BSD layout carries a length byte)."""
    addr = socket.inet_aton(ip)
    if sys.platform == "darwin" or "bsd" in sys.platform:
        return struct.pack("BB", 16, socket.AF_INET) + struct.pack("!H", port) + addr + bytes(8)
    return struct.pack("=H", socket.AF_INET) + struct.pack("!H", port) + addr + bytes(8)


def _openssl_errors(lib) -> str:
    errors = []
    while True:
        code = lib.ERR_get_error()
        if not code:
            break
        buf = ctypes.create_string_buffer(256)
        lib.ERR_error_string_n(code, buf, len(buf))
        errors.append(buf.value.decode(errors="replace"))
    return "; ".join(errors)


class DTLSClient:
    """One DTLS-PSK session to a Hue bridge (UDP port 2100)."""

    def __init__(self, host: str, port: int, psk_identity: str, psk_hex: str):
        self.host = host
        self.port = port
        self._identity = psk_identity.encode("ascii")
        try:
            self._psk = bytes.fromhex(psk_hex)
        except ValueError as e:
            raise DTLSError(f"client key is not valid hex: {e}") from e

        self._lib = _load_libssl()
        self._sock: Optional[socket.socket] = None
        self._ctx = None
        self._ssl = None
        self._closed = True
        # Must stay referenced for the lifetime of the SSL_CTX
        self._psk_cb = _PSK_CLIENT_CB(self._on_psk)

    def _on_psk(self, _ssl, _hint, identity, max_identity_len, psk, max_psk_len):
        if len(self._identity) + 1 > max_identity_len or len(self._psk) > max_psk_len:
            return 0
        ctypes.memmove(identity, self._identity + b"\0", len(self._identity) + 1)
        ctypes.memmove(psk, self._psk, len(self._psk))
        return len(self._psk)

    def connect(self, timeout: float = 5.0) -> None:
        """Perform the DTLS handshake. Raises DTLSError on failure."""
        lib = self._lib
        lib.ERR_clear_error()
        try:
            self._ctx = lib.SSL_CTX_new(lib.DTLS_client_method())
            if not self._ctx:
                raise DTLSError("SSL_CTX_new failed")
            lib.SSL_CTX_ctrl(self._ctx, _SSL_CTRL_SET_MIN_PROTO_VERSION, _DTLS1_2_VERSION, None)
            lib.SSL_CTX_ctrl(self._ctx, _SSL_CTRL_SET_MAX_PROTO_VERSION, _DTLS1_2_VERSION, None)
            if lib.SSL_CTX_set_cipher_list(self._ctx, _HUE_CIPHER) != 1:
                raise DTLSError(f"cipher {_HUE_CIPHER.decode()} unavailable in libssl")
            lib.SSL_CTX_set_psk_client_callback(self._ctx, self._psk_cb)

            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.connect((self.host, self.port))
            self._sock.setblocking(False)

            self._ssl = lib.SSL_new(self._ctx)
            if not self._ssl:
                raise DTLSError("SSL_new failed")
            bio = lib.BIO_new_dgram(self._sock.fileno(), _BIO_NOCLOSE)
            if not bio:
                raise DTLSError("BIO_new_dgram failed")
            peer = ctypes.create_string_buffer(_sockaddr_in(self.host, self.port))
            lib.BIO_ctrl(bio, _BIO_CTRL_DGRAM_SET_CONNECTED, 0, peer)
            lib.SSL_set_bio(self._ssl, bio, bio)  # SSL now owns the BIO

            self._handshake(timeout)
            self._closed = False
        except Exception:
            self.close(notify=False)
            raise

    def _handshake(self, timeout: float) -> None:
        lib = self._lib
        deadline = time.monotonic() + timeout
        while True:
            ret = lib.SSL_connect(self._ssl)
            if ret == 1:
                return
            err = lib.SSL_get_error(self._ssl, ret)
            if err not in (_SSL_ERROR_WANT_READ, _SSL_ERROR_WANT_WRITE):
                detail = _openssl_errors(lib) or f"SSL error {err}"
                raise DTLSError(f"DTLS handshake failed: {detail}")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DTLSError(
                    f"DTLS handshake timed out after {timeout:.0f}s "
                    f"(bridge unreachable on UDP {self.port}, or streaming not active)"
                )
            # Honour DTLS retransmission timers while waiting for the bridge
            tv = _Timeval()
            wait = remaining
            if lib.SSL_ctrl(self._ssl, _DTLS_CTRL_GET_TIMEOUT, 0, ctypes.byref(tv)):
                wait = min(wait, tv.tv_sec + tv.tv_usec / 1e6)
            readable, _, _ = select.select([self._sock], [], [], max(0.0, wait))
            if not readable:
                lib.SSL_ctrl(self._ssl, _DTLS_CTRL_HANDLE_TIMEOUT, 0, None)

    @property
    def closed(self) -> bool:
        return self._closed

    def send(self, payload: bytes) -> None:
        """Send one datagram. Raises DTLSError if the session is gone."""
        if self._closed:
            raise DTLSError("DTLS session is closed")
        ret = self._lib.SSL_write(self._ssl, payload, len(payload))
        if ret <= 0:
            err = self._lib.SSL_get_error(self._ssl, ret)
            if err in (_SSL_ERROR_WANT_READ, _SSL_ERROR_WANT_WRITE):
                return  # socket buffer full; drop this frame, next one follows
            self._closed = True
            detail = _openssl_errors(self._lib) or f"SSL error {err}"
            raise DTLSError(f"DTLS send failed: {detail}")

    def poll_closed(self) -> bool:
        """Drain anything the bridge sent; return True if the session ended."""
        if self._closed:
            return True
        buf = ctypes.create_string_buffer(512)
        while True:
            ret = self._lib.SSL_read(self._ssl, buf, len(buf))
            if ret > 0:
                continue  # bridge does not normally send data; ignore it
            err = self._lib.SSL_get_error(self._ssl, ret)
            if err in (_SSL_ERROR_WANT_READ, _SSL_ERROR_WANT_WRITE):
                return False
            # ZERO_RETURN = close_notify; anything else = socket/ICMP error
            self._lib.ERR_clear_error()
            self._closed = True
            return True

    def close(self, notify: bool = True) -> None:
        lib = self._lib
        if self._ssl:
            if notify and not self._closed:
                try:
                    lib.SSL_shutdown(self._ssl)
                except Exception:
                    pass
            lib.SSL_free(self._ssl)
            self._ssl = None
        if self._ctx:
            lib.SSL_CTX_free(self._ctx)
            self._ctx = None
        if self._sock:
            self._sock.close()
            self._sock = None
        lib.ERR_clear_error()
        self._closed = True

    def __del__(self):
        try:
            self.close(notify=False)
        except Exception:
            pass
