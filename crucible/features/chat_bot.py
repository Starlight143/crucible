from __future__ import annotations
"""Telegram and Discord bot script generator for Crucible.

This feature creates runnable bot entrypoints with rate-limited run, status,
and report commands. Optional bot dependencies are imported softly inside the
generated scripts.
"""

import json
import os
import time
from typing import Any, Dict

from crucible.feature_registry import (
    BaseFeature,
    FeatureConfig,
    FeatureResult,
    register,
)


TELEGRAM_BOT_SCRIPT = r'''from __future__ import annotations
"""Telegram bot for Crucible pipeline control."""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict

try:
    from telegram.ext import ApplicationBuilder, CommandHandler  # type: ignore
except ImportError:
    ApplicationBuilder = None  # type: ignore[assignment]
    CommandHandler = None  # type: ignore[assignment]


RATE_LIMIT_SECONDS = 1800
_last_run_by_user: Dict[str, float] = {}
_rate_limit_lock: threading.Lock = threading.Lock()


def _workspace() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _runner() -> str:
    return os.path.join(_workspace(), "run_crucible_enhanced.py")


def _rate_limited(user_id: str) -> bool:
    now = time.time()
    with _rate_limit_lock:
        last = _last_run_by_user.get(user_id, 0)
        if now - last < RATE_LIMIT_SECONDS:
            return True
        _last_run_by_user[user_id] = now
    return False


def _run_pipeline(args: str) -> Dict[str, Any]:
    runner = _runner()
    if not os.path.isfile(runner):
        return {"returncode": 2, "stdout": "", "stderr": f"runner not found: {runner}"}
    command = [sys.executable, runner]
    if args:
        command.extend(args.split())
    try:
        process = subprocess.run(command, cwd=_workspace(), text=True, capture_output=True, timeout=3600)
        return {"returncode": process.returncode, "stdout": process.stdout[-1500:], "stderr": process.stderr[-1500:]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"returncode": 1, "stdout": "", "stderr": str(exc)}


async def run_command(update: Any, context: Any) -> None:
    user_id = str(update.effective_user.id if update.effective_user else "unknown")
    if _rate_limited(user_id):
        await update.message.reply_text("Rate limit active. Try again later.")
        return
    result = _run_pipeline(" ".join(context.args))
    await update.message.reply_text(json.dumps(result, indent=2, ensure_ascii=False)[-3500:])


async def status_command(update: Any, context: Any) -> None:
    await update.message.reply_text("Crucible bot is online.")


async def report_command(update: Any, context: Any) -> None:
    await update.message.reply_text(f"Reports directory: {os.path.join(_workspace(), 'saved_projects')}")


def _raw_api_loop(token: str) -> int:
    offset = 0
    base = f"https://api.telegram.org/bot{token}"
    _consecutive_errors = 0
    while True:
        query = urllib.parse.urlencode({"timeout": 30, "offset": offset})
        try:
            with urllib.request.urlopen(f"{base}/getUpdates?{query}", timeout=40) as response:
                updates = json.loads(response.read().decode("utf-8")).get("result", [])
            _consecutive_errors = 0
        except Exception:
            _consecutive_errors += 1
            _sleep_secs = min(5 * (2 ** (_consecutive_errors - 1)), 60)
            time.sleep(_sleep_secs)
            continue
        for item in updates:
            offset = max(offset, int(item.get("update_id", 0)) + 1)
            message = item.get("message") or {}
            chat = message.get("chat") or {}
            text = str(message.get("text") or "")
            chat_id = chat.get("id")
            if not chat_id:
                continue
            if text.startswith("/status"):
                reply = "Crucible bot is online."
            elif text.startswith("/report"):
                reply = f"Reports directory: {os.path.join(_workspace(), 'saved_projects')}"
            elif text.startswith("/run"):
                user_id = str((message.get("from") or {}).get("id") or chat_id)
                reply = "Rate limit active. Try again later." if _rate_limited(user_id) else json.dumps(_run_pipeline(text.replace("/run", "", 1).strip()), indent=2, ensure_ascii=False)[-3500:]
            else:
                continue
            data = urllib.parse.urlencode({"chat_id": chat_id, "text": reply}).encode("utf-8")
            try:
                urllib.request.urlopen(f"{base}/sendMessage", data=data, timeout=10).read()
            except Exception:
                pass
    return 0


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    if ApplicationBuilder is None:
        return _raw_api_loop(token)
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("run", run_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("report", report_command))
    app.run_polling()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


DISCORD_BOT_SCRIPT = r'''from __future__ import annotations
"""Discord bot for Crucible pipeline control."""

import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict

try:
    import discord  # type: ignore
    from discord.ext import commands  # type: ignore
except ImportError as exc:
    raise SystemExit("discord.py is required: pip install discord.py") from exc


RATE_LIMIT_SECONDS = 1800
_last_run_by_user: Dict[str, float] = {}
_rate_limit_lock: threading.Lock = threading.Lock()


def _workspace() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _rate_limited(user_id: str) -> bool:
    now = time.time()
    with _rate_limit_lock:
        last = _last_run_by_user.get(user_id, 0)
        if now - last < RATE_LIMIT_SECONDS:
            return True
        _last_run_by_user[user_id] = now
    return False


def _run_pipeline(arg_text: str) -> Dict[str, Any]:
    runner = os.path.join(_workspace(), "run_crucible_enhanced.py")
    if not os.path.isfile(runner):
        return {"returncode": 2, "stdout": "", "stderr": f"runner not found: {runner}"}
    command = [sys.executable, runner]
    if arg_text:
        command.extend(arg_text.split())
    try:
        process = subprocess.run(command, cwd=_workspace(), text=True, capture_output=True, timeout=3600)
        return {"returncode": process.returncode, "stdout": process.stdout[-1000:], "stderr": process.stderr[-1000:]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"returncode": 1, "stdout": "", "stderr": str(exc)}


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.command(name="run")
async def run_command(ctx: Any, *, arg_text: str = "") -> None:
    user_id = str(ctx.author.id)
    if _rate_limited(user_id):
        await ctx.send("Rate limit active. Try again later.")
        return
    result = _run_pipeline(arg_text)
    embed = discord.Embed(title="Crucible Run", color=0x2E7D32 if result["returncode"] == 0 else 0xC62828)
    embed.add_field(name="Exit Code", value=str(result["returncode"]), inline=True)
    embed.add_field(name="Stdout", value=(result["stdout"] or "empty")[:1024], inline=False)
    embed.add_field(name="Stderr", value=(result["stderr"] or "empty")[:1024], inline=False)
    await ctx.send(embed=embed)


@bot.command(name="status")
async def status_command(ctx: Any) -> None:
    embed = discord.Embed(title="Crucible Status", description="Bot is online.", color=0x1565C0)
    embed.add_field(name="Workspace", value=_workspace(), inline=False)
    await ctx.send(embed=embed)


@bot.command(name="report")
async def report_command(ctx: Any) -> None:
    embed = discord.Embed(title="Crucible Reports", color=0x6A1B9A)
    embed.add_field(name="Directory", value=os.path.join(_workspace(), "saved_projects"), inline=False)
    await ctx.send(embed=embed)


def main() -> int:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN is required")
    bot.run(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _write_text(path: str, content: str) -> None:
    _tmp = path + ".tmp"
    try:
        with open(_tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        os.replace(_tmp, path)
    except OSError as exc:
        try:
            os.unlink(_tmp)
        except OSError:
            pass
        raise RuntimeError(f"cannot write {path}: {exc}") from exc


def _write_json(path: str, payload: Dict[str, Any]) -> None:
    _write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _mkdir(path: str) -> None:
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"cannot create directory {path}: {exc}") from exc


def _setup_md() -> str:
    return """# Crucible Chat Bot Setup

Telegram:

```bash
pip install python-telegram-bot
set TELEGRAM_BOT_TOKEN=your-token
python bots/telegram_bot.py
```

Discord:

```bash
pip install discord.py
set DISCORD_BOT_TOKEN=your-token
python bots/discord_bot.py
```

Both bots expose run, status, and report commands with a per-user 1800 second
rate limit for pipeline execution.
"""


@register("chat_bot")
class ChatBotFeature(BaseFeature):
    name = "chat_bot"
    label = "Telegram + Discord Bot Generator"
    requires: list[str] = []

    def run(self, run_dir: str, config: FeatureConfig) -> FeatureResult:
        start = time.monotonic()
        if os.environ.get("CHATBOT_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            return FeatureResult(feature=self.name, success=True, summary="disabled", skipped=True, skip_reason="disabled")
        try:
            bots_dir = os.path.join(run_dir, "bots")
            _mkdir(bots_dir)
            telegram_path = os.path.join(bots_dir, "telegram_bot.py")
            discord_path = os.path.join(bots_dir, "discord_bot.py")
            setup_path = os.path.join(run_dir, "bots_setup.md")
            report_path = os.path.join(run_dir, "chat_bot_report.json")
            _write_text(telegram_path, TELEGRAM_BOT_SCRIPT)
            _write_text(discord_path, DISCORD_BOT_SCRIPT)
            _write_text(setup_path, _setup_md())
            report = {"telegram_generated": True, "discord_generated": True, "telegram_path": telegram_path, "discord_path": discord_path, "channel_id": os.environ.get("CHATBOT_CHANNEL_ID", "")}
            _write_json(report_path, report)
            return FeatureResult(feature=self.name, success=True, summary="Chat bot scripts generated", details={**report, "report_path": report_path}, duration_seconds=time.monotonic() - start)
        except Exception as exc:
            return FeatureResult(feature=self.name, success=False, summary="Chat bot generation failed", error=str(exc), duration_seconds=time.monotonic() - start)
