"""Client-side tool loop: message -> Claude -> tools -> results -> ... -> reply.

The final Telegram reply is assembled in code: a confirmation rendered from the
action log (read-back data), plus the model's text only for questions and answers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import anthropic

from .actions import Actions, ActionResult
from .render import DRY_RUN_PREFIX, render_confirmation
from .tools import TOOLS, run_tool

log = logging.getLogger(__name__)

NO_TEXT = "NONE"

SYSTEM_PROMPT = f"""You are the household assistant for a family. Two adults message you on Telegram.
You manage their Yuvomi family planner: the grocery list, the to-do list and the calendar.

How to act:
- Each request becomes separate tool calls: one call per grocery item, one per task, one per event.
  "Add X to my grocery list and my to do list" means two calls: add_grocery_item and add_todo.
- "Grocery list", "shopping list" -> add_grocery_item. "To do list", "todo", "reminder to do X" -> add_todo.
- For events: resolve relative dates ("Friday", "tomorrow at 3") against the current date and time given
  in the message. If the date or start time is missing or ambiguous, do not call the tool. Ask ONE short
  clarifying question instead. An event the user explicitly calls all-day needs no time.
- Never invent items, dates, or details the user did not give.

How to reply:
- The app sends its own confirmation built from what Yuvomi returns. Do NOT confirm, list, or describe
  added items, tasks, or events in your text, and never say something was done.
- Use your text only to ask a clarifying question, to answer a question (based only on tool results),
  or to explain why you could not do something.
- If you have nothing else to say, reply with exactly {NO_TEXT}.
- Keep replies short and friendly. Plain text, no markdown. Do not use em dashes."""


@dataclass
class AgentResult:
    reply: str
    actions: list[ActionResult] = field(default_factory=list)
    model_text: str = ""
    stop: str = "end_turn"
    iterations: int = 0
    tokens: int = 0


def _context_line(user_name: str, now: datetime) -> str:
    return f"[From: {user_name}] [Now: {now:%A} {now:%Y-%m-%d %H:%M} {now.tzname()} ({now.tzinfo})]"


class Agent:
    def __init__(
        self,
        client: Any,
        *,
        model: str,
        effort: str = "medium",
        refusal_fallback: bool = True,
        max_iterations: int = 6,
        max_tokens_per_message: int = 60000,
        tz: ZoneInfo = ZoneInfo("America/Chicago"),
    ) -> None:
        self._client = client
        self._model = model
        self._effort = effort
        # The server-side fallback is not offered for Haiku models.
        self._fallback = refusal_fallback and "haiku" not in model
        self._max_iterations = max_iterations
        self._max_tokens = max_tokens_per_message
        self._tz = tz

    async def _create(self, messages: list) -> Any:
        params: dict[str, Any] = dict(
            model=self._model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
            output_config={"effort": self._effort},
            cache_control={"type": "ephemeral"},
        )
        if self._fallback:
            return await self._client.beta.messages.create(
                **params, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            )
        return await self._client.messages.create(**params)

    async def run(
        self,
        user_name: str,
        text: str,
        actions: Actions,
        history: list[dict] | None = None,
        now: datetime | None = None,
    ) -> AgentResult:
        now = now or datetime.now(self._tz)
        messages: list = list(history or [])
        messages.append({"role": "user", "content": f"{_context_line(user_name, now)}\n{text}"})

        result = AgentResult(reply="")
        notes: list[str] = []
        response = None

        while True:
            if result.iterations >= self._max_iterations:
                result.stop = "iteration_cap"
                notes.append("I stopped early because this took too many steps. Anything not listed above was not done.")
                break
            result.iterations += 1
            try:
                response = await self._create(messages)
            except anthropic.APIConnectionError:
                log.warning("Claude API connection error")
                result.stop = "api_error"
                notes.append("I could not reach Claude just now. Anything not listed above was not done.")
                break
            except anthropic.APIStatusError as exc:
                log.warning("Claude API error %s", exc.status_code)
                result.stop = "api_error"
                notes.append(f"Claude returned an error (HTTP {exc.status_code}). Anything not listed above was not done.")
                break

            usage = getattr(response, "usage", None)
            if usage is not None:
                result.tokens += (usage.input_tokens or 0) + (usage.output_tokens or 0)

            if response.stop_reason == "refusal":
                result.stop = "refusal"
                notes.append("Sorry, I can't help with that request.")
                break

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "tool_use":
                tool_results = []
                # Sequential on purpose: keeps duplicate checks accurate within one message.
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    content, is_error = await run_tool(actions, block.name, dict(block.input or {}))
                    tool_results.append({
                        "type": "tool_result", "tool_use_id": block.id, "content": content, "is_error": is_error,
                    })
                messages.append({"role": "user", "content": tool_results})
                if result.tokens >= self._max_tokens:
                    result.stop = "token_cap"
                    notes.append("I stopped early to stay within the usage limit. Anything not listed above was not done.")
                    break
                continue

            if response.stop_reason == "pause_turn":
                continue

            result.stop = response.stop_reason or "end_turn"
            if response.stop_reason == "max_tokens":
                notes.append("My reply was cut off. Anything not listed above was not done.")
            break

        if response is not None and result.stop in ("end_turn", "max_tokens", "stop_sequence"):
            texts = [b.text for b in response.content if getattr(b, "type", None) == "text"]
            model_text = "\n".join(t.strip() for t in texts if t.strip())
            if model_text.strip().upper() != NO_TEXT:
                result.model_text = model_text

        result.actions = list(actions.log)
        result.reply = compose_reply(result.actions, result.model_text, notes, actions.dry_run)
        log.info(
            "agent done stop=%s iterations=%d tokens=%d actions=%s",
            result.stop, result.iterations, result.tokens, [(a.kind, a.status) for a in result.actions],
        )
        return result


def compose_reply(actions: list[ActionResult], model_text: str, notes: list[str], dry_run: bool) -> str:
    parts = []
    confirmation = render_confirmation(actions, dry_run)
    if confirmation:
        parts.append(confirmation)
    if model_text:
        parts.append(model_text.replace(chr(0x2014), "-"))  # no em dashes in user-facing text
    parts.extend(notes)
    if not parts:
        parts.append("Sorry, I did not get a reply for that. Nothing was changed.")
    reply = "\n\n".join(parts)
    if dry_run and not reply.startswith(DRY_RUN_PREFIX):
        reply = DRY_RUN_PREFIX + reply
    return reply
