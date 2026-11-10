"""cuentod: the local daemon. Owns media, caches and render workers; serves the
editor UI and a JSON API that the UI and the MCP server share."""
import json
import mimetypes
import os
import queue
import re
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import media, render
from .project import HOME, OpError, layout, open_project, registry, timecode

PORT = int(os.environ.get("CUENTO_PORT", "47821"))
UI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}

_render_slot = threading.Semaphore(1)
_render_jobs = {}  # hash -> {"status": queued|rendering|ready|error, "error": str, "done": Event}
_render_guard = threading.Lock()
_probed = {}


def source_file(p, sid, kind="proxy"):
    src = p.doc["sources"].get(sid)
    if not src:
        raise OpError(f"no source {sid}")
    if kind == "cam":
        return os.path.join(p.cache, f"{sid}.cam.m4a")
    return src["proxy"]


def media_status(p):
    out = {}
    for sid, src in p.doc["sources"].items():
        frames = src.get("frames")
        key = (src["proxy"], os.path.getmtime(src["proxy"]) if os.path.exists(src["proxy"]) else 0)
        if key not in _probed:
            # a proxy can still be encoding; keep asking until it reads as a real file
            n = media.probe_frames(src["proxy"])
            if n:
                _probed[key] = n
                if n != frames:
                    p.set_source_frames(sid, n)
                    frames = n
        ready = key in _probed
        out[sid] = {"frames": frames, "ready": ready, "cam_audio": os.path.exists(source_file(p, sid, "cam"))}
    return out


def queue_render(p, layer, wait=False):
    """Render one layer if its content hash has no file yet. Nothing else is touched."""
    from .project import layer_hash
    h = layer_hash(layer)
    out = p.render_path(layer)
    with _render_guard:
        job = _render_jobs.get(h)
        if os.path.exists(out):
            job = {"status": "ready", "done": threading.Event()}
            job["done"].set()
        elif job is None or job["status"] == "error":
            job = _render_jobs[h] = {"status": "queued", "done": threading.Event()}
            snapshot = json.loads(json.dumps(layer))

            def work():
                with _render_slot:
                    job["status"] = "rendering"
                    p.notify({"type": "render", "layer": layer["id"], "hash": h, "status": "rendering"})
                    try:
                        render.render_layer(snapshot, out)
                        job["status"] = "ready"
                    except Exception as e:
                        job["status"], job["error"] = "error", str(e)
                    job["done"].set()
                    p.notify({"type": "render", "layer": layer["id"], "hash": h, "status": job["status"],
                              "error": job.get("error")})

            threading.Thread(target=work, daemon=True).start()
    if wait:
        job["done"].wait(timeout=600)
    return {"id": layer["id"], "hash": h, "status": job["status"], "error": job.get("error"),
            "file": out if job["status"] == "ready" else None}


def queue_missing_renders(p):
    for layer in p.doc["layers"]:
        queue_render(p, layer)


def render_states(p):
    from .project import layer_hash
    out = {}
    for l in p.doc["layers"]:
        h = layer_hash(l)
        job = _render_jobs.get(h, {})
        ready = os.path.exists(p.render_path(l))
        out[l["id"]] = {"hash": h, "ready": ready, "status": "ready" if ready else job.get("status", "queued"),
                        "error": job.get("error")}
    return out


def full_state(p):
    s = p.state()
    s["media"] = media_status(p)
    s["renders"] = render_states(p)
    return s


def timeline_view(p, start=None, end=None):
    """Compact timeline for Claude: one row per clip, frames and timecodes."""
    items, total = layout(p.doc)
    rows = []
    for i, (it, s, d) in enumerate(items):
        if (start is not None and s + d <= start) or (end is not None and s >= end):
            continue
        row = {"index": i, "id": it["id"], "type": it["type"], "start": s, "dur": d, "tc": timecode(s)}
        if it["type"] == "block":
            row.update(source=it["source"], **{"in": it["in"], "out": it["out"]}, audio=it.get("audio", "lav"),
                       section=it.get("section", ""), text=it.get("text", ""))
        rows.append(row)
    return {"project": p.name, "rev": p.rev, "fps": "24000/1001", "total_frames": total, "total_tc": timecode(total),
            "playhead": p.ui.get("playhead", 0), "marks": p.ui.get("marks"), "clips": rows,
            "layers": [{k: l[k] for k in ("id", "name", "start", "dur", "z", "comment_id")} for l in p.doc["layers"]],
            "audio_fx": p.doc["audio_fx"],
            "open_comments": sum(1 for c in p.doc["comments"] if c["status"] == "open")}


def transcript_search(p, query):
    q = query.lower()
    hits = []
    for it, s, d in layout(p.doc)[0]:
        if it["type"] == "block" and q in it.get("text", "").lower():
            hits.append({"id": it["id"], "start": s, "dur": d, "tc": timecode(s), "source": it["source"],
                         "in": it["in"], "out": it["out"], "section": it.get("section", ""), "text": it["text"]})
    return {"query": query, "scope": "clips on the timeline", "hits": hits}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "cuentod"

    def log_message(self, *a):
        pass

    # ---- plumbing
    def _send(self, code, body=b"", ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _file(self, path, ctype=None, cache=False):
        if not path or not os.path.isfile(path):
            return self._send(404, {"error": "not found"})
        size = os.path.getsize(path)
        ctype = ctype or mimetypes.guess_type(path)[0] or "application/octet-stream"
        start, end, code = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", ""))
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                return self._send(416, b"", headers={"Content-Range": f"bytes */{size}"})
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "max-age=31536000, immutable" if cache else "no-cache")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 18, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def _body(self):
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise OpError("JSON body required")
        n = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(n) or b"{}")

    def _guard(self):
        # local only: refuse other hosts (DNS rebinding) and cross-site callers
        if self.headers.get("Host") not in ALLOWED_HOSTS:
            self._send(403, {"error": "forbidden host"})
            return False
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc not in ALLOWED_HOSTS:
            self._send(403, {"error": "forbidden origin"})
            return False
        return True

    def _run(self, fn):
        if not self._guard():
            return
        try:
            fn()
        except OpError as e:
            self._send(400, {"error": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            traceback.print_exc()
            try:
                self._send(500, {"error": f"{type(e).__name__}: {e}"})
            except Exception:
                pass

    def do_GET(self):
        self._run(self._get)

    do_HEAD = do_GET

    def do_POST(self):
        self._run(self._post)

    # ---- routes
    def _get(self):
        url = urlparse(self.path)
        parts = [unquote(x) for x in url.path.strip("/").split("/")]
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path in ("/", "/index.html"):
            return self._file(os.path.join(UI_DIR, "index.html"))
        if parts[0] == "ui" and len(parts) == 2 and re.match(r"^[\w.-]+$", parts[1]):
            return self._file(os.path.join(UI_DIR, parts[1]))
        if url.path == "/api/health":
            return self._send(200, {"ok": True, "pid": os.getpid(), "ui": UI_DIR})
        if url.path == "/api/projects":
            return self._send(200, {"projects": registry()})
        if len(parts) == 4 and parts[:2] == ["api", "p"]:
            p = open_project(parts[2])
            if parts[3] == "events":
                return self._events(p)
            if parts[3] == "state":
                queue_missing_renders(p)
                return self._send(200, full_state(p))
            return self._send(404, {"error": "not found"})
        if len(parts) < 3:
            return self._send(404, {"error": "not found"})
        kind, p = parts[0], open_project(parts[1])
        if kind == "media":
            sid, _, ext = parts[2].partition(".")
            if ext == "cam.m4a":
                return self._file(source_file(p, sid, "cam"), "audio/mp4")
            return self._file(source_file(p, sid), "video/mp4")
        if kind == "peaks":
            sid, track = parts[2], q.get("track", "a2")
            path = source_file(p, sid, "cam" if track == "a1" else "proxy")
            if not os.path.exists(path) or (track != "a1" and not media.probe_frames(path)):
                return self._send(404, {"error": "media not ready"})
            data = media.peaks(path, os.path.join(p.cache, f"{sid}.{track}.peaks"))
            return self._send(200 if data else 404, data or b"", "application/octet-stream")
        if kind == "thumb" and len(parts) == 4:
            frame = int(parts[3].split(".")[0]) // 12 * 12
            path = media.thumb(source_file(p, parts[2]), frame, os.path.join(p.cache, "thumbs", parts[2], f"{frame}.jpg"))
            return self._file(path, "image/jpeg", cache=True)
        if kind == "layer":
            layer_id = parts[2].removesuffix(".html")
            layer = next((l for l in p.doc["layers"] if l["id"] == layer_id), None)
            if not layer:
                return self._send(404, {"error": "no layer"})
            return self._send(200, render.layer_page(layer), "text/html; charset=utf-8")
        if kind == "render":
            name = parts[2]
            if not re.match(r"^[0-9a-f]{16}\.(webm|mov)$", name):
                return self._send(404, {"error": "not found"})
            return self._file(os.path.join(p.cache, "renders", name), cache=True)
        return self._send(404, {"error": "not found"})

    def _post(self):
        url = urlparse(self.path)
        parts = [unquote(x) for x in url.path.strip("/").split("/")]
        if len(parts) != 4 or parts[0] != "api" or parts[1] != "p":
            return self._send(404, {"error": "not found"})
        p, action = open_project(parts[2]), parts[3]
        body = self._body()
        actor = body.get("actor", "ui")
        if action == "op":
            result = p.apply(body["op"], body.get("args", {}), actor)
            queue_missing_renders(p)
            return self._send(200, {"result": result, "rev": p.rev})
        if action in ("undo", "redo"):
            result = getattr(p, action)(actor)
            queue_missing_renders(p)
            return self._send(200, {"result": result, "rev": p.rev})
        if action == "ui":
            p.set_ui(body.get("ui", {}), actor)
            return self._send(200, {"ui": p.ui})
        if action == "timeline":
            return self._send(200, timeline_view(p, body.get("start"), body.get("end")))
        if action == "search":
            return self._send(200, transcript_search(p, body["query"]))
        if action == "render":
            layer = next((l for l in p.doc["layers"] if l["id"] == body["id"]), None)
            if not layer:
                raise OpError(f"no layer {body['id']}")
            return self._send(200, queue_render(p, layer, wait=body.get("wait", True)))
        return self._send(404, {"error": "not found"})

    def _events(self, p):
        """Server-sent events: the UI refetches state whenever anyone edits."""
        q = queue.Queue()
        p.listeners.append(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b": hello\n\n")
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=15)
                    self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            p.listeners.remove(q)
            self.close_connection = True


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # browsers drop media connections all the time while seeking
        import sys
        if not isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            super().handle_error(request, client_address)


def serve():
    os.makedirs(HOME, exist_ok=True)
    httpd = Server(("127.0.0.1", PORT), Handler)
    with open(os.path.join(HOME, "daemon.json"), "w") as f:
        json.dump({"pid": os.getpid(), "port": PORT}, f)
    print(f"cuentod on http://127.0.0.1:{PORT}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
