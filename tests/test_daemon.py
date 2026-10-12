"""lumux-daemon control socket (JSON lines over a Unix socket)."""

import json
import os
import signal
import socket
import subprocess
import sys
import time

import pytest

import fake_bridge as fb
import fake_gamescope as fg


def _settings(path, **hue):
    data = {"hue": {"bridge_ip": "", "app_key": "", "client_key": "",
                    "entertainment_config_id": "", **hue},
            "sync": {"fps": 30, "brightness_scale": 1.0}}
    (path / "settings.json").write_text(json.dumps(data))


def _load(path):
    return json.loads((path / "settings.json").read_text())


class Daemon:
    def __init__(self, tmp_path, *extra):
        self.config = tmp_path / "config"
        self.config.mkdir(exist_ok=True)
        self.sock = str(tmp_path / "run" / "daemon.sock")
        self.log = open(tmp_path / "daemon.log", "w+")
        self.argv = [sys.executable, "-m", "lumux.daemon", "--socket", self.sock,
                     "--config-dir", str(self.config), "--backend", "gamescope", *extra]
        self.proc = None

    def start(self):
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
        self.proc = subprocess.Popen(self.argv, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("daemon exited:\n" + self.output())
            try:
                if self.call("ping")["ok"]:
                    return self
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("daemon did not come up:\n" + self.output())

    def output(self):
        self.log.flush()
        self.log.seek(0)
        return self.log.read()

    def call(self, cmd, sock_timeout=15, **args):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(sock_timeout)
            s.connect(self.sock)
            s.sendall((json.dumps({"id": 7, "cmd": cmd, **args}) + "\n").encode())
            return json.loads(s.makefile().readline())

    def result(self, cmd, **args):
        reply = self.call(cmd, **args)
        assert reply["ok"], reply
        return reply["result"]

    def wait_for(self, pred, timeout=15):
        deadline = time.time() + timeout
        status = None
        while time.time() < deadline:
            status = self.result("status")
            if pred(status):
                return status
            time.sleep(0.2)
        raise AssertionError(f"timed out; last status {status}\n{self.output()}")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.call("shutdown")
            except OSError:
                pass
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log.close()


@pytest.fixture
def daemon(tmp_path):
    d = Daemon(tmp_path)
    _settings(d.config)
    d.start()
    yield d
    d.stop()


def test_ping_and_unpaired_status(daemon):
    pong = daemon.result("ping")
    assert pong["pong"] and pong["protocol"] == 1
    status = daemon.result("status")
    assert status["state"] == "unpaired" and not status["paired"]
    assert status["capture"]["backend"] == "gamescope"
    assert status["capture"]["node"] == "gamescope"
    assert status["settings_file"] == str(daemon.config / "settings.json")


def test_start_without_pairing_explains_what_to_do(daemon):
    reply = daemon.call("start")
    assert not reply["ok"] and "Not paired" in reply["error"]
    assert daemon.call("list_areas")["ok"] is False


def test_bad_requests_get_errors_not_crashes(daemon):
    assert "unknown command" in daemon.call("frobnicate")["error"]
    assert "numeric" in daemon.call("set_brightness", value="loud")["error"]
    assert "area_id" in daemon.call("select_area")["error"]
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(daemon.sock)
        f = s.makefile("rw")
        f.write("{not json\n[1,2]\n" + json.dumps({"id": 3, "cmd": "ping"}) + "\n")
        f.flush()
        replies = [json.loads(f.readline()) for _ in range(3)]
    assert "invalid JSON" in replies[0]["error"]
    assert "JSON object" in replies[1]["error"]
    assert replies[2]["ok"] and replies[2]["id"] == 3  # same connection still works
    assert daemon.proc.poll() is None


def test_set_brightness_is_clamped_and_saved(daemon):
    assert daemon.result("set_brightness", value=0.4)["brightness"] == 0.4
    assert _load(daemon.config)["sync"]["brightness_scale"] == 0.4
    assert daemon.result("set_brightness", value=9)["brightness"] == 2.0
    assert daemon.result("status")["brightness"] == 2.0


def test_set_node_overrides_and_persists(daemon):
    daemon.result("set_node", node="77")
    assert daemon.result("status")["capture"]["node"] == "77"
    assert _load(daemon.config)["capture"]["pipewire_node"] == "77"
    # the desktop app's capture backend is not overwritten by --backend
    assert _load(daemon.config)["capture"]["backend"] == "auto"


def test_socket_is_private_and_single_instance(daemon, tmp_path):
    assert oct(os.stat(daemon.sock).st_mode & 0o777) == "0o600"
    second = Daemon(tmp_path)
    second.sock = daemon.sock
    second.argv[second.argv.index("--socket") + 1] = daemon.sock
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    out = subprocess.run(second.argv, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode != 0 and "already running" in (out.stdout + out.stderr)
    second.log.close()
    assert daemon.result("ping")["pong"]  # first one unaffected


def test_shutdown_and_sigterm_clean_up(tmp_path):
    d = Daemon(tmp_path)
    _settings(d.config)
    d.start()
    assert d.result("shutdown")["shutting_down"]
    assert d.proc.wait(timeout=15) == 0
    assert not os.path.exists(d.sock)

    d2 = Daemon(tmp_path)
    d2.start()  # stale-free restart on the same path
    d2.proc.send_signal(signal.SIGTERM)
    assert d2.proc.wait(timeout=15) == 0
    assert not os.path.exists(d2.sock)
    d.log.close()
    d2.log.close()


def test_ctl_client(daemon):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    out = subprocess.run([sys.executable, "-m", "lumux.daemon", "--socket", daemon.sock,
                          "--ctl", "set_brightness", "--arg", "value=0.5"],
                         env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["brightness"] == 0.5


# -- against the fake Hue bridge ---------------------------------------------

needs_bridge = pytest.mark.skipif(not fb.can_run(), reason="needs root + openssl (run in container)")


@pytest.fixture
def bridge_srv():
    srv = fb.FakeBridge().start()
    time.sleep(0.5)
    yield srv
    srv.stop()


@needs_bridge
def test_areas_start_stop(tmp_path, bridge_srv):
    pw = None
    if fg.can_run():
        pw = fg.PipeWireSession().start()
        pw.add_fake_gamescope(rgb=(255, 0, 0))
    d = Daemon(tmp_path)
    _settings(d.config, bridge_ip="127.0.0.1", app_key=fb.APP_KEY, client_key=fb.CLIENT_KEY)
    try:
        d.start()
        areas = d.result("list_areas")
        assert areas["areas"] == [{"id": fb.ENT_ID, "name": "TV area", "channels": 2,
                                   "status": "inactive"}]
        assert areas["selected"] == ""
        assert "No entertainment area" in d.call("start")["error"]

        assert d.result("select_area", area_id=fb.ENT_ID) == {"selected": fb.ENT_ID, "restarted": False}
        assert _load(d.config)["hue"]["entertainment_config_id"] == fb.ENT_ID

        assert d.result("start")["state"] in ("starting", "syncing")
        status = d.wait_for(lambda s: s["state"] == "syncing" and s["streaming"])
        assert status["bridge"]["connected"] and status["mode"] == "video"
        before = bridge_srv.frames
        time.sleep(1.0)
        assert bridge_srv.frames > before + 10  # ~30 Hz HueStream frames
        if pw:
            status = d.wait_for(lambda s: s["capture"]["capturing"])
            assert status["capture"]["error"] == ""

        assert d.result("stop")["state"] == "idle"
        status = d.wait_for(lambda s: not s["syncing"])
        assert not status["streaming"]
        assert bridge_srv.stream_actions()[:1] == ["start"]
        assert "stop" in bridge_srv.stream_actions()
    finally:
        d.stop()
        if pw:
            pw.stop()


@needs_bridge
def test_pair_with_link_button(tmp_path, bridge_srv):
    d = Daemon(tmp_path)
    _settings(d.config, bridge_ip="127.0.0.1")
    try:
        d.start()
        reply = d.call("pair", sock_timeout=30, timeout=2)
        assert not reply["ok"] and "link button" in reply["error"]

        bridge_srv.link_pressed = True
        result = d.result("pair", timeout=5)
        assert result["paired"] and result["bridge_ip"] == "127.0.0.1"
        saved = _load(d.config)["hue"]
        assert saved["app_key"] == fb.APP_KEY and saved["client_key"] == fb.CLIENT_KEY
        # the bridge has exactly one entertainment area, so it is preselected
        assert saved["entertainment_config_id"] == fb.ENT_ID
        status = d.result("status", refresh=True)
        assert status["paired"] and status["bridge"]["connected"]
    finally:
        d.stop()
