"""Gmail scan -> candidate events -> proposals (or direct adds in AUTO mode), and approvals.

Nothing here knows about Telegram; bot.py sends the messages.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from .actions import Actions, ActionResult, InvalidInput
from .extract import EventExtractor
from .gmail import GmailAccount, GmailAuthError, GmailError
from .render import format_when, render_confirmation
from .store import Store
from .yuvomi import YuvomiError

log = logging.getLogger(__name__)

Mode = Literal["propose", "auto"]

# ActionResult.status -> proposal status
_STATUS = {"done": "added", "duplicate": "duplicate", "dry_run": "dry_run", "unverified": "unverified", "failed": "failed"}


@dataclass
class ScanReport:
    emails_checked: int = 0
    batch_id: int | None = None
    proposals: list[dict] = field(default_factory=list)
    auto_results: list[ActionResult] = field(default_factory=list)
    already_known: int = 0
    errors: list[str] = field(default_factory=list)


def proposal_text(p: dict, account_names: dict[str, str]) -> str:
    owner = account_names.get(p["account"], p["account"])
    where = f"\nWhere: {p['location']}" if p.get("location") else ""
    return (
        f"Event found in {owner}'s email:\n"
        f"{p['title']}\n"
        f"{format_when(p['start'], p['end_at'], bool(p['all_day']))}{where}\n"
        f'From: {p["sender"]}, "{p["subject"]}"'
    )


class Scanner:
    def __init__(
        self,
        *,
        accounts: list[GmailAccount],
        extractor: EventExtractor,
        store: Store,
        actions_factory: Callable[[], Actions],
        mode: Mode = "propose",
        query: str = "newer_than:2d",
        limit: int = 25,
    ) -> None:
        self.accounts = accounts
        self.extractor = extractor
        self.store = store
        self.actions_factory = actions_factory
        self.mode = mode
        self.query = query
        self.limit = limit

    async def scan(self, now: datetime) -> ScanReport:
        report = ScanReport()
        for account in self.accounts:
            try:
                ids = await account.list_message_ids(self.query, self.limit)
            except GmailAuthError as exc:
                report.errors.append(str(exc))
                continue
            except GmailError as exc:
                report.errors.append(f"{account.label}: {exc}")
                continue
            for message_id in ids:
                if self.store.is_processed(account.label, message_id):
                    continue
                report.emails_checked += 1
                if not await self._process(account, message_id, now, report):
                    # Stop this account on errors; unprocessed emails are retried next scan.
                    break
        return report

    async def _process(self, account: GmailAccount, message_id: str, now: datetime, report: ScanReport) -> bool:
        try:
            email = await account.get_email(message_id)
            candidates = await self.extractor.extract(email, now)
        except GmailError as exc:
            report.errors.append(f"{account.label}: {exc}")
            return False
        except Exception as exc:  # Claude API or extraction problem; never log the email itself
            log.warning("extraction failed for %s: %s", account.label, exc.__class__.__name__)
            report.errors.append(f"{account.label}: could not read one email ({exc.__class__.__name__}), will retry")
            return False

        actions = self.actions_factory()
        for c in candidates:
            if self.store.has_open_proposal(c.title, c.start):
                report.already_known += 1
                continue
            try:
                if await actions.find_duplicate_event(c.title, c.start):
                    report.already_known += 1
                    continue
            except YuvomiError as exc:
                report.errors.append(f"Yuvomi duplicate check failed: {exc}")
                return False

            if report.batch_id is None:
                report.batch_id = self.store.new_batch()
            fields = dict(
                account=account.label, email_id=email.message_id, subject=email.subject[:200],
                sender=email.sender[:200], title=c.title, start=c.start, end=c.end, all_day=c.all_day,
                location=c.location,
            )
            if self.mode == "auto":
                result = await actions.create_event(
                    c.title, c.start, c.end, c.all_day, c.location, _event_description(email.subject, email.sender),
                )
                report.auto_results.append(result)
                pid = self.store.add_proposal(report.batch_id, **fields, status=_STATUS[result.status])
                self.store.finish(pid, _STATUS[result.status], render_confirmation([result], actions.dry_run))
            else:
                pid = self.store.add_proposal(report.batch_id, **fields)
                report.proposals.append(self.store.get_proposal(pid))

        self.store.mark_processed(account.label, message_id, len(candidates))
        return True


def _event_description(subject: str, sender: str) -> str:
    return f'Added from email "{subject}" from {sender}.'


@dataclass
class Decision:
    ok: bool  # False when someone else already handled it
    text: str
    status: str


async def approve(store: Store, actions: Actions, proposal_id: int, handler: str) -> Decision:
    if not store.claim(proposal_id, handler):
        return _already(store, proposal_id)
    p = store.get_proposal(proposal_id)
    try:
        result = await actions.create_event(
            p["title"], p["start"], p["end_at"], bool(p["all_day"]), p["location"],
            _event_description(p["subject"], p["sender"]),
        )
    except InvalidInput as exc:
        store.finish(proposal_id, "failed", f"Could not add: {exc}")
        return Decision(True, f"Could not add {p['title']}: {exc}", "failed")

    status = _STATUS[result.status]
    text = render_confirmation([result], actions.dry_run)
    if status == "failed":
        # Nothing was written; put it back so either person can try again.
        store.release(proposal_id, text)
    else:
        store.finish(proposal_id, status, text)
    return Decision(True, text, status)


async def skip(store: Store, proposal_id: int, handler: str) -> Decision:
    if not store.claim(proposal_id, handler):
        return _already(store, proposal_id)
    store.finish(proposal_id, "skipped", "Skipped.")
    return Decision(True, "Skipped.", "skipped")


def _already(store: Store, proposal_id: int) -> Decision:
    p = store.get_proposal(proposal_id)
    if p is None:
        return Decision(False, "That proposal no longer exists.", "missing")
    who = p.get("handled_by") or "someone"
    return Decision(False, f"Already handled by {who}.", p["status"])
