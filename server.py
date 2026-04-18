#!/usr/bin/env python3
"""
Autonomous Slack Designer
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
4. Call get_screenshot during the process to review your work.
5. Call finish_working_on_nodes when the design is complete.

After finishing, write a short recap for the Slack user. Format it as:
- A one-line title describing what the screen is (e.g. "Bank App — Login screen")
- A bullet list of the main changes or features, written from a product/UX perspective
Keep it brief and jargon-free. No mention of colours, fonts, or technical implementation details."""


async def run_design_agent(thread: str, on_screenshot) -> str:
    """
    Spawns `claude -p <prompt>` as a subprocess with stream-json output.
    Parses the event stream to extract screenshots and the final text summary.
    Calls `on_screenshot(image_bytes, filename)` for every screenshot Claude produces.
    Returns Claude's final text response.
    """
    prompt = f"{DESIGN_SYSTEM_PROMPT}\n\n---\n\nSlack thread:\n{thread}"

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
    Lightweight Claude call that reads only the last user message and returns
    either 'design' or 'implement' — nothing else.
    """
    # Extract the last user message from the thread for focused intent detection
    last_user_line = ""
    for line in reversed(thread.splitlines()):
        if "[user]:" in line:
            last_user_line = line.split("[user]:", 1)[-1].strip()
            break

    prompt = (
        "Classify the intent of this Slack message.\n\n"
        "Reply with exactly one word:\n"
        "- 'design'     → the user wants to create or update a UI design\n"
        "- 'implement'  → the user wants to export, build, or deploy a design as a real webpage\n\n"
        "Examples of 'implement': implement, build, deploy, ship, export, make it real, create the webpage\n"
        "Examples of 'design': design, create, make, update, change, improve, add, redesign\n\n"
        "Only reply with the single word. No punctuation, no explanation.\n\n"
        f"Message: {last_user_line}"
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
        logger.info("Intent detected: implement")
        return "implement"

    logger.info("Intent detected: design")
    return "design"


# ── Implement flow ────────────────────────────────────────────────────────────

async def run_implement_agent(thread: str, project_dir: str) -> list[str]:
    """
    Claude identifies which artboards to implement from the thread,
    calls get_jsx on each, and writes .jsx files directly to src/screens/.
    Returns the list of component names created (scanned from disk after Claude finishes).
    """
    screens_dir = os.path.join(project_dir, "src", "screens")
    public_dir  = os.path.join(project_dir, "public")

    prompt = (
        "You are exporting UI designs from Paper as React components.\n\n"
        "Read the Slack thread to understand which artboard(s) the user wants to implement. "
        "If they say 'all' or 'everything', export every artboard on the canvas.\n\n"
        "Steps:\n"
        "1. Call get_basic_info to list all artboards\n"
        "2. Match the user's request to the correct artboard ID(s)\n"
        "3. For each artboard:\n"
        "   a. Call get_jsx with exportFormat 'inline-styles'\n"
        "   b. Call get_computed_styles on the artboard node and use the exact values "
        "      to override any approximate values in the JSX\n"
        "   c. Call get_fill_image on any nodes that have image or icon fills; "
        f"     write those as binary files into {public_dir}/\n"
        f"   d. Write the final React component to {screens_dir}/<ComponentName>.jsx\n"
        "4. Each component must be a valid default export React component.\n"
        "5. When all files are written, reply with ONLY the component names "
        "   you created, one per line — no prose, no markdown, no file extensions.\n\n"
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
    Claude generates App.jsx content; Python writes it to disk.
    No MCP tools are needed here — it's pure code generation.
    """
    screens_list = "\n".join(f"- {name}" for name in component_names)

    prompt = (
        "Write a React App.jsx file for a Vite project.\n\n"
        f"The src/screens/ directory contains these components:\n{screens_list}\n\n"
        "Requirements:\n"
        "1. Import all screen components from ./screens/<name>\n"
        "2. Use React.useState to track which screen is currently shown\n"
        "3. Pass a navigate(screenName) function as a prop to each screen\n"
        "   so screens can link to each other (e.g. a login button navigates to Dashboard)\n"
        "4. Start on the first screen in the list\n"
        "5. Add an import for './index.css' at the top\n"
        "6. The function MUST be declared as `export default function App()` — not a separate export statement\n\n"
        "Reply with ONLY the raw file content — no markdown, no code fences, no explanation."
    )

    cmd = [CLAUDE_BIN, "-p", prompt, "--model", "claude-opus-4-6"]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_claude_env(),
    )
    stdout, _ = await process.communicate()
    code = stdout.decode("utf-8", errors="replace").strip()

    app_jsx_path = os.path.join(project_dir, "src", "App.jsx")
    with open(app_jsx_path, "w") as f:
        f.write(code)
    logger.info("App.jsx written (%d chars)", len(code))


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

    await say(text=":thinking_face: Got it — figuring out what you need...", **reply_args)

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
    await say(text=":pencil: On it! Generating your design...", **reply_args)

    # ── Buffer screenshots — only the last one goes to Slack ─────────────────
    screenshot_buffer: list[tuple[bytes, str]] = []

    async def collect_screenshot(image_bytes: bytes, filename: str):
        logger.info("Screenshot buffered (%d bytes)", len(image_bytes))
        screenshot_buffer.append((image_bytes, filename))

    # ── Run the agent ─────────────────────────────────────────────────────────
    try:
        summary = await run_design_agent(thread, collect_screenshot)

        if screenshot_buffer:
            final_bytes, final_filename = screenshot_buffer[-1]
            logger.info("Uploading final screenshot %s (%d bytes) to Slack", final_filename, len(final_bytes))
            await client.files_upload_v2(
                channel=channel,
                filename=final_filename,
                content=final_bytes,
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
    logger.info("Autonomous Slack Designer is running (Socket Mode).")
    logger.info("Using claude binary: %s", CLAUDE_BIN)
    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
