#!/usr/bin/env python3
"""
Paper Builder on Slack
=========================
The receptionist: bridges Slack mentions → Claude CLI (with Paper MCP) → Slack screenshots.

Flow:
  1. Someone tags the bot in Slack with a design brief
  2. This server calls the `claude` CLI as a subprocess, passing the brief
  3. Claude (which already has Paper MCP configured) autonomously creates the design
  4. We parse the streaming JSON output to intercept screenshots in real-time
  5. Each screenshot is uploaded back to the Slack thread as it arrives
"""

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone

from dotenv import load_dotenv
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

SLACK_BOT_TOKEN  = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN  = os.environ["SLACK_APP_TOKEN"]
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")  # optional — uses Pro subscription if unset

# Path to the `claude` CLI — defaults to the one on PATH
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")

# Project root — build directories are created here so Claude's sandbox allows writes
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

def _claude_env() -> dict:
    """Subprocess environment — injects API key if set, enabling API billing over Pro rate limits."""
    env = os.environ.copy()
    if ANTHROPIC_API_KEY:
        env["ANTHROPIC_API_KEY"] = ANTHROPIC_API_KEY
    return env

# ── Design system preferences ─────────────────────────────────────────────────

# Persistent per-channel choice when both folder and artboard are present.
# Stored as { channel_id: "folder" | "artboard" }
_DS_CHOICES_FILE = os.path.join(PROJECT_ROOT, ".design_system_choices.json")

# In-memory map: channel_id -> timestamp (float) of the bot's question.
# Present means we're waiting for the user's design system choice reply.
_pending_ds_choice: dict[str, float] = {}


def _msg_ts(thread_line: str) -> float:
    """Extract the UTC timestamp as a float from a '[YYYY-MM-DD HH:MM UTC] ...' thread line."""
    match = re.match(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}) UTC\]', thread_line)
    if not match:
        return 0.0
    return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp()


def _load_ds_choices() -> dict:
    try:
        with open(_DS_CHOICES_FILE) as f:
            return json.load(f)
    except OSError:
        return {}


def _save_ds_choice(channel_id: str, choice: str) -> None:
    choices = _load_ds_choices()
    choices[channel_id] = choice
    with open(_DS_CHOICES_FILE, "w") as f:
        json.dump(choices, f)


async def _check_paper_design_system() -> bool:
    """
    Spawns a lightweight Claude call to check if a 'design_system' artboard
    exists on the active Paper canvas. Returns True if found.
    """
    prompt = (
        "Call get_basic_info. Check whether any artboard is named exactly "
        "'design_system' (case-insensitive). Reply with only 'yes' or 'no'."
    )
    cmd = [
        CLAUDE_BIN, "-p", prompt,
        "--model", "claude-opus-4-6",
        "--output-format", "stream-json",
        "--verbose",
    ]
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_claude_env(),
    )
    final_text = ""
    async for raw_line in process.stdout:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            final_text = event.get("result", "")
    await process.wait()
    return "yes" in final_text.lower()


# ── Clients ───────────────────────────────────────────────────────────────────

app = AsyncApp(token=SLACK_BOT_TOKEN)

# ── Design agent (Option A: claude CLI subprocess) ────────────────────────────

DESIGN_SYSTEM_PROMPT = """You are an expert UI/UX designer working inside the Paper design tool.

You will receive a Slack thread — a conversation between one or more users and you (the bot).
The thread may contain multiple messages, previous design requests, past bot replies, and follow-up instructions.

Each message includes a timestamp. Use them to understand the chronology.

Your job:
1. Read the full thread to understand the context and history.
2. Focus on the MOST RECENT messages — they represent the current request.
   Older messages are background context only; do not act on them.
   If the latest message is a refinement ("make it darker", "add a nav bar"), apply it to the existing design.
   If it's a new request, start fresh.
3. Use the Paper MCP tools to build or update the design.

Paper workflow:
1. Call get_guide with topic 'paper-mcp-instructions' to load your working instructions.
2. Call get_basic_info to understand the canvas and any existing artboards.
3. Build or update the design:
   - For NEW designs: use write_html to build one visual group at a time.
   - For EDITS to existing designs (colour changes, text updates, style tweaks):
     prefer update_styles and set_text_content over rewriting with write_html.
     Only use write_html when adding entirely new elements.
4. Call get_screenshot to review your work, but ALWAYS pass the node ID of the specific
   artboard you just created or modified — never screenshot the full canvas.
   This ensures the preview shows only the new or changed design, not pre-existing artboards.
5. Call finish_working_on_nodes when the design is complete.

After finishing, write a short recap for the Slack user. Format it as:
- A one-line title describing what the screen is (e.g. "Bank App — Login screen")
- A bullet list of the main changes or features, written from a product/UX perspective
Keep it brief and jargon-free. No mention of colours, fonts, or technical implementation details."""


async def run_design_agent(thread: str, on_screenshot, design_system_source: str = "none") -> str:
    """
    Spawns `claude -p <prompt>` as a subprocess with stream-json output.
    Parses the event stream to extract screenshots and the final text summary.
    Calls `on_screenshot(image_bytes, filename)` for every screenshot Claude produces.
    design_system_source: "folder" | "artboard" | "none"
    Returns Claude's final text response.
    """
    if design_system_source == "folder":
        ds_dir = os.path.join(PROJECT_ROOT, "design_system")
        design_system_instruction = (
            f"\n\n---\n\nDesign system: a design system exists at {ds_dir}. "
            "Before designing, explore that directory to understand its tokens — "
            "colours, typography, spacing, radius, and any other design decisions. "
            "Apply those values consistently throughout your work."
        )
    elif design_system_source == "artboard":
        design_system_instruction = (
            "\n\n---\n\nDesign system: there is a 'design_system' artboard on the canvas. "
            "Before designing, read its styles using get_computed_styles to extract all tokens — "
            "colours, typography, spacing, radius, and any other design decisions. "
            "Apply those values consistently throughout your work."
        )
    else:
        design_system_instruction = ""

    prompt = f"{DESIGN_SYSTEM_PROMPT}{design_system_instruction}\n\n---\n\nSlack thread:\n{thread}"

    cmd = [
        CLAUDE_BIN,
        "-p", prompt,
        "--model", "claude-opus-4-6",
        "--output-format", "stream-json",
        "--verbose",
    ]

    logger.info("Spawning claude CLI...")
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=100 * 1024 * 1024,  # 100 MB — screenshots arrive as large base64 JSON lines
        env=_claude_env(),
    )

    final_text   = ""
    screenshot_n = 0

    # Read the stream line by line — each line is a JSON event
    async for raw_line in process.stdout:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue

        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("Non-JSON line: %s", line[:120])
            continue

        event_type = event.get("type", "")

        logger.debug("EVENT: %s", event_type)

        # ── Final result ──────────────────────────────────────────────────────
        if event_type == "result":
            final_text = event.get("result", "")
            logger.info("Claude finished. Cost: $%.4f", event.get("cost_usd", 0))

        # ── User events contain tool_result blocks (including screenshots) ─────
        elif event_type == "user":
            for msg_block in event.get("message", {}).get("content", []):
                if not isinstance(msg_block, dict) or msg_block.get("type") != "tool_result":
                    continue
                for inner in msg_block.get("content", []):
                    if not isinstance(inner, dict) or inner.get("type") != "image":
                        continue
                    b64 = inner.get("source", {}).get("data", "")
                    if b64:
                        screenshot_n += 1
                        filename = f"design_{screenshot_n}.png"
                        try:
                            image_bytes = base64.b64decode(b64)
                            logger.info("Screenshot %s (%d bytes)", filename, len(image_bytes))
                            await on_screenshot(image_bytes, filename)
                        except Exception as exc:
                            logger.warning("Failed to decode screenshot: %s", exc)

        # ── Tool calls — log progress ─────────────────────────────────────────
        elif event_type == "tool_use":
            logger.info("Tool call → %s", event.get("name", "?"))

        # ── Assistant text — log progress ─────────────────────────────────────
        elif event_type == "assistant":
            message = event.get("message", {})
            for block in message.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    snippet = block["text"][:80].replace("\n", " ")
                    logger.info("Claude: %s…", snippet)

    stderr_output = (await process.stderr.read()).decode("utf-8", errors="replace")
    if stderr_output.strip():
        logger.warning("claude stderr: %s", stderr_output[:500])

    await process.wait()

    if process.returncode != 0:
        raise RuntimeError(
            f"`claude` exited with code {process.returncode}. "
            f"stderr: {stderr_output[:300]}"
        )

    return final_text.strip() or "Design complete."


async def detect_intent(thread: str) -> str:
    """
    Lightweight Claude call that reads the last few user messages and returns
    either 'design' or 'implement' — nothing else.
    """
    # Pass all user messages with timestamps so Claude can reason about recency
    user_lines = [
        line
        for line in thread.splitlines()
        if "[user]:" in line
    ]
    recent_messages = "\n".join(user_lines)

    prompt = (
        "Classify the overall intent of a Slack conversation based on the user's messages below.\n"
        "Each message includes a timestamp — use them to understand which messages are most recent.\n\n"
        "Reply with exactly one word:\n"
        "- 'design'     → the user wants to create or update a UI design\n"
        "- 'implement'  → the user wants to export, build, or deploy a design as a real webpage\n\n"
        "Examples of 'implement': implement, build, deploy, ship, export, make it real, create the webpage\n"
        "Examples of 'design': design, create, make, update, change, improve, add, redesign\n\n"
        "The most recent messages carry the most weight.\n"
        "Only reply with the single word. No punctuation, no explanation.\n\n"
        f"User messages:\n{recent_messages}"
    )

    cmd = [CLAUDE_BIN, "-p", prompt, "--model", "claude-opus-4-6"]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await process.communicate()
    intent = stdout.decode("utf-8", errors="replace").strip().lower()

    # Guard against unexpected output — default to design
    if "implement" in intent:
        logger.info("Intent detected: implement (from: %s)", recent_messages.replace("\n", " | "))
        return "implement"

    logger.info("Intent detected: design (from: %s)", recent_messages.replace("\n", " | "))
    return "design"


# ── Implement flow ────────────────────────────────────────────────────────────

async def run_implement_agent(thread: str, project_dir: str) -> list[str]:
    """
    Claude identifies which artboards to implement from the thread,
    calls get_jsx on each, and writes .jsx files directly to src/screens/.
    Artboards are sorted left-to-right by their canvas position, which defines
    the navigation order. Each screen receives `navigate` (forward) and
    `navigateBack` (backward) props and has its CTA / back buttons wired up.
    Returns the component names in canvas order (left to right).
    """
    screens_dir = os.path.join(project_dir, "src", "screens")
    public_dir  = os.path.join(project_dir, "public")

    prompt = (
        "You are exporting UI designs from Paper as interactive React components.\n\n"
        "Read the Slack thread to understand which artboard(s) the user wants to implement. "
        "If they say 'all' or 'everything', export every artboard on the canvas "
        "(excluding any artboard named 'design_system').\n\n"
        "Steps:\n"
        "1. Call get_basic_info to list all artboards.\n"
        "2. Sort the target artboards by their `left` position ascending — "
        "   this left-to-right canvas order is the navigation order.\n"
        "3. For each artboard (in sorted order):\n"
        "   a. Call get_jsx with exportFormat 'inline-styles'\n"
        "   b. Call get_computed_styles on the artboard node and use the exact values "
        "      to override any approximate values in the JSX\n"
        "   c. Call get_fill_image on any nodes that have image or icon fills; "
        f"     write those as binary files into {public_dir}/\n"
        "   d. Add two props to the default-export function signature: "
        "      `navigate` (go forward) and `navigateBack` (go back).\n"
        "   e. Wire interactivity:\n"
        "      - The PRIMARY forward action (the most prominent CTA button — "
        "        e.g. 'Get Started', 'Continue', 'Next', 'Select', the last/bottom button) "
        "        must call `onClick={() => navigate()}` — but ONLY if this is not the last screen.\n"
        "      - The BACK element (a '<' chevron, back arrow SVG, or any element in the "
        "        top-left header area that visually reads as a back control) "
        "        must call `onClick={() => navigateBack()}` — but ONLY if this is not the first screen.\n"
        "      - All other elements remain static.\n"
        f"   f. Write the final React component to {screens_dir}/<ComponentName>.jsx\n"
        "4. Each component must be a valid default export React component.\n"
        "5. When all files are written, reply with ONLY the component names "
        "   in canvas order (left to right), one per line — "
        "   no prose, no markdown, no file extensions.\n\n"
        f"Slack thread:\n{thread}"
    )

    cmd = [
        CLAUDE_BIN, "-p", prompt,
        "--model", "claude-opus-4-6",
        "--output-format", "stream-json",
        "--verbose",
    ]

    logger.info("Spawning implement agent...")
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=100 * 1024 * 1024,
        env=_claude_env(),
    )

    final_text = ""
    async for raw_line in process.stdout:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            final_text = event.get("result", "")
        elif event.get("type") == "tool_use":
            logger.info("Implement agent → %s", event.get("name", "?"))
        elif event.get("type") == "assistant":
            for block in event.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    logger.info("Implement: %s…", block["text"][:80].replace("\n", " "))

    await process.wait()

    component_names = [name.strip() for name in final_text.splitlines() if name.strip()]
    logger.info("Components written: %s", component_names)
    return component_names


def scaffold_vite_project(project_dir: str) -> None:
    """Write static Vite + React project files."""
    src_dir     = os.path.join(project_dir, "src")
    screens_dir = os.path.join(src_dir, "screens")
    public_dir  = os.path.join(project_dir, "public")
    os.makedirs(screens_dir, exist_ok=True)
    os.makedirs(public_dir, exist_ok=True)

    files = {
        os.path.join(project_dir, "package.json"): json.dumps({
            "name": "paper-design-demo",
            "version": "1.0.0",
            "type": "module",
            "scripts": {"dev": "vite", "build": "vite build", "preview": "vite preview"},
            "dependencies": {"react": "^18.2.0", "react-dom": "^18.2.0"},
            "devDependencies": {"@vitejs/plugin-react": "^4.0.0", "vite": "^5.0.0"},
        }, indent=2),

        os.path.join(project_dir, "vite.config.js"): (
            "import { defineConfig } from 'vite'\n"
            "import react from '@vitejs/plugin-react'\n\n"
            "export default defineConfig({ plugins: [react()] })\n"
        ),

        os.path.join(project_dir, "index.html"): (
            '<!DOCTYPE html>\n<html lang="en">\n  <head>\n'
            '    <meta charset="UTF-8" />\n'
            '    <meta name="viewport" content="width=device-width, initial-scale=1.0" />\n'
            '    <title>Design Demo</title>\n  </head>\n  <body>\n'
            '    <div id="root"></div>\n'
            '    <script type="module" src="/src/main.jsx"></script>\n'
            '  </body>\n</html>\n'
        ),

        os.path.join(src_dir, "main.jsx"): (
            "import React from 'react'\n"
            "import ReactDOM from 'react-dom/client'\n"
            "import App from './App'\n\n"
            "ReactDOM.createRoot(document.getElementById('root')).render(\n"
            "  <React.StrictMode><App /></React.StrictMode>\n"
            ")\n"
        ),

        os.path.join(src_dir, "index.css"): (
            "*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }\n"
            "body { font-family: sans-serif; background: #000; }\n"
        ),
    }

    for path, content in files.items():
        with open(path, "w") as f:
            f.write(content)

    logger.info("Vite project scaffolded at %s", project_dir)


async def wire_navigation_agent(project_dir: str, component_names: list[str]) -> None:
    """
    Claude generates and writes App.jsx directly to disk via its Write tool.
    """
    screens_list = "\n".join(f"- {name}" for name in component_names)
    app_jsx_path = os.path.join(project_dir, "src", "App.jsx")

    prompt = (
        f"Write a React App.jsx file to {app_jsx_path}.\n\n"
        f"The screens below are listed in navigation order (first → last):\n{screens_list}\n\n"
        "Requirements:\n"
        "1. Import all screen components from ./screens/<name>\n"
        "2. Store them in an array called `screens` in the same order as the list above.\n"
        "3. Use React.useState to track `currentIndex` (integer), starting at 0.\n"
        "4. Define two navigation helpers:\n"
        "   - `navigate`     → setCurrentIndex(i => Math.min(i + 1, screens.length - 1))\n"
        "   - `navigateBack` → setCurrentIndex(i => Math.max(i - 1, 0))\n"
        "5. Render only `screens[currentIndex]` and pass both `navigate` and `navigateBack` as props.\n"
        "6. Add an import for './index.css' at the top.\n"
        "7. Declare the function as `export default function App()`\n\n"
        "Write the file, then confirm."
    )

    cmd = [
        CLAUDE_BIN, "-p", prompt,
        "--model", "claude-opus-4-6",
        "--output-format", "stream-json",
        "--verbose",
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_claude_env(),
    )

    async for raw_line in process.stdout:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "tool_use":
            logger.info("Nav agent → %s", event.get("name", "?"))

    await process.wait()
    logger.info("App.jsx written by Claude")


async def deploy_to_vercel(project_dir: str) -> str:
    """
    Runs npm install + vercel deploy inside project_dir.
    Returns the live deployment URL.
    """
    # Install dependencies
    logger.info("Running npm install...")
    install = await asyncio.create_subprocess_exec(
        "npm", "install",
        cwd=project_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await install.communicate()

    # Build locally first so errors surface before deploying
    logger.info("Running npm run build...")
    build = await asyncio.create_subprocess_exec(
        "npm", "run", "build",
        cwd=project_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    build_stdout, build_stderr = await build.communicate()
    build_output = build_stdout.decode("utf-8", errors="replace") + build_stderr.decode("utf-8", errors="replace")
    if build.returncode != 0:
        # Log App.jsx so we can diagnose what Claude generated
        app_jsx = os.path.join(project_dir, "src", "App.jsx")
        try:
            with open(app_jsx) as f:
                logger.error("App.jsx content:\n%s", f.read())
        except OSError:
            logger.error("App.jsx not found")
        raise RuntimeError(f"npm run build failed:\n{build_output}")
    logger.info("Build succeeded")

    # Deploy to Vercel
    logger.info("Deploying to Vercel...")
    deploy = await asyncio.create_subprocess_exec(
        "npx", "vercel", "--yes", "--prod",
        cwd=project_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await deploy.communicate()
    output = stdout.decode("utf-8", errors="replace") + stderr.decode("utf-8", errors="replace")
    logger.info("Vercel output:\n%s", output)

    # Extract URL from output
    match = re.search(r'https://[^\s]+\.vercel\.app', output)
    if match:
        return match.group()

    raise RuntimeError(f"Could not find deployment URL in Vercel output:\n{output[:500]}")


# ── Slack event handler ───────────────────────────────────────────────────────

@app.event("app_mention")
async def handle_mention(event: dict, say, client):
    """
    Triggered whenever someone tags the bot in a channel.
    Runs the design agent and posts screenshots back to the same thread.
    """
    channel   = event["channel"]
    thread_ts = event.get("thread_ts", event["ts"])

    # ── Gather context BEFORE acknowledging so our reply isn't included ─────
    bot_info    = await client.auth_test()
    bot_user_id = bot_info["user_id"]

    in_thread = "thread_ts" in event and event["thread_ts"] != event["ts"]

    if in_thread:
        # Message is a reply inside a thread — fetch the full thread
        history  = await client.conversations_replies(channel=channel, ts=thread_ts)
        messages = history.get("messages", [])
    else:
        # Message is in the main channel — fetch recent channel history up to this message
        history  = await client.conversations_history(
            channel=channel,
            latest=event["ts"],
            limit=50,
            inclusive=True,
        )
        messages = list(reversed(history.get("messages", [])))

    # Patterns that indicate Slack system messages, not real user content
    NOISE_PATTERNS = (
        "has joined the channel",
        "has left the channel",
        "has renamed the channel",
        "This message was deleted",
        "set the channel",
    )

    thread_lines = []
    for m in messages:
        text = m.get("text", "").strip()
        if not text:
            continue
        # Skip Slack system messages
        if any(p in text for p in NOISE_PATTERNS):
            continue
        # Strip bare bot mentions with no surrounding content
        stripped = text.replace(f"<@{bot_user_id}>", "").strip()
        if not stripped:
            continue
        ts_readable = datetime.fromtimestamp(
            float(m["ts"].split(".")[0]), tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")
        speaker = "[bot]" if (m.get("user") == bot_user_id or m.get("bot_id")) else "[user]"
        thread_lines.append(f"[{ts_readable}] {speaker}: {stripped}")

    # Guard: if there's nothing meaningful to act on, ask the user for a brief
    reply_args = {"thread_ts": thread_ts} if in_thread else {}

    if not thread_lines:
        await say(
            text="Please describe what you'd like me to design! e.g. _@PaperDesigner create a login screen for a fitness app_",
            **reply_args,
        )
        return

    thread = "\n".join(thread_lines)
    logger.info("Thread sent to Claude:\n%s", thread)

    await say(text=":thought_balloon: Got it, figuring out what you need...", **reply_args)

    # ── Detect intent ─────────────────────────────────────────────────────────
    intent = await detect_intent(thread)

    if intent == "implement":
        await say(text=":hammer_and_wrench: On it! Exporting and deploying your design...", **reply_args)
        builds_root = os.path.join(PROJECT_ROOT, "builds")
        os.makedirs(builds_root, exist_ok=True)
        project_dir = tempfile.mkdtemp(prefix="paper_design_", dir=builds_root)
        try:

            # 1. Scaffold static Vite files
            scaffold_vite_project(project_dir)

            # 2. Export artboards as React components
            component_names = await run_implement_agent(thread, project_dir)
            if not component_names:
                await say(text=":x: Couldn't find any artboards to export. Make sure Paper has designs on the canvas.", **reply_args)
                return

            logger.info("Exported components: %s", component_names)

            # 3. Wire navigation between screens
            await wire_navigation_agent(project_dir, component_names)

            # 4. Deploy to Vercel
            url = await deploy_to_vercel(project_dir)

            await say(text=f":rocket: Live at: {url}", **reply_args)

        except Exception as exc:
            logger.exception("Implement flow failed")
            await say(text=f":x: Something went wrong: `{exc}`", **reply_args)
        finally:
            shutil.rmtree(project_dir, ignore_errors=True)
        return

    # ── Design flow ───────────────────────────────────────────────────────────

    # ── Detect which design systems are available ─────────────────────────────
    has_folder  = os.path.isdir(os.path.join(PROJECT_ROOT, "design_system"))
    has_artboard = await _check_paper_design_system()
    logger.info("Design systems — folder: %s, artboard: %s", has_folder, has_artboard)

    # ── Case 4: both exist — ask the user (once per channel) ─────────────────
    if has_folder and has_artboard:
        choices = _load_ds_choices()
        if channel in choices:
            ds_source = choices[channel]
            logger.info("Using remembered design system choice: %s", ds_source)
        elif channel in _pending_ds_choice:
            # User just replied — only consider messages sent after the bot's question
            asked_at = _pending_ds_choice[channel]
            last_user_msg = next(
                (l.split("[user]:", 1)[-1].strip().lower()
                 for l in reversed(thread_lines)
                 if "[user]:" in l and _msg_ts(l) > asked_at),
                ""
            )
            if any(w in last_user_msg for w in ("folder", "repo", "repository", "file")):
                ds_source = "folder"
            elif any(w in last_user_msg for w in ("artboard", "paper", "canvas")):
                ds_source = "artboard"
            else:
                await say(
                    text="Sorry, I didn't catch that. Please reply with *folder* (use the repo design system) or *artboard* (use the Paper canvas design system).",
                    **reply_args,
                )
                return
            del _pending_ds_choice[channel]
            _save_ds_choice(channel, ds_source)
            logger.info("Design system choice saved: %s", ds_source)
        else:
            # First time — ask and record when we asked
            _pending_ds_choice[channel] = float(event["ts"])
            await say(
                text=(
                    "I found a design system in two places: the *repository folder* and a *Paper canvas artboard*. "
                    "Which one should I use for this channel going forward?\n\n"
                    "Reply by mentioning me with *folder* or *artboard*."
                ),
                **reply_args,
            )
            return
    elif has_folder:
        ds_source = "folder"
    elif has_artboard:
        ds_source = "artboard"
    else:
        ds_source = "none"

    if ds_source == "none":
        design_msg = ":art: On it! Bringing your design to life..."
    elif has_folder and has_artboard:
        source_label = "repository" if ds_source == "folder" else "Paper canvas"
        design_msg = f":art: On it! Building your design using your design system from the {source_label}..."
    else:
        design_msg = ":art: On it! Building your design using your design system..."
    await say(text=design_msg, **reply_args)

    # ── Buffer screenshots — all will be uploaded to Slack ───────────────────
    screenshot_buffer: list[tuple[bytes, str]] = []

    async def collect_screenshot(image_bytes: bytes, filename: str):
        logger.info("Screenshot buffered (%d bytes)", len(image_bytes))
        screenshot_buffer.append((image_bytes, filename))

    # ── Run the agent ─────────────────────────────────────────────────────────
    try:
        summary = await run_design_agent(thread, collect_screenshot, design_system_source=ds_source)
        summary = re.sub(r'\*\*(.+?)\*\*', r'*\1*', summary)

        if screenshot_buffer:
            for img_bytes, img_filename in screenshot_buffer:
                logger.info("Uploading screenshot %s (%d bytes) to Slack", img_filename, len(img_bytes))
                await client.files_upload_v2(
                    channel=channel,
                    filename=img_filename,
                    content=img_bytes,
                    title="Design preview",
                    **reply_args,
                )
            await say(
                text=f":white_check_mark: Done!\n\n{summary}".strip(),
                **reply_args,
            )
        else:
            await say(text=summary, **reply_args)

    except Exception as exc:
        logger.exception("Design agent failed")
        await say(text=f":x: Something went wrong: `{exc}`", **reply_args)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    handler = AsyncSocketModeHandler(app, SLACK_APP_TOKEN)
    logger.info("Paper Builder on Slack is running (Socket Mode).")
    logger.info("Using claude binary: %s", CLAUDE_BIN)
    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
