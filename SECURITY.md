# Security

Cuento is local-first. The daemon binds to `127.0.0.1` only, refuses requests whose
`Host` or `Origin` is not its own address, and makes no network requests of its
own. Layer renders run in headless Chrome with a throwaway profile. Your footage,
proxies and project files never leave your machine.

Things to know before you open someone else's project:

- **A layer is code.** Layers are HTML that runs in your browser and in headless
  Chrome at render time. In the editor each live layer runs in a sandboxed frame
  with its own opaque origin, and every layer page carries a content security
  policy that blocks network access. Treat a project from an untrusted source the
  way you would treat a web page from one.
- **A project file names files on disk.** `project.json` holds absolute paths to
  proxies and camera originals, and the daemon serves the proxies it points at to
  the local editor. Only register projects you made or trust.
- **The Chat tab runs an agent.** A message there starts a headless Claude Code run under your login, allowed to
  call the cuento MCP tools and nothing else (no shell, no file access); the story pass is limited to the
  read-only tools. It is started only by the local editor.
- The MCP server is a client of the local daemon. It exposes edit operations on
  registered projects and nothing else on the machine.

To report a problem, open a private security advisory on the GitHub repository.
