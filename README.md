# Paper Builder on Slack

An AI-powered design bot that listens for mentions in Slack and autonomously creates UI designs or deploys them as live web apps.

It connects three systems: **Slack** (user interface), **Claude CLI with Paper MCP** (AI design agent), and **Vercel** (deployment).

---

## What it does

Mention the bot in any Slack channel and describe what you want:

- **Design requests** — Claude opens Paper, creates or edits the design on canvas, and posts screenshots back to the thread.
- **Implement requests** — Claude exports the design as React JSX, scaffolds a Vite project, and deploys it to Vercel. The bot posts the live URL.

The bot reads the full thread history so it understands context and can iterate on prior designs.

---

## Architecture

```
User mentions bot in Slack
        ↓
server.py receives event (Socket Mode)
        ↓
Fetch thread history → detect_intent() → "design" or "implement"?
        │
        ├── DESIGN FLOW
        │     ├─ Check for design system (folder / artboard / ask user)
        │     ├─ Spawn: claude -p <prompt> --output-format stream-json
        │     ├─ Claude calls Paper MCP tools to create/edit the design
        │     └─ Post screenshots + summary to Slack thread
        │
        └── IMPLEMENT FLOW
              ├─ Scaffold a Vite + React project in builds/paper_design_<id>/
              ├─ Claude exports artboards as JSX + images
              ├─ Claude writes App.jsx with routing between artboards
              ├─ npm install → npm run build → npx vercel --prod
              └─ Post live URL to Slack thread
```

### Design system detection

Before running the design agent, the bot checks for a design system to guide Claude's choices:

| `design_system/` folder | `design_system` artboard | Behavior |
|------------------------|--------------------------|----------|
| No | No | Claude designs freely |
| Yes | No | Claude reads the folder's component structure |
| No | Yes | Claude reads the artboard's computed styles |
| Yes | Yes | Ask the user once per channel; preference is saved to `.design_system_choices.json` |

### Key files

| File | Role |
|------|------|
| `server.py` | Main orchestrator: Slack event handler, Claude subprocess manager, deploy pipeline |
| `.env` | Credentials (gitignored — copy from `.env.example`) |
| `.design_system_choices.json` | Per-channel design system preference (auto-created) |
| `.claude/settings.json` | Claude Code permissions scoped to this project |
| `design_system/` | Optional reference design system (React + Tailwind + shadcn/ui) |
| `builds/` | Temporary Vite projects created during implement flow (gitignored) |

---

## Prerequisites

- Python 3.8+
- [Claude Code CLI](https://claude.ai/code) installed and authenticated (`claude` on your PATH)
- Paper desktop app running with MCP server enabled
- Vercel CLI: `npm install -g vercel` (and logged in via `vercel login`)
- A Slack app with Socket Mode enabled (see below)

---

## Setup

### 1. Clone and create a virtual environment

```bash
cd paper_builder_on_slack
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```env
# Slack — from api.slack.com/apps
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...

# Anthropic — from console.anthropic.com
ANTHROPIC_API_KEY=sk-ant-...

# Optional: path to the claude binary if not on your PATH
# CLAUDE_BIN=/usr/local/bin/claude
```

### 3. Create the Slack app

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and create a new app.
2. Under **Socket Mode**, enable it and generate an **App-Level Token** (`xapp-...`). Add the `connections:write` scope.
3. Under **OAuth & Permissions**, add these **Bot Token Scopes**:
   - `app_mentions:read`
   - `channels:history`
   - `chat:write`
   - `files:write`
4. Install the app to your workspace and copy the **Bot User OAuth Token** (`xoxb-...`).
5. Under **Event Subscriptions → Subscribe to bot events**, add `app_mention`.

### 4. (Optional) Set up the reference design system

```bash
cd design_system
npm install
cd ..
```

### 5. Start the bot

```bash
python server.py
```

You should see:

```
Paper Builder on Slack is running (Socket Mode).
Using claude binary: claude
```

---

## Usage

Mention the bot in any Slack channel:

```
@paper-builder design a login screen with email and password fields
@paper-builder update the button colors to match our brand
@paper-builder implement this design
```

- Design results are posted as screenshots in the thread.
- Implement results are posted as a live Vercel URL.
- Design system preference (if both folder and artboard exist) is asked once per channel and remembered.

---

## Dependencies

```
slack-bolt>=1.21.0    # Slack API client with Socket Mode
aiohttp>=3.9.0        # Async HTTP
python-dotenv>=1.0.0  # Load .env
```
