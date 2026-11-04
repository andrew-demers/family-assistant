"""Build the Telegram confirmation from action results (read-back data only)."""

from __future__ import annotations

from datetime import datetime

from .actions import ActionResult

DRY_RUN_PREFIX = "[DRY RUN] "


def _fmt_datetime(value: str | None, all_day: bool) -> str:
    if not value:
        return "?"
    value = value.replace(" ", "T")
    try:
        if all_day or len(value) == 10:
            d = datetime.strptime(value[:10], "%Y-%m-%d")
            return f"{d:%a} {d:%b} {d.day}"
        d = datetime.strptime(value[:16], "%Y-%m-%dT%H:%M")
    except ValueError:
        return value
    hour = d.hour % 12 or 12
    return f"{d:%a} {d:%b} {d.day}, {hour}:{d:%M} {'AM' if d.hour < 12 else 'PM'}"


def _fmt_end(start: str | None, end: str | None, all_day: bool) -> str:
    if not end or all_day:
        return ""
    end_fmt = _fmt_datetime(end, False)
    # Same day: show only the time.
    if start and start[:10] == end[:10] and ", " in end_fmt:
        end_fmt = end_fmt.split(", ", 1)[1]
    return f" - {end_fmt}"


def format_when(start: str | None, end: str | None, all_day: bool) -> str:
    return _fmt_datetime(start, all_day) + _fmt_end(start, end, all_day) + (" (all day)" if all_day else "")


def describe(result: ActionResult) -> str:
    """One item as Yuvomi returned it."""
    rec = result.record or {}
    rid = rec.get("id")
    id_part = f" [#{rid}]" if rid is not None else ""
    if result.kind == "grocery":
        qty = rec.get("quantity")
        return f"{rec.get('name', result.requested)}{f' ({qty})' if qty else ''}{id_part}"
    if result.kind == "task":
        due = rec.get("due_date")
        when = ""
        if due:
            when = f" (due {_fmt_datetime(due + ('T' + rec['due_time'] if rec.get('due_time') else ''), not rec.get('due_time'))})"
        return f"{rec.get('title', result.requested)}{when}{id_part}"
    all_day = bool(rec.get("all_day"))
    start = rec.get("start_datetime")
    where = f" at {rec['location']}" if rec.get("location") else ""
    return f"{rec.get('title', result.requested)}, {format_when(start, rec.get('end_datetime'), all_day)}{where}{id_part}"


def _group(results: list[ActionResult]) -> dict[str, list[ActionResult]]:
    groups: dict[str, list[ActionResult]] = {}
    for r in results:
        groups.setdefault(r.container, []).append(r)
    return groups


def render_confirmation(results: list[ActionResult], dry_run: bool) -> str:
    if not results:
        return ""
    lines: list[str] = []

    done = [r for r in results if r.status == "done"]
    dry = [r for r in results if r.status == "dry_run"]
    dupes = [r for r in results if r.status == "duplicate"]
    failed = [r for r in results if r.status == "failed"]
    unverified = [r for r in results if r.status == "unverified"]

    if failed or unverified:
        lines.append("Partly done." if (done or dupes or dry) else "Not done.")
    elif done or dupes:
        lines.append("Done.")

    for container, group in _group(done).items():
        lines.append(f"Added to {container}: {', '.join(describe(r) for r in group)}.")
    for container, group in _group(dry).items():
        names = ", ".join(describe(r) if r.kind == "event" else r.requested for r in group)
        lines.append(f"Would add to {container}: {names}.")
    for container, group in _group(dupes).items():
        lines.append(f"Already on {container}, skipped: {', '.join(describe(r) for r in group)}.")
    for r in failed:
        lines.append(f'Could not add "{r.requested}" to {r.container}: {r.error} (failed at: {r.failed_step}). Nothing was added.')
    for r in unverified:
        sent = f" as #{r.written_id}" if r.written_id is not None else ""
        lines.append(
            f'"{r.requested}" was sent to {r.container}{sent}, but I could not confirm it '
            f"({r.failed_step}: {r.error}). Please check Yuvomi."
        )

    text = "\n".join(lines)
    return DRY_RUN_PREFIX + text if dry_run else text
