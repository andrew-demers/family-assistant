import json
from datetime import datetime
from zoneinfo import ZoneInfo

from family_assistant.actions import Actions
from family_assistant.agent import NO_TEXT, Agent

from .fakes import FakeClaude, FakeYuvomi, response, text, tool_use

TZ = ZoneInfo("America/Chicago")
NOW = datetime(2026, 11, 3, 17, 45, tzinfo=TZ)


def agent(claude: FakeClaude, **kw) -> Agent:
    return Agent(claude, model=kw.pop("model", "claude-sonnet-5-5"), tz=TZ, **kw)


async def run(claude: FakeClaude, fake: FakeYuvomi, msg: str, dry_run: bool = False, **kw):
    actions = Actions(fake.client(), "Shopping", dry_run)
    return await agent(claude, **kw).run("Andrew", msg, actions, now=NOW)


async def test_compound_request_runs_two_separate_actions():
    fake = FakeYuvomi()
    claude = FakeClaude(
        response(tool_use("add_grocery_item", {"name": "batteries", "quantity": None}),
                 tool_use("add_todo", {"title": "Buy batteries", "due_date": None, "due_time": None, "notes": None})),
        response(text(NO_TEXT)),
    )
    result = await run(claude, fake, "add batteries to my grocery list and my to do list")

    assert [(a.kind, a.status) for a in result.actions] == [("grocery", "done"), ("task", "done")]
    assert result.reply == (
        f"Done.\nAdded to Shopping: batteries [#{fake.items[0]['id']}].\n"
        f"Added to To-do list: Buy batteries [#{fake.tasks[0]['id']}]."
    )


async def test_confirmation_uses_read_back_data_not_model_text():
    fake = FakeYuvomi()
    fake.items.append({"id": 50, "list_id": 1, "name": "bread", "is_checked": 1})
    # Yuvomi trims and stores the name; the model asked with odd spacing and a quantity.
    claude = FakeClaude(
        response(tool_use("add_grocery_item", {"name": "  whole milk ", "quantity": "2"})),
        response(text(NO_TEXT)),
    )
    result = await run(claude, fake, "milk x2")
    stored = fake.items[-1]
    assert f"whole milk (2) [#{stored['id']}]" in result.reply
    # The model's tool result was the read-back record too.
    sent = json.loads(claude.tool_results(1)[0]["content"])
    assert sent["record"] == stored


async def test_failed_read_back_gives_honest_message():
    fake = FakeYuvomi()
    fake.fail_reads_after_write = True
    claude = FakeClaude(
        response(tool_use("add_grocery_item", {"name": "milk", "quantity": None})),
        response(text("Sorry, I couldn't confirm that one.")),
    )
    result = await run(claude, fake, "add milk")

    assert result.reply.startswith("Not done.")
    assert '"milk" was sent to Shopping' in result.reply
    assert "could not confirm" in result.reply
    assert "Added to" not in result.reply
    assert claude.tool_results(1)[0]["is_error"] is True


async def test_partial_failure_says_what_was_and_was_not_done():
    fake = FakeYuvomi(lists=["Shopping"])
    claude = FakeClaude(
        response(tool_use("add_todo", {"title": "Call plumber", "due_date": None, "due_time": None, "notes": None})),
        response(tool_use("add_grocery_item", {"name": "milk", "quantity": None})),
        response(text(NO_TEXT)),
    )
    # Break writes only after the first one succeeded.
    original = fake.handle

    def handle(request):
        if request.method == "POST" and fake.tasks:
            fake.fail_writes = True
        return original(request)

    fake.handle = handle
    result = await run(claude, fake, "plumber todo, milk on grocery")
    assert result.reply.startswith("Partly done.")
    assert "Added to To-do list: Call plumber" in result.reply
    assert 'Could not add "milk" to Shopping' in result.reply
    assert "Nothing was added." in result.reply


async def test_clarifying_question_passes_through_without_actions():
    fake = FakeYuvomi()
    claude = FakeClaude(response(text("What day and time is the dentist?")))
    result = await run(claude, fake, "add an event dentist")
    assert result.actions == []
    assert result.reply == "What day and time is the dentist?"
    assert fake.writes == []


async def test_context_line_has_sender_and_chicago_time():
    claude = FakeClaude(response(text(NO_TEXT)))
    await run(claude, FakeYuvomi(), "hi")
    first = claude.calls[0]["messages"][-1]["content"]
    assert first.startswith("[From: Andrew] [Now: Tuesday 2026-11-03 17:45 CST (America/Chicago)]")


async def test_invalid_tool_input_returns_error_to_model_and_it_can_ask():
    fake = FakeYuvomi()
    claude = FakeClaude(
        response(tool_use("create_calendar_event", {
            "title": "Dentist", "start": "2026-11-06", "end": None, "all_day": False, "location": None, "description": None,
        })),
        response(text("What time is the dentist on Friday?")),
    )
    result = await run(claude, fake, "dentist friday")
    assert claude.tool_results(1)[0]["is_error"] is True
    assert result.reply == "What time is the dentist on Friday?"
    assert fake.writes == []


async def test_iteration_cap_stops_and_reports_honestly():
    fake = FakeYuvomi()
    claude = FakeClaude(*[response(tool_use("get_grocery_list", {})) for _ in range(3)])
    result = await run(claude, fake, "loop", max_iterations=3)
    assert result.stop == "iteration_cap"
    assert len(claude.calls) == 3
    assert "stopped early" in result.reply


async def test_token_cap_stops_after_tools():
    fake = FakeYuvomi()
    claude = FakeClaude(response(tool_use("add_grocery_item", {"name": "milk", "quantity": None})))
    result = await run(claude, fake, "milk", max_tokens_per_message=500)
    assert result.stop == "token_cap"
    assert "Added to Shopping: milk" in result.reply
    assert "usage limit" in result.reply


async def test_refusal_is_reported():
    claude = FakeClaude(response(text(""), stop="refusal"))
    result = await run(claude, FakeYuvomi(), "something")
    assert result.stop == "refusal"
    assert "can't help" in result.reply


async def test_dry_run_prefix_and_no_writes():
    fake = FakeYuvomi()
    claude = FakeClaude(
        response(tool_use("add_grocery_item", {"name": "milk", "quantity": None})),
        response(text(NO_TEXT)),
    )
    result = await run(claude, fake, "milk", dry_run=True)
    assert result.reply == "[DRY RUN] Would add to Shopping: milk."
    assert fake.writes == []


async def test_dry_run_prefix_on_plain_answers_too():
    claude = FakeClaude(response(text("Sure thing.")))
    result = await run(claude, FakeYuvomi(), "hi", dry_run=True)
    assert result.reply == "[DRY RUN] Sure thing."


async def test_sonnet_uses_server_side_fallback_and_haiku_does_not():
    claude = FakeClaude(response(text(NO_TEXT)), response(text(NO_TEXT)))
    await run(claude, FakeYuvomi(), "hi")
    await run(claude, FakeYuvomi(), "hi", model="claude-haiku-5-5")
    sonnet, haiku = claude.calls
    assert sonnet["beta"] and sonnet["fallbacks"] == "default"
    assert sonnet["betas"] == ["server-side-fallback-2026-07-01"]
    assert not haiku["beta"] and "fallbacks" not in haiku
    assert sonnet["output_config"] == {"effort": "medium"}
    assert all(t.get("strict") for t in sonnet["tools"])


async def test_em_dashes_removed_from_model_text():
    claude = FakeClaude(response(text("Sure — which list?")))
    result = await run(claude, FakeYuvomi(), "add it")
    assert "—" not in result.reply
