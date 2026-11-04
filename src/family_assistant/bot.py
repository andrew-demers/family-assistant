"""Telegram front end (long polling). Only allowlisted user IDs get any response."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

import anthropic
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
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
from . import health
from .photo import IMAGE_TYPES, MAX_IMAGE_BYTES, ItemIdentifier, PhotoError
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
    "- Order the groceries (sends the list as heb.com links)\n"
    "- Send a photo of an item to add it to the grocery list\n"
    "/scan checks email for events now."
)

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
TELEGRAM_LIMIT = 4000  # Telegram allows 4096 characters per message; leave room.


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:  # a single very long line
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current or not chunks:
        chunks.append(current)
    return chunks


def photo_prompt(options: tuple[str, ...] | list[str], quantity: str | None, list_name: str) -> str:
    qty = f" (x{quantity})" if quantity and quantity != "1" else ""
    text = f"Looks like: {options[0]}{qty}\nAdd it to {list_name}?"
    if len(options) > 1:
        text += "\nNot sure? Pick the right one below."
    return text


def photo_buttons(item_id: int, options: tuple[str, ...] | list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"Add: {opt}"[:60], callback_data=f"ph:{item_id}:{n}")]
            for n, opt in enumerate(options)]
    rows.append([InlineKeyboardButton("Cancel", callback_data=f"ph:{item_id}:x")])
    return InlineKeyboardMarkup(rows)


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
        self.last_conflict: float | None = None
        self.identifier = ItemIdentifier(
            claude, model=settings.claude_model, effort="low", refusal_fallback=settings.claude_refusal_fallback,
        )

    # ---------------------------------------------------------------- helpers

    def new_actions(self) -> Actions:
        return Actions(self.yuvomi, self.settings.yuvomi_grocery_list, self.settings.dry_run)

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
        await self._reply(message, reply)

    async def _reply(self, message, text: str) -> None:
        # No link previews (the grocery links would each show a heb.com card), and split long
        # replies at line breaks to stay under Telegram's message length limit.
        for chunk in split_message(text):
            await message.reply_text(chunk, link_preview_options=NO_PREVIEW)

    # ------------------------------------------------------------------ photos

    async def on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """A photo (or image file) of an item: identify it, then offer buttons to add it."""
        message = update.effective_message
        if not self.limiter.allow(update.effective_user.id):
            await message.reply_text("Slow down a little, please try again in a minute.")
            return
        if message.photo:
            source, media_type, size = message.photo[-1], "image/jpeg", message.photo[-1].file_size
        else:
            doc = message.document
            source, media_type, size = doc, doc.mime_type or "", doc.file_size
        if media_type not in IMAGE_TYPES:
            await message.reply_text(self._prefix("I can read JPEG, PNG, GIF or WebP images. Nothing was added."))
            return
        if size and size > MAX_IMAGE_BYTES:
            await message.reply_text(self._prefix("That image is over 5 MB. Try sending it as a photo instead."))
            return

        await context.bot.send_chat_action(message.chat_id, ChatAction.TYPING)
        try:
            file = await source.get_file()
            image = bytes(await file.download_as_bytearray())
            result = await self.identifier.identify(image, media_type, message.caption)
        except (PhotoError, TelegramError, anthropic.APIError) as exc:
            log.warning("photo identification failed: %s", exc.__class__.__name__)
            await message.reply_text(self._prefix("Sorry, I could not read that photo. Nothing was added."))
            return

        caption = f"[photo] {message.caption}" if message.caption else "[photo]"
        if not result.options:
            reply = f"{result.reason} Nothing was added. Try a closer photo of the label, or type the item."
            self.store.append_exchange(message.chat_id, caption, reply)
            await message.reply_text(self._prefix(reply))
            return

        item_id = self.store.add_photo_item(message.chat_id, list(result.options), result.quantity)
        text = photo_prompt(result.options, result.quantity, self.settings.yuvomi_grocery_list)
        self.store.append_exchange(message.chat_id, caption, text)
        await message.reply_text(self._prefix(text), reply_markup=photo_buttons(item_id, result.options))

    async def on_photo_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        # CallbackQueryHandler has no user filter, so the allowlist is enforced here.
        if query.from_user is None or query.from_user.id not in self.settings.allowed_telegram_ids:
            return
        try:
            _, raw_id, choice = (query.data or "").split(":")
            item_id = int(raw_id)
        except ValueError:
            await query.answer()
            return
        name = self.name_for(query.from_user)
        item = self.store.get_photo_item(item_id)
        if item is None:
            await query.answer("That item is no longer available.")
            return
        if not self.store.claim_photo_item(item_id, name):
            current = self.store.get_photo_item(item_id) or item
            await query.answer(f"Already handled by {current.get('handled_by') or 'someone'}.")
            return
        await query.answer()

        if choice == "x":
            self.store.set_photo_status(item_id, "cancelled")
            await self._edit_query(query, f"Cancelled by {name}. Nothing was added.")
            return
        try:
            chosen = item["options"][int(choice)]
        except (ValueError, IndexError):
            self.store.set_photo_status(item_id, "pending")
            return

        try:
            result = await self.new_actions().add_grocery_item(chosen, item["quantity"])
        except Exception:
            log.exception("photo item add failed")
            self.store.set_photo_status(item_id, "pending")
            await self._edit_query(query, "Something went wrong on my side. Nothing was confirmed as added.",
                                   photo_buttons(item_id, item["options"]))
            return
        confirmation = render_confirmation([result], self.settings.dry_run)
        if result.status == "failed":
            # Nothing was confirmed as written, and the duplicate check stops a double add, so
            # the buttons stay for another try.
            self.store.set_photo_status(item_id, "pending")
            await self._edit_query(query, confirmation, photo_buttons(item_id, item["options"]))
            return
        self.store.set_photo_status(item_id, "done")
        await self._edit_query(query, f"{confirmation}\n\nChosen by {name}.")

    async def _edit_query(self, query, text: str, markup=None) -> None:
        message = query.message
        if message is None:
            return
        await self._edit(query.get_bot(), message.chat.id, message.message_id, text, markup)

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

    async def heartbeat(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Refresh the health file if Telegram answers and no other copy is polling."""
        if self.last_conflict is not None and time.monotonic() - self.last_conflict < health.CONFLICT_GRACE:
            return
        try:
            await context.bot.get_me()
        except TelegramError as exc:
            log.warning("health check: Telegram did not answer (%s)", exc.__class__.__name__)
            return
        try:
            health.write_heartbeat()
        except OSError as exc:
            log.warning("health check: could not write heartbeat file: %s", exc)

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        error = context.error
        if isinstance(error, Conflict):
            # Telegram allows one poller per bot token; the library keeps retrying.
            self.last_conflict = time.monotonic()
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
        app.add_handler(CallbackQueryHandler(self.on_photo_button, pattern=r"^ph:\d+:(\d|x)$"))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & allowed, self.on_text))
        app.add_handler(MessageHandler((filters.PHOTO | filters.Document.IMAGE) & allowed, self.on_photo))
        app.add_error_handler(self.on_error)
        app.job_queue.run_repeating(self.heartbeat, interval=health.HEARTBEAT_INTERVAL, first=5, name="heartbeat")
        if self.scanner is not None:
            for t in self.settings.gmail_scan_times:
                app.job_queue.run_daily(self.scheduled_scan, time=t, name=f"gmail-scan-{t:%H%M}")
        return app

    async def _shutdown(self, _: Application) -> None:
        await self.yuvomi.aclose()
        for account in self.gmail_accounts:
            await account.aclose()
        self.store.close()
