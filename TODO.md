# TODO

Что осталось, чтобы hh-agent могли ставить и использовать другие люди.

## Перед публикацией

- [ ] Убедиться, что CI зелёный (вкладка Actions: ruff и тесты на Ubuntu и macOS).
- [ ] Выпустить тег `v0.1.5`, чтобы у первых пользователей была версия с CHANGELOG.
- [ ] Открыть репозиторий: Settings → General → Danger Zone → Change visibility.
      Личных данных в файлах и в истории git нет (проверено).

## Проверить на «чистой» машине

- [ ] Пройти установку по README с нуля у человека без конфига, входа и Codex: `./setup.sh`,
      `hh-agent login`, первый `scan`, `review`, `auto --dry-run`. Записать, где застрял, и поправить.

## Документация и поддержка

- [ ] Страница «Если сломалось» (`docs/troubleshooting.md`): начать с `hh-agent doctor --online`; капча,
      слетел вход на hh/Хабр/Telegram, поменялась вёрстка (`tests/fixtures/refresh.py`), лимиты LLM,
      где смотреть `logs/events.jsonl`, `logs/autopilot.log` и `debug/`.
- [ ] Шаблоны issue (`.github/ISSUE_TEMPLATE/`): просить вывод `hh-agent doctor`, `hh-agent stats`
      и кусок лога.
- [ ] README на английском (низкий приоритет: аудитория hh.ru и Хабра русскоязычная).
