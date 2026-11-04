"""Telegram front end (long polling). Only allowlisted user IDs get any response."""

from __future__ import annotations

import logging

import anthropic
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from .actions import Actions
from .agent import Agent, compose_reply
from .config import Settings
from .render import DRY_RUN_PREFIX
from .store import RateLimiter, Store
from .yuvomi import YuvomiClient

log = logging.getLogger(__name__)

HELP_TEXT = (
    "Hi! Message me in plain English, for example:\n"
    "- Add milk and eggs to the grocery list\n"
    "- Add call the plumber to my to do list\n"
    "- Add an event: dentist Friday at 3pm\n"
    "- What's on the grocery list?"
)


class Bot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = Store(settings.db_path)
        self.limiter = RateLimiter(settings.rate_limit_per_minute)
        self.yuvomi = YuvomiClient(settings.yuvomi_url, settings.yuvomi_api_token)
        self.agent = Agent(
            anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key),
            model=settings.claude_model,
            effort=settings.claude_effort,
            refusal_fallback=settings.claude_refusal_fallback,
            max_iterations=settings.max_tool_iterations,
            max_tokens_per_message=settings.max_tokens_per_message,
            tz=settings.tz,
        )

    def user_name(self, update: Update) -> str:
        user = update.effective_user
        return self.settings.user_names.get(user.id) or user.first_name or "Someone"

    async def on_help(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        prefix = DRY_RUN_PREFIX if self.settings.dry_run else ""
        await update.effective_message.reply_text(prefix + HELP_TEXT)

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        user = update.effective_user
        if not self.limiter.allow(user.id):
            await message.reply_text("Slow down a little, please try again in a minute.")
            return

        await context.bot.send_chat_action(message.chat_id, ChatAction.TYPING)
        actions = Actions(self.yuvomi, self.settings.yuvomi_grocery_list, self.settings.dry_run)
        history = self.store.recent_history(message.chat_id)
        try:
            result = await self.agent.run(self.user_name(update), message.text, actions, history)
            reply = result.reply
        except Exception:
            log.exception("agent run failed")
            reply = compose_reply(
                actions.log, "", ["Something went wrong on my side. Anything not listed above was not done."],
                self.settings.dry_run,
            )
        self.store.append_exchange(message.chat_id, message.text, reply)
        await message.reply_text(reply)

    def build(self) -> Application:
        app = ApplicationBuilder().token(self.settings.telegram_bot_token).post_shutdown(self._shutdown).build()
        # Messages from anyone else match no handler and are dropped silently.
        allowed = filters.User(user_id=list(self.settings.allowed_telegram_ids))
        app.add_handler(CommandHandler(["start", "help"], self.on_help, filters=allowed))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & allowed, self.on_text))
        return app

    async def _shutdown(self, _: Application) -> None:
        await self.yuvomi.aclose()
        self.store.close()
