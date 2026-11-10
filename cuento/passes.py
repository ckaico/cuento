"""Chat passes: the editor's Chat tab hands a message to a headless Claude Code
run that works the project through the cuento MCP tools.

Each mode is a role with its own model, so a cheaper model can do the cutting
while a stronger one is called in for a story pass:

  edit   one continuing conversation that changes the timeline
  story  a read-only pass over every version that reports the strongest story;
         its report is handed to the edit conversation on the next message

Passes run under the user's own Claude Code login, so they draw on that plan's
usage like any other Claude Code session. API-key variables are removed from the
pass environment so a pass can never be billed per token by accident.
Models are set in ~/.cuento/config.json: {"models": {"edit": "...", "story": "..."}}
"""
import json
import os
import shutil
import subprocess
import threading
import uuid

from .project import HOME, _read_json, _write_json, now, timecode

READ_ONLY = ["project_open", "project_list", "timeline_get", "version_list", "version_diff", "comment_list",
             "layer_list", "layer_get", "transcript_search", "playhead"]

MODES = {
    "edit": {
        "label": "Edit", "model": "claude-opus-5-5", "tools": ["mcp__cuento"],
        "role": "You are the editor's hands inside the Cuento project named below. Work only through the cuento MCP "
                "tools. Do what the message asks on the version the user is viewing unless they name another: trim, "
                "split, move and delete clips, turn comments into layers, switch or duplicate versions. When story "
                "pass findings are attached, treat them as advice from a colleague: apply only what the user asks "
                "for. Words on screen are the speaker's own, verbatim from the transcript or the comment; never "
                "write copy. Reply in at most six short lines: what changed, with clip ids or timecodes, and "
                "anything you did not do.",
    },
    "story": {
        "label": "Story", "model": "fable", "tools": [f"mcp__cuento__{t}" for t in READ_ONLY],
        "role": "You are doing a story pass on the Cuento project named below. You are read-only: change nothing. "
                "Read every version (version_list, then timeline_get with each version name) and what differs "
                "between them (version_diff). Find the strongest story this footage supports. Report: the spine "
                "in one sentence; then for each version, what to cut, what to move and what is missing, as "
                "concrete moves by clip id and timecode that an editor can apply; then which material that only "
                "the longer version has is worth keeping in the shorter one, and the reverse. Judge structure and "
                "order, not the speaker's wording, and do not rewrite their words. At most 30 lines.",
    },
}

_busy = {}
_guard = threading.Lock()


def modes():
    conf = _read_json(os.path.join(HOME, "config.json"), {}) or {}
    out = {}
    for key, m in MODES.items():
        out[key] = {**m, "model": conf.get("models", {}).get(key, m["model"])}
    return out


def _file(p):
    return os.path.join(p.dir, "chat.json")


def load(p):
    return _read_json(_file(p), {"messages": [], "edit_session": None})


def view(p):
    chat = load(p)
    return {"messages": chat["messages"], "busy": _busy.get(p.name),
            "modes": {k: {"label": m["label"], "model": m["model"]} for k, m in modes().items()}}


def claude_bin():
    for c in (os.environ.get("CUENTO_CLAUDE"), shutil.which("claude"), os.path.expanduser("~/.local/bin/claude"),
              "/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
        if c and os.path.exists(c):
            return c
    return None


def send(p, mode, text):
    """Queue one pass. Returns at once; the reply lands in the chat log."""
    if mode not in MODES:
        raise ValueError(f"mode is one of {list(MODES)}")
    text = (text or "").strip()
    if mode == "edit" and not text:
        raise ValueError("say what to do")
    with _guard:
        if _busy.get(p.name):
            raise ValueError(f"a {_busy[p.name]} pass is still running")
        _busy[p.name] = mode
    chat = load(p)
    m = modes()[mode]
    user = {"id": uuid.uuid4().hex[:8], "at": now(), "role": "user", "mode": mode,
            "text": text or "Find the best story."}
    reply = {"id": uuid.uuid4().hex[:8], "at": now(), "role": "pass", "mode": mode, "model": m["model"],
             "text": "", "status": "running"}
    chat["messages"] += [user, reply]
    _write_json(_file(p), chat)
    p.notify({"type": "chat"})
    threading.Thread(target=_run, args=(p, mode, m, user, reply["id"]), daemon=True).start()
    return reply


def _context(p):
    ph = int(p.ui.get("playhead", 0))
    marks = p.ui.get("marks") or {}
    return (f"[Cuento project \"{p.name}\" · user is viewing version \"{p.doc.get('version', 'A')}\" · playhead frame {ph} "
            f"({timecode(ph)}) · marks in={marks.get('in')} out={marks.get('out')}]")


def _run(p, mode, m, user, reply_id):
    chat = load(p)
    prompt = f"{_context(p)}\n\n{user['text']}"
    fed = []
    if mode == "edit":
        fed = [x for x in chat["messages"] if x["role"] == "pass" and x["mode"] == "story"
               and x["status"] == "done" and not x.get("fed")]
        if fed:
            notes = "\n\n---\n\n".join(f"Story pass ({x['model']}, {x['at']}):\n{x['text']}" for x in fed)
            prompt = f"{_context(p)}\n\nStory pass findings since your last turn:\n\n{notes}\n\n---\n\nThe user says: {user['text']}"
    result = {"status": "error", "text": ""}
    try:
        exe = claude_bin()
        if not exe:
            raise RuntimeError("Claude Code CLI not found. Install it, or set CUENTO_CLAUDE to its path.")
        cwd = os.path.join(HOME, "chat", p.name)
        os.makedirs(cwd, exist_ok=True)
        base = [exe, "-p", prompt, "--model", m["model"], "--output-format", "json",
                "--append-system-prompt", m["role"], "--allowedTools", *m["tools"]]
        env = dict(os.environ, PATH=f"/opt/homebrew/bin:{os.path.expanduser('~/.local/bin')}:{os.environ.get('PATH', '')}")
        # passes must ride on the Claude Code login (the subscription), never on a metered API key
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            env.pop(key, None)
        attempts = [[]]
        if mode == "edit":
            sid = chat.get("edit_session")
            fresh = str(uuid.uuid4())
            attempts = ([["--resume", sid]] if sid else []) + [["--session-id", fresh]]
        out = None
        for extra in attempts:
            r = subprocess.run(base + extra, cwd=cwd, env=env, capture_output=True, text=True, timeout=1500,
                               stdin=subprocess.DEVNULL)
            line = next((l for l in r.stdout.splitlines() if l.startswith("{")), None)
            out = json.loads(line) if line else None
            if out and not out.get("is_error"):
                break
            if not out:
                out = {"is_error": True, "result": (r.stderr or r.stdout or "no output").strip()[-600:]}
        result["text"] = (out.get("result") or "").strip() or "(no reply)"
        if not out.get("is_error"):
            result["status"] = "done"
            if mode == "edit":
                result["session"] = out.get("session_id")
        result["cost_usd"] = out.get("total_cost_usd")
    except subprocess.TimeoutExpired:
        result["text"] = "The pass ran past 25 minutes and was stopped."
    except Exception as e:
        result["text"] = str(e)
    with _guard:
        chat = load(p)
        for x in chat["messages"]:
            if x["id"] == reply_id:
                x.update(status=result["status"], text=result["text"], cost_usd=result.get("cost_usd"), done=now())
            if result["status"] == "done" and any(x["id"] == f["id"] for f in fed):
                x["fed"] = True
        if result.get("session"):
            chat["edit_session"] = result["session"]
        _write_json(_file(p), chat)
        _busy.pop(p.name, None)
    p.notify({"type": "chat"})
