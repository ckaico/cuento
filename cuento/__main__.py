"""cuento command line."""
import argparse
import glob
import json
import os
import signal
import subprocess
import sys

from . import media
from .project import CACHE, HOME, OpError, Project, empty_doc, new_id, open_project, registry, timecode, layout


def import_edl(name, edl, proxies, project_dir=None, camera_dir=None, lav_dir=None):
    """Build a project from an EDL (list of {clip, in_frame, out_frame, section, text}) and a folder of proxies."""
    with open(edl) as f:
        rows = json.load(f)
    project_dir = project_dir or os.path.dirname(os.path.abspath(proxies.rstrip("/")))
    doc = empty_doc(name)
    doc["imported_from"] = os.path.abspath(edl)
    for proxy in sorted(glob.glob(os.path.join(proxies, "*_proxy.mp4"))):
        sid = os.path.basename(proxy)[: -len("_proxy.mp4")]
        camera = os.path.join(camera_dir, f"{sid}.MP4") if camera_dir else None
        lav = os.path.join(lav_dir, f"LAV_{sid}_synced.wav") if lav_dir else None
        frames = media.probe_frames(proxy) or media.probe_frames(camera)
        doc["sources"][sid] = {
            "proxy": os.path.abspath(proxy), "camera": camera, "lav": lav, "frames": frames,
            # lav_time = a + (1+b)*cam_time; the synced lav files are already corrected, so identity here
            "sync": {"a": 0.0, "b": 0.0},
        }
    for r in rows:
        if r["clip"] not in doc["sources"]:
            raise OpError(f"EDL uses {r['clip']} but there is no {r['clip']}_proxy.mp4 in {proxies}")
        doc["seq"].append({"id": new_id("b"), "type": "block", "source": r["clip"], "in": int(r["in_frame"]),
                           "out": int(r["out_frame"]), "section": r.get("section", ""), "text": r.get("text", ""),
                           "note": r.get("note", ""), "audio": "lav"})
    p = Project.create(name, project_dir, doc)
    total = layout(p.doc)[1]
    print(f"{name}: {len(rows)} clips, {timecode(total)} ({total} frames) -> {p.file}")
    return p


def cam_audio(name):
    p = open_project(name)
    for sid, src in p.doc["sources"].items():
        out = os.path.join(p.cache, f"{sid}.cam.m4a")
        if os.path.exists(out):
            print(f"{sid}: have {out}")
        elif not src.get("camera") or not os.path.exists(src["camera"]):
            print(f"{sid}: camera file not reachable ({src.get('camera')}); A1 stays silent until it is")
        else:
            print(f"{sid}: extracting camera audio...", flush=True)
            print(f"{sid}: {media.extract_camera_audio(src['camera'], out) or 'FAILED'}")


def main():
    ap = argparse.ArgumentParser(prog="cuento")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("daemon", help="run the daemon in the foreground")
    sub.add_parser("start", help="start the daemon in the background")
    sub.add_parser("stop")
    sub.add_parser("status")
    sub.add_parser("list")
    sub.add_parser("mcp", help="run the MCP server on stdio")
    o = sub.add_parser("open", help="start the daemon and print (and open) the editor URL")
    o.add_argument("project")
    o.add_argument("--no-browser", action="store_true")
    i = sub.add_parser("import-edl")
    i.add_argument("name")
    i.add_argument("--edl", required=True)
    i.add_argument("--proxies", required=True)
    i.add_argument("--dir", help="project folder (default: the proxies folder's parent)")
    i.add_argument("--camera-dir")
    i.add_argument("--lav-dir")
    c = sub.add_parser("cam-audio", help="extract camera mic audio for A1 from the camera originals")
    c.add_argument("project")
    a = ap.parse_args()

    from . import mcp
    try:
        if a.cmd == "daemon":
            from .daemon import serve
            serve()
        elif a.cmd == "mcp":
            mcp.main()
        elif a.cmd == "start":
            mcp.ensure_daemon()
            print(mcp.BASE)
        elif a.cmd == "status":
            print(json.dumps({"running": mcp.daemon_up(), "url": mcp.BASE, "projects": registry(), "cache": CACHE}, indent=1))
        elif a.cmd == "stop":
            try:
                with open(os.path.join(HOME, "daemon.json")) as f:
                    os.kill(json.load(f)["pid"], signal.SIGTERM)
                print("stopped")
            except (OSError, ValueError, KeyError):
                print("not running")
        elif a.cmd == "list":
            for name, path in registry().items():
                print(f"{name}\t{path}")
        elif a.cmd == "open":
            if a.project not in registry():
                raise OpError(f"unknown project {a.project}; known: {', '.join(sorted(registry())) or 'none'}")
            mcp.ensure_daemon()
            url = mcp.project_url(a.project)
            print(url)
            if not a.no_browser:
                subprocess.run(["open", url])
        elif a.cmd == "import-edl":
            import_edl(a.name, a.edl, a.proxies, a.dir, a.camera_dir, a.lav_dir)
        elif a.cmd == "cam-audio":
            cam_audio(a.project)
    except (OpError, RuntimeError) as e:
        sys.exit(f"cuento: {e}")


main()
