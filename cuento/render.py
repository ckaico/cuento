"""Render one layer, alone, to an alpha file.

A layer is an HTML document on a 1920x1080 transparent stage. Headless Chrome
steps it frame by frame (animations are seeked, never played in real time) and
each frame is screenshotted with a transparent background, then encoded:

  preview  960x540 WebM VP9 with alpha, for the browser compositor
  master   ProRes 4444 at full stage size (or larger), for export

Chrome is driven over the DevTools pipe, so there is nothing to install beyond
Chrome itself.
"""
import base64
import fcntl
import json
import os
import shutil
import subprocess
import tempfile

from .project import FPS_DEN, FPS_NUM, STAGE

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]

# Layers are self-contained by contract. This keeps them off the network, which also keeps renders repeatable.
LAYER_CSP = ("<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'unsafe-inline'; "
             "style-src 'unsafe-inline'; img-src data: blob:; font-src data:; media-src data: blob:\">")

RUNTIME_JS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui", "layer-runtime.js")


class RenderError(Exception):
    pass


def find_chrome():
    env = os.environ.get("CUENTO_CHROME")
    for c in ([env] if env else []) + CHROME_CANDIDATES:
        if os.path.exists(c):
            return c
    raise RenderError("no Chrome found; set CUENTO_CHROME to a Chromium-based browser binary")


def layer_page(layer):
    """The layer's HTML with the timing runtime in front. Same page for live preview and render."""
    with open(RUNTIME_JS) as f:
        runtime = f.read()
    meta = {"frames": layer["dur"], "dur": layer["dur"] * FPS_DEN / FPS_NUM, "fps": FPS_NUM / FPS_DEN,
            "width": STAGE[0], "height": STAGE[1], "z": layer.get("z")}
    return (f"<!doctype html><meta charset=\"utf-8\">{LAYER_CSP}<script>window.cuento={json.dumps(meta)};</script>"
            f"<script>{runtime}</script>\n{layer['source']}")


class Chrome:
    def __init__(self):
        cmd_r, cmd_w = os.pipe()
        out_r, out_w = os.pipe()
        # the browser expects commands on fd 3 and answers on fd 4
        child_r = fcntl.fcntl(cmd_r, fcntl.F_DUPFD, 10)
        child_w = fcntl.fcntl(out_w, fcntl.F_DUPFD, 10)
        self.profile = tempfile.mkdtemp(prefix="cuento-chrome-")
        args = ["--headless=new", "--remote-debugging-pipe", f"--user-data-dir={self.profile}", "--no-first-run",
                "--no-default-browser-check", "--hide-scrollbars", "--mute-audio",
                f"--window-size={STAGE[0]},{STAGE[1]}", "--force-device-scale-factor=1", "about:blank"]
        self.proc = subprocess.Popen(["/bin/sh", "-c", f'exec "$0" "$@" 3<&{child_r} 4>&{child_w}', find_chrome(), *args],
                                     pass_fds=(child_r, child_w), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for fd in (cmd_r, out_w, child_r, child_w):
            os.close(fd)
        self.w, self.r = cmd_w, out_r
        self.buf = b""
        self.next_id = 0
        self.events = []
        self.session = None

    def _read(self):
        while b"\0" not in self.buf:
            chunk = os.read(self.r, 1 << 20)
            if not chunk:
                raise RenderError("Chrome closed the pipe")
            self.buf += chunk
        raw, self.buf = self.buf.split(b"\0", 1)
        return json.loads(raw)

    def call(self, method, **params):
        self.next_id += 1
        msg = {"id": self.next_id, "method": method, "params": params}
        if self.session:
            msg["sessionId"] = self.session
        os.write(self.w, json.dumps(msg).encode() + b"\0")
        while True:
            m = self._read()
            if m.get("id") == self.next_id:
                if "error" in m:
                    raise RenderError(f"{method}: {m['error'].get('message')}")
                return m["result"]
            self.events.append(m)

    def wait_event(self, name):
        while True:
            for i, m in enumerate(self.events):
                if m.get("method") == name:
                    del self.events[: i + 1]
                    return m
            self.events.append(self._read())

    def evaluate(self, expr):
        r = self.call("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            raise RenderError(f"layer script error: {d.get('exception', {}).get('description') or d.get('text')}")
        return r["result"].get("value")

    def open(self, url=None, html=None):
        target = self.call("Target.createTarget", url="about:blank")["targetId"]
        self.session = self.call("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
        self.call("Page.enable")
        self.call("Emulation.setDeviceMetricsOverride", width=STAGE[0], height=STAGE[1], deviceScaleFactor=1, mobile=False)
        self.call("Emulation.setDefaultBackgroundColorOverride", color={"r": 0, "g": 0, "b": 0, "a": 0})
        if url:
            self.call("Page.navigate", url=url)
            self.wait_event("Page.loadEventFired")
        else:
            frame = self.call("Page.getFrameTree")["frameTree"]["frame"]["id"]
            self.call("Page.setDocumentContent", frameId=frame, html=html)
        self.evaluate("document.fonts.ready.then(() => true)")

    def close(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        for fd in (self.w, self.r):
            try:
                os.close(fd)
            except OSError:
                pass
        shutil.rmtree(self.profile, ignore_errors=True)


def _encoder(kind, out_file, scale):
    w, h = round(STAGE[0] * scale), round(STAGE[1] * scale)
    head = ["ffmpeg", "-v", "error", "-y", "-f", "image2pipe", "-framerate", f"{FPS_NUM}/{FPS_DEN}",
            "-c:v", "png", "-i", "-", "-vf", f"scale={w}:{h}:flags=lanczos", "-an"]
    if kind == "master":
        tail = ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", "-vendor", "apl0", "-f", "mov"]
    else:
        tail = ["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-auto-alt-ref", "0", "-b:v", "0", "-crf", "30",
                "-g", "12", "-deadline", "good", "-cpu-used", "5", "-row-mt", "1", "-f", "webm"]
    return subprocess.Popen(head + tail + [out_file], stdin=subprocess.PIPE, stderr=subprocess.PIPE)


def render_layer(layer, out_file, kind="preview", url=None, progress=None):
    """Render `layer` (dict with source + dur) to out_file. Returns the frame count."""
    scale = 0.5 if kind == "preview" else 1
    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    tmp = f"{out_file}.part"
    chrome = Chrome()
    enc = _encoder(kind, tmp, scale)
    try:
        chrome.open(url=url, html=None if url else layer_page(layer))
        if chrome.evaluate("typeof window.cuento?.seek") != "function":
            raise RenderError("layer runtime missing")
        clip = {"x": 0, "y": 0, "width": STAGE[0], "height": STAGE[1], "scale": scale}
        for f in range(layer["dur"]):
            chrome.evaluate(f"window.cuento.seek({f * FPS_DEN / FPS_NUM!r}); new Promise(r => requestAnimationFrame(() => r(true)))")
            shot = chrome.call("Page.captureScreenshot", format="png", clip=clip, captureBeyondViewport=False)
            enc.stdin.write(base64.b64decode(shot["data"]))
            if progress and f % 12 == 0:
                progress(f, layer["dur"])
        enc.stdin.close()
        if enc.wait() != 0:
            raise RenderError(f"encode failed: {enc.stderr.read().decode()[-400:]}")
        os.replace(tmp, out_file)
        return layer["dur"]
    finally:
        chrome.close()
        if enc.poll() is None:
            enc.kill()
        if os.path.exists(tmp):
            os.remove(tmp)
