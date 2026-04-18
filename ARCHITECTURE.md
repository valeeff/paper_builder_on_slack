# Autonomous Slack Designer — Architecture

A Python server that listens for Slack mentions and autonomously designs or deploys UIs using Claude and Paper.

---

## Components

| Component | Role |
|-----------|------|
| **Slack** | User interface — where requests come in and results are posted |
| **server.py** | Orchestrator — receives events, routes to flows, posts results |
| **Claude CLI** (`claude -p`) | AI brain — spawned as a subprocess for every task |
| **Paper MCP** | Design tool — Claude uses it to draw, update, and screenshot UIs |
| **Vercel** | Hosting — receives the built React project and returns a live URL |

---

## High-level flow

```
User mentions bot in Slack
        │
        ▼
  server.py receives event
  fetches full thread history
        │
        ▼
  detect_intent() ──── "design" ────────────────────────────────────┐
        │                                                            │
        │ "implement"                                                │
        ▼                                                            ▼
  IMPLEMENT FLOW                                             DESIGN FLOW
```

---

## Design flow

User asks the bot to create or update a UI (e.g. "design a login screen").

```
Slack mention
     │
     ▼
server.py
  builds thread string
  (timestamps + [user]/[bot] labels)
     │
     ▼
run_design_agent()
  spawns: claude -p <prompt> --output-format stream-json
     │
     ▼
Claude subprocess
  1. get_guide          → loads Paper working instructions
  2. get_basic_info     → reads canvas, existing artboards
  3. write_html         → draws new elements (new designs)
     or
     update_styles      → edits existing elements (refinements)
     set_text_content   → edits text content
  4. get_screenshot     → takes a snapshot to review work
  5. finish_working_on_nodes → finalises the design
  6. streams result back as JSON events
     │
     ▼
server.py
  parses stream-json line by line
  buffers all screenshots
  captures final text summary
     │
     ▼
Slack
  uploads last screenshot as image
  posts text summary in thread
```

---

## Implement flow

User asks to deploy a design (e.g. "implement this" / "deploy it").

```
Slack mention
     │
     ▼
server.py
  creates temp dir: builds/paper_design_<id>/
  scaffold_vite_project()
    writes: package.json, vite.config.js,
            index.html, src/main.jsx, src/index.css
     │
     ▼
run_implement_agent()
  spawns: claude -p <prompt> --output-format stream-json
     │
     ▼
Claude subprocess
  1. get_basic_info           → lists artboards
  2. get_jsx                  → exports artboard as React JSX
  3. get_computed_styles      → gets exact pixel values
  4. get_fill_image           → extracts image/icon fills as base64
  5. writes <ComponentName>.jsx → builds/paper_design_<id>/src/screens/
  6. writes image files        → builds/paper_design_<id>/public/
  7. replies with component names (one per line)
     │
     ▼
server.py
  parses component names from Claude's final reply
     │
     ▼
wire_navigation_agent()
  spawns: claude -p <prompt>  (no MCP tools — pure code generation)
     │
     ▼
Claude subprocess
  generates App.jsx with:
    - imports for all screen components
    - useState-based screen router
    - navigate() prop passed to each screen
  returns raw file content as text
     │
     ▼
server.py
  writes src/App.jsx to disk
     │
     ▼
deploy_to_vercel()
  npm install
  npm run build  (fails fast with error if JSX is broken)
  npx vercel --yes --prod
     │
     ▼
Vercel
  receives project files
  builds on remote infrastructure
  returns live URL
     │
     ▼
Slack
  posts: ":rocket: Live at: https://..."
  cleans up temp build dir
```

---

## File structure written during implement flow

```
builds/
└── paper_design_<id>/          ← temp dir, deleted after deploy
    ├── package.json            ← Python (scaffold)
    ├── vite.config.js          ← Python (scaffold)
    ├── index.html              ← Python (scaffold)
    ├── public/
    │   └── icon.png            ← Claude (get_fill_image)
    └── src/
        ├── main.jsx            ← Python (scaffold)
        ├── index.css           ← Python (scaffold)
        ├── App.jsx             ← Python (wire_navigation_agent output)
        └── screens/
            ├── LoginScreen.jsx ← Claude (get_jsx + get_computed_styles)
            └── Dashboard.jsx   ← Claude (get_jsx + get_computed_styles)
```

---

## Who writes what

| File | Written by | How |
|------|-----------|-----|
| `package.json`, `vite.config.js`, `index.html`, `main.jsx`, `index.css` | Python | Hardcoded templates in `scaffold_vite_project()` |
| `src/screens/*.jsx` | Claude subprocess | Via `Write` tool (MCP), using JSX from Paper |
| `public/*.png` | Claude subprocess | Via `Write` tool (MCP), using base64 from `get_fill_image` |
| `src/App.jsx` | Python | Claude generates content, Python writes the file |

---

## Intent detection

Before either flow runs, a lightweight Claude call reads only the **last user message** in the thread and returns a single word: `design` or `implement`. This avoids the full thread polluting the classification.

```
"implement this" / "deploy" / "build" / "ship"  →  implement flow
"design" / "create" / "update" / "change"        →  design flow
```

---

## Permissions & sandbox

Claude subprocesses run with project-scoped permissions defined in `.claude/settings.json`:

```
builds/**  →  Write, Edit allowed
builds/*   →  mkdir, npm install, npm run build, npx vercel allowed
```

Nothing outside `builds/` is writable by the subprocess. Global `~/.claude/settings.json` is untouched.

---

## Thread context

Every message in the Slack thread is passed to Claude formatted as:

```
[2026-04-18 10:30 UTC] [user]: design a fitness app dashboard
[2026-04-18 10:31 UTC] [bot]:  Here's your dashboard — clean layout with activity rings...
[2026-04-18 10:45 UTC] [user]: make the background darker
```

Timestamps let Claude understand chronology. The most recent message is the active request; older ones are context.
