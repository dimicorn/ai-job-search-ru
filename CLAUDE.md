# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`hh-agent` applies to job vacancies on hh.ru and Хабр Карьера (career.habr.com) by driving a real Chromium through Playwright. hh.ru closed its job-seeker API on 15.12.2025, so there is no API client: everything is page scraping and form filling. It also talks to Telegram from the user's own account (Telethon) to answer AI-recruiter bots and read vacancy channels. LLMs score vacancies, write cover letters and answer employer questions.

All docstrings, comments, CLI output, docs and commit-facing text are in **Russian**. Keep new code consistent with that.

## Commands

```bash
uv sync && uv run playwright install chromium   # dev env (uv sync includes ruff, pytest)
uv run hh-agent doctor [--online]               # environment/config self-check
uv run python tests/fixtures/refresh.py         # re-capture live page fixtures (needs logged-in browser profile)
```

`login`, `review` and `run` refuse to start without a TTY. Don't try to run them from Claude Code (including `!`). Ask the user to run them in their own terminal.

## Pipeline and module roles

```
scout (search results + local stop-word filters) → analyst (LLM score 0–100 + letter) → review/auto → applier (fill form, submit)
```

- `scout.py`: builds search URLs from `Search.url` or `Search.params`, walks the result pages and stores cards. **Parsing is kept apart from the browser**: `parse_hh_*`/`parse_habr_*` take the page's SSR state dict (from `browser.page_state()`, which reads `template#HH-Lux-InitialState` on hh and `script[data-ssr-state]` on Habr) or the Habr API JSON. These functions are tested against `tests/fixtures/*.json`. When a site changes its markup, refresh the fixtures and fix the parsers.
- `llm.py`: one entry point, `ask_json(cfg, system, user, PydanticModel, purpose)`, which returns a validated pydantic object. It has five backends: `codex`, `claude_code` (`claude -p`), `api` (Anthropic SDK), `openai` (any compatible endpoint) and `command` (stdin→JSON stdout). When a backend raises `LimitReached`, its calls go to `fallback_backend` until the reset time parsed from the error text. That state lives in `.llm-limits.json` and in module globals `_limited`/`_FORMAT`, which `conftest.py` resets for each test.
- `applier.py`: picks the hh or Habr flow by URL (`browser.source_of`). It has three modes (`Mode`): `review` (the human confirms; unusual forms go back to the human with the letter in the clipboard), `auto` (random pauses, typing one character at a time, unusual forms → status `manual`) and `dry_run` (a screenshot instead of submitting). Unrecognised forms are saved as snapshots in `debug/`.
- `questions.py` + `faq.py`: employer questionnaires. `extract()` finds questions generically, without relying on hh `data-qa` attributes (they change), and tags elements with `data-hhagent-*`. `faq.yaml` answers common questions without an LLM, and the LLM's answers to questions that don't depend on the vacancy are written back to it with `learned: true` and a hash of `questions.facts`. Questions the agent can't answer from the facts are marked `needs_human`; it does not make them up.
- `recruiters.py` / `channels.py`: Telegram (Telethon, `.telegram.session`). Recruiters finds `t.me/<bot>?start=` links in hh vacancy chats, then answers bot surveys. Progress is tracked in the `tg_*` tables.
- `autopilot.py`: `auto`/`autopilot`. It runs threads (scan+analyze per site, one apply worker per site, Telegram, resume raise) inside **one browser process**. `open_browser(cdp_port=…)` opens a localhost CDP port, and each worker thread connects through `browser.attach(cdp_port)` with its own tab, because sync Playwright is tied to a thread. Daily and per-run limits are shared across sites. Each site has its own pauses and error counters.
- `stop.py`: the first Ctrl+C/SIGTERM sets a flag that workers check (`stop.requested()`, `stop.wait()`), and the vacancy being worked on stays in the queue. A second signal exits immediately. Long waits in workers should use `stop.wait` so that a stop request cuts them short.
- `learning.py` never changes limits or thresholds; only `config.yaml` sets those.

## State and invariants

- `db.py` is SQLite (WAL, one connection per thread). The `vacancies.status` lifecycle is `new → filtered | scored → skipped | applied | unconfirmed | manual | error`. There is no source column: hh and Habr vacancy IDs don't overlap (Habr IDs are ≥1e9), so the site is derived from the URL. Writes go through the `COLUMNS` allowlist: inserts drop unknown keys, and updates reject them.
- `browser.open_browser` takes an `fcntl` lock on `.hh-agent.lock`, because the Chromium profile (`.browser-profile/`) can only be open in one process. A second run waits up to 3 hours.
- `Config` (pydantic, `config.py`) resolves relative paths against the config file's directory through `cfg.path()`. `config.example.yaml` is the documented reference, and tests load it as their config, so add every new option there with a comment.
- These files are personal and gitignored, and must never be committed: `config.yaml`, `resume.md`, `faq.yaml`, `*.db`, `.browser-profile/`, `.telegram*`, `logs/`, `debug/`, `screenshots/`, `learned.json`, `.env`. Fixtures from `refresh.py` keep only public vacancy fields.

## Tests

`tests/conftest.py` copies `config.example.yaml` into `tmp_path` and creates a fresh DB, so tests never touch real user state. Network, browser, LLM and Telegram are replaced with fakes (`FakeBot` for recruiter dialogs).

The LLM in these tests is `analyst.backend: command` with a stub program. No test calls a real LLM.

## Commits and releases

- Use Conventional Commits (`feat(scope): …`, `fix: …`, `docs:`, `refactor:`, `perf:`, `test:`, `ci:`, `chore:`). git-cliff (`cliff.toml`) builds CHANGELOG.md from these prefixes, and commits without a prefix are left out.
- `main` is protected: changes land only through a PR with green CI, merged as a squash commit, so **the PR title must also be a Conventional Commit**.
- The version comes from the git tag through hatch-vcs. Don't edit the version in `pyproject.toml`. Release steps (release branch → `uvx git-cliff --tag vX.Y.Z -o CHANGELOG.md` → PR → tag after merge) are in CONTRIBUTING.md.

## User-facing docs

README.md and `docs/*.md` (llm, search, habr, auto, questions, telegram, troubleshooting) describe behaviour for end users. Update them when you change CLI flags, config options or behaviour.
