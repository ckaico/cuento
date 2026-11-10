---
name: cuento
description: Open and work in Cuento, the local nonlinear editor (timeline, linked camera + lav audio, comments, alpha overlay layers). Use for "/cuento open <project>", "open the edit", "work the comments", "trim that clip", "add a graphic here", or any request about a Cuento project.
---

# Cuento

Cuento is a local nonlinear editor. A daemon on this machine owns the media and the
project file; the editor UI and the `cuento` MCP server both edit that one file,
so hand edits and your edits share one undo history. This skill assumes the repo is
at `~/Projects/cuento`; adjust the paths if it lives elsewhere. Each project keeps
its own `project.json` next to its proxies.

## /cuento open <project>

1. Start the daemon (no-op if it is running) and check the project exists:
   ```bash
   ~/Projects/cuento/bin/cuento start && ~/Projects/cuento/bin/cuento list
   ```
2. Open the editor where the user can see it. In the Claude desktop app, open
   `http://127.0.0.1:47821/?project=<project>` in Claude's browser pane
   (`mcp__Claude_Browser__preview_start` with that `url`). Anywhere else:
   ```bash
   ~/Projects/cuento/bin/cuento open <project>
   ```
3. Call `project_open` (MCP) and report in two lines: length, clip count, open
   comments, and whether every proxy is readable. If a proxy reads as not ready,
   say so and stop there: it is still encoding, or it sits in a cloud-synced folder
   and is not downloaded yet. Do not re-encode or replace it.
4. If `project_open` returns `project_notes` (a `CUENTO.md` beside the project file),
   read them: they hold that project's look references and house rules, and they
   outrank the defaults below.

If the user has not named a project, run `cuento list` and request their selection before opening it.

## MCP tools (server `cuento`, tools appear as `mcp__cuento__<name>`)

| Tool | Use |
|---|---|
| `project_list`, `project_open` | find and open a project, get the editor URL |
| `timeline_get` | every clip: id, start, source in/out, section, transcript text; layers; playhead; marks |
| `clip_trim`, `clip_split`, `clip_move`, `clip_delete`, `clip_insert`, `clip_set` | edit the cut. A1 camera and A2 lav audio derive from the clip, so they always follow |
| `version_list`, `version_diff`, `version_switch`, `version_duplicate` | several cuts of the same footage (a shorter and a longer one). Edit tools work on the active version; pass `version` to `timeline_get` to read another |
| `comment_list`, `comment_add`, `comment_reply`, `comment_resolve` | the task queue |
| `layer_list`, `layer_get`, `layer_create`, `layer_update`, `layer_render`, `layer_delete` | overlay elements, one alpha file each |
| `audio_effect` | mark an effect chain on A1 or A2 over a range (applied at export) |
| `transcript_search` | find clips by what is said |
| `playhead` | read the user's playhead and I/O marks, or move the playhead to show a spot |
| `undo`, `redo` | shared history with the UI |
| `matte_generate`, `export` | not built yet (M3, M4): they return an error and change nothing |

If the MCP tools are not loaded in this session, the same operations are one HTTP
call each: `curl -s -X POST http://127.0.0.1:47821/api/p/<project>/op -H 'Content-Type: application/json' -d '{"op":"clip_trim","args":{"id":"b_…","out":1234},"actor":"claude"}'`
(`GET …/state` for everything, `POST …/render {"id":…}` to render a layer).

All positions are whole frames at 24000/1001. `out` is exclusive.

## The loop

The user trims by hand and leaves comments (M key) on exact ranges. Work them one at a time:

1. `comment_list` (open only). Read the comment, its range and its target (`v1`, `a1`, `a2`).
2. Do the one thing it asks:
   - a cut change → `clip_trim` / `clip_split` / `clip_delete` / `clip_move`
   - a graphic or effect → `layer_create` over the comment's range with `comment_id` and `prompt` set to the comment text
   - an audio request → `audio_effect` on that track and range
   - a question → `comment_reply`, leave it open
3. `comment_resolve` with a one-line note (and `layer_id` when a layer answers it).
4. An edited comment reopens. If it already has a `layer_id`, `layer_get` that layer and `layer_update` it; never make a second layer for the same comment.

Never re-render the cut. A layer render touches only that layer; moving or retiming
a layer, trimming clips and moving the playhead render nothing.

## Writing a layer

- One element per layer, self-contained HTML/CSS/inline SVG on a 1920×1080 transparent stage. No page background, no external files.
- Time is driven by the editor, never by the clock. Write CSS `@keyframes` (with `animation-fill-mode: both`) or Web Animations as if the layer starts at 0 s; they are seeked frame by frame. For canvas, set `window.cuento.onframe = (seconds, frame) => { … }`. `window.cuento.dur` is the layer length in seconds. No `setTimeout`, no `Date.now()`.
- Keep the face clear: graphics sit beside the head. `z: behind_person` is stored but draws over the picture until the matte exists (M3).
- `backdrop-filter` blurs nothing in an alpha render (there is no picture behind the layer). Use a translucent fill for glass panels for now.
- Words on screen are the editor's words: take them verbatim from the transcript or the comment. Do not write their copy for them. Dates, numbers and names come from real sources, never invented.
- Match the project's own look. When the notes point at reference mockups, read them before writing the first layer.

## Hotkeys (for when the user asks)

J K L shuttle (tap L again for 2× 4× 8×; K+L slow; K+tap one frame) · Space · ←/→ frame, ⇧ = 5 ·
↑/↓ edits · ⌘K split · C razor (click to split), V select · Q / W ripple trim to playhead · I / O / X marks · Delete ripple, ⇧Delete lift (leaves a gap) ·
M comment, ⇧M / ⇧⌘M next / previous · ⌘Z / ⇧⌘Z · `?` shows the sheet in the editor.
