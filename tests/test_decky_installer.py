"""Decky plugin: installing/updating the Lumux Flatpak from Game Mode
(decky/py_modules/lumux_decky/installer.py and its supervisor hooks).

GitHub, the download and flatpak are all faked: no network, no flatpak.
"""

import asyncio
import os
import re
import sys
import urllib.error

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "decky", "py_modules"))

from lumux_decky import _version, installer as inst  # noqa: E402
from lumux_decky import supervisor as supmod  # noqa: E402
from lumux_decky.installer import (  # noqa: E402
    InstallError, Installer, NotFound, find_release, is_older, parse_version,
)
from lumux_decky.supervisor import Supervisor, child_env  # noqa: E402

API = "https://api.github.com/repos/sjredondo/lumux/releases"


def _release(tag, *names):
    return {"tag_name": tag, "assets": [
        {"name": n, "browser_download_url": f"https://dl.example/{tag}/{n}", "size": 1234}
        for n in names]}


def _fetcher(responses):
    """fetch_json fake: URL -> dict, or NotFound when absent."""
    calls = []

    async def fetch(url):
        calls.append(url)
        if url not in responses:
            raise NotFound(url)
        res = responses[url]
        if isinstance(res, Exception):
            raise res
        return res

    fetch.calls = calls
    return fetch


# -- versions -------------------------------------------------------------------

@pytest.mark.parametrize("installed,wanted,older", [
    ("0.8.4", "0.8.5", True),
    ("0.8.5", "0.8.5", False),
    ("0.8.10", "0.8.9", False),  # numeric, not string comparison
    ("0.8", "0.8.1", True),
    ("0.8.0", "0.8", False),
    ("v0.7.0", "0.8.5", True),
    ("0.8.4-1", "0.8.5", True),
    ("", "0.8.5", False),  # unknown version: never claim it's old
    (None, "0.8.5", False),
    ("git", "0.8.5", False),
])
def test_version_comparison(installed, wanted, older):
    assert is_older(installed, wanted) is older


def test_parse_version():
    assert parse_version("0.8.4") == (0, 8, 4)
    assert parse_version(" v1.2 ") == (1, 2)
    assert parse_version("unknown") is None


def test_pinned_version_matches_the_lumux_release():
    """The plugin installs the release it was built with; the pin must move
    with every version bump (pyproject, __version__, metainfo)."""
    import lumux

    with open(os.path.join(ROOT, "pyproject.toml")) as f:
        project = re.search(r'^version = "(.*)"$', f.read(), re.MULTILINE).group(1)
    with open(os.path.join(ROOT, "data", "io.github.enginkirmaci.lumux.metainfo.xml")) as f:
        newest = re.search(r'<release version="([^"]+)"', f.read()).group(1)
    assert _version.LUMUX_VERSION == project == lumux.__version__ == newest
    assert _version.GITHUB_REPO == "sjredondo/lumux"


# -- release lookup ------------------------------------------------------------------

def test_release_lookup_uses_the_pinned_tag():
    fetch = _fetcher({
        f"{API}/tags/v0.8.5": _release("v0.8.5", "decky-lumux.zip", "lumux.flatpak"),
        f"{API}/latest": _release("v0.9.0", "lumux.flatpak"),
    })
    rel = asyncio.run(find_release(fetch, "0.8.5"))
    assert rel.pinned and rel.tag == "v0.8.5"
    assert rel.url == "https://dl.example/v0.8.5/lumux.flatpak"
    assert fetch.calls == [f"{API}/tags/v0.8.5"]  # latest never asked


def test_release_lookup_falls_back_when_the_tag_is_missing():
    fetch = _fetcher({f"{API}/latest": _release("v0.9.0", "lumux.flatpak")})
    rel = asyncio.run(find_release(fetch, "0.8.5"))
    assert not rel.pinned and rel.tag == "v0.9.0"
    assert fetch.calls == [f"{API}/tags/v0.8.5", f"{API}/latest"]


def test_release_lookup_falls_back_when_the_tag_has_no_bundle():
    fetch = _fetcher({
        f"{API}/tags/v0.8.5": _release("v0.8.5", "decky-lumux.zip"),
        f"{API}/latest": _release("v0.9.0", "decky-lumux.zip", "lumux.flatpak"),
    })
    rel = asyncio.run(find_release(fetch, "0.8.5"))
    assert not rel.pinned and rel.url.endswith("/v0.9.0/lumux.flatpak")


def test_release_lookup_prefers_the_exact_asset_name():
    fetch = _fetcher({f"{API}/tags/v0.8.5": _release("v0.8.5", "other.flatpak", "lumux.flatpak")})
    assert asyncio.run(find_release(fetch, "0.8.5")).url.endswith("/lumux.flatpak")


def test_release_lookup_without_any_bundle_fails():
    fetch = _fetcher({f"{API}/latest": _release("v0.9.0", "decky-lumux.zip")})
    with pytest.raises(InstallError, match="No lumux.flatpak"):
        asyncio.run(find_release(fetch, "0.8.5"))
    with pytest.raises(InstallError, match="no published releases"):
        asyncio.run(find_release(_fetcher({}), "0.8.5"))


def test_github_errors_become_readable(monkeypatch):
    def offline(req, timeout=None, context=None):
        raise urllib.error.URLError(OSError(-3, "Temporary failure in name resolution"))

    monkeypatch.setattr(inst.urllib.request, "urlopen", offline)
    with pytest.raises(InstallError, match="Couldn't reach GitHub.*online"):
        inst.http_get_json(f"{API}/latest")

    def status(code):
        def urlopen(req, timeout=None, context=None):
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, None)
        return urlopen

    monkeypatch.setattr(inst.urllib.request, "urlopen", status(404))
    with pytest.raises(NotFound):
        inst.http_get_json(f"{API}/tags/v0.0.1")
    monkeypatch.setattr(inst.urllib.request, "urlopen", status(403))
    with pytest.raises(InstallError, match="rate limit"):
        inst.http_get_json(f"{API}/latest")


# -- commands --------------------------------------------------------------------------

def test_flatpak_commands():
    assert Installer.remote_add_command("/usr/bin/flatpak") == [
        "/usr/bin/flatpak", "remote-add", "--user", "--if-not-exists", "flathub",
        "https://dl.flathub.org/repo/flathub.flatpakrepo"]
    assert Installer.install_command("/usr/bin/flatpak", "/tmp/x/lumux.flatpak") == [
        "/usr/bin/flatpak", "install", "--user", "-y", "--noninteractive", "/tmp/x/lumux.flatpak"]
    assert Installer.install_command("flatpak", "b.flatpak", update=True) == [
        "flatpak", "install", "--user", "-y", "--noninteractive", "--reinstall", "b.flatpak"]


def test_run_streaming_splits_progress_lines():
    seen = []
    code, lines = asyncio.run(inst.run_streaming(
        ["sh", "-c", "printf 'Installing 1/2\\r50%%\\rdone\\nerror: boom\\n' >&2; exit 3"],
        dict(os.environ), seen.append))
    assert code == 3
    assert seen == lines == ["Installing 1/2", "50%", "done", "error: boom"]
    assert inst.flatpak_error("flatpak install failed", lines) == "flatpak install failed: error: boom"


# -- the install task ---------------------------------------------------------------------

class FakeFlatpak:
    """Records commands; fails the ones whose argv[1] is in `fail`."""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail or {}

    async def __call__(self, argv, env, on_line):
        self.calls.append((list(argv), env))
        if argv[1] == "install":
            assert os.path.getsize(argv[-1]) > 0  # the downloaded bundle
            on_line("Installing app/io.github.enginkirmaci.lumux/x86_64/master")
        out = self.fail.get(argv[1])
        if out:
            for line in out:
                on_line(line)
            return 1, list(out)
        return 0, []


def _installer(tmp_path, flatpak=None, fetch=None, download=None, euid=1000, **kw):
    pyinstaller_env = {"LD_LIBRARY_PATH": "/tmp/_MEI42", "PYTHONHOME": "/tmp/_MEI42",
                       "PATH": "/tmp/_MEI42", "XDG_DATA_HOME": "/root/.local/share"}
    events = []

    async def default_download(url, dest, progress):
        events.append(("download", url))
        with open(dest, "wb") as f:
            f.write(b"x" * 1000)
        progress(500, 1000)
        events.append(("progress", ins.snapshot()))
        progress(1000, 1000)

    async def before():
        events.append(("before",))

    async def after(ok):
        events.append(("after", ok))

    ins = Installer(
        env=lambda: child_env(str(tmp_path / "home"), 1000, base=pyinstaller_env),
        which=lambda name, path=None: f"/usr/bin/{name}",
        version="0.8.5",
        fetch_json=fetch or _fetcher({f"{API}/tags/v0.8.5": _release("v0.8.5", "lumux.flatpak")}),
        download=download or default_download,
        run=flatpak or FakeFlatpak(),
        before_install=before, after_install=after,
        tmp_root=str(tmp_path), euid=lambda: euid, **kw,
    )
    return ins, events


def test_install_happy_path(tmp_path):
    flatpak = FakeFlatpak()
    ins, events = _installer(tmp_path, flatpak=flatpak)

    async def go():
        assert ins.snapshot()["state"] == "idle"
        assert ins.start() is True
        assert ins.snapshot()["state"] == "checking" and ins.busy
        await ins.wait()

    asyncio.run(go())
    snap = ins.snapshot()
    assert snap["state"] == "done" and not snap["busy"], snap
    assert snap["tag"] == "v0.8.5" and snap["pinned"] and snap["progress"] == 1.0
    assert snap["message"] == "Lumux v0.8.5 installed"
    mid = [e[1] for e in events if e[0] == "progress"][0]
    assert mid["state"] == "downloading" and mid["progress"] == 0.5 and "0 / 0 MB" in mid["message"]

    (remote, env1), (install, env2) = flatpak.calls
    assert remote[:2] == ["/usr/bin/flatpak", "remote-add"] and "--user" in remote
    assert install[:-1] == ["/usr/bin/flatpak", "install", "--user", "-y", "--noninteractive"]
    assert install[-1].endswith("/lumux.flatpak")
    for env in (env1, env2):  # sanitized: Decky's PyInstaller paths would break flatpak
        assert "PYTHONHOME" not in env and "LD_LIBRARY_PATH" not in env
        assert "XDG_DATA_HOME" not in env  # root's: would hide the --user installation
        assert env["HOME"] == str(tmp_path / "home") and "/usr/bin" in env["PATH"].split(":")
    # the old daemon is stopped right before installing and restarted after
    assert [e[0] for e in events] == ["download", "progress", "before", "after"]
    assert events[-1] == ("after", True)
    assert not [p for p in os.listdir(tmp_path) if p.startswith("decky-lumux-")]  # temp dir gone
    assert snap["detail"].startswith("Installing app/")


def test_update_reinstalls(tmp_path):
    flatpak = FakeFlatpak()
    ins, _ = _installer(tmp_path, flatpak=flatpak)

    async def go():
        ins.start(update=True)
        await ins.wait()

    asyncio.run(go())
    assert "--reinstall" in flatpak.calls[-1][0]
    assert ins.snapshot()["message"] == "Lumux v0.8.5 updated"


def test_concurrent_clicks_start_one_install(tmp_path):
    gate = asyncio.Event
    fetch_calls = []

    async def go():
        release_gate = gate()
        base_fetch = _fetcher({f"{API}/tags/v0.8.5": _release("v0.8.5", "lumux.flatpak")})

        async def slow_fetch(url):
            fetch_calls.append(url)
            await release_gate.wait()
            return await base_fetch(url)

        ins, _ = _installer(tmp_path, fetch=slow_fetch)
        assert ins.start() is True
        await asyncio.sleep(0)
        assert ins.start() is False and ins.start(update=True) is False
        assert ins.snapshot()["mode"] == "install"  # the second click didn't change it
        release_gate.set()
        await ins.wait()
        assert ins.snapshot()["state"] == "done"
        assert ins.start() is True  # finished: a new run is allowed again
        await ins.wait()

    asyncio.run(go())
    assert len(fetch_calls) == 2


def test_no_network(tmp_path):
    fetch = _fetcher({f"{API}/tags/v0.8.5": InstallError(
        "Couldn't reach GitHub (Temporary failure in name resolution). Check that the Deck is online.")})
    flatpak = FakeFlatpak()
    ins, events = _installer(tmp_path, flatpak=flatpak, fetch=fetch)

    async def go():
        ins.start()
        await ins.wait()

    asyncio.run(go())
    snap = ins.snapshot()
    assert snap["state"] == "error" and not snap["busy"]
    assert "Couldn't reach GitHub" in snap["error"]
    assert flatpak.calls == [] and events == []  # daemon untouched


def test_download_failure(tmp_path):
    async def broken(url, dest, progress):
        raise InstallError("Download was cut short. Check the connection and try again.")

    flatpak = FakeFlatpak()
    ins, events = _installer(tmp_path, flatpak=flatpak, download=broken)

    async def go():
        ins.start()
        await ins.wait()

    asyncio.run(go())
    assert ins.state == "error" and "cut short" in ins.error
    assert flatpak.calls == [] and events == []
    assert not [p for p in os.listdir(tmp_path) if p.startswith("decky-lumux-")]


def test_flatpak_install_failure_shows_flatpak_error(tmp_path):
    flatpak = FakeFlatpak(fail={"install": [
        "Looking for matches…",
        "error: Unable to load summary from remote flathub: Could not resolve hostname",
    ]})
    ins, events = _installer(tmp_path, flatpak=flatpak)

    async def go():
        ins.start()
        await ins.wait()

    asyncio.run(go())
    assert ins.state == "error"
    assert ins.error.startswith("flatpak install failed: error: Unable to load summary")
    assert ins.error.endswith("Check that the Deck is online.")
    assert events[-1] == ("after", False)  # the old daemon is brought back


def test_remote_add_failure(tmp_path):
    flatpak = FakeFlatpak(fail={"remote-add": ["error: Can't write to /home/deck/.local/share/flatpak"]})
    ins, events = _installer(tmp_path, flatpak=flatpak)

    async def go():
        ins.start()
        await ins.wait()

    asyncio.run(go())
    assert ins.error == "Couldn't add the Flathub remote: error: Can't write to /home/deck/.local/share/flatpak"
    assert len(flatpak.calls) == 1 and ("before",) not in events


def test_refuses_to_run_as_root(tmp_path):
    flatpak = FakeFlatpak()
    ins, _ = _installer(tmp_path, flatpak=flatpak, euid=0)

    async def go():
        ins.start()
        await ins.wait()

    asyncio.run(go())
    assert ins.state == "error" and "root" in ins.error and flatpak.calls == []


def test_fallback_release_is_reported(tmp_path):
    fetch = _fetcher({f"{API}/latest": _release("v0.9.0", "lumux.flatpak")})
    ins, _ = _installer(tmp_path, fetch=fetch)

    async def go():
        ins.start()
        await ins.wait()

    asyncio.run(go())
    snap = ins.snapshot()
    assert snap["state"] == "done" and snap["tag"] == "v0.9.0" and snap["pinned"] is False


def test_cancel_stops_the_task(tmp_path):
    async def go():
        async def hang(url, dest, progress):
            await asyncio.sleep(60)

        ins, events = _installer(tmp_path, download=hang)
        ins.start()
        await asyncio.sleep(0.05)
        assert ins.snapshot()["state"] == "downloading"
        await ins.cancel()
        return ins, events

    ins, events = asyncio.run(go())
    assert not ins.busy and ins.state == "error" and events == []


# -- supervisor: detection, runner and what the panel offers ---------------------------------

def _fake_flatpak_info(monkeypatch, user=None, system=None):
    """flatpak info --user/--system fake: a version string means installed."""
    calls = []

    class _Proc:
        def __init__(self, version):
            self.returncode = 0 if version is not None else 1
            self.version = version

        async def communicate(self):
            if self.version is None:
                return b"", b"error: app/io.github.enginkirmaci.lumux/x86_64/master not installed"
            line = f"     Version: {self.version}\n" if self.version else ""
            return (f"Lumux - Hue sync\n\n          ID: io.github.enginkirmaci.lumux\n{line}"
                    "      Origin: lumux-origin\n").encode(), b""

    async def fake_exec(*argv, **kw):
        calls.append((argv, kw.get("env", {})))
        return _Proc(user if "--user" in argv else system)

    monkeypatch.setattr(supmod.asyncio, "create_subprocess_exec", fake_exec)
    return calls


def _sup(tmp_path, **kw):
    return Supervisor(settings_dir=str(tmp_path / "s"), log_dir=str(tmp_path / "l"),
                      home=str(tmp_path), uid=1000, sock_path=str(tmp_path / "d.sock"),
                      which=lambda name, path=None: f"/usr/bin/{name}", **kw)


def test_detects_user_and_system_installs(tmp_path, monkeypatch):
    calls = _fake_flatpak_info(monkeypatch, user="0.8.4", system="0.7.0")
    sup = _sup(tmp_path)
    assert asyncio.run(sup.flatpak_installed()) is True
    assert sup.flatpak_versions == {"user": "0.8.4", "system": "0.7.0"}
    assert [a[2] for a, _ in calls] == ["--user", "--system"]
    assert all(env["LC_ALL"] == "C" for _, env in calls)  # "Version:" isn't translated
    # both installed: run ours (the user install) explicitly
    assert sup.build_command("flatpak")[:4] == [
        "/usr/bin/flatpak", "run", "--user", "--command=lumux-daemon"]


def test_system_only_install_runs_without_user_flag(tmp_path, monkeypatch):
    _fake_flatpak_info(monkeypatch, system="")
    sup = _sup(tmp_path)
    assert asyncio.run(sup.flatpak_installed()) is True
    assert sup.flatpak_versions == {"user": None, "system": ""}
    assert sup.build_command("flatpak")[:3] == ["/usr/bin/flatpak", "run", "--command=lumux-daemon"]


@pytest.mark.parametrize("user,system,expected", [
    (None, None, "install"),
    ("0.8.4", None, "update"),
    ("0.8.5", None, ""),
    ("0.9.0", None, ""),
    ("", None, ""),  # version unknown and working: don't nag
    (None, "0.7.0", "install"),  # old system-wide copy: add a user install
    (None, "0.8.5", ""),
    ("0.8.4", "0.9.0", "update"),  # the user install is the one we run
])
def test_install_action(tmp_path, monkeypatch, user, system, expected):
    monkeypatch.setattr(supmod, "LUMUX_VERSION", "0.8.5")
    _fake_flatpak_info(monkeypatch, user=user, system=system)
    sup = _sup(tmp_path)
    assert sup.install_action() == ""  # nothing known before the first check
    asyncio.run(sup.flatpak_installed())
    info = sup.lumux_info()
    assert info["action"] == expected
    assert (info["user"], info["system"], info["pinned"]) == (user, system, "0.8.5")


def test_install_action_for_a_flatpak_without_daemon(tmp_path, monkeypatch):
    monkeypatch.setattr(supmod, "LUMUX_VERSION", "0.8.5")
    _fake_flatpak_info(monkeypatch, user="")
    sup = _sup(tmp_path)
    asyncio.run(sup.flatpak_installed())
    sup.state, sup.error = "crashed", sup._explain_exit(1, "bwrap: execvp lumux-daemon: No such file")
    assert "Update Lumux" in sup.error and sup.install_action() == "update"


def test_no_install_offer_for_other_runners(tmp_path, monkeypatch):
    _fake_flatpak_info(monkeypatch)
    sup = _sup(tmp_path)
    asyncio.run(sup.flatpak_installed())
    assert sup.install_action() == "install"
    sup.set_setting("runner", "host")
    assert sup.install_action() == ""


def test_after_install_restarts_and_resumes_sync(tmp_path, monkeypatch):
    sup = _sup(tmp_path)
    sent = []

    async def fake_request(sock, cmd, timeout=10.0, **args):
        sent.append(cmd)
        return {"syncing": True}

    async def fake_restart():
        sent.append("restart")
        assert sup._flatpak_cache is None  # detection is redone
        return True

    class _Running:
        returncode = None

    monkeypatch.setattr(supmod.client, "request", fake_request)
    monkeypatch.setattr(sup, "restart", fake_restart)
    monkeypatch.setattr(sup, "stop", lambda: asyncio.sleep(0))
    sup.proc = _Running()
    sup._flatpak_cache = True

    async def go():
        await sup._before_install()
        await sup._after_install(True)

    asyncio.run(go())
    assert sent == ["status", "restart", "start"]


def test_plugin_install_callable(tmp_path, monkeypatch):
    from test_decky_backend import _load_plugin

    main = _load_plugin(tmp_path, monkeypatch)
    plugin = main.Plugin()

    async def go():
        await plugin._main()
        ins = plugin.supervisor.installer
        gate = asyncio.Event()

        async def fetch(url):
            await gate.wait()
            raise InstallError("offline")

        ins._fetch_json = fetch
        ins._euid = lambda: 1000  # the container runs tests as root
        ins._which = lambda name, path=None: "/usr/bin/flatpak"
        first = await plugin.install_lumux(False)
        second = await plugin.install_lumux(True)
        assert first["ok"] and first["result"]["started"] is True
        assert second["ok"] and second["result"]["started"] is False
        status = await plugin.get_status()
        assert status["install"]["busy"] and status["install"]["mode"] == "install"
        assert status["lumux"]["pinned"] == _version.LUMUX_VERSION
        gate.set()
        await ins.wait()
        status = await plugin.get_status()
        assert status["install"]["state"] == "error" and status["install"]["error"] == "offline"
        await plugin._unload()

    asyncio.run(go())
