"""Project document, edit operations and versioned history.

The project file is the single source of truth. The editor UI and the MCP
server both edit it through `Project.apply`, so hand edits and Claude edits
share one undo history.

Everything on the timeline is counted in whole frames at 24000/1001.
"""
import copy
import hashlib
import json
import os
import threading
import time
import uuid

FPS_NUM, FPS_DEN = 24000, 1001
FPS = FPS_NUM / FPS_DEN
STAGE = (1920, 1080)
RENDER_VERSION = 1

HOME = os.path.expanduser("~/.cuento")
REGISTRY = os.path.join(HOME, "projects.json")
CACHE = os.path.expanduser("~/Library/Caches/cuento")

ANCHORED = ("layers", "comments", "audio_fx")
Z_VALUES = ("above_person", "behind_person", "background")
TRACKS = ("v1", "a1", "a2")


class OpError(Exception):
    pass


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def timecode(frame):
    frame = int(frame)
    s, f = divmod(frame, 24)
    m, s = divmod(s, 60)
    return f"{m:02d}:{s:02d}:{f:02d}"


def item_dur(it):
    return it["dur"] if it["type"] == "gap" else it["out"] - it["in"]


def layout(doc):
    """[(item, start, dur)] and the total length."""
    out, t = [], 0
    for it in doc["seq"]:
        d = item_dur(it)
        out.append((it, t, d))
        t += d
    return out, t


def find(doc, item_id):
    for i, it in enumerate(doc["seq"]):
        if it["id"] == item_id:
            return i, it
    raise OpError(f"no clip {item_id}")


def find_in(doc, key, item_id):
    for x in doc[key]:
        if x["id"] == item_id:
            return x
    raise OpError(f"no {key[:-1]} {item_id}")


def anchor_at(doc, frame):
    for it, s, d in layout(doc)[0]:
        if s <= frame < s + d:
            return {"item": it["id"], "offset": frame - s}
    return None


def normalize(doc):
    """Resolve anchored ranges to absolute frames, tidy gaps, re-anchor.

    Layers, comments and audio effects ride on the clip they start in, so a
    ripple edit upstream carries them along.
    """
    starts = {it["id"]: (s, d) for it, s, d in layout(doc)[0]}
    for key in ANCHORED:
        for x in doc[key]:
            a = x.get("anchor")
            if a and a["item"] in starts:
                s, d = starts[a["item"]]
                x["start"] = s + max(0, min(a["offset"], d - 1))
    seq = []
    for it in doc["seq"]:
        if it["type"] == "gap":
            if it["dur"] <= 0:
                continue
            if seq and seq[-1]["type"] == "gap":
                seq[-1]["dur"] += it["dur"]
                continue
        seq.append(it)
    while seq and seq[-1]["type"] == "gap":
        seq.pop()
    doc["seq"] = seq
    for key in ANCHORED:
        for x in doc[key]:
            x["start"] = max(0, int(x["start"]))
            x["dur"] = max(1, int(x["dur"]))
            x["anchor"] = anchor_at(doc, x["start"])


def layer_hash(layer):
    """Content hash of everything that changes a layer's pixels."""
    h = hashlib.sha1()
    h.update(json.dumps([RENDER_VERSION, STAGE, FPS_NUM, FPS_DEN, layer["dur"], layer["source"]]).encode())
    return h.hexdigest()[:16]


# ---------------------------------------------------------------- operations
# Each op mutates `doc` in place and returns a small result dict.

def _source_frames(doc, sid):
    src = doc["sources"].get(sid)
    if src is None:
        raise OpError(f"no source {sid}")
    return src.get("frames")


def _check_range(doc, sid, fin, fout):
    n = _source_frames(doc, sid)
    if fin < 0 or fout <= fin or (n and fout > n):
        raise OpError(f"bad range {fin}-{fout} for {sid} ({n or '?'} frames)")


def op_clip_trim(doc, id, **kw):
    """Set a block's source in/out. Camera and lav audio derive from the block, so both follow."""
    _, it = find(doc, id)
    if it["type"] != "block":
        raise OpError("not a clip")
    fin = int(kw["in"]) if kw.get("in") is not None else it["in"]
    fout = int(kw["out"]) if kw.get("out") is not None else it["out"]
    _check_range(doc, it["source"], fin, fout)
    delta = fin - it["in"]
    if delta:
        # keep things pinned to the same source frame when the head moves
        for key in ANCHORED:
            for x in doc[key]:
                a = x.get("anchor")
                if a and a["item"] == id:
                    a["offset"] = max(0, a["offset"] - delta)
    it["in"], it["out"] = fin, fout
    return {"id": id, "in": fin, "out": fout}


def _split(doc, frame):
    for i, (it, s, d) in enumerate(layout(doc)[0]):
        if s < frame < s + d:
            k = frame - s
            b = copy.deepcopy(it)
            b["id"] = new_id("b" if it["type"] == "block" else "g")
            if it["type"] == "gap":
                it["dur"], b["dur"] = k, d - k
            else:
                b["in"] = it["in"] + k
                it["out"] = b["in"]
            doc["seq"].insert(i + 1, b)
            return [it["id"], b["id"]]
    return None


def op_clip_split(doc, frame):
    r = _split(doc, int(frame))
    return {"split": r}


def op_clip_move(doc, id, index=None, before=None):
    i, it = find(doc, id)
    doc["seq"].pop(i)
    if before is not None:
        index, _ = find(doc, before)
    index = len(doc["seq"]) if index is None else max(0, min(int(index), len(doc["seq"])))
    doc["seq"].insert(index, it)
    return {"id": id, "index": index}


def _remove(doc, ids, ripple):
    for item_id in ids:
        i, it = find(doc, item_id)
        if ripple:
            doc["seq"].pop(i)
        elif it["type"] == "block":
            doc["seq"][i] = {"id": it["id"], "type": "gap", "dur": item_dur(it)}


def op_clip_delete(doc, ids, ripple=False):
    if isinstance(ids, str):
        ids = [ids]
    _remove(doc, ids, ripple)
    return {"deleted": ids, "ripple": bool(ripple)}


def op_range_delete(doc, start, end, ripple=False):
    start, end = int(start), int(end)
    if end <= start:
        raise OpError("empty range")
    _split(doc, start)
    _split(doc, end)
    ids = [it["id"] for it, s, d in layout(doc)[0] if s >= start and s + d <= end]
    _remove(doc, ids, ripple)
    return {"deleted": ids, "ripple": bool(ripple)}


def op_ripple_trim_to(doc, frame, side):
    """Q / W: ripple-trim the previous or next edit to the playhead."""
    frame = int(frame)
    for it, s, d in layout(doc)[0]:
        if s < frame < s + d:
            k = frame - s
            if it["type"] == "gap":
                it["dur"] = d - k if side == "prev" else k
            elif side == "prev":
                op_clip_trim(doc, it["id"], **{"in": it["in"] + k})
            else:
                op_clip_trim(doc, it["id"], out=it["in"] + k)
            return {"id": it["id"], "playhead": s if side == "prev" else frame}
    return {"id": None, "playhead": frame}


def op_clip_insert(doc, source, index=None, after=None, text="", section="", **kw):
    fin, fout = int(kw["in"]), int(kw["out"])
    _check_range(doc, source, fin, fout)
    b = {"id": new_id("b"), "type": "block", "source": source, "in": fin, "out": fout,
         "section": section, "text": text, "note": "", "audio": "lav"}
    if after is not None:
        index = find(doc, after)[0] + 1
    index = len(doc["seq"]) if index is None else max(0, min(int(index), len(doc["seq"])))
    doc["seq"].insert(index, b)
    return {"id": b["id"], "index": index}


def op_clip_set(doc, id, **fields):
    _, it = find(doc, id)
    for k, v in fields.items():
        if k not in ("text", "section", "note", "audio"):
            raise OpError(f"cannot set {k}")
        if k == "audio" and v not in ("lav", "cam"):
            raise OpError("audio is lav or cam")
        it[k] = v
    return {"id": id}


def op_comment_add(doc, start, text, dur=1, target="v1", by="user"):
    c = {"id": new_id("c"), "start": int(start), "dur": int(dur), "target": target,
         "text": text, "status": "open", "thread": [], "layer_id": None,
         "created": now(), "edited": None, "by": by}
    doc["comments"].append(c)
    return {"id": c["id"]}


def op_comment_edit(doc, id, text=None, start=None, dur=None, target=None):
    c = find_in(doc, "comments", id)
    if text is not None and text != c["text"]:
        # an edited comment is work again, even if it was resolved before
        c["text"], c["edited"], c["status"] = text, now(), "open"
    if start is not None:
        c["start"], c["anchor"] = int(start), None
    if dur is not None:
        c["dur"] = int(dur)
    if target is not None:
        c["target"] = target
    return {"id": id, "status": c["status"]}


def op_comment_reply(doc, id, text, by="claude"):
    c = find_in(doc, "comments", id)
    c["thread"].append({"by": by, "text": text, "at": now()})
    return {"id": id}


def op_comment_resolve(doc, id, note=None, layer_id=None, by="claude"):
    c = find_in(doc, "comments", id)
    c["status"] = "resolved"
    if layer_id:
        find_in(doc, "layers", layer_id)
        c["layer_id"] = layer_id
    if note:
        c["thread"].append({"by": by, "text": note, "at": now()})
    return {"id": id, "status": "resolved"}


def op_comment_reopen(doc, id):
    find_in(doc, "comments", id)["status"] = "open"
    return {"id": id, "status": "open"}


def op_comment_delete(doc, id):
    find_in(doc, "comments", id)
    doc["comments"] = [c for c in doc["comments"] if c["id"] != id]
    return {"deleted": id}


def op_layer_create(doc, source, start, dur, name="", z="above_person", prompt=None, comment_id=None):
    if z not in Z_VALUES:
        raise OpError(f"z is one of {Z_VALUES}")
    l = {"id": new_id("l"), "name": name or "layer", "start": int(start), "dur": int(dur), "z": z,
         "source": source, "prompt_history": [], "comment_id": comment_id, "created": now()}
    if prompt:
        l["prompt_history"].append({"at": now(), "text": prompt})
    doc["layers"].append(l)
    if comment_id:
        find_in(doc, "comments", comment_id)["layer_id"] = l["id"]
    return {"id": l["id"], "hash": layer_hash(l)}


def op_layer_update(doc, id, source=None, start=None, dur=None, z=None, name=None, prompt=None):
    l = find_in(doc, "layers", id)
    if source is not None:
        l["source"] = source
    if start is not None:
        l["start"], l["anchor"] = int(start), None
    if dur is not None:
        l["dur"] = int(dur)
    if z is not None:
        if z not in Z_VALUES:
            raise OpError(f"z is one of {Z_VALUES}")
        l["z"] = z
    if name is not None:
        l["name"] = name
    if prompt:
        l["prompt_history"].append({"at": now(), "text": prompt})
    return {"id": id, "hash": layer_hash(l)}


def op_layer_delete(doc, id):
    find_in(doc, "layers", id)
    doc["layers"] = [l for l in doc["layers"] if l["id"] != id]
    for c in doc["comments"]:
        if c.get("layer_id") == id:
            c["layer_id"] = None
    return {"deleted": id}


def op_audio_effect(doc, track, start, dur, chain, id=None):
    """Mark an effect chain on one audio track over a range. Applied at export, not in the preview mix."""
    if track not in ("a1", "a2"):
        raise OpError("track is a1 (camera) or a2 (lav)")
    if id:
        fx = find_in(doc, "audio_fx", id)
        if not chain:
            doc["audio_fx"] = [x for x in doc["audio_fx"] if x["id"] != id]
            return {"deleted": id}
        fx.update(track=track, start=int(start), dur=int(dur), chain=chain, anchor=None)
    else:
        fx = {"id": new_id("fx"), "track": track, "start": int(start), "dur": int(dur), "chain": chain}
        doc["audio_fx"].append(fx)
    return {"id": fx["id"]}


# ---- versions
# A project holds several cuts of the same footage (a shorter and a longer one,
# say). One is active: its sequence, layers, comments and audio marks sit at the
# top level of the doc, where every other operation works. The rest are parked
# whole under doc["versions"]. Switching swaps them.

VERSIONED = ("seq", "layers", "comments", "audio_fx")


def version_names(doc):
    return [doc.get("version", "A"), *doc.get("versions", {})]


def version_state(doc, name=None):
    """The seq/layers/comments/audio_fx of any version, active or parked."""
    if name in (None, doc.get("version", "A")):
        return {k: doc[k] for k in VERSIONED}
    try:
        return doc["versions"][name]
    except KeyError:
        raise OpError(f"no version {name!r}; have {version_names(doc)}")


def seq_layout(seq):
    out, t = [], 0
    for it in seq:
        d = item_dur(it)
        out.append((it, t, d))
        t += d
    return out, t


def _covered(seq):
    """Per source, the merged frame ranges a sequence uses."""
    spans = {}
    for it in seq:
        if it["type"] == "block":
            spans.setdefault(it["source"], []).append([it["in"], it["out"]])
    for sid, rs in spans.items():
        rs.sort()
        merged = [rs[0]]
        for a, b in rs[1:]:
            if a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        spans[sid] = merged
    return spans


def version_diff(doc, a, b):
    """What each of two versions has that the other does not, by source footage."""
    out = {}
    for name, other in ((a, b), (b, a)):
        seq, there = version_state(doc, name)["seq"], _covered(version_state(doc, other)["seq"])
        only, total = [], seq_layout(seq)[1]
        for it, s, d in seq_layout(seq)[0]:
            if it["type"] != "block":
                continue
            shared = sum(max(0, min(it["out"], y) - max(it["in"], x)) for x, y in there.get(it["source"], []))
            if shared < d:
                only.append({"id": it["id"], "start": s, "tc": timecode(s), "dur": d, "frames_only_here": d - shared,
                             "whole_clip": shared == 0, "source": it["source"], "in": it["in"], "out": it["out"],
                             "section": it.get("section", ""), "text": it.get("text", "")})
        out[name] = {"total_frames": total, "total_tc": timecode(total), "frames_only_here": sum(o["frames_only_here"] for o in only),
                     "clips_only_here": only}
    return out


def op_version_add(doc, name, seq):
    """Add a parked version from a list of {source, in, out, section, text}."""
    if not name or name in version_names(doc):
        raise OpError(f"version name {name!r} is empty or taken")
    blocks = []
    for r in seq:
        fin, fout = int(r["in"]), int(r["out"])
        _check_range(doc, r["source"], fin, fout)
        blocks.append({"id": new_id("b"), "type": "block", "source": r["source"], "in": fin, "out": fout,
                       "section": r.get("section", ""), "text": r.get("text", ""), "note": r.get("note", ""),
                       "audio": r.get("audio", "lav")})
    doc.setdefault("versions", {})[name] = {"seq": blocks, "layers": [], "comments": [], "audio_fx": []}
    return {"version": name, "clips": len(blocks)}


def op_version_duplicate(doc, name, source=None):
    if not name or name in version_names(doc):
        raise OpError(f"version name {name!r} is empty or taken")
    doc.setdefault("versions", {})[name] = copy.deepcopy(version_state(doc, source))
    return {"version": name, "from": source or doc.get("version", "A")}


def op_version_switch(doc, name):
    cur = doc.get("version", "A")
    if name == cur:
        return {"version": cur}
    state = version_state(doc, name)
    doc.setdefault("versions", {})[cur] = {k: doc[k] for k in VERSIONED}
    del doc["versions"][name]
    for k in VERSIONED:
        doc[k] = state[k]
    doc["version"] = name
    return {"version": name}


def op_version_rename(doc, name, to):
    if not to or to in version_names(doc):
        raise OpError(f"version name {to!r} is empty or taken")
    if name == doc.get("version", "A"):
        doc["version"] = to
    else:
        doc["versions"][to] = version_state(doc, name)
        del doc["versions"][name]
    return {"version": to}


def op_version_delete(doc, name):
    if name == doc.get("version", "A"):
        raise OpError("switch to another version before deleting this one")
    version_state(doc, name)
    del doc["versions"][name]
    return {"deleted": name}


OPS = {name[3:]: fn for name, fn in list(globals().items()) if name.startswith("op_")}


# ------------------------------------------------------------------ storage

def _write_json(path, data):
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def _read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def registry():
    return _read_json(REGISTRY, {})


def register(name, path):
    os.makedirs(HOME, exist_ok=True)
    reg = registry()
    reg[name] = os.path.abspath(path)
    _write_json(REGISTRY, reg)


def empty_doc(name):
    return {"cuento": 1, "name": name, "fps": [FPS_NUM, FPS_DEN], "sources": {}, "version": "A", "versions": {},
            "seq": [], "layers": [], "comments": [], "audio_fx": []}


class Project:
    """A project folder: project.json (head), history/ (one snapshot per revision), ui.json."""

    def __init__(self, name, path):
        self.name, self.dir = name, path
        self.file = os.path.join(path, "project.json")
        self.lock = threading.RLock()
        self.listeners = []
        if not os.path.exists(self.file):
            raise OpError(f"no project file at {self.file}")
        if os.path.getsize(self.file) == 0:
            raise OpError(f"{self.file} reads empty; if cloud-synced, download it before opening")
        head = _read_json(self.file)
        self.doc, self.hist = head["doc"], head["history"]
        self.doc.setdefault("version", "A")
        self.doc.setdefault("versions", {})
        self.ui = _read_json(os.path.join(path, "ui.json"), {"playhead": 0})
        self.cache = os.path.join(CACHE, name)
        os.makedirs(self.cache, exist_ok=True)

    @classmethod
    def create(cls, name, path, doc):
        os.makedirs(os.path.join(path, "history"), exist_ok=True)
        normalize(doc)
        file = os.path.join(path, "project.json")
        if os.path.exists(file):
            raise OpError(f"{file} already exists")
        _write_json(os.path.join(path, "history", "000001.json"),
                    {"rev": 1, "parent": None, "label": "import", "actor": "import", "at": now(), "doc": doc})
        _write_json(file, {"doc": doc, "history": {"undo": [1], "redo": [], "next": 2}})
        register(name, path)
        return cls(name, path)

    @property
    def rev(self):
        return self.hist["undo"][-1]

    def _save(self):
        _write_json(self.file, {"doc": self.doc, "history": self.hist})

    def _snapshot(self, rev):
        return _read_json(os.path.join(self.dir, "history", f"{rev:06d}.json"))

    def apply(self, op, args, actor="ui"):
        fn = OPS.get(op)
        if fn is None:
            raise OpError(f"unknown op {op}")
        with self.lock:
            doc = copy.deepcopy(self.doc)
            try:
                result = fn(doc, **args)
            except (TypeError, KeyError, ValueError) as e:
                raise OpError(f"{op}: {e}")
            normalize(doc)
            if doc != self.doc:
                rev = self.hist["next"]
                _write_json(os.path.join(self.dir, "history", f"{rev:06d}.json"),
                            {"rev": rev, "parent": self.rev, "label": op, "actor": actor, "at": now(), "doc": doc})
                self.hist["next"] = rev + 1
                self.hist["undo"].append(rev)
                self.hist["redo"] = []
                self.doc = doc
                self._save()
            self.notify({"type": "doc", "rev": self.rev, "op": op, "actor": actor})
            return result

    def undo(self, actor="ui"):
        return self._step("undo", "redo", actor)

    def redo(self, actor="ui"):
        return self._step("redo", "undo", actor)

    def _step(self, kind, other, actor):
        with self.lock:
            h = self.hist
            if kind == "undo":
                if len(h["undo"]) < 2:
                    return {"ok": False, "rev": self.rev}
                h["redo"].append(h["undo"].pop())
            else:
                if not h["redo"]:
                    return {"ok": False, "rev": self.rev}
                h["undo"].append(h["redo"].pop())
            self.doc = self._snapshot(self.rev)["doc"]
            self._save()
            self.notify({"type": "doc", "rev": self.rev, "op": kind, "actor": actor})
            return {"ok": True, "rev": self.rev}

    def set_source_frames(self, sid, frames):
        """Media facts are not edits: fill them in without making a revision."""
        with self.lock:
            if self.doc["sources"][sid].get("frames") != frames:
                self.doc["sources"][sid]["frames"] = frames
                self._save()

    def set_ui(self, patch, actor="ui"):
        with self.lock:
            self.ui.update(patch)
            _write_json(os.path.join(self.dir, "ui.json"), self.ui)
        if actor != "ui":
            self.notify({"type": "ui", "ui": self.ui, "actor": actor})

    def render_path(self, layer, ext="webm"):
        return os.path.join(self.cache, "renders", f"{layer_hash(layer)}.{ext}")

    def state(self):
        with self.lock:
            h = self.hist
            return {"name": self.name, "doc": self.doc, "rev": self.rev, "ui": self.ui,
                    "can_undo": len(h["undo"]) > 1, "can_redo": bool(h["redo"]),
                    "renders": {l["id"]: {"hash": layer_hash(l), "ready": os.path.exists(self.render_path(l))}
                                for l in self.doc["layers"]}}

    def notify(self, event):
        for q in list(self.listeners):
            q.put(event)


_open = {}
_open_lock = threading.Lock()


def open_project(name):
    with _open_lock:
        if name not in _open:
            path = registry().get(name)
            if not path:
                raise OpError(f"unknown project {name}; known: {sorted(registry())}")
            _open[name] = Project(name, path)
        return _open[name]
