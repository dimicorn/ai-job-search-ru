# Разработка hh-agent

## Окружение

```bash
./setup.sh          # или: uv sync && uv run playwright install chromium
```

`uv sync` ставит и dev-зависимости (ruff, pytest).

## Проверки

- **Линтер** — ruff, правила в `pyproject.toml`: `uv run ruff check .`, автоисправление — `--fix`.
- **Тесты** — pytest: `uv run pytest`. Сеть, браузер, LLM и Telegram в тестах подменены, конфиг
  и база — во временной папке (`tests/conftest.py`), поэтому ваши `config.yaml`, журнал и база
  не трогаются. Подставной бот для сценариев с AI-рекрутером — `FakeBot` там же.
- **CI** — на каждый push в `main` и pull request GitHub Actions (`.github/workflows/ci.yml`)
  запускает ruff и тесты на Ubuntu и macOS.

## Коммиты

Сообщения коммитов — по [Conventional Commits](https://www.conventionalcommits.org/ru/):
`feat: …`, `fix: …`, `docs: …`, `refactor: …`, `perf: …`, `test: …`, `ci: …`, `chore: …`,
при желании с областью: `feat(recruiters): …`. По префиксу коммит попадает в раздел CHANGELOG,
а коммиты без префикса в CHANGELOG не попадают.

## Релизы и CHANGELOG

Версию пакета задаёт git-тег (hatch-vcs): на теге `v0.2.0` это `0.2.0`, после него —
`0.2.1.devN+g<коммит>`; в `pyproject.toml` её править не нужно, текущая — `uv run hh-agent --version`.

CHANGELOG.md собирает git-cliff по `cliff.toml` из истории коммитов. При push тега вида `v0.2.0`
workflow `.github/workflows/changelog.yml` обновляет файл и сам коммитит его в `main`
(после этого сделайте `git pull`):

```bash
git tag -a v0.2.0 -m v0.2.0 && git push origin v0.2.0
```

Посмотреть заранее: `uvx git-cliff --offline --unreleased` (`--offline` — без запросов к GitHub API,
которому для приватного репозитория нужен токен).
