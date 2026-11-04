from datetime import datetime
from zoneinfo import ZoneInfo

from family_assistant.actions import Actions
from family_assistant.extract import CandidateEvent, ExtractionError
from family_assistant.gmail import Email, GmailAuthError
from family_assistant.scanner import Scanner, approve, proposal_text, skip
from family_assistant.store import Store

from .fakes import FakeYuvomi

NOW = datetime(2026, 11, 3, 17, 45, tzinfo=ZoneInfo("America/Chicago"))
DENTIST = CandidateEvent("Dentist", "2026-11-06T15:00", "2026-11-06T16:00", False, "Smile Dental")


class FakeAccount:
    def __init__(self, label: str, emails: dict[str, Email], fail: Exception | None = None) -> None:
        self.label, self.emails, self.fail = label, emails, fail
        self.fetched: list[str] = []

    async def list_message_ids(self, query, limit):
        if self.fail:
            raise self.fail
        return list(self.emails)

    async def get_email(self, message_id):
        self.fetched.append(message_id)
        return self.emails[message_id]


class FakeExtractor:
    def __init__(self, results: dict[str, list | Exception]) -> None:
        self.results = results

    async def extract(self, email, now):
        result = self.results[email.message_id]
        if isinstance(result, Exception):
            raise result
        return result


def mail(mid: str, account: str = "andrew") -> Email:
    return Email(account, mid, f"Subject {mid}", "Office <o@x.test>", "date", "body")


def setup(results, accounts=None, mode="propose", dry_run=False, fake=None):
    fake = fake or FakeYuvomi()
    store = Store(":memory:")
    accounts = accounts or [FakeAccount("andrew", {mid: mail(mid) for mid in results})]
    scanner = Scanner(accounts=accounts, extractor=FakeExtractor(results), store=store,
                      actions_factory=lambda: Actions(fake.client(), "Shopping", dry_run), mode=mode)
    return scanner, store, fake


async def test_scan_creates_proposals_and_never_writes_in_propose_mode():
    scanner, store, fake = setup({"m1": [DENTIST], "m2": []})
    report = await scanner.scan(NOW)
    assert report.emails_checked == 2
    assert [p["title"] for p in report.proposals] == ["Dentist"]
    assert fake.writes == []
    assert store.is_processed("andrew", "m1") and store.is_processed("andrew", "m2")


async def test_processed_emails_are_not_read_again():
    scanner, store, _ = setup({"m1": [DENTIST]})
    await scanner.scan(NOW)
    report = await scanner.scan(NOW)
    assert report.emails_checked == 0 and report.proposals == []
    assert scanner.accounts[0].fetched == ["m1"]


async def test_dedupe_against_yuvomi_and_existing_proposals():
    fake = FakeYuvomi()
    fake.events.append({"id": 9, "title": "dentist", "start_datetime": "2026-11-06T15:00:00"})
    other = CandidateEvent("Soccer", "2026-11-07T09:00", None, False, None)
    accounts = [FakeAccount("andrew", {"m1": mail("m1")}), FakeAccount("sam", {"m2": mail("m2", "sam")})]
    scanner, store, _ = setup({"m1": [DENTIST, other], "m2": [other]}, accounts=accounts, fake=fake)
    report = await scanner.scan(NOW)
    # Dentist is already in Yuvomi; Soccer appears in both inboxes but is proposed once.
    assert [p["title"] for p in report.proposals] == ["Soccer"]
    assert report.already_known == 2


async def test_extraction_failure_leaves_email_for_next_scan():
    scanner, store, _ = setup({"m1": ExtractionError("cut off")})
    report = await scanner.scan(NOW)
    assert report.errors and not store.is_processed("andrew", "m1")


async def test_auth_error_is_reported_per_account():
    accounts = [FakeAccount("sam", {}, fail=GmailAuthError("Gmail token for 'sam' was rejected; run gmail-auth again")),
                FakeAccount("andrew", {"m1": mail("m1")})]
    scanner, _, _ = setup({"m1": [DENTIST]}, accounts=accounts)
    report = await scanner.scan(NOW)
    assert "gmail-auth" in report.errors[0]
    assert len(report.proposals) == 1


async def test_auto_mode_adds_with_read_back_proof():
    scanner, store, fake = setup({"m1": [DENTIST]}, mode="auto")
    report = await scanner.scan(NOW)
    assert report.proposals == []
    [result] = report.auto_results
    assert result.status == "done" and result.record == fake.events[0]
    assert fake.events[0]["location"] == "Smile Dental"


async def test_first_approval_wins_and_second_is_told_who():
    scanner, store, fake = setup({"m1": [DENTIST]})
    [p] = (await scanner.scan(NOW)).proposals
    actions = Actions(fake.client(), "Shopping", False)

    first = await approve(store, actions, p["id"], "Andrew")
    second = await approve(store, actions, p["id"], "Sam")
    assert first.ok and first.status == "added"
    assert f"[#{fake.events[0]['id']}]" in first.text
    assert not second.ok and second.text == "Already handled by Andrew."
    assert len(fake.events) == 1
    assert (await skip(store, p["id"], "Sam")).text == "Already handled by Andrew."


async def test_failed_add_returns_to_pending_for_retry():
    scanner, store, fake = setup({"m1": [DENTIST]})
    [p] = (await scanner.scan(NOW)).proposals
    fake.fail_writes = True
    decision = await approve(store, Actions(fake.client(), "Shopping", False), p["id"], "Andrew")
    assert decision.status == "failed" and "Nothing was added" in decision.text
    assert store.get_proposal(p["id"])["status"] == "pending"
    fake.fail_writes = False
    assert (await approve(store, Actions(fake.client(), "Shopping", False), p["id"], "Sam")).status == "added"


async def test_unverified_add_is_final_and_honest():
    scanner, store, fake = setup({"m1": [DENTIST]})
    [p] = (await scanner.scan(NOW)).proposals
    fake.fail_reads_after_write = True
    decision = await approve(store, Actions(fake.client(), "Shopping", False), p["id"], "Andrew")
    assert decision.status == "unverified" and "could not confirm" in decision.text


def test_proposal_text():
    p = {"account": "andrew", "title": "Dentist", "start": "2026-11-06T15:00", "end_at": "2026-11-06T16:00",
         "all_day": 0, "location": "Smile Dental", "sender": "Office <o@x.test>", "subject": "Reminder"}
    assert proposal_text(p, {"andrew": "Andrew"}) == (
        "Event found in Andrew's email:\nDentist\nFri Nov 6, 3:00 PM - 4:00 PM\nWhere: Smile Dental\n"
        'From: Office <o@x.test>, "Reminder"'
    )
