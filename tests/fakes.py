"""In-memory fakes for Yuvomi (HTTP level) and the Anthropic client."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any

import httpx

from family_assistant.yuvomi import YuvomiClient


class FakeYuvomi:
    """Mimics the /api/v1 endpoints the app uses, with switches to inject failures."""

    def __init__(self, lists: list[str] | None = None) -> None:
        self.lists = [
            {"id": i + 1, "name": n, "item_total": 0, "item_checked": 0, "accepts_new_items": 1}
            for i, n in enumerate(lists or ["Shopping"])
        ]
        self.items: list[dict] = []
        self.tasks: list[dict] = []
        self.events: list[dict] = []
        self.next_id = 100
        self.requests: list[tuple[str, str]] = []
        # Failure switches.
        self.fail_reads_after_write = False
        self.fail_writes = False
        self.lose_writes = False  # POST returns an id but the record never shows up
        self.rename_on_store: str | None = None
        self._wrote = False

    def client(self) -> YuvomiClient:
        return YuvomiClient("http://yuvomi.test", "test-token", transport=httpx.MockTransport(self.handle))

    @property
    def writes(self) -> list[tuple[str, str]]:
        return [r for r in self.requests if r[0] == "POST"]

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    @staticmethod
    def _ok(data: Any, status: int = 200) -> httpx.Response:
        return httpx.Response(status, json={"data": data})

    @staticmethod
    def _err(status: int, message: str) -> httpx.Response:
        return httpx.Response(status, json={"error": message, "code": status})

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-token"
        path = request.url.path.removeprefix("/api/v1")
        method = request.method
        self.requests.append((method, path))
        body = json.loads(request.content) if request.content else {}

        if method == "POST":
            if self.fail_writes:
                return self._err(500, "Internal server error.")
            self._wrote = True
        elif self._wrote and self.fail_reads_after_write:
            return self._err(503, "Service unavailable.")

        if method == "GET" and path == "/shopping":
            return self._ok(self.lists)
        if m := re.fullmatch(r"/shopping/(\d+)/items", path):
            list_id = int(m.group(1))
            if method == "GET":
                return self._ok([i for i in self.items if i["list_id"] == list_id])
            item = {"id": self._id(), "list_id": list_id, "name": self.rename_on_store or body["name"].strip(),
                    "quantity": body.get("quantity"), "category": "Sonstiges", "is_checked": 0}
            if not self.lose_writes:
                self.items.append(item)
            return self._ok(item, 201)
        if path == "/tasks":
            if method == "GET":
                return self._ok(self.tasks)
            task = {"id": self._id(), "title": self.rename_on_store or body["title"], "status": "open",
                    "due_date": body.get("due_date"), "due_time": body.get("due_time")}
            if not self.lose_writes:
                self.tasks.append(task)
            return self._ok(task, 201)
        if m := re.fullmatch(r"/tasks/(\d+)", path):
            task = next((t for t in self.tasks if t["id"] == int(m.group(1))), None)
            return self._ok(task) if task else self._err(404, "Task not found.")
        if path == "/calendar":
            if method == "GET":
                lo, hi = request.url.params["from"], request.url.params["to"]
                return self._ok([e for e in self.events if lo <= e["start_datetime"][:10] <= hi])
            event = {"id": self._id(), "title": self.rename_on_store or body["title"],
                     "start_datetime": body["start_datetime"], "end_datetime": body.get("end_datetime"),
                     "all_day": 1 if body.get("all_day") else 0, "location": body.get("location")}
            if not self.lose_writes:
                self.events.append(event)
            return self._ok(event, 201)
        if m := re.fullmatch(r"/calendar/(\d+)", path):
            event = next((e for e in self.events if e["id"] == int(m.group(1))), None)
            return self._ok(event) if event else self._err(404, "Event not found.")
        return self._err(404, f"no route {method} {path}")


# ----------------------------------------------------------------------------- Claude


def text(t: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=t)


def tool_use(name: str, args: dict, id_: str | None = None) -> SimpleNamespace:
    tool_use.counter += 1
    return SimpleNamespace(type="tool_use", id=id_ or f"toolu_{tool_use.counter}", name=name, input=args)


tool_use.counter = 0


def response(*blocks: SimpleNamespace, stop: str | None = None) -> SimpleNamespace:
    if stop is None:
        stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
    return SimpleNamespace(
        content=list(blocks), stop_reason=stop, usage=SimpleNamespace(input_tokens=1000, output_tokens=100),
    )


class FakeMessages:
    def __init__(self, owner: FakeClaude, beta: bool) -> None:
        self._owner, self._beta = owner, beta

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        self._owner.calls.append({"beta": self._beta, **kwargs, "messages": list(kwargs["messages"])})
        if not self._owner.script:
            raise AssertionError("FakeClaude ran out of scripted responses")
        return self._owner.script.pop(0)


class FakeClaude:
    """Returns scripted responses in order and records every request."""

    def __init__(self, *script: SimpleNamespace) -> None:
        self.script = list(script)
        self.calls: list[dict] = []
        self.messages = FakeMessages(self, beta=False)
        self.beta = SimpleNamespace(messages=FakeMessages(self, beta=True))

    def tool_results(self, call_index: int) -> list[dict]:
        """tool_result blocks sent in a given request."""
        last = self.calls[call_index]["messages"][-1]
        return [b for b in last["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
