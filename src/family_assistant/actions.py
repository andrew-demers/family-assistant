"""Write actions against Yuvomi that prove themselves.

Every write follows the same steps: duplicate check, write, independent GET read-back,
field comparison. The result carries only data that came back from the read-back, so the
confirmation sent to Telegram never depends on what the model says happened.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from .yuvomi import YuvomiClient, YuvomiError

Kind = Literal["grocery", "task", "event"]
Status = Literal["done", "duplicate", "dry_run", "failed", "unverified"]

TODO_LIST_LABEL = "To-do list"
CALENDAR_LABEL = "Calendar"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T([01]\d|2[0-3]):[0-5]\d$")


class InvalidInput(ValueError):
    """Bad tool arguments; reported back to the model so it can fix them or ask."""


@dataclass
class ActionResult:
    kind: Kind
    status: Status
    requested: str
    container: str
    record: dict | None = None
    error: str | None = None
    failed_step: str | None = None
    written_id: int | None = None

    def for_model(self) -> dict:
        """What the model sees as the tool result."""
        out: dict = {"status": self.status, "container": self.container, "requested": self.requested}
        if self.record is not None:
            out["record"] = self.record
        if self.error:
            out["error"] = self.error
            out["failed_step"] = self.failed_step
        return out


def normalize(text: str | None) -> str:
    return " ".join((text or "").split()).casefold()


def _same_start(a: str | None, b: str | None) -> bool:
    # Yuvomi may return seconds; compare up to minutes (or the date for all-day events).
    a, b = (a or "").replace(" ", "T"), (b or "").replace(" ", "T")
    width = 10 if len(a) == 10 or len(b) == 10 else 16
    return a[:width] == b[:width]


@dataclass
class Actions:
    yuvomi: YuvomiClient
    grocery_list_name: str
    dry_run: bool
    log: list[ActionResult] = field(default_factory=list)
    # Phase 3: builds the HEB cart and returns a summary made from HEB tool results.
    orderer: Callable[[Actions], Awaitable[str]] | None = None
    reports: list[str] = field(default_factory=list)

    def _record(self, result: ActionResult) -> ActionResult:
        self.log.append(result)
        return result

    async def order_groceries(self) -> dict:
        if self.orderer is None:
            raise InvalidInput("HEB ordering is not set up (HEB_DEFAULT_STORE is empty)")
        if self.reports:
            raise InvalidInput("the HEB cart was already built for this message")
        summary = await self.orderer(self)
        self.reports.append(summary)
        return {"status": "summary sent to the user", "summary": summary}

    # ------------------------------------------------------------------ grocery
    async def _grocery_list(self) -> dict:
        lists = await self.yuvomi.list_shopping_lists()
        wanted = normalize(self.grocery_list_name)
        for item in lists:
            if normalize(item.get("name")) == wanted:
                if item.get("accepts_new_items") == 0:
                    raise YuvomiError(f'the list "{item.get("name")}" is read-only')
                return item
        names = ", ".join(f'"{x.get("name")}"' for x in lists) or "none"
        raise YuvomiError(f'no shopping list named "{self.grocery_list_name}" (found: {names})')

    async def read_grocery_list(self) -> dict:
        lst = await self._grocery_list()
        items = await self.yuvomi.list_shopping_items(lst["id"])
        open_items = [
            {"id": i.get("id"), "name": i.get("name"), "quantity": i.get("quantity")}
            for i in items
            if not i.get("is_checked")
        ]
        return {"list": lst.get("name"), "items": open_items}

    async def add_grocery_item(self, name: str, quantity: str | None = None) -> ActionResult:
        name = (name or "").strip()
        if not name:
            raise InvalidInput("name is required")
        container = self.grocery_list_name
        step = "find list"
        try:
            lst = await self._grocery_list()
            container = lst.get("name") or container

            step = "duplicate check"
            for existing in await self.yuvomi.list_shopping_items(lst["id"]):
                if not existing.get("is_checked") and normalize(existing.get("name")) == normalize(name):
                    return self._record(ActionResult("grocery", "duplicate", name, container, record=existing))

            if self.dry_run:
                return self._record(ActionResult("grocery", "dry_run", name, container))

            step = "write"
            created = await self.yuvomi.add_shopping_item(lst["id"], name, quantity)
            written_id = created.get("id")
        except YuvomiError as exc:
            return self._record(ActionResult("grocery", "failed", name, container, error=str(exc), failed_step=step))

        try:
            items = await self.yuvomi.list_shopping_items(lst["id"])
        except YuvomiError as exc:
            return self._record(ActionResult(
                "grocery", "unverified", name, container, error=str(exc), failed_step="read-back", written_id=written_id,
            ))
        record = next((i for i in items if i.get("id") == written_id), None)
        if record is None:
            return self._record(ActionResult(
                "grocery", "unverified", name, container, written_id=written_id, failed_step="read-back",
                error=f"item #{written_id} was not on the list when read back",
            ))
        if normalize(record.get("name")) != normalize(name):
            return self._record(ActionResult(
                "grocery", "unverified", name, container, record=record, written_id=written_id, failed_step="verify",
                error=f'read back as "{record.get("name")}"',
            ))
        return self._record(ActionResult("grocery", "done", name, container, record=record, written_id=written_id))

    # --------------------------------------------------------------------- tasks
    async def read_todo_list(self) -> dict:
        tasks = await self.yuvomi.list_tasks()
        open_tasks = [
            {"id": t.get("id"), "title": t.get("title"), "status": t.get("status"),
             "due_date": t.get("due_date"), "due_time": t.get("due_time")}
            for t in tasks
            if t.get("status") in ("open", "in_progress")
        ]
        return {"list": TODO_LIST_LABEL, "tasks": open_tasks}

    async def add_task(
        self, title: str, due_date: str | None = None, due_time: str | None = None, notes: str | None = None,
    ) -> ActionResult:
        title = (title or "").strip()
        if not title:
            raise InvalidInput("title is required")
        if due_date and not _DATE_RE.match(due_date):
            raise InvalidInput("due_date must be YYYY-MM-DD")
        if due_time and not _TIME_RE.match(due_time):
            raise InvalidInput("due_time must be HH:MM (24h)")
        if due_time and not due_date:
            raise InvalidInput("due_time needs a due_date")

        step = "duplicate check"
        try:
            for existing in await self.yuvomi.list_tasks():
                if existing.get("status") in ("open", "in_progress") and normalize(existing.get("title")) == normalize(title):
                    return self._record(ActionResult("task", "duplicate", title, TODO_LIST_LABEL, record=existing))

            if self.dry_run:
                return self._record(ActionResult("task", "dry_run", title, TODO_LIST_LABEL))

            step = "write"
            payload: dict = {"title": title}
            if due_date:
                payload["due_date"] = due_date
            if due_time:
                payload["due_time"] = due_time
            if notes:
                payload["description"] = notes
            created = await self.yuvomi.create_task(payload)
            written_id = created.get("id")
        except YuvomiError as exc:
            return self._record(ActionResult("task", "failed", title, TODO_LIST_LABEL, error=str(exc), failed_step=step))

        try:
            record = await self.yuvomi.get_task(written_id)
        except YuvomiError as exc:
            return self._record(ActionResult(
                "task", "unverified", title, TODO_LIST_LABEL, error=str(exc), failed_step="read-back", written_id=written_id,
            ))
        if normalize(record.get("title")) != normalize(title):
            return self._record(ActionResult(
                "task", "unverified", title, TODO_LIST_LABEL, record=record, written_id=written_id, failed_step="verify",
                error=f'read back as "{record.get("title")}"',
            ))
        return self._record(ActionResult("task", "done", title, TODO_LIST_LABEL, record=record, written_id=written_id))

    # ------------------------------------------------------------------ calendar
    async def read_events(self, date_from: str, date_to: str) -> dict:
        if not (_DATE_RE.match(date_from or "") and _DATE_RE.match(date_to or "")):
            raise InvalidInput("from_date and to_date must be YYYY-MM-DD")
        events = await self.yuvomi.list_events(date_from, date_to)
        return {"events": [
            {"id": e.get("id"), "title": e.get("title"), "start": e.get("start_datetime"),
             "end": e.get("end_datetime"), "all_day": e.get("all_day"), "location": e.get("location")}
            for e in events
        ]}

    async def find_duplicate_event(self, title: str, start: str) -> dict | None:
        """An existing event with the same title and start (minute, or date for all-day)."""
        day = start[:10]
        for existing in await self.yuvomi.list_events(day, day):
            if normalize(existing.get("title")) == normalize(title) and _same_start(existing.get("start_datetime"), start):
                return existing
        return None

    async def create_event(
        self,
        title: str,
        start: str,
        end: str | None = None,
        all_day: bool = False,
        location: str | None = None,
        description: str | None = None,
    ) -> ActionResult:
        title = (title or "").strip()
        if not title:
            raise InvalidInput("title is required")
        start_pattern = _DATE_RE if all_day else _DATETIME_RE
        if not start_pattern.match(start or ""):
            raise InvalidInput(
                "start must be YYYY-MM-DD for all-day events, otherwise YYYY-MM-DDTHH:MM; "
                "if the user did not give a clear date and time, ask them"
            )
        if end and not (_DATE_RE.match(end) if all_day else _DATETIME_RE.match(end)):
            raise InvalidInput("end must use the same format as start")
        if end and end < start:
            raise InvalidInput("end is before start")

        step = "duplicate check"
        try:
            if existing := await self.find_duplicate_event(title, start):
                return self._record(ActionResult("event", "duplicate", title, CALENDAR_LABEL, record=existing))

            if self.dry_run:
                preview = {"title": title, "start_datetime": start, "end_datetime": end, "all_day": int(all_day), "location": location}
                return self._record(ActionResult("event", "dry_run", title, CALENDAR_LABEL, record=preview))

            step = "write"
            payload: dict = {"title": title, "start_datetime": start, "all_day": all_day}
            if end:
                payload["end_datetime"] = end
            if location:
                payload["location"] = location
            if description:
                payload["description"] = description
            created = await self.yuvomi.create_event(payload)
            written_id = created.get("id")
        except YuvomiError as exc:
            return self._record(ActionResult("event", "failed", title, CALENDAR_LABEL, error=str(exc), failed_step=step))

        try:
            record = await self.yuvomi.get_event(written_id)
        except YuvomiError as exc:
            return self._record(ActionResult(
                "event", "unverified", title, CALENDAR_LABEL, error=str(exc), failed_step="read-back", written_id=written_id,
            ))
        if normalize(record.get("title")) != normalize(title) or not _same_start(record.get("start_datetime"), start):
            return self._record(ActionResult(
                "event", "unverified", title, CALENDAR_LABEL, record=record, written_id=written_id, failed_step="verify",
                error=f'read back as "{record.get("title")}" at {record.get("start_datetime")}',
            ))
        return self._record(ActionResult("event", "done", title, CALENDAR_LABEL, record=record, written_id=written_id))
