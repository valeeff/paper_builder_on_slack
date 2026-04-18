# Paper Builder on Slack — Architecture

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
  posts: "Got it, figuring out what you need..."
        │
        ▼
  detect_intent()  ──── "design" ───────────────────────────────────┐
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
  detects intent → "design"
     │
     ▼
Design system detection (runs in parallel before the agent starts)
  ┌─────────────────────────────────────────────────────────────┐
  │  has_folder  = does  ./design_system/  directory exist?     │
  │  has_artboard = does Paper canvas have a "design_system"    │
  │                 artboard? (lightweight Claude call)         │
  └─────────────────────────────────────────────────────────────┘
     │
     ▼
  ┌──────────────┬────────────────────────────────────────────────┐
  │  neither     │ proceed with no design system                  │
  │  folder only │ Claude explores ./design_system/ directory     │
  │  artboard    │ Claude reads "design_system" artboard styles   │
  │  only        │ via get_computed_styles                        │
  │  both        │ check .design_system_choices.json for saved    │
  │              │ channel preference; if none, ask user once     │
  │              │ ("reply with 'folder' or 'artboard'"), save    │
  │              │ answer, never ask again for this channel       │
  └──────────────┴────────────────────────────────────────────────┘
     │
     ▼
run_design_agent()
  spawns: claude -p <prompt> --output-format stream-json
     │
     ▼
Claude subprocess
  1. get_guide          → loads Paper working instructions
  2. get_basic_info     → reads canvas, existing artboards
  [if design_system_source == "folder"]
  3. explores ./design_system/ with Read/Glob tools
  [if design_system_source == "artboard"]
  3. get_computed_styles on "design_system" artboard → extracts tokens
  4. write_html         → draws new elements (new designs)
     or
     update_styles      → edits existing elements (refinements)
     set_text_content   → edits text content
  5. get_screenshot     → takes a snapshot to review work
  6. finish_working_on_nodes → finalises the design
  7. streams result back as JSON events
     │
     ▼
server.py
  parses stream-json line by line
  buffers all screenshots
  captures final text summary
  converts **bold** → *bold* for Slack formatting
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
  detects intent → "implement"
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
  spawns: claude -p <prompt> --output-format stream-json
     │
     ▼
Claude subprocess
  writes App.jsx directly to builds/paper_design_<id>/src/App.jsx
  with useState-based screen router and navigate() prop
     │
     ▼
deploy_to_vercel()
  npm install
  npm run build  (fails fast locally if JSX is broken — logs App.jsx)
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

## Design system detection

Runs on every design request. Four outcomes:

| has_folder | has_artboard | Outcome |
|------------|--------------|---------|
| No | No | No design system — Claude designs freely |
| Yes | No | Claude explores `./design_system/` directory |
| No | Yes | Claude reads `design_system` artboard via `get_computed_styles` |
| Yes | Yes | Ask user once per channel, save preference to `.design_system_choices.json` |

The "both" case:
- Preference is saved per channel in `.design_system_choices.json` (gitignored)
- Persists across server restarts
- Bot asks once, never again for that channel
- User replies with `folder` or `artboard` (also accepts: `repo`, `repository`, `file`, `paper`, `canvas`)

---

## Intent detection

Before either flow runs, a lightweight Claude call reads **all user messages** in the thread (with timestamps) and returns a single word: `design` or `implement`. Timestamps let Claude weight the most recent messages correctly.

```
"implement" / "deploy" / "build" / "ship" / "export"  →  implement flow
"design" / "create" / "update" / "change" / "improve"  →  design flow
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
        ├── App.jsx             ← Claude (wire_navigation_agent, Write tool)
        └── screens/
            ├── LoginScreen.jsx ← Claude (get_jsx + get_computed_styles)
            └── Dashboard.jsx   ← Claude (get_jsx + get_computed_styles)
```

---

## Who writes what

| File | Written by | How |
|------|------------|-----|
| `package.json`, `vite.config.js`, `index.html`, `main.jsx`, `index.css` | Python | Hardcoded templates in `scaffold_vite_project()` |
| `src/screens/*.jsx` | Claude subprocess | Via `Write` tool, using JSX from Paper |
| `public/*.png` | Claude subprocess | Via `Write` tool, using base64 from `get_fill_image` |
| `src/App.jsx` | Claude subprocess | Via `Write` tool in `wire_navigation_agent` |

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

Timestamps let Claude understand chronology. The most recent message is the active request; older ones are context. System messages (joins, renames, deletions) are filtered out before sending.

---

## Slack messages sequence

| Step | Message |
|------|---------|
| On every mention | `:thought_balloon: Got it, figuring out what you need...` |
| Design (with design system) | `:art: On it! Building your design using your design system...` |
| Design (no design system) | `:art: On it! Bringing your design to life...` |
| Implement | `:hammer_and_wrench: On it! Exporting and deploying your design...` |
| Both design systems found | Asks user to choose `folder` or `artboard` |
| Design complete | Screenshot + summary with title and bullet points |
| Implement complete | `:rocket: Live at: <url>` |
| Any error | `:x: Something went wrong: <error>` |
