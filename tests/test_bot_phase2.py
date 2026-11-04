"""The Telegram side of the email flow, with a fake Telegram bot object."""

from types import SimpleNamespace

from family_assistant.actions import Actions
from family_assistant.bot import Bot
from family_assistant.config import Settings
from family_assistant.scanner import Scanner

from .fakes import FakeYuvomi
from .test_scanner import DENTIST, FakeAccount, FakeExtractor, mail
from .test_support import BASE_ENV

SOCCER = DENTIST.__class__("Soccer", "2026-11-07T09:00", None, False, None)


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edits: dict[tuple[int, int], dict] = {}
        self._next = 0

    async def send_message(self, chat_id, text, reply_markup=None, **_):
        self._next += 1
        self.sent.append({"chat_id": chat_id, "text": text, "markup": reply_markup, "message_id": self._next})
        return SimpleNamespace(message_id=self._next)

    async def edit_message_text(self, text, chat_id=None, message_id=None, reply_markup=None, **_):
        self.edits[(chat_id, message_id)] = {"text": text, "markup": reply_markup}


def make_bot(tmp_path, results, **env):
    settings = Settings.from_env({**BASE_ENV, "CONFIG_DIR": str(tmp_path), "DRY_RUN": "false",
                                  "GMAIL_ACCOUNTS": "andrew", "GMAIL_ACCOUNT_NAMES": "Andrew", **env})
    bot = Bot(settings)
    fake = FakeYuvomi()
    bot.yuvomi = fake.client()
    bot.scanner = Scanner(
        accounts=[FakeAccount("andrew", {mid: mail(mid) for mid in results})],
        extractor=FakeExtractor(results), store=bot.store, actions_factory=bot.new_actions,
    )
    return bot, fake


def press(user_id: int, name: str, data: str):
    answers: list = []

    async def answer(text=None, **_):
        answers.append(text)

    query = SimpleNamespace(from_user=SimpleNamespace(id=user_id, first_name=name), data=data, answer=answer)
    return SimpleNamespace(callback_query=query), answers


def callback_data(message) -> list[str]:
    return [b.callback_data for row in message["markup"].inline_keyboard for b in row]


async def test_proposals_go_to_both_users_with_buttons(tmp_path):
    bot, _ = make_bot(tmp_path, {"m1": [DENTIST, SOCCER]})
    tg = FakeTelegram()
    await bot.run_scan(tg)
    # Two proposals plus an "Add all" message, to each of the two users.
    assert sorted({m["chat_id"] for m in tg.sent}) == [111, 222]
    assert len(tg.sent) == 6
    first = tg.sent[0]
    assert first["text"].startswith("Event found in Andrew's email:\nDentist")
    assert callback_data(first) == ["ev:add:1", "ev:skip:1"]
    assert callback_data(tg.sent[-1]) == ["ev:all:1"]


async def test_add_creates_event_and_updates_both_copies_then_second_is_refused(tmp_path):
    bot, fake = make_bot(tmp_path, {"m1": [DENTIST]})
    tg = FakeTelegram()
    await bot.run_scan(tg)
    ctx = SimpleNamespace(bot=tg)

    update, answers = press(111, "Andrew", "ev:add:1")
    await bot.on_button(update, ctx)
    assert len(fake.events) == 1
    assert answers == [None]
    # Both users' copies now show the outcome built from the read-back, without buttons.
    assert len(tg.edits) == 2
    for edit in tg.edits.values():
        assert "Added by Andrew." in edit["text"]
        assert f"[#{fake.events[0]['id']}]" in edit["text"]
        assert edit["markup"] is None

    update, answers = press(222, "Sam", "ev:add:1")
    await bot.on_button(update, ctx)
    assert answers == ["Already handled by Andrew."]
    assert len(fake.events) == 1


async def test_add_all_adds_each_pending_event(tmp_path):
    bot, fake = make_bot(tmp_path, {"m1": [DENTIST, SOCCER]})
    tg = FakeTelegram()
    await bot.run_scan(tg)
    ctx = SimpleNamespace(bot=tg)
    await bot.on_button(press(222, "Sam", "ev:skip:2")[0], ctx)
    await bot.on_button(press(111, "Andrew", "ev:all:1")[0], ctx)
    assert [e["title"] for e in fake.events] == ["Dentist"]
    batch_edits = [e["text"] for (_, mid), e in tg.edits.items() if mid in (5, 6)]
    assert batch_edits == ["Add all, by Andrew: 1 added."] * 2


async def test_strangers_cannot_press_buttons(tmp_path):
    bot, fake = make_bot(tmp_path, {"m1": [DENTIST]})
    tg = FakeTelegram()
    await bot.run_scan(tg)
    update, answers = press(999, "Mallory", "ev:add:1")
    await bot.on_button(update, SimpleNamespace(bot=tg))
    assert fake.events == [] and answers == [] and tg.edits == {}
    assert bot.store.get_proposal(1)["status"] == "pending"


async def test_dry_run_prefixes_proposals_and_does_not_write(tmp_path):
    bot, fake = make_bot(tmp_path, {"m1": [DENTIST]}, DRY_RUN="true")
    tg = FakeTelegram()
    await bot.run_scan(tg)
    assert tg.sent[0]["text"].startswith("[DRY RUN] Event found")
    await bot.on_button(press(111, "Andrew", "ev:add:1")[0], SimpleNamespace(bot=tg))
    assert fake.writes == []
    assert all("Would add by Andrew." in e["text"] for e in tg.edits.values())


def test_scan_jobs_scheduled_at_configured_chicago_times(tmp_path):
    bot, _ = make_bot(tmp_path, {})
    app = bot.build()
    jobs = sorted(j.name for j in app.job_queue.jobs())
    assert jobs == ["gmail-scan-0700", "gmail-scan-1700"]
