# Family Assistant

A small Dockerized service for Unraid. Two people message a private Telegram bot in plain English.
Claude decides which actions to take, the service runs them against the self-hosted
[Yuvomi](https://github.com/ulsklyc/yuvomi) family planner, and every confirmation is built from data
read back from Yuvomi, never from what the model says.

Status: **Phase 1** (Telegram + Claude + Yuvomi grocery list, to-do list, calendar).
Phase 2 (Gmail event proposals) and Phase 3 (HEB cart) are not built yet.

## How it works

```
Telegram (long polling) -> allowlist -> agent loop (Claude, tools) -> Yuvomi REST API
                                                          |
                         reply <- confirmation built from GET read-back
```

Each write tool runs four steps in code: duplicate check, write, separate GET read-back, field
comparison. The Telegram reply lists only records that came back from the read-back. If the write or
the read-back fails, the reply says which step failed and what was and was not done.

## Setup

### 1. Telegram bot
1. In Telegram, message **@BotFather**, send `/newbot`, follow the prompts. Copy the token into
   `TELEGRAM_BOT_TOKEN`.
2. Get each person's numeric user ID: message **@userinfobot** and it replies with your ID. Put both
   IDs in `ALLOWED_TELEGRAM_IDS`, comma-separated. Messages from anyone else are ignored silently.
3. Optional: set `TELEGRAM_USER_NAMES` (same order as the IDs) for the names Claude sees.

### 2. Yuvomi API token
1. In Yuvomi, open Settings, then API tokens, and create a token limited to these scopes:
   `shopping:write`, `tasks:write`, `calendar:write` (write includes read). Nothing else is needed.
2. Set `YUVOMI_URL` (LAN address, no `/api/v1`) and `YUVOMI_API_TOKEN`.
3. Make sure Yuvomi's household time zone is **America/Chicago**. Event times are sent as local wall-clock time.
4. Set `YUVOMI_GROCERY_LIST` to the exact name of the shopping list to use (default `Shopping`), then
   verify it:

```bash
docker compose run --rm family-assistant check
```

Without Docker (for example on your PC, after `pip install -e .` in a venv), the same check reads `.env`
from the current folder:

```bash
python -m family_assistant check
```

This is read-only. It lists your shopping lists and confirms the token can read tasks and the calendar.

### 3. Claude
Set `ANTHROPIC_API_KEY` and `CLAUDE_MODEL` (`claude-sonnet-5-5` by default; `claude-haiku-5-5` is the cheaper option).
`CLAUDE_EFFORT` (low, medium, high) trades thoroughness for cost. On non-Haiku models the service turns on
Anthropic's server-side refusal fallback (`CLAUDE_REFUSAL_FALLBACK=true`).

### 4. Run
```bash
cp .env.example .env    # then fill it in
docker compose up -d --build
docker compose logs -f
```

State (conversation history for follow-up questions) is kept in SQLite under `/config`.

## DRY_RUN

`DRY_RUN=true` is the default. In dry-run mode the bot still reads Yuvomi (so duplicate checks work)
but never writes. Every reply starts with `[DRY RUN]` and says "Would add to ...". Set `DRY_RUN=false`
when you are happy with how it interprets messages.

## What you can say

| Message | What happens |
|---|---|
| Add milk and eggs to the grocery list | Two separate `add_grocery_item` actions |
| Add call the plumber to my to do list | One `add_todo` action |
| Add batteries to my grocery list and my to do list | Two actions: grocery item and task |
| Add an event: dentist Friday at 3pm | Calendar event, read back and confirmed |
| Add an event: dentist | Bot asks one short question (day and time) first |
| What's on the grocery list? | Read-only answer |

Example reply: `Done. Added to Shopping: milk [#412], eggs [#413]. Added to To-do list: Call plumber [#88].`
The numbers in brackets are Yuvomi IDs from the read-back.

## Manual test checklist (Phase 1)

Run these with `DRY_RUN=true` first, then again with `DRY_RUN=false`, and check Yuvomi after each.

- [ ] A message from a Telegram account not in the allowlist gets no reply at all.
- [ ] `/start` shows the help text (with `[DRY RUN]` when dry-run is on).
- [ ] "Add milk to my grocery list": item appears on the configured list; reply shows its ID.
- [ ] Send the same message again: reply says it is already on the list and was skipped.
- [ ] "Add mow the lawn to my to do list": task appears in Yuvomi tasks.
- [ ] "Add batteries to my grocery list and my to do list": one item and one task.
- [ ] "Add an event dentist": bot asks for day and time; answer "Friday 3pm"; event is created at 3:00 PM local.
- [ ] "Add an event: school holiday next Monday, all day": all-day event.
- [ ] Stop Yuvomi (or use a wrong token) and send "add milk": reply says what failed and that nothing was added.
- [ ] Send 11 messages within a minute: the 11th gets the slow-down reply.

## Development

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"     # Linux/macOS: .venv/bin/pip
.venv/Scripts/python -m pytest
```

Tests use an in-memory fake of the Yuvomi API and a scripted fake Claude client. They cover the
agent loop, read-back confirmations, failed read-backs, duplicates, dry-run, the iteration and token
caps, and the Telegram allowlist.

## Troubleshooting

- **Bot does not answer:** check `ALLOWED_TELEGRAM_IDS` contains your numeric ID, and the logs for
  `Configuration error`.
- **"no shopping list named ..."**: run the `check` command and copy the exact list name into
  `YUVOMI_GROCERY_LIST`.
- **"YUVOMI_URL must start with http://"**: write the full address, e.g. `http://192.168.1.10:3000`.
- **`failed to connect to the docker API ... docker_engine`** (Windows): Docker Desktop is not running.
  Start it, or run the check with Python as shown above.
- **HTTP 401 / 403 from Yuvomi:** the token is wrong, revoked, or missing a scope.
- **Event times off by an hour or more:** Yuvomi's household time zone is not America/Chicago.
- **Claude returned an error (HTTP 4xx):** check `ANTHROPIC_API_KEY` and that `CLAUDE_MODEL` is a current model ID.

## Known limitations

- Yuvomi's API has no single-item GET for shopping items, so grocery read-back reads the whole list and
  finds the item by ID.
- Duplicate checks are exact matches after trimming and ignoring case ("milk" vs "whole milk" are different).
- "To-do list" means Yuvomi's Tasks module; tasks have no separate named lists.
- Conversation memory is the last few messages from the past 30 minutes, enough for follow-up answers.
- The model's free text (questions and answers) is not machine-verified. Only confirmations of writes
  are built from read-back data.
