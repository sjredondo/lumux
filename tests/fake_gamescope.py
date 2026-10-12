"""A private PipeWire session with a fake gamescope video node.

gamescope publishes its composited output as a PipeWire ``Video/Source``
node named ``gamescope``. Here ``pipewiresink mode=provide`` publishes a
solid-colour test pattern under the same name, so the gamescope capture
backend can be exercised without a Steam Deck.

Needs pipewire, wireplumber and gst-plugin-pipewire (the Arch container
in the README installs them); tests skip otherwise.
"""

import os
import shutil
import subprocess
import tempfile
import time


def can_run() -> bool:
    return all(shutil.which(b) for b in ("pipewire", "wireplumber", "pw-cli", "gst-launch-1.0"))


class PipeWireSession:
    def __init__(self):
        self.runtime_dir = tempfile.mkdtemp(prefix="lumux-xdg")
        os.chmod(self.runtime_dir, 0o700)
        self._procs = []
        self._old_env = {}

    def _spawn(self, *argv):
        env = dict(os.environ, XDG_RUNTIME_DIR=self.runtime_dir)
        proc = subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._procs.append(proc)
        return proc

    def _nodes(self) -> str:
        env = dict(os.environ, XDG_RUNTIME_DIR=self.runtime_dir)
        try:
            return subprocess.run(["pw-cli", "ls", "Node"], env=env, capture_output=True,
                                  text=True, timeout=10).stdout
        except subprocess.TimeoutExpired:
            return ""

    def start(self):
        self._old_env["XDG_RUNTIME_DIR"] = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = self.runtime_dir
        self._spawn("pipewire")
        self._wait(lambda: os.path.exists(os.path.join(self.runtime_dir, "pipewire-0")), "pipewire")
        self._spawn("wireplumber")
        self._wait(lambda: "Dummy-Driver" in self._nodes() or "Freewheel" in self._nodes(), "wireplumber")
        return self

    def add_fake_gamescope(self, rgb=(255, 128, 0), name="gamescope", description=None,
                           width=1280, height=800):
        props = f"props,node.name={name},media.class=Video/Source"
        if description:
            props += f",node.description=\"{description}\""
        colour = 0xFF000000 | (rgb[0] << 16) | (rgb[1] << 8) | rgb[2]
        self._spawn(
            "gst-launch-1.0", "-q", "videotestsrc", "pattern=solid-color",
            f"foreground-color={colour}", "!",
            f"video/x-raw,format=BGRx,width={width},height={height},framerate=30/1", "!",
            "pipewiresink", "mode=provide", f"stream-properties={props}",
        )
        self._wait(lambda: f'node.name = "{name}"' in self._nodes(), f"node {name}")

    @staticmethod
    def _wait(cond, what, timeout=20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return
            time.sleep(0.2)
        raise RuntimeError(f"{what} did not come up")

    def stop(self):
        for proc in reversed(self._procs):
            proc.terminate()
        for proc in self._procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        old = self._old_env.get("XDG_RUNTIME_DIR")
        if old is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = old
        shutil.rmtree(self.runtime_dir, ignore_errors=True)
