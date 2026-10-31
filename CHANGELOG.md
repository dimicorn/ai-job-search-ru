# Changelog

## [0.1.6] - 2026-10-31

### 🚀 Features

- *(resume)* Raise resume in hh.ru search
- *(channels)* Find vacancies in Telegram channels
- Count discovered applies in the daily limit
- *(scan)* Mark known vacancies applied when the site shows a response
- Stop gracefully on Ctrl+C and SIGTERM

### 🐛 Bug Fixes

- *(recruiters)* Answer the question asked and wait for a slow bot
- FAQ answering the wrong question, captcha false positive and more
- Telegram wait hang, torn faq.yaml reads and other bugs
- False Codex errors, silent thread deaths and other bugs found in review
- *(applier)* Open relative Habr external-response links

### 📚 Documentation

- Rename repo to ai-job-search-ru and add star history chart

### 🧪 Testing

- Add apply-flow, autopilot and CLI smoke tests

## [0.1.5] - 2026-10-20

### 🚀 Features

- Add hh-agent doctor
- Import the resume from hh.ru and ask common facts in setup
- Add setup.sh for one-command install
- *(log)* Show parallel autopilot work in blocks: hh.ru, Habr, Telegram
- *(recruiters)* Log every bot check, with time and reason
- *(recruiters)* Ask for another vacancy when the bot dialog is idle
- *(stats)* Show tokens, calls and cost per model

### 🐛 Bug Fixes

- *(recruiters)* Lock the Telegram session against concurrent runs
- Exit with a clear message on Windows
- *(wizard)* Add a missing analyst key when model is the last line of the section
- *(recruiters)* Stop when the bot has no more vacancies to survey

### 💼 Other

- Take the version from git tags and add --version

### 📚 Documentation

- Add troubleshooting page and issue templates
- Warn about account risks, list platforms and LLM costs
- Split README into pages under docs/
- Add MIT license and CONTRIBUTING.md

### 🧪 Testing

- Check hh.ru and Habr parsing on saved page samples
- Add pytest suite and run it in CI

## [0.1.4] - 2026-10-20

### ⚙️ Miscellaneous Tasks

- Align changelog workflow with jet_garden_pre

## [0.1.3] - 2026-10-20

### ⚙️ Miscellaneous Tasks

- Attribute changelog commits to the github-actions bot

## [0.1.2] - 2026-10-20

### ⚙️ Miscellaneous Tasks

- Switch changelog to conventional commits

## [0.1.0] - 2026-10-20

