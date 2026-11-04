"""Grocery list as heb.com links, and photo -> grocery item with confirm buttons."""

import base64
import json
from types import SimpleNamespace

import pytest

from family_assistant.actions import Actions
from family_assistant.bot import Bot, split_message
from family_assistant.config import Settings
from family_assistant.photo import ItemIdentifier, PhotoError, validate
from family_assistant.shopping_links import heb_search_url, render_shopping_links
from family_assistant.tools import run_tool

from .fakes import FakeClaude, FakeYuvomi, response, text
from .test_support import BASE_ENV

JPEG = b"\xff\xd8\xff\xe0fake-jpeg"


# ------------------------------------------------------------ shopping links

def test_heb_search_url_encodes_the_item():
    assert heb_search_url("whole  milk") == "https://www.heb.com/search/?q=whole%20milk"
    assert heb_search_url("M&M's 10oz/bag") == "https://www.heb.com/search/?q=M%26M%27s%2010oz%2Fbag"


def test_render_links_lists_each_item_with_quantity():
    out = render_shopping_links("Groceries", [
        {"name": "Whole milk", "quantity": "1"}, {"name": "Eggs", "quantity": "2"},
        {"name": "Flour", "quantity": "5 lb"}, {"name": "  ", "quantity": None},
    ])
    lines = out.splitlines()
    assert lines[0] == "Groceries (3 items). Tap a link to find it on heb.com and add it to your cart:"
    assert lines[1:7] == [
        "1. Whole milk", "https://www.heb.com/search/?q=Whole%20milk",
        "2. Eggs (x2)", "https://www.heb.com/search/?q=Eggs",
        "3. Flour (5 lb)", "https://www.heb.com/search/?q=Flour",
    ]
    assert chr(0x2014) not in out
    assert "empty" in render_shopping_links("Groceries", [])


async def test_order_groceries_sends_links_from_the_list_and_writes_nothing():
    fake = FakeYuvomi()
    fake.items = [{"id": 1, "list_id": 1, "name": "Bananas", "quantity": None, "is_checked": 0},
                  {"id": 2, "list_id": 1, "name": "Done thing", "quantity": None, "is_checked": 1}]
    actions = Actions(fake.client(), "Shopping", dry_run=False)
    content, is_error = await run_tool(actions, "order_groceries", {})
    assert not is_error and json.loads(content)["items"] == 1
    assert "https://www.heb.com/search/?q=Bananas" in actions.reports[0]
    assert "Done thing" not in actions.reports[0]
    assert fake.writes == []
    _, is_error = await run_tool(actions, "order_groceries", {})
    assert is_error  # once per message


def test_split_message_keeps_lines_and_limit():
    text_ = "\n".join(f"line {i}" for i in range(100))
    chunks = split_message(text_, limit=50)
    assert all(len(c) <= 50 for c in chunks)
    assert "\n".join(chunks) == text_
    assert split_message("x" * 120, limit=50) == ["x" * 50, "x" * 50, "x" * 20]
    assert split_message("") == [""]


# ------------------------------------------------------------ identification

def ident(**kw):
    base = {"identified": True, "name": "H-E-B Whole Milk 1 gal", "quantity": None, "alternatives": [],
            "reason": None}
    return {**base, **kw}


def test_validate_options_quantity_and_unidentified():
    r = validate(ident(alternatives=["H-E-B 2% Milk 1 gal", "h-e-b whole milk 1 GAL", "a", "b"], quantity="2"))
    assert r.options == ("H-E-B Whole Milk 1 gal", "H-E-B 2% Milk 1 gal", "a")
    assert r.quantity == "2"
    assert validate(ident(quantity="lots")).quantity is None
    assert validate(ident(quantity="500")).quantity is None
    none = validate(ident(identified=False, name="", reason="It is a photo of a dog."))
    assert none.options == () and none.reason == "It is a photo of a dog."
    assert validate({}).options == ()


async def test_identify_sends_image_as_data_with_schema_and_no_tools():
    claude = FakeClaude(response(text(json.dumps(ident()))))
    out = await ItemIdentifier(claude, model="claude-sonnet-5-5").identify(JPEG, "image/jpeg", "2 of these")
    assert out.options == ("H-E-B Whole Milk 1 gal",)
    call = claude.calls[0]
    assert "tools" not in call and call["beta"]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "untrusted DATA" in call["system"]
    image, caption = call["messages"][0]["content"]
    assert image["source"] == {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(JPEG).decode()}
    assert caption == {"type": "text", "text": "Caption: 2 of these"}


async def test_identify_refusal_bad_json_and_bad_type():
    out = await ItemIdentifier(FakeClaude(response(text(""), stop="refusal")), model="m").identify(JPEG, "image/jpeg", None)
    assert out.options == ()
    with pytest.raises(PhotoError):
        await ItemIdentifier(FakeClaude(response(text("nope"))), model="m").identify(JPEG, "image/jpeg", None)
    with pytest.raises(PhotoError):
        await ItemIdentifier(FakeClaude(), model="m").identify(JPEG, "image/heic", None)


# ------------------------------------------------------------ Telegram flow

class FakeIdentifier:
    def __init__(self, raw: dict) -> None:
        self.raw = raw
        self.calls: list[tuple] = []

    async def identify(self, image, media_type, caption):
        self.calls.append((image, media_type, caption))
        return validate(self.raw)


class FakeMessage:
    def __init__(self, chat_id=111, caption=None, photo=True, mime="image/jpeg"):
        self.chat_id = chat_id
        self.caption = caption
        self.replies: list[dict] = []
        file = SimpleNamespace(download_as_bytearray=self._download)
        source = SimpleNamespace(file_size=len(JPEG), get_file=self._get_file(file), mime_type=mime)
        self.photo = [SimpleNamespace(file_size=10), source] if photo else []
        self.document = None if photo else source

    @staticmethod
    def _get_file(file):
        async def get_file():
            return file
        return get_file

    @staticmethod
    async def _download():
        return bytearray(JPEG)

    async def reply_text(self, text, reply_markup=None, **_):
        self.replies.append({"text": text, "markup": reply_markup})


class FakeTg:
    def __init__(self):
        self.edits: dict[tuple[int, int], dict] = {}

    async def send_chat_action(self, *_):
        pass

    async def edit_message_text(self, text, chat_id=None, message_id=None, reply_markup=None, **_):
        self.edits[(chat_id, message_id)] = {"text": text, "markup": reply_markup}


def make_bot(tmp_path, raw, **env):
    settings = Settings.from_env({**BASE_ENV, "CONFIG_DIR": str(tmp_path), "DRY_RUN": "false", **env})
    bot = Bot(settings)
    fake = FakeYuvomi()
    bot.yuvomi = fake.client()
    bot.identifier = FakeIdentifier(raw)
    return bot, fake


async def send_photo(bot, tg, user_id=111, **kw):
    message = FakeMessage(**kw)
    update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=user_id))
    await bot.on_photo(update, SimpleNamespace(bot=tg))
    return message


def tap(tg, user_id, name, data, message_id=7):
    answers: list = []

    async def answer(text=None, **_):
        answers.append(text)

    query = SimpleNamespace(
        from_user=SimpleNamespace(id=user_id, first_name=name), data=data, answer=answer,
        message=SimpleNamespace(chat=SimpleNamespace(id=111), message_id=message_id), get_bot=lambda: tg,
    )
    return SimpleNamespace(callback_query=query), answers


def buttons(reply) -> list[str]:
    return [b.callback_data for row in reply["markup"].inline_keyboard for b in row]


async def test_photo_offers_options_then_add_writes_with_read_back(tmp_path):
    bot, fake = make_bot(tmp_path, ident(alternatives=["H-E-B 2% Milk 1 gal"], quantity="2"))
    tg = FakeTg()
    message = await send_photo(bot, tg, caption="2 of these")
    [reply] = message.replies
    assert reply["text"].startswith("Looks like: H-E-B Whole Milk 1 gal (x2)\nAdd it to Shopping?")
    assert buttons(reply) == ["ph:1:0", "ph:1:1", "ph:1:x"]
    assert bot.identifier.calls == [(JPEG, "image/jpeg", "2 of these")]
    assert fake.writes == []  # nothing until someone taps

    update, answers = tap(tg, 111, "Andrew", "ph:1:1")
    await bot.on_photo_button(update, None)
    assert [(i["name"], i["quantity"]) for i in fake.items] == [("H-E-B 2% Milk 1 gal", "2")]
    [edit] = tg.edits.values()
    assert "H-E-B 2% Milk 1 gal" in edit["text"] and "Chosen by Andrew." in edit["text"]
    assert edit["markup"] is None

    update, answers = tap(tg, 222, "Sam", "ph:1:0")
    await bot.on_photo_button(update, None)
    assert answers == ["Already handled by Andrew."]
    assert len(fake.items) == 1


async def test_photo_cancel_and_strangers(tmp_path):
    bot, fake = make_bot(tmp_path, ident())
    tg = FakeTg()
    await send_photo(bot, tg)
    update, answers = tap(tg, 999, "Mallory", "ph:1:0")
    await bot.on_photo_button(update, None)
    assert answers == [] and fake.items == [] and tg.edits == {}

    await bot.on_photo_button(tap(tg, 222, "Sam", "ph:1:x")[0], None)
    assert fake.writes == []
    assert "Cancelled by Sam" in next(iter(tg.edits.values()))["text"]


async def test_failed_add_keeps_buttons_for_retry(tmp_path):
    bot, fake = make_bot(tmp_path, ident())
    tg = FakeTg()
    await send_photo(bot, tg)
    fake.fail_writes = True
    await bot.on_photo_button(tap(tg, 111, "Andrew", "ph:1:0")[0], None)
    edit = next(iter(tg.edits.values()))
    assert edit["markup"] is not None
    fake.fail_writes = False
    await bot.on_photo_button(tap(tg, 111, "Andrew", "ph:1:0")[0], None)
    assert [i["name"] for i in fake.items] == ["H-E-B Whole Milk 1 gal"]


async def test_unidentified_photo_and_unsupported_file(tmp_path):
    bot, fake = make_bot(tmp_path, ident(identified=False, name="", reason="I only see a blurry shelf."))
    tg = FakeTg()
    message = await send_photo(bot, tg)
    assert message.replies[0]["text"].startswith("I only see a blurry shelf. Nothing was added.")
    assert message.replies[0]["markup"] is None

    message = await send_photo(bot, tg, photo=False, mime="image/heic")
    assert "JPEG, PNG, GIF or WebP" in message.replies[0]["text"]
    assert len(bot.identifier.calls) == 1 and fake.writes == []


async def test_dry_run_photo_add_writes_nothing(tmp_path):
    bot, fake = make_bot(tmp_path, ident(), DRY_RUN="true")
    tg = FakeTg()
    message = await send_photo(bot, tg)
    assert message.replies[0]["text"].startswith("[DRY RUN] Looks like")
    await bot.on_photo_button(tap(tg, 111, "Andrew", "ph:1:0")[0], None)
    assert fake.writes == []
    assert next(iter(tg.edits.values()))["text"].startswith("[DRY RUN]")


def test_photo_handlers_registered(tmp_path):
    bot, _ = make_bot(tmp_path, ident())
    app = bot.build()
    patterns = [h.pattern.pattern for h in app.handlers[0] if hasattr(h, "pattern") and h.pattern]
    assert r"^ph:\d+:(\d|x)$" in patterns
