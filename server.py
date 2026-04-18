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
from datetime import datetime

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

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]

# Path to the `claude` CLI — defaults to the one on PATH
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")

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
3. Build or update the design using write_html, one visual group at a time.
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

    thread_lines = []
    for m in messages:
        text = m.get("text", "").strip()
        if not text:
            continue
        # Strip bare bot mentions with no surrounding content
        stripped = text.replace(f"<@{bot_user_id}>", "").strip()
        if not stripped:
            continue
        ts_readable = datetime.utcfromtimestamp(float(m["ts"].split(".")[0])).strftime("%Y-%m-%d %H:%M UTC")
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

    # Acknowledge after capturing the thread
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
