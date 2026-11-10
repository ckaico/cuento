"""Cuento MCP server (stdio). A thin client of the local daemon, so Claude's
edits and hand edits in the UI land in the same project file and undo history."""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

from .project import HOME, timecode

PORT = int(os.environ.get("CUENTO_PORT", "47821"))
BASE = f"http://127.0.0.1:{PORT}"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LAYER_CONTRACT = (
    "A layer is ONE graphic as self-contained HTML/CSS (inline <style>, inline SVG, no external files) on a "
    "1920x1080 transparent stage; do not paint a page background. Time is driven by the editor, never by the "
    "clock: write CSS @keyframes/Web Animations as if the layer starts at 0s (they are seeked per frame; use "
    "animation-fill-mode: both), or set window.cuento.onframe = (seconds, frame) => {...} for canvas. "
    "window.cuento.dur is the layer length in seconds. Leave the subject's face clear: graphics sit beside the head."
)


def http(path, body=None, timeout=660):
    req = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            raise RuntimeError(json.loads(e.read()).get("error", str(e)))
        except ValueError:
            raise RuntimeError(str(e))


def daemon_up():
    try:
        return http("/api/health", timeout=2).get("ok", False)
    except Exception:
        return False


def ensure_daemon():
    if daemon_up():
        return
    os.makedirs(HOME, exist_ok=True)
    log = open(os.path.join(HOME, "daemon.log"), "a")
    env = dict(os.environ, PYTHONPATH=ROOT)
    subprocess.Popen([sys.executable, "-m", "cuento", "daemon"], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                     stdout=log, stderr=log, start_new_session=True)
    for _ in range(50):
        time.sleep(0.1)
        if daemon_up():
            return
    raise RuntimeError(f"cuentod did not start; see {HOME}/daemon.log")


def project_url(name):
    return f"{BASE}/?project={name}"


# ------------------------------------------------------------------ tools

def _op(op):
    def run(a):
        project = a.pop("project")
        return http(f"/api/p/{project}/op", {"op": op, "args": a, "actor": "claude"})
    return run


def _layer_op(op):
    def run(a):
        project = a["project"]
        out = _op(op)(a)
        layer_id = out["result"]["id"]
        out["render"] = http(f"/api/p/{project}/render", {"id": layer_id, "wait": True})
        return out
    return run


def t_project_list(a):
    return http("/api/projects")


def t_project_open(a):
    s = http(f"/api/p/{a['project']}/state")
    tl = http(f"/api/p/{a['project']}/timeline", {})
    notes = None
    try:
        # house rules and look references for this project, kept beside its project file
        with open(os.path.join(http("/api/projects")["projects"][a["project"]], "CUENTO.md")) as f:
            notes = f.read(8000)
    except (OSError, KeyError):
        pass
    return {"url": project_url(a["project"]), "open_in": "Claude's browser pane (preview_start url) or `open <url>`",
            "project_notes": notes,
            "rev": s["rev"], "total": tl["total_tc"], "clips": len(tl["clips"]), "layers": len(tl["layers"]),
            "open_comments": tl["open_comments"], "media": s["media"]}


def t_timeline_get(a):
    return http(f"/api/p/{a['project']}/timeline", {"start": a.get("start"), "end": a.get("end"), "version": a.get("version")})


def t_version_list(a):
    return http(f"/api/p/{a['project']}/versions", {})


def t_version_diff(a):
    return http(f"/api/p/{a['project']}/diff", {"a": a.get("a"), "b": a.get("b")})


def t_comment_list(a):
    s = http(f"/api/p/{a['project']}/state")
    want = a.get("status", "open")
    out = []
    for c in sorted(s["doc"]["comments"], key=lambda c: c["start"]):
        if want in ("all", c["status"]):
            out.append({**{k: c[k] for k in ("id", "status", "target", "start", "dur", "text", "thread", "layer_id", "edited")},
                        "tc": timecode(c["start"]), "tc_end": timecode(c["start"] + c["dur"])})
    return {"comments": out}


def t_layer_list(a):
    s = http(f"/api/p/{a['project']}/state")
    return {"layers": [{**{k: l[k] for k in ("id", "name", "start", "dur", "z", "comment_id", "prompt_history")},
                        "tc": timecode(l["start"]), "render": s["renders"].get(l["id"])} for l in s["doc"]["layers"]]}


def t_layer_get(a):
    s = http(f"/api/p/{a['project']}/state")
    for l in s["doc"]["layers"]:
        if l["id"] == a["id"]:
            return {**l, "render": s["renders"].get(l["id"])}
    raise RuntimeError(f"no layer {a['id']}")


def t_layer_render(a):
    return http(f"/api/p/{a['project']}/render", {"id": a["id"], "wait": True})


def t_transcript_search(a):
    return http(f"/api/p/{a['project']}/search", {"query": a["query"]})


def t_playhead(a):
    if a.get("frame") is not None:
        return http(f"/api/p/{a['project']}/ui", {"ui": {"playhead": int(a["frame"])}, "actor": "claude"})
    return http(f"/api/p/{a['project']}/state")["ui"]


def t_undo(a):
    return http(f"/api/p/{a['project']}/undo", {"actor": "claude"})


def t_redo(a):
    return http(f"/api/p/{a['project']}/redo", {"actor": "claude"})


def _later(milestone, what):
    def run(a):
        raise RuntimeError(f"{what} is not built yet (milestone {milestone}). Nothing was changed.")
    return run


F, S, B = {"type": "integer"}, {"type": "string"}, {"type": "boolean"}
FRAME = {"type": "integer", "description": "timeline frame (24000/1001 fps)"}


def tool(name, desc, fn, props=None, required=()):
    props = {"project": {"type": "string", "description": "project name, as shown by project_list"}, **(props or {})}
    return {"name": name, "description": desc, "fn": fn,
            "inputSchema": {"type": "object", "properties": props, "required": ["project", *required]}}


TOOLS = [
    {"name": "project_list", "description": "List Cuento projects on this Mac.", "fn": t_project_list,
     "inputSchema": {"type": "object", "properties": {}}},
    tool("project_open", "Start the daemon if needed and return the editor URL plus a project summary. "
         "Open the URL in Claude's browser pane so the user can edit by hand.", t_project_open),
    tool("timeline_get", "Read the timeline: every clip with id, timeline start, source in/out (frames), section "
         "and transcript text; plus layers, audio effects, playhead and marks. Optional frame window. Pass "
         "`version` to read a cut the user is not viewing, without switching.",
         t_timeline_get, {"start": FRAME, "end": FRAME, "version": S}),
    tool("version_list", "List the project's versions (for example a shorter and a longer cut) with lengths; one is active.",
         t_version_list),
    tool("version_diff", "What two versions each have that the other lacks, by source footage: clips and frames only "
         "in one. Defaults to the active version against the other one.", t_version_diff, {"a": S, "b": S}),
    tool("version_switch", "Make another version the active one. Every edit tool works on the active version, and "
         "the user's editor follows the switch.", _op("version_switch"), {"name": S}, ["name"]),
    tool("version_duplicate", "Copy a version (the active one unless `source` is given) under a new name, to try a "
         "change without touching the original.", _op("version_duplicate"), {"name": S, "source": S}, ["name"]),
    tool("clip_trim", "Set a clip's source in and/or out frame (out is exclusive). Ripples the timeline. Camera "
         "audio (A1) and lav audio (A2) are derived from the clip, so both always follow.",
         _op("clip_trim"), {"id": S, "in": F, "out": F}, ["id"]),
    tool("clip_split", "Split whatever is under a timeline frame (add edit).", _op("clip_split"), {"frame": FRAME}, ["frame"]),
    tool("clip_move", "Move a clip to a new position: `index` in the sequence or `before` another clip id.",
         _op("clip_move"), {"id": S, "index": F, "before": S}, ["id"]),
    tool("clip_delete", "Delete clips. ripple=true closes the hole; false lifts and leaves a gap.",
         _op("clip_delete"), {"ids": {"type": "array", "items": S}, "ripple": B}, ["ids"]),
    tool("clip_insert", "Insert a new clip from a source (e.g. an alternate take) at `index` or `after` a clip id.",
         _op("clip_insert"), {"source": S, "in": F, "out": F, "index": F, "after": S, "text": S, "section": S},
         ["source", "in", "out"]),
    tool("clip_set", "Set a clip's text, section, note, or which audio is heard: audio = lav | cam (never both).",
         _op("clip_set"), {"id": S, "text": S, "section": S, "note": S, "audio": {"enum": ["lav", "cam"]}}, ["id"]),
    tool("comment_list", "List comments (status: open | resolved | all; default open). Each is a task on an exact "
         "time range and target (v1, a1, a2). An edited comment reopens; if it has a layer_id, update that layer.",
         t_comment_list, {"status": {"enum": ["open", "resolved", "all"]}}),
    tool("comment_add", "Add a comment on a range.", _op("comment_add"),
         {"start": FRAME, "dur": F, "text": S, "target": {"enum": ["v1", "a1", "a2"]}, "by": S}, ["start", "text"]),
    tool("comment_reply", "Reply in a comment's thread without resolving it.", _op("comment_reply"),
         {"id": S, "text": S}, ["id", "text"]),
    tool("comment_resolve", "Mark a comment done. Pass layer_id when a layer answers it, and a short note of what was done.",
         _op("comment_resolve"), {"id": S, "note": S, "layer_id": S}, ["id"]),
    tool("layer_list", "List overlay layers with range, z, render state and prompt history.", t_layer_list),
    tool("layer_get", "Get one layer including its HTML source.", t_layer_get, {"id": S}, ["id"]),
    tool("layer_create", "Create ONE overlay element over a timeline range and render it alone to an alpha file "
         "(cached by content hash). " + LAYER_CONTRACT + " Pass comment_id to tie it to the comment that asked for it.",
         _layer_op("layer_create"),
         {"source": {"type": "string", "description": "the layer's HTML"}, "start": FRAME,
          "dur": {"type": "integer", "description": "length in frames"}, "name": S,
          "z": {"enum": ["above_person", "behind_person", "background"]},
          "prompt": {"type": "string", "description": "the request in the user's words; kept as prompt history"},
          "comment_id": S}, ["source", "start", "dur"]),
    tool("layer_update", "Change one layer (source, range, z, name) and re-render only that layer. Moving or "
         "retiming without a source/dur change re-renders nothing. " + LAYER_CONTRACT,
         _layer_op("layer_update"),
         {"id": S, "source": S, "start": FRAME, "dur": F, "name": S,
          "z": {"enum": ["above_person", "behind_person", "background"]}, "prompt": S}, ["id"]),
    tool("layer_render", "Render one layer now and wait for it. A no-op when its content hash is already cached.",
         t_layer_render, {"id": S}, ["id"]),
    tool("layer_delete", "Delete a layer.", _op("layer_delete"), {"id": S}, ["id"]),
    tool("audio_effect", "Mark an effect chain on one audio track over a range (a1 = camera, a2 = lav), e.g. "
         "chain [{\"type\":\"eq\",\"warmth\":2}]. Stored on the timeline and applied at export; the preview mix is "
         "unchanged. Pass id to replace one, or id with an empty chain to remove it.",
         _op("audio_effect"), {"track": {"enum": ["a1", "a2"]}, "start": FRAME, "dur": F,
                               "chain": {"type": "array", "items": {"type": "object"}}, "id": S},
         ["track", "start", "dur", "chain"]),
    tool("transcript_search", "Find clips on the timeline whose transcript text contains a phrase.",
         t_transcript_search, {"query": S}, ["query"]),
    tool("playhead", "Read the user's playhead and in/out marks, or move the playhead (pass frame) to show them a spot.",
         t_playhead, {"frame": FRAME}),
    tool("undo", "Undo the last edit (shared history with the UI).", t_undo),
    tool("redo", "Redo.", t_redo),
    tool("matte_generate", "Person matte for text behind the head. Not built yet (M3).", _later("M3", "Matte generation"),
         {"source": S}),
    tool("export", "Export to OTIO / Premiere XML / final render. Not built yet (M4).", _later("M4", "Export"),
         {"format": S}),
]
BY_NAME = {t["name"]: t for t in TOOLS}


def handle(msg):
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        return {"protocolVersion": msg["params"].get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "cuento", "version": "0.2.0"},
                "instructions": "Cuento is a nonlinear editor. Call project_open first and open its URL in the "
                                "browser pane. If project_open returns project_notes, follow them. Work the open comments one at a time: each becomes one clip edit or "
                                "one layer, then comment_resolve. All positions are frames at 24000/1001."}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{k: t[k] for k in ("name", "description", "inputSchema")} for t in TOOLS]}
    if method == "tools/call":
        t = BY_NAME.get(msg["params"]["name"])
        if not t:
            raise KeyError(f"unknown tool {msg['params']['name']}")
        try:
            ensure_daemon()
            out = t["fn"](dict(msg["params"].get("arguments") or {}))
            return {"content": [{"type": "text", "text": json.dumps(out, indent=1)}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}
    if mid is None:
        return None
    raise KeyError(f"method not found: {method}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        try:
            result = handle(msg)
            if msg.get("id") is None:
                continue
            out = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        except Exception as e:
            if msg.get("id") is None:
                continue
            out = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": str(e)}}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()
