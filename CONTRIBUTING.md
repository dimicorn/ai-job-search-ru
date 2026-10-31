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
- **Интеграционные и smoke-тесты** — `tests/test_apply_flow.py`: отклик целиком в настоящем Chromium
  на подставных страницах hh и Хабра (`page.route`, без сети); `tests/test_autopilot.py`: autopilot
  с настоящими потоками, базой и лимитами, но подставными браузером и формами; `tests/test_smoke.py`:
  команды `hh-agent` настоящим процессом на временном конфиге, включая Ctrl+C. LLM в них — подставная
  команда (`analyst.backend: command`), настоящие LLM тесты не вызывают.
- **Образцы страниц** — разбор выдачи и вакансий hh.ru и Хабра (`scout.parse_*`) проверяется на
  `tests/fixtures/*.json`, снятых с живых сайтов. Сайт поменял разметку — обновите образцы:
  `uv run python tests/fixtures/refresh.py` (нужен вход в профиле браузера агента; сохраняются только
  публичные поля вакансий), затем `uv run pytest` покажет, что сломалось. Форма отклика
  (`hh_response_form.html`) и выгрузка резюме (`hh_resume_export.html`) — синтетические, по разметке hh.
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

`main` защищён: изменения попадают в него только через PR, в котором прошли проверки CI
(Ruff и тесты на Ubuntu и macOS). PR сливается squash-коммитом, и его заголовок становится
сообщением коммита, поэтому заголовок PR тоже пишется по Conventional Commits.

CHANGELOG.md собирает git-cliff по `cliff.toml` из истории коммитов. Релиз — тоже через PR:

```bash
git switch -c release/v0.2.0
uvx git-cliff --tag v0.2.0 -o CHANGELOG.md
git commit -am "chore(release): v0.2.0" && git push -u origin release/v0.2.0
gh pr create --fill
# после слияния PR:
git switch main && git pull
git tag -a v0.2.0 -m v0.2.0 && git push origin v0.2.0
```

На push тега workflow `.github/workflows/release.yml` публикует GitHub Release с разделом
этой версии из CHANGELOG. Посмотреть заранее, что попадёт в релиз: `uvx git-cliff --unreleased -o -` (без `-o -` git-cliff перезапишет CHANGELOG.md).
