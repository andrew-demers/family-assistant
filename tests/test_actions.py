import pytest

from family_assistant.actions import Actions, InvalidInput

from .fakes import FakeYuvomi


def make(fake: FakeYuvomi, dry_run: bool = False, list_name: str = "Shopping") -> Actions:
    return Actions(fake.client(), list_name, dry_run)


async def test_grocery_item_confirmed_from_read_back():
    fake = FakeYuvomi()
    result = await make(fake).add_grocery_item("  milk ", "1 gallon")
    assert result.status == "done"
    assert result.record == fake.items[0]
    assert result.container == "Shopping"
    # A separate GET happened after the POST.
    assert fake.requests[-2:] == [("POST", "/shopping/1/items"), ("GET", "/shopping/1/items")]


async def test_grocery_list_matched_case_insensitively_and_named_from_yuvomi():
    fake = FakeYuvomi(lists=["Costco", "SHOPPING"])
    result = await make(fake, list_name="shopping").add_grocery_item("eggs")
    assert result.status == "done"
    assert result.container == "SHOPPING"


async def test_missing_grocery_list_fails_without_writing():
    fake = FakeYuvomi(lists=["Costco"])
    result = await make(fake).add_grocery_item("eggs")
    assert result.status == "failed"
    assert result.failed_step == "find list"
    assert '"Costco"' in result.error
    assert fake.writes == []


async def test_duplicate_grocery_item_is_skipped():
    fake = FakeYuvomi()
    fake.items.append({"id": 7, "list_id": 1, "name": "Milk", "quantity": None, "is_checked": 0})
    result = await make(fake).add_grocery_item("milk")
    assert result.status == "duplicate"
    assert result.record["id"] == 7
    assert fake.writes == []


async def test_checked_item_is_not_a_duplicate():
    fake = FakeYuvomi()
    fake.items.append({"id": 7, "list_id": 1, "name": "milk", "quantity": None, "is_checked": 1})
    assert (await make(fake).add_grocery_item("milk")).status == "done"


async def test_failed_read_back_is_unverified_not_done():
    fake = FakeYuvomi()
    fake.fail_reads_after_write = True
    result = await make(fake).add_grocery_item("milk")
    assert result.status == "unverified"
    assert result.failed_step == "read-back"
    assert result.written_id is not None
    assert "503" in result.error


async def test_write_accepted_but_missing_on_read_back_is_unverified():
    fake = FakeYuvomi()
    fake.lose_writes = True
    result = await make(fake).add_grocery_item("milk")
    assert result.status == "unverified"
    assert "not on the list" in result.error


async def test_read_back_with_different_value_is_unverified():
    fake = FakeYuvomi()
    fake.rename_on_store = "oat milk"
    result = await make(fake).add_grocery_item("milk")
    assert result.status == "unverified"
    assert result.failed_step == "verify"


async def test_write_failure_reports_step():
    fake = FakeYuvomi()
    fake.fail_writes = True
    result = await make(fake).add_grocery_item("milk")
    assert result.status == "failed"
    assert result.failed_step == "write"


async def test_dry_run_never_writes():
    fake = FakeYuvomi()
    actions = make(fake, dry_run=True)
    assert (await actions.add_grocery_item("milk")).status == "dry_run"
    assert (await actions.add_task("call plumber")).status == "dry_run"
    assert (await actions.create_event("Dentist", "2026-11-06T15:00")).status == "dry_run"
    assert fake.writes == []


async def test_task_confirmed_with_get_by_id():
    fake = FakeYuvomi()
    result = await make(fake).add_task("Call plumber", "2026-11-05", "09:30")
    assert result.status == "done"
    assert fake.requests[-1] == ("GET", f"/tasks/{result.written_id}")
    assert result.record["due_time"] == "09:30"


async def test_task_read_back_404_is_unverified():
    fake = FakeYuvomi()
    fake.lose_writes = True
    result = await make(fake).add_task("Call plumber")
    assert result.status == "unverified"
    assert "404" in result.error


async def test_duplicate_open_task_skipped():
    fake = FakeYuvomi()
    fake.tasks.append({"id": 3, "title": "call plumber", "status": "open"})
    assert (await make(fake).add_task("Call  Plumber")).status == "duplicate"


@pytest.mark.parametrize("args", [
    {"due_date": "11/5/2026"},
    {"due_date": "2026-11-05", "due_time": "9am"},
    {"due_time": "09:00"},
])
async def test_task_bad_dates_rejected(args):
    with pytest.raises(InvalidInput):
        await make(FakeYuvomi()).add_task("x", **args)


async def test_event_confirmed_with_get_by_id():
    fake = FakeYuvomi()
    result = await make(fake).create_event("Dentist", "2026-11-06T15:00", "2026-11-06T16:00", location="Main St")
    assert result.status == "done"
    assert fake.requests[-1] == ("GET", f"/calendar/{result.written_id}")
    assert result.record["location"] == "Main St"


async def test_event_without_time_is_rejected():
    with pytest.raises(InvalidInput):
        await make(FakeYuvomi()).create_event("Dentist", "2026-11-06")


async def test_all_day_event_takes_date():
    result = await make(FakeYuvomi()).create_event("Trash day", "2026-11-06", all_day=True)
    assert result.status == "done"


async def test_duplicate_event_same_title_and_time_skipped():
    fake = FakeYuvomi()
    fake.events.append({"id": 5, "title": "Dentist", "start_datetime": "2026-11-06T15:00:00"})
    assert (await make(fake).create_event("dentist", "2026-11-06T15:00")).status == "duplicate"
    assert (await make(fake).create_event("dentist", "2026-11-06T16:00")).status == "done"
