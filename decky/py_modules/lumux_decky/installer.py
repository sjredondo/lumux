"""Install or update the Lumux Flatpak from Game Mode.

The plugin doesn't bundle lumux.flatpak: the GNOME runtime it needs always
comes from Flathub anyway, and big binaries don't belong in a Decky plugin.
Instead "Install Lumux" downloads lumux.flatpak from the GitHub release that
matches this plugin (_version.LUMUX_VERSION; the latest release only if that
one has no bundle) and installs it as a user Flatpak, the same steps as
scripts/install-steamos.sh:

    flatpak remote-add --user --if-not-exists flathub <flathub repo>
    flatpak install --user -y --noninteractive [--reinstall] lumux.flatpak

Everything runs as the Deck user (the plugin has no root flag) with the
sanitized environment from supervisor.child_env(). The panel polls
Installer.snapshot() for progress while the task runs in the background:
the first install also pulls the GNOME runtime, which takes minutes.

Network, download and flatpak are injected so tests never touch either.
"""

import asyncio
import json
import os
import re
import shutil
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Awaitable, Callable, Dict, List, NamedTuple, Optional, Tuple

from ._version import GITHUB_REPO, LUMUX_VERSION

FLATHUB_REPO = "https://dl.flathub.org/repo/flathub.flatpakrepo"
ASSET_NAME = "lumux.flatpak"
RELEASES_API = "https://api.github.com/repos/{repo}/releases"
# Decky's PyInstaller Python may not find the system's CA bundle by itself
CA_FILES = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/ca-certificates/extracted/tls-ca-bundle.pem",
    "/etc/pki/tls/certs/ca-bundle.crt",
)
STATES = ("idle", "checking", "downloading", "installing", "finishing", "done", "error")


class InstallError(Exception):
    """A failure the panel can show as is."""


class NotFound(Exception):
    """GitHub answered 404."""


class Release(NamedTuple):
    tag: str
    url: str
    size: int
    pinned: bool  # False: fell back to the latest release


# -- versions -------------------------------------------------------------------

def parse_version(text: Optional[str]) -> Optional[Tuple[int, ...]]:
    """'0.8.4', 'v0.8.4', '0.8.4-1' -> (0, 8, 4); None if there's no number."""
    m = re.match(r"\s*v?(\d+(?:\.\d+)*)", text or "")
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def is_older(installed: Optional[str], wanted: str) -> bool:
    """True only when both versions parse and installed < wanted."""
    a, b = parse_version(installed), parse_version(wanted)
    if a is None or b is None:
        return False
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) < b + (0,) * (width - len(b))


# -- GitHub release lookup ----------------------------------------------------------

def _pick_asset(release: Optional[dict]) -> Optional[dict]:
    assets = (release or {}).get("assets") or []
    for asset in assets:
        if asset.get("name") == ASSET_NAME:
            return asset
    for asset in assets:
        if str(asset.get("name", "")).endswith(".flatpak"):
            return asset
    return None


async def find_release(
    fetch_json: Callable[[str], Awaitable[dict]],
    version: str = LUMUX_VERSION,
    repo: str = GITHUB_REPO,
) -> Release:
    """The release tagged v<version>; the latest release only if that tag
    doesn't exist or has no Flatpak bundle."""
    base = RELEASES_API.format(repo=repo)
    try:
        release = await fetch_json(f"{base}/tags/v{version}")
    except NotFound:
        release = None
    asset = _pick_asset(release)
    if asset:
        return Release(release.get("tag_name") or f"v{version}", asset["browser_download_url"],
                       int(asset.get("size") or 0), True)
    try:
        release = await fetch_json(f"{base}/latest")
    except NotFound:
        raise InstallError(f"{repo} has no published releases") from None
    asset = _pick_asset(release)
    if not asset:
        raise InstallError(f"No {ASSET_NAME} in {repo} release v{version} or in the latest release")
    return Release(release.get("tag_name") or "latest", asset["browser_download_url"],
                   int(asset.get("size") or 0), False)


# -- default network implementation (urllib, in a worker thread) --------------------

def ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # bundled with Decky Loader

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    for path in CA_FILES:
        if os.path.exists(path):
            return ssl.create_default_context(cafile=path)
    return ssl.create_default_context()


def _open(url: str, timeout: float):
    req = urllib.request.Request(url, headers={
        "User-Agent": f"decky-lumux (Lumux {LUMUX_VERSION})",
        "Accept": "application/vnd.github+json" if "api.github.com" in url else "*/*",
    })
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=ssl_context())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NotFound(url) from None
        if e.code in (403, 429):
            raise InstallError(
                "GitHub refused the request (rate limit?). Try again in a few minutes.") from None
        raise InstallError(f"GitHub returned HTTP {e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", None) or e
        raise InstallError(
            f"Couldn't reach GitHub ({reason}). Check that the Deck is online.") from None


def http_get_json(url: str) -> dict:
    with _open(url, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_download(url: str, dest: str, progress: Callable[[int, int], None],
                  cancel: threading.Event) -> None:
    try:
        with _open(url, timeout=60) as resp, open(dest, "wb") as f:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                if cancel.is_set():
                    raise InstallError("Cancelled")
                chunk = resp.read(1 << 18)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                progress(done, total)
    except NotFound:
        raise InstallError("The release's Flatpak bundle is gone (HTTP 404); try again later") from None
    except OSError as e:  # connection dropped or disk full mid-download
        raise InstallError(f"Download failed ({e}). Check the connection and try again.") from None
    if total and done != total:
        raise InstallError("Download was cut short. Check the connection and try again.")


async def run_streaming(argv: List[str], env: Dict[str, str],
                        on_line: Callable[[str], None]) -> Tuple[int, List[str]]:
    """Run argv, feeding each output line (stdout+stderr, \\r or \\n
    separated) to on_line. Returns (exit code, last lines)."""
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, env=env,
    )
    lines: List[str] = []
    buf = b""

    def emit(raw: bytes) -> None:
        text = raw.decode("utf-8", "replace").strip()
        if text:
            lines.append(text)
            del lines[:-200]
            on_line(text)

    try:
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            parts = re.split(rb"[\r\n]", buf + chunk)
            buf = parts.pop()
            for part in parts:
                emit(part)
        emit(buf)
        return await proc.wait(), lines
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise


def flatpak_error(prefix: str, lines: List[str]) -> str:
    """The most useful bit of flatpak's output for the panel."""
    errors = [line for line in lines if re.match(r"(?i)^(error|fatal)\b", line)]
    detail = errors[-1] if errors else " ".join(lines[-3:])
    msg = f"{prefix}: {detail[:300]}" if detail else prefix
    if re.search(r"(?i)could not resolve|network|timed? ?out|connection", detail):
        msg += " Check that the Deck is online."
    elif re.search(r"(?i)no space|not enough", detail):
        msg += " Free up some storage and try again."
    return msg


# -- the install task ---------------------------------------------------------------

class Installer:
    """At most one install/update at a time. All methods run on Decky's
    asyncio loop; the download itself runs in a worker thread."""

    def __init__(
        self,
        env: Callable[[], Dict[str, str]],
        which: Callable[..., Optional[str]],
        logger=None,
        version: str = LUMUX_VERSION,
        repo: str = GITHUB_REPO,
        fetch_json: Optional[Callable[[str], Awaitable[dict]]] = None,
        download: Optional[Callable[[str, str, Callable[[int, int], None]], Awaitable[None]]] = None,
        run: Optional[Callable[..., Awaitable[Tuple[int, List[str]]]]] = None,
        before_install: Optional[Callable[[], Awaitable[None]]] = None,
        after_install: Optional[Callable[[bool], Awaitable[None]]] = None,
        tmp_root: Optional[str] = None,
        euid: Callable[[], int] = os.geteuid,
    ):
        self._env = env
        self._which = which
        self.log = logger
        self.version = version
        self.repo = repo
        self._fetch_json = fetch_json or self._default_fetch_json
        self._download = download or self._default_download
        self._run = run or run_streaming
        self.before_install = before_install
        self.after_install = after_install
        self._tmp_root = tmp_root
        self._euid = euid
        self._cancel = threading.Event()
        self._task: Optional[asyncio.Task] = None

        self.state = "idle"
        self.mode = ""  # install | update
        self.progress: Optional[float] = None  # 0..1, None = indeterminate
        self.message = ""
        self.detail = ""  # last line of flatpak output
        self.error = ""
        self.release: Optional[Release] = None
        self._started = 0.0
        self._finished = 0.0

    # -- public -------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, update: bool = False) -> bool:
        """Start an install (or, with update, a reinstall). Returns False if
        one is already running: repeated clicks just keep showing it."""
        if self.busy:
            return False
        self._cancel.clear()
        self.mode = "update" if update else "install"
        self.error = self.detail = ""
        self.release = None
        self._started, self._finished = time.monotonic(), 0.0
        self._set("checking", f"Looking up Lumux {self.version} on GitHub…")
        self._task = asyncio.ensure_future(self._install(update))
        return True

    async def wait(self) -> None:
        if self._task is not None:
            await asyncio.shield(self._task)

    async def cancel(self) -> None:
        if not self.busy:
            return
        self._cancel.set()
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass

    def snapshot(self) -> dict:
        end = self._finished or time.monotonic()
        return {
            "state": self.state,
            "busy": self.busy,
            "mode": self.mode,
            "progress": self.progress,
            "message": self.message,
            "detail": self.detail,
            "error": self.error,
            "elapsed": round(end - self._started) if self._started else 0,
            "tag": self.release.tag if self.release else "",
            "pinned": self.release.pinned if self.release else True,
            "version": self.version,
        }

    # -- commands -------------------------------------------------------------

    @staticmethod
    def remote_add_command(flatpak: str) -> List[str]:
        return [flatpak, "remote-add", "--user", "--if-not-exists", "flathub", FLATHUB_REPO]

    @staticmethod
    def install_command(flatpak: str, bundle: str, update: bool = False) -> List[str]:
        argv = [flatpak, "install", "--user", "-y", "--noninteractive"]
        if update:
            argv.append("--reinstall")
        return [*argv, bundle]

    # -- the task ---------------------------------------------------------------

    def _set(self, state: str, message: str, progress: Optional[float] = None) -> None:
        self.state, self.message, self.progress = state, message, progress

    def _log(self, level: str, msg: str) -> None:
        if self.log is not None:
            getattr(self.log, level)(msg)

    def _on_download(self, done: int, total: int) -> None:
        # Called from the download thread; plain attribute writes only
        mb = done / 1e6
        if total:
            self.progress = min(1.0, done / total)
            self.message = f"Downloading Lumux {self.release.tag}… {mb:.0f} / {total / 1e6:.0f} MB"
        else:
            self.message = f"Downloading Lumux {self.release.tag}… {mb:.0f} MB"

    def _on_line(self, line: str) -> None:
        self.detail = line[:160]
        self._log("info", f"flatpak: {line}")

    async def _install(self, update: bool) -> None:
        tmp = None
        reached_install = False
        ok = False
        try:
            if self._euid() == 0:
                # A --user install as root would land in root-owned files
                # under the Deck user's home
                raise InstallError("decky-lumux is running as root; refusing to install a user Flatpak")
            env = self._env()
            flatpak = self._which("flatpak", path=env.get("PATH"))
            if not flatpak:
                raise InstallError("The flatpak command wasn't found")

            self.release = rel = await find_release(self._fetch_json, self.version, self.repo)
            if not rel.pinned:
                self._log("warning", f"v{self.version} has no {ASSET_NAME}; using {rel.tag} instead")

            tmp = tempfile.mkdtemp(prefix="decky-lumux-", dir=self._tmp_root)
            bundle = os.path.join(tmp, ASSET_NAME)
            self._set("downloading", f"Downloading Lumux {rel.tag}…", 0.0)
            self._log("info", f"downloading {rel.url}")
            await self._download(rel.url, bundle, self._on_download)

            self._set("installing", "Adding the Flathub remote…")
            code, out = await self._run(self.remote_add_command(flatpak), env, self._on_line)
            if code != 0:
                raise InstallError(flatpak_error("Couldn't add the Flathub remote", out))

            reached_install = True
            if self.before_install is not None:
                await self.before_install()  # stops the old daemon
            self._set("installing", "Installing Lumux and its GNOME runtime from Flathub. "
                                    "The first install downloads a few hundred MB.")
            argv = self.install_command(flatpak, bundle, update)
            self._log("info", "running " + " ".join(argv))
            code, out = await self._run(argv, env, self._on_line)
            if code != 0:
                raise InstallError(flatpak_error("flatpak install failed", out))
            ok = True
            self._set("finishing", "Starting Lumux…", 1.0)
        except InstallError as e:
            self._fail(str(e))
        except asyncio.CancelledError:
            self._fail("Cancelled")
            raise
        except Exception as e:  # never leave the panel spinning
            self._log("exception", "Lumux install failed")
            self._fail(f"{type(e).__name__}: {e}")
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)

        if reached_install and self.after_install is not None:
            try:
                await self.after_install(ok)  # restarts the daemon either way
            except Exception as e:
                self._log("error", f"restarting lumux-daemon after install: {e}")
        if ok:
            what = "updated" if update else "installed"
            self._set("done", f"Lumux {self.release.tag} {what}", 1.0)
        self._finished = time.monotonic()

    def _fail(self, error: str) -> None:
        self._log("error", f"Lumux {self.mode} failed: {error}")
        self.error = error
        self._set("error", error, None)
        self._finished = time.monotonic()

    # -- defaults ---------------------------------------------------------------

    async def _default_fetch_json(self, url: str) -> dict:
        return await asyncio.get_running_loop().run_in_executor(None, http_get_json, url)

    async def _default_download(self, url: str, dest: str,
                                progress: Callable[[int, int], None]) -> None:
        await asyncio.get_running_loop().run_in_executor(
            None, http_download, url, dest, progress, self._cancel)
