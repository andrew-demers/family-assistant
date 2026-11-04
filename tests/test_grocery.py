import json
import sys
from pathlib import Path

import pytest

from family_assistant.actions import Actions
from family_assistant.agent import NO_TEXT, Agent
from family_assistant.grocery import Matcher, build_cart, render_order
from family_assistant.heb import HebError, HebMcp

from .fakes import FakeClaude, FakeYuvomi, response, text, tool_use

STORE = "737"


def product(pid, sku, name, price, size="1 gal", available=True, **kw):
    return {"product_id": pid, "sku": sku, "name": name, "price": price, "available": available,
            "brand": "H-E-B", "size": size, **kw}


MILK = product("101", "9001", "H-E-B Whole Milk", 3.48)
OAT = product("102", "9002", "Oatly Oat Milk", 4.99, size="64 oz")
EGGS = product("201", "9101", "H-E-B Grade A Large Eggs", 2.97, size="12 ct")


class FakeHeb:
    """Mimics texas-grocery-mcp tool results (shapes from its source)."""

    def __init__(self, search: dict[str, list[dict]], cart: list[dict] | None = None) -> None:
        self.search = search
        self.cart = list(cart or [])
        self.calls: list[tuple[str, dict]] = []
        self.authenticated = True
        self.refresh_result = {"status": "success", "success": True}
        self.store_error: dict | None = None
        self.drop_on_add: set[str] = set()  # skus that silently fail to appear
        self.price_override: dict[str, float] = {}

    def _cart_item(self, p, qty):
        price = self.price_override.get(p["sku"], p["price"])
        return {"product_id": p["product_id"], "sku_id": p["sku"], "name": p["name"], "quantity": qty,
                "price": price, "total": round(price * qty, 2)}

    async def call(self, name, args=None):
        args = args or {}
        self.calls.append((name, args))
        if name == "session_status":
            return {"authenticated": self.authenticated, "needs_refresh": not self.authenticated}
        if name == "session_refresh":
            if self.refresh_result.get("status") == "success":
                self.authenticated = True
            return self.refresh_result
        if name == "cart_check_auth":
            return {"authenticated": self.authenticated}
        if name == "store_change":
            return self.store_error or {"success": True, "store_id": args["store_id"], "store_name": "The Heights H-E-B"}
        if name == "cart_get":
            return {"items": self.cart, "item_count": len(self.cart),
                    "subtotal": round(sum(i["price"] * i["quantity"] for i in self.cart), 2)}
        if name == "product_search_batch":
            return {"results": [{"query": q, "success": True, "products": self.search.get(q, [])} for q in args["queries"]]}
        if name == "cart_add_many":
            if not args["confirm"]:
                return {"preview": True, "items_to_add": args["items"], "count": len(args["items"])}
            by_sku = {p["sku"]: p for ps in self.search.values() for p in ps}
            for item in args["items"]:
                if item["sku_id"] not in self.drop_on_add:
                    self.cart.append(self._cart_item(by_sku[item["sku_id"]], item["quantity"]))
            return {"success": True, "added": args["items"], "summary": {}}
        raise AssertionError(f"unexpected tool {name}")

    def called(self, name):
        return [a for n, a in self.calls if n == name]


def matcher(*choices):
    return Matcher(FakeClaude(response(text(json.dumps({"choices": list(choices)})))), model="claude-sonnet-5-5")


def pick(index, pid, substitution=False, note=None):
    return {"index": index, "product_id": pid, "substitution": substitution, "note": note}


ITEMS = [{"name": "milk", "quantity": "2"}, {"name": "Fairlife milk", "quantity": None},
         {"name": "unicorn steak", "quantity": None}]


async def test_order_builds_cart_and_summary_from_tool_results_only():
    heb = FakeHeb({"milk": [MILK, OAT], "Fairlife milk": [MILK, OAT], "unicorn steak": []},
                  cart=[{"product_id": "201", "sku_id": "9101", "name": "H-E-B Grade A Large Eggs", "quantity": 1,
                         "price": 2.97, "total": 2.97}])
    m = matcher(pick(0, "101"), pick(1, "102", True, "Fairlife not found, picked Oatly"))
    report = await build_cart(heb, m, ITEMS, STORE, dry_run=False)
    text_ = render_order(report)

    assert heb.called("cart_add_many") == [{"items": [
        {"product_id": "101", "sku_id": "9001", "quantity": 2},
        {"product_id": "102", "sku_id": "9002", "quantity": 1},
    ], "confirm": True}]
    assert [l.status for l in report.lines] == ["added", "added", "no_match"]
    assert "HEB cart, The Heights H-E-B (store 737)." in text_
    assert "- H-E-B Whole Milk, 1 gal: 2 x $3.48" in text_
    assert '- Oatly Oat Milk, 64 oz: 1 x $4.99 [substitution for "Fairlife milk": Fairlife not found, picked Oatly]' in text_
    assert "No match at HEB: unicorn steak" in text_
    assert "Also in the cart from before:\n- H-E-B Grade A Large Eggs: 1 x $2.97" in text_
    assert "Cart subtotal (from HEB tool, sum of item prices, before tax and fees): $14.92" in text_
    for line in ("Total with tax and fees", "Payment card", "Pickup or delivery time slot"):
        assert f"{line}: not available from HEB tool." in text_
    assert "Nothing has been purchased" in text_
    # Store set explicitly (the tool ignores HEB_DEFAULT_STORE).
    assert heb.called("store_change") == [{"store_id": STORE}]


async def test_item_missing_on_read_back_is_reported_not_claimed():
    heb = FakeHeb({"milk": [MILK]})
    heb.drop_on_add = {"9001"}
    report = await build_cart(heb, matcher(pick(0, "101")), [{"name": "milk"}], STORE, dry_run=False)
    text_ = render_order(report)
    assert report.lines[0].status == "failed"
    assert "Could not add: milk (not in the cart when read back)" in text_
    assert "Added to the HEB cart" not in text_


async def test_zero_price_is_never_shown_as_money():
    heb = FakeHeb({"milk": [MILK]})
    heb.price_override = {"9001": 0.0}
    text_ = render_order(await build_cart(heb, matcher(pick(0, "101")), [{"name": "milk"}], STORE, False))
    assert "$0.00" not in text_
    assert "1 x price not available from HEB tool" in text_
    assert "Cart subtotal: not available from HEB tool (some prices missing)." in text_


async def test_dry_run_previews_only():
    heb = FakeHeb({"milk": [MILK]})
    report = await build_cart(heb, matcher(pick(0, "101")), [{"name": "milk"}], STORE, dry_run=True)
    assert [a["confirm"] for a in heb.called("cart_add_many")] == [False]
    assert heb.cart == []
    text_ = render_order(report)
    assert "Would add to the HEB cart:\n- H-E-B Whole Milk, 1 gal: 1 x $3.48" in text_
    assert "Nothing has been purchased" not in text_


async def test_login_needing_a_person_stops_before_any_cart_call():
    heb = FakeHeb({"milk": [MILK]})
    heb.authenticated = False
    heb.refresh_result = {"status": "human_action_required", "action": "captcha"}
    text_ = render_order(await build_cart(heb, matcher(), [{"name": "milk"}], STORE, False))
    assert text_.startswith("HEB cart not built. HEB needs a person to finish signing in (captcha).")
    assert not heb.called("cart_add_many") and not heb.called("store_change")
    assert heb.called("session_refresh") == [{"headless": True, "use_saved_credentials": True}]


async def test_store_conflict_stops_safely():
    heb = FakeHeb({"milk": [MILK]})
    heb.store_error = {"error": True, "code": "STORE_CONFLICT", "message": "2 cart items unavailable at this store"}
    text_ = render_order(await build_cart(heb, matcher(), [{"name": "milk"}], STORE, False))
    assert "Could not select HEB store 737: 2 cart items unavailable" in text_
    assert not heb.called("cart_add_many")


async def test_model_cannot_pick_outside_candidates_or_unavailable_items():
    sold_out = product("103", "9003", "Sold Out Milk", 2.00, available=False)
    heb = FakeHeb({"milk": [MILK, sold_out], "bread": [sold_out]})
    report = await build_cart(heb, matcher(pick(0, "999"), pick(1, "103")),
                              [{"name": "milk"}, {"name": "bread"}], STORE, False)
    assert [l.status for l in report.lines] == ["no_match", "no_match"]
    assert not heb.called("cart_add_many")


async def test_item_already_in_cart_is_not_added_again():
    heb = FakeHeb({"milk": [MILK]}, cart=[{"product_id": "101", "sku_id": "9001", "name": "H-E-B Whole Milk",
                                            "quantity": 1, "price": 3.48}])
    report = await build_cart(heb, matcher(pick(0, "101")), [{"name": "milk"}], STORE, False)
    assert report.lines[0].status == "already_in_cart"
    assert "Already in the cart, not added again: H-E-B Whole Milk." in render_order(report)
    assert not heb.called("cart_add_many")


async def test_free_text_quantity_adds_one_and_flags_it():
    heb = FakeHeb({"milk": [MILK]})
    report = await build_cart(heb, matcher(pick(0, "101")), [{"name": "milk", "quantity": "1 gallon"}], STORE, False)
    assert heb.called("cart_add_many")[0]["items"][0]["quantity"] == 1
    assert "Check quantities (list said): milk: 1 gallon." in render_order(report)


async def test_empty_list():
    report = await build_cart(FakeHeb({}), matcher(), [], STORE, False)
    assert render_order(report) == "HEB cart not built. The grocery list is empty, so there is nothing to order."


async def test_agent_order_tool_puts_code_built_summary_in_reply():
    fake = FakeYuvomi(lists=["Groceries"])
    fake.items.append({"id": 1, "list_id": 1, "name": "milk", "quantity": None, "is_checked": 0})
    heb = FakeHeb({"milk": [MILK]})
    m = matcher(pick(0, "101"))

    async def orderer(actions):
        grocery = await actions.read_grocery_list()
        return render_order(await build_cart(heb, m, grocery["items"], STORE, actions.dry_run))

    claude = FakeClaude(
        response(tool_use("order_groceries", {})),
        response(tool_use("order_groceries", {})),
        response(text(NO_TEXT)),
    )
    actions = Actions(fake.client(), "Groceries", True, orderer=orderer)
    result = await Agent(claude, model="claude-sonnet-5-5").run("Andrew", "order the groceries", actions)
    assert result.reply.startswith("[DRY RUN] HEB cart, The Heights H-E-B (store 737).")
    assert "Would add to the HEB cart:\n- H-E-B Whole Milk" in result.reply
    # A second call in the same message is refused.
    assert claude.tool_results(2)[0]["is_error"] is True
    assert len(heb.called("product_search_batch")) == 1


async def test_order_tool_without_store_configured():
    claude = FakeClaude(response(tool_use("order_groceries", {})), response(text("HEB ordering is not set up yet.")))
    result = await Agent(claude, model="m").run("A", "order", Actions(FakeYuvomi().client(), "Shopping", False))
    assert "not set up" in json.loads(claude.tool_results(1)[0]["content"])["error"]


# ------------------------------------------------------------- real stdio client

SERVER = [str(Path(__file__).with_name("fake_heb_server.py"))]


async def test_heb_mcp_client_over_real_stdio(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    async with HebMcp(sys.executable, tmp_path, store_id=STORE, args=SERVER) as heb:
        assert (await heb.call("cart_check_auth"))["authenticated"] is False
        env = await heb.call("debug_env")
        with pytest.raises(HebError, match="broken"):
            await heb.call("broken")
    # The HEB tool never sees our secrets; its state lives under CONFIG_DIR/heb.
    assert "ANTHROPIC_API_KEY" not in env["keys"]
    assert env["auth_state_path"] == str(tmp_path / "heb" / "auth.json")
    assert "HEB_DEFAULT_STORE" in env["keys"]


async def test_heb_mcp_bad_command_raises_heb_error(tmp_path):
    with pytest.raises(HebError, match="could not start"):
        async with HebMcp(str(tmp_path / "missing-binary"), tmp_path):
            pass
