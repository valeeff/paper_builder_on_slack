# Paper Builder on Slack

Paper Builder is an AI agent that designs and deploys a product based on teammates' Slack messages, showing the results right inside the channel.

With Paper Builder you can design, refine, and deploy without leaving the Slack thread.

From a single screen to entire flows.

It is a multi-agent orchestrator, using a Python server and Claude CLI to create designs in Paper through its MCP, and generate a navigable live demo on Vercel.

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
              ├─ Claude exports artboards as JSX + images, sorted left-to-right by canvas position
              ├─ Each screen gets forward (navigate) and back (navigateBack) props wired to CTAs
              ├─ Claude writes App.jsx with useState-based index navigation
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

## Step-by-Step Setup

Follow these steps exactly to get your Paper Builder bot running.

### 1. Clone the repository
First, download the code to your machine and install the Python requirements. The final command below will create a hidden `.env` file. This is a secure configuration file where all your private API keys and tokens will live so they aren't accidentally shown in your code.

```bash
# Clone the repository
git clone https://github.com/YOUR_USERNAME/paper_builder_on_slack.git
cd paper_builder_on_slack

# Setup a Python virtual environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Create your settings file
cp .env.example .env
```

### 2. Set up Claude
The bot uses the Claude CLI to power its agent logic. There are two ways to authenticate:

**Option A — Claude Pro subscription (no API key needed)**
If you have a Claude Pro account, the CLI will use it automatically after login. No API key is required.

**Option B — Anthropic API key (pay-per-use)**
If you want to use API billing instead:
1. Get an API key from the [Anthropic Console](https://console.anthropic.com).
2. Paste it into your `.env` file as `ANTHROPIC_API_KEY=sk-ant-...`

**Both options:**
1. Install the Claude CLI by running: `npm install -g @anthropic-ai/claude-code`
2. Log into the Claude CLI by running: `claude login`

### 3. Setup the Paper Desktop App
Claude needs the Paper app to actually draw the designs. First, ensure the **MCP Server** (Model Context Protocol) is enabled in your Paper app settings.

Next, install the Paper plugin into the **Claude Code CLI** — this is the `claude` binary that `server.py` invokes as a subprocess. The plugin must be installed in the CLI, not in Claude Desktop or any other interface.

**Install the Paper plugin in Claude Code CLI:**
1. Add the custom marketplace: `/plugin marketplace add paper-design/agent-plugins`
2. Install the plugin: `/plugin install paper-desktop@paper`

*Once connected, you should see the Paper MCP server in the list of available MCPs when you run the `/mcp` command in your terminal.*

### 4. Setup Vercel for Deployments
Vercel is used to host your generated React applications.

1. Install the Vercel CLI by running: `npm install -g vercel`
2. Log into Vercel via your terminal by running: `vercel login`

### 5. Create your Slack App
You need a bot in Slack for your team to talk to.

1. Create a new app at [api.slack.com/apps](https://api.slack.com/apps).
2. Go to **Socket Mode** and enable it. Copy the **App-Level Token** (`xapp-...`) to your `.env` file as `SLACK_APP_TOKEN=`.
3. Go to **Socket Mode** settings and add the `connections:write` scope.
4. Under **OAuth & Permissions**, add these **Bot Token Scopes**:
   - `app_mentions:read`
   - `channels:history`
   - `chat:write`
   - `files:write`
5. Install the app to your workspace. Copy the **Bot User OAuth Token** (`xoxb-...`) to your `.env` file as `SLACK_BOT_TOKEN=`.
6. Under **Event Subscriptions** → **Subscribe to bot events**, add `app_mention`.

### 6. Start the bot!
Everything is connected. Make sure the Paper app is open and on a new canvas page, then run:

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

You can use simple standalone requests, or you can have a normal conversation with your team in a Slack thread and simply tag the bot at the very end. 

Paper Builder reads the entire thread history, so it automatically understands the context of your team's discussion without you needing to write a massive, detailed prompt.

```text
@paper-builder design a login screen with email and password fields
@paper-builder update the button colors to match our brand
@paper-builder implement this design

(Or, at the end of a team brainstorming thread):
@paper-builder can you create this flow based on our ideas above?
```

- Design results are posted as screenshots in the thread.
- Implement results are posted as a live Vercel URL.
- Design system preference (if both folder and artboard exist) is asked once per channel and remembered.

---

## Dependencies

```
slack-bolt>=1.21.0    # Slack API client with Socket Mode
python-dotenv>=1.0.0  # Load .env
```
