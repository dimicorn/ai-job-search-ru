# Changelog

## [0.1.1] — 2026-10-20

### Changed

- Let the changelog workflow read pull requests (a7a9c3d)

## [0.1.0] — 2026-10-20

### Added

- Add ruff lint config, lint and changelog workflows (5e61b5c)
- Support any LLM and add a first-run setup wizard (4d77037)
- Add a help command listing all commands (8a7b1c0)
- Add a daily apply target that tops up with lower-scored vacancies (08e63c9)
- Add period stats; widen apply hours to 8:00–23:00 (9b034a6)
- Initial commit: hh.ru job search agent (549ae57)

### Changed

- Keep the Telegram connection alive during reply pauses (050f557)
- Move to the next vacancy right after rating the recruiter bot (95388d0)
- Reply to recruiter bots at a human pace (0f2583e)
- Answer experience questions about the asked technology (912fbd3)
- Answer frequent questions from faq.yaml without an LLM call (7a1b37b)
- Trim idle time in the autopilot Telegram thread (f2751ea)
- Run the autopilot pipeline in parallel threads (f060267)
- Answer recruiter bot ratings with a button (fbe549b)
- Answer recruiter bot questions left hanging (2522809)
- Let Codex model reasoning effort be configured (c656746)
- Answer confidently and tell experience from knowledge questions (55df982)
- Handle recruiter bots that keep one chat for all vacancies (edab895)
- Answer AI recruiter bots in Telegram after applying (d585eac)
- Always start cover letters with a greeting (586ebee)
- Cut LLM token use; track it in stats; stop scans at known pages (35925b5)
- Interleave hh.ru and Habr applies; simplify learning to rules (5407dc4)
- Keep the Mac awake while the agent runs (d30d730)
- Hand captchas to the human; restore browser profile lock (526b9b5)
- Document Habr Career support; loosen Habr letter selector (1596089)
- Open each job site in its own tab; don't count repeat applies (e2a099d)
- Paste cover letters instead of typing them (33b35cb)

### Fixed

- Fix letter typing collapsing the hh.ru response modal (b00a388)

### Removed

- Remove employer chat replies, clean up hh.ru agent (495b43a)


