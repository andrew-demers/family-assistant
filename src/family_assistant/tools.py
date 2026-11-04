"""Tool definitions exposed to Claude and their dispatch onto Actions."""

from __future__ import annotations

import json
import logging

from .actions import Actions, InvalidInput
from .yuvomi import YuvomiError

log = logging.getLogger(__name__)


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    # strict tool use: every property listed in required, optional ones are nullable.
    return {
        "type": "object",
        "properties": properties,
        "required": required if required is not None else list(properties),
        "additionalProperties": False,
    }


_NULLABLE_STR = {"type": ["string", "null"]}

TOOLS: list[dict] = [
    {
        "name": "add_grocery_item",
        "description": (
            "Add ONE item to the family grocery (shopping) list in Yuvomi. Call once per item. "
            "Skips items already on the list. Returns the item as read back from Yuvomi."
        ),
        "strict": True,
        "input_schema": _schema({
            "name": {"type": "string", "description": "Item name, e.g. 'milk'. Singular, no quantity."},
            "quantity": {**_NULLABLE_STR, "description": "Optional free-text quantity, e.g. '2' or '1 gallon'."},
        }),
    },
    {
        "name": "add_todo",
        "description": (
            "Add ONE task to the family to-do list (Yuvomi tasks). Call once per task. "
            "Skips open tasks with the same title. Returns the task as read back from Yuvomi."
        ),
        "strict": True,
        "input_schema": _schema({
            "title": {"type": "string", "description": "Short task title."},
            "due_date": {**_NULLABLE_STR, "description": "Optional, YYYY-MM-DD."},
            "due_time": {**_NULLABLE_STR, "description": "Optional, HH:MM 24h. Requires due_date."},
            "notes": {**_NULLABLE_STR, "description": "Optional longer description."},
        }),
    },
    {
        "name": "create_calendar_event",
        "description": (
            "Create ONE calendar event in Yuvomi. Only call when the date and start time are clear; "
            "otherwise ask the user one short question first. Times are household local time "
            "(America/Chicago). Skips an event with the same title and start. Returns the event as read back."
        ),
        "strict": True,
        "input_schema": _schema({
            "title": {"type": "string"},
            "start": {"type": "string", "description": "YYYY-MM-DDTHH:MM local time, or YYYY-MM-DD if all_day."},
            "end": {**_NULLABLE_STR, "description": "Optional, same format as start."},
            "all_day": {"type": "boolean"},
            "location": _NULLABLE_STR,
            "description": _NULLABLE_STR,
        }),
    },
    {
        "name": "get_grocery_list",
        "description": "Read the open (unchecked) items on the grocery list.",
        "strict": True,
        "input_schema": _schema({}),
    },
    {
        "name": "get_todo_list",
        "description": "Read open tasks on the to-do list.",
        "strict": True,
        "input_schema": _schema({}),
    },
    {
        "name": "get_calendar_events",
        "description": "Read calendar events between two dates (inclusive).",
        "strict": True,
        "input_schema": _schema({
            "from_date": {"type": "string", "description": "YYYY-MM-DD"},
            "to_date": {"type": "string", "description": "YYYY-MM-DD"},
        }),
    },
]

WRITE_TOOLS = {"add_grocery_item", "add_todo", "create_calendar_event"}


async def run_tool(actions: Actions, name: str, args: dict) -> tuple[str, bool]:
    """Execute one tool call. Returns (json content, is_error)."""
    try:
        if name == "add_grocery_item":
            result = (await actions.add_grocery_item(args.get("name", ""), args.get("quantity"))).for_model()
        elif name == "add_todo":
            result = (await actions.add_task(
                args.get("title", ""), args.get("due_date"), args.get("due_time"), args.get("notes"),
            )).for_model()
        elif name == "create_calendar_event":
            result = (await actions.create_event(
                args.get("title", ""), args.get("start", ""), args.get("end"), bool(args.get("all_day")),
                args.get("location"), args.get("description"),
            )).for_model()
        elif name == "get_grocery_list":
            result = await actions.read_grocery_list()
        elif name == "get_todo_list":
            result = await actions.read_todo_list()
        elif name == "get_calendar_events":
            result = await actions.read_events(args.get("from_date", ""), args.get("to_date", ""))
        else:
            return json.dumps({"error": f"unknown tool {name}"}), True
    except InvalidInput as exc:
        return json.dumps({"error": f"invalid input: {exc}"}), True
    except YuvomiError as exc:
        return json.dumps({"error": f"Yuvomi error: {exc}"}), True
    except Exception:  # never let one tool crash the conversation
        log.exception("tool %s failed", name)
        return json.dumps({"error": "internal error running tool"}), True

    is_error = isinstance(result, dict) and result.get("status") in ("failed", "unverified")
    return json.dumps(result, default=str), is_error
