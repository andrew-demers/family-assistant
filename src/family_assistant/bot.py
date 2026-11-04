"""Telegram front end (long polling). Only allowlisted user IDs get any response."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import anthropic
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, Conflict, NetworkError, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .actions import Actions
from .agent import Agent, compose_reply
from .config import Settings
from .extract import EventExtractor
from .gmail import GmailAccount, token_path
from .grocery import Matcher, build_cart, render_order
from .heb import HebError, HebMcp
from .render import DRY_RUN_PREFIX, render_confirmation
from .scanner import Decision, ScanReport, Scanner, approve, proposal_text, skip
from .store import RateLimiter, Store
from .yuvomi import YuvomiClient

log = logging.getLogger(__name__)

HELP_TEXT = (
    "Hi! Message me in plain English, for example:\n"
    "- Add milk and eggs to the grocery list\n"
    "- Add call the plumber to my to do list\n"
    "- Add an event: dentist Friday at 3pm\n"
    "- What's on the grocery list?\n"
    "- Order the groceries (builds the HEB cart; you pay in the HEB app)\n"
    "/scan checks email for events now."
)

_FINAL_STATUS_LABEL = {
    "added": "Added", "duplicate": "Already on the calendar", "dry_run": "Would add",
    "unverified": "Sent but not confirmed", "skipped": "Skipped",
}


class Bot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = Store(settings.db_path)
        self.limiter = RateLimiter(settings.rate_limit_per_minute)
        self.yuvomi = YuvomiClient(settings.yuvomi_url, settings.yuvomi_api_token)
        claude = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self.agent = Agent(
            claude,
            model=settings.claude_model,
            effort=settings.claude_effort,
            refusal_fallback=settings.claude_refusal_fallback,
            max_iterations=settings.max_tool_iterations,
            max_tokens_per_message=settings.max_tokens_per_message,
            tz=settings.tz,
        )
        self.gmail_accounts = [
            GmailAccount(label, token_path(settings.config_dir, label)) for label in settings.gmail_accounts
        ]
        self.scanner: Scanner | None = None
        if self.gmail_accounts:
            self.scanner = Scanner(
                accounts=self.gmail_accounts,
                extractor=EventExtractor(
                    claude, model=settings.claude_model, effort="low", refusal_fallback=settings.claude_refusal_fallback,
                ),
                store=self.store,
                actions_factory=self.new_actions,
                mode=settings.gmail_event_mode,
                query=settings.gmail_query,
                limit=settings.gmail_max_per_scan,
            )
        self._scan_lock = asyncio.Lock()
        self.matcher = Matcher(
            claude, model=settings.claude_model, effort="low", refusal_fallback=settings.claude_refusal_fallback,
        )
        self._order_lock = asyncio.Lock()
        self.heb_factory = lambda: HebMcp(settings.heb_mcp_command, settings.config_dir, settings.heb_store_id)

    # ---------------------------------------------------------------- helpers

    def new_actions(self) -> Actions:
        orderer = self.order_groceries if self.settings.heb_store_id else None
        return Actions(self.yuvomi, self.settings.yuvomi_grocery_list, self.settings.dry_run, orderer=orderer)

    async def order_groceries(self, actions: Actions) -> str:
        """Build the HEB cart from the grocery list; returns the summary text."""
        if self._order_lock.locked():
            return "An HEB cart is already being built. Try again in a minute."
        async with self._order_lock:
            grocery = await actions.read_grocery_list()
            try:
                async with self.heb_factory() as heb:
                    report = await build_cart(
                        heb, self.matcher, grocery["items"], self.settings.heb_store_id, self.settings.dry_run,
                    )
            except HebError as exc:
                log.warning("HEB order failed: %s", exc)
                return f"HEB cart not built. The HEB tool had a problem: {exc}. Nothing was added."
        log.info("HEB order: %s", [(l.requested, l.status) for l in report.lines])
        return render_order(report)

    def name_for(self, user) -> str:
        return self.settings.user_names.get(user.id) or user.first_name or "Someone"

    def _prefix(self, text: str) -> str:
        if self.settings.dry_run and not text.startswith(DRY_RUN_PREFIX):
            return DRY_RUN_PREFIX + text
        return text

    async def _send_all(self, bot, text: str, **kwargs) -> list[tuple[int, int]]:
        """Send to every allowlisted user. Returns (chat_id, message_id) for each delivery."""
        sent = []
        for chat_id in sorted(self.settings.allowed_telegram_ids):
            try:
                msg = await bot.send_message(chat_id, self._prefix(text), **kwargs)
                sent.append((chat_id, msg.message_id))
            except TelegramError as exc:
                # Usually: that person has not pressed Start in the bot yet.
                log.warning("could not message chat %s: %s", chat_id, exc.__class__.__name__)
        return sent

    async def _edit(self, bot, chat_id: int, message_id: int, text: str, markup=None) -> None:
        try:
            await bot.edit_message_text(self._prefix(text), chat_id=chat_id, message_id=message_id, reply_markup=markup)
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                log.warning("could not edit message: %s", exc)
        except TelegramError as exc:
            log.warning("could not edit message: %s", exc.__class__.__name__)

    # ---------------------------------------------------------- text messages

    async def on_help(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(self._prefix(HELP_TEXT))

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        user = update.effective_user
        if not self.limiter.allow(user.id):
            await message.reply_text("Slow down a little, please try again in a minute.")
            return

        await context.bot.send_chat_action(message.chat_id, ChatAction.TYPING)
        actions = self.new_actions()
        history = self.store.recent_history(message.chat_id)
        try:
            result = await self.agent.run(self.name_for(user), message.text, actions, history)
            reply = result.reply
        except Exception:
            log.exception("agent run failed")
            reply = compose_reply(
                actions.log, "", ["Something went wrong on my side. Anything not listed above was not done."],
                self.settings.dry_run,
            )
        self.store.append_exchange(message.chat_id, message.text, reply)
        await message.reply_text(reply)

    # -------------------------------------------------------------- Gmail scan

    async def on_scan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if self.scanner is None:
            await message.reply_text(self._prefix("Email scanning is not set up (GMAIL_ACCOUNTS is empty)."))
            return
        if self._scan_lock.locked():
            await message.reply_text(self._prefix("A scan is already running."))
            return
        await message.reply_text(self._prefix("Checking email..."))
        report = await self.run_scan(context.bot)
        if not report.proposals and not report.auto_results:
            summary = f"No new events found ({report.emails_checked} new emails checked)."
            if report.errors:
                summary += "\n" + "\n".join(f"Problem: {e}" for e in report.errors)
            await message.reply_text(self._prefix(summary))

    async def scheduled_scan(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._scan_lock.locked():
            return
        await self.run_scan(context.bot, announce_errors=True)

    async def run_scan(self, bot, announce_errors: bool = False) -> ScanReport:
        async with self._scan_lock:
            report = await self.scanner.scan(datetime.now(self.settings.tz))
        log.info(
            "scan: checked=%d proposals=%d auto=%d known=%d errors=%d",
            report.emails_checked, len(report.proposals), len(report.auto_results), report.already_known,
            len(report.errors),
        )
        await self._deliver(bot, report)
        if announce_errors and report.errors:
            await self._send_all(bot, "Email scan problem:\n" + "\n".join(report.errors))
        return report

    async def _deliver(self, bot, report: ScanReport) -> None:
        names = self.settings.gmail_account_names
        for p in report.proposals:
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("Add", callback_data=f"ev:add:{p['id']}"),
                InlineKeyboardButton("Skip", callback_data=f"ev:skip:{p['id']}"),
            ]])
            for chat_id, message_id in await self._send_all(bot, proposal_text(p, names), reply_markup=markup):
                self.store.add_proposal_message(chat_id, message_id, proposal_id=p["id"])
        if len(report.proposals) > 1:
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton(f"Add all ({len(report.proposals)})", callback_data=f"ev:all:{report.batch_id}"),
            ]])
            for chat_id, message_id in await self._send_all(
                bot, f"{len(report.proposals)} events found. Add them all?", reply_markup=markup,
            ):
                self.store.add_proposal_message(chat_id, message_id, batch_id=report.batch_id)
        if report.auto_results:
            await self._send_all(bot, "From email:\n" + render_confirmation(report.auto_results, self.settings.dry_run))

    # ---------------------------------------------------------------- buttons

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        # CallbackQueryHandler has no user filter, so the allowlist is enforced here.
        if query.from_user is None or query.from_user.id not in self.settings.allowed_telegram_ids:
            return
        try:
            _, action, raw_id = (query.data or "").split(":")
            target = int(raw_id)
        except ValueError:
            await query.answer()
            return
        name = self.name_for(query.from_user)

        if action == "all":
            await self._approve_all(context.bot, query, target, name)
            return

        if action == "add":
            decision = await approve(self.store, self.new_actions(), target, name)
        else:
            decision = await skip(self.store, target, name)
        await query.answer(None if decision.ok else decision.text)
        await self._refresh_proposal(context.bot, target, name, decision)

    async def _approve_all(self, bot, query, batch_id: int, name: str) -> None:
        pending = [p for p in self.store.batch_proposals(batch_id) if p["status"] == "pending"]
        if not pending:
            handlers = {p["handled_by"] for p in self.store.batch_proposals(batch_id) if p.get("handled_by")}
            await query.answer(f"Already handled by {', '.join(sorted(handlers)) or 'someone'}.")
            return
        await query.answer()
        counts: dict[str, int] = {}
        for p in pending:
            decision = await approve(self.store, self.new_actions(), p["id"], name)
            if decision.ok:
                counts[decision.status] = counts.get(decision.status, 0) + 1
                await self._refresh_proposal(bot, p["id"], name, decision)
        summary = ", ".join(f"{n} {_FINAL_STATUS_LABEL.get(s, s).lower()}" for s, n in counts.items()) or "nothing to do"
        for chat_id, message_id in self.store.proposal_messages(batch_id=batch_id):
            await self._edit(bot, chat_id, message_id, f"Add all, by {name}: {summary}.")

    async def _refresh_proposal(self, bot, proposal_id: int, name: str, decision: Decision) -> None:
        """Rewrite every copy of the proposal so both people see the outcome."""
        p = self.store.get_proposal(proposal_id)
        if p is None:
            return
        base = proposal_text(p, self.settings.gmail_account_names)
        if p["status"] == "pending":
            # The add failed and nothing was written; keep the buttons so it can be retried.
            text = f"{base}\n\n{p['result'] or ''}"
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("Try again", callback_data=f"ev:add:{proposal_id}"),
                InlineKeyboardButton("Skip", callback_data=f"ev:skip:{proposal_id}"),
            ]])
        else:
            who = p.get("handled_by") or name
            label = _FINAL_STATUS_LABEL.get(p["status"], p["status"])
            text = f"{base}\n\n{label} by {who}." + (f"\n{p['result']}" if p["status"] != "skipped" and p["result"] else "")
            markup = None
        for chat_id, message_id in self.store.proposal_messages(proposal_id=proposal_id):
            await self._edit(bot, chat_id, message_id, text, markup)

    # ------------------------------------------------------------------ errors

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        error = context.error
        if isinstance(error, Conflict):
            # Telegram allows one poller per bot token; the library keeps retrying.
            log.warning(
                "Telegram conflict: another copy of this bot is running with the same token "
                "(for example on a PC). Stop the other copy; this one keeps retrying."
            )
        elif isinstance(error, NetworkError):
            log.warning("Telegram network error (will retry): %s", error)
        else:
            log.error("Unhandled error", exc_info=error)

    # ------------------------------------------------------------------ wiring

    def build(self) -> Application:
        app = ApplicationBuilder().token(self.settings.telegram_bot_token).post_shutdown(self._shutdown).build()
        # Messages from anyone else match no handler and are dropped silently.
        allowed = filters.User(user_id=list(self.settings.allowed_telegram_ids))
        app.add_handler(CommandHandler(["start", "help"], self.on_help, filters=allowed))
        app.add_handler(CommandHandler("scan", self.on_scan, filters=allowed))
        app.add_handler(CallbackQueryHandler(self.on_button, pattern=r"^ev:(add|skip|all):\d+$"))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & allowed, self.on_text))
        app.add_error_handler(self.on_error)
        if self.scanner is not None:
            for t in self.settings.gmail_scan_times:
                app.job_queue.run_daily(self.scheduled_scan, time=t, name=f"gmail-scan-{t:%H%M}")
        return app

    async def _shutdown(self, _: Application) -> None:
        await self.yuvomi.aclose()
        for account in self.gmail_accounts:
            await account.aclose()
        self.store.close()
