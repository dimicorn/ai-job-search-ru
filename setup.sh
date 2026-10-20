#!/usr/bin/env bash
# Установка hh-agent одной командой: ./setup.sh
# Ставит зависимости и Chromium, запускает мастер настройки (конфиг и LLM), предлагает войти
# на сайты и в Telegram и включить ежедневный autopilot (macOS, launchd). Повторный запуск безопасен:
# готовые шаги он пропускает или спрашивает, переделать ли.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
LABEL="ru.hh-agent.autopilot"
AGENT="$HOME/Library/LaunchAgents/$LABEL.plist"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
# ask "вопрос" y|n — да/нет с ответом по умолчанию; без терминала — ответ по умолчанию.
ask() {
    local hint="[y/N]"; [[ $2 == y ]] && hint="[Y/n]"
    [[ -t 0 ]] || { [[ $2 == y ]]; return; }
    read -r -p "$1 $hint " reply
    reply="${reply:-$2}"
    [[ $reply =~ ^[YyДд] ]]
}

say "1/6 uv — менеджер Python-пакетов"
if ! command -v uv >/dev/null; then
    export PATH="$HOME/.local/bin:$PATH"  # uv мог установиться раньше, но не попасть в PATH этой оболочки
fi
if ! command -v uv >/dev/null; then
    if ask "uv не найден. Установить с astral.sh (официальный установщик)?" y; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.local/bin:$PATH"
    else
        warn "Без uv дальше нельзя: https://docs.astral.sh/uv/getting-started/installation/"; exit 1
    fi
fi
echo "uv: $(uv --version)"

say "2/6 Зависимости и браузер Chromium для Playwright"
uv sync
uv run playwright install chromium

say "3/6 Настройка: config.yaml и LLM"
if [[ ! -f config.yaml ]]; then
    uv run hh-agent setup
elif ask "config.yaml уже есть. Заново выбрать LLM (основную и запасную)?" n; then
    uv run hh-agent setup
else
    echo "Оставляю config.yaml как есть."
fi
echo "Проверьте в config.yaml searches (фильтры поиска) и analyst.preferences (что ищете)."

say "4/6 Вход на hh.ru, Хабр Карьеру и в Telegram"
if [[ -t 0 ]] && ask "Войти сейчас? Откроется браузер; Telegram спросит номер и код" y; then
    uv run hh-agent login
else
    echo "Позже: uv run hh-agent login"
fi

say "5/6 Резюме"
if [[ ! -f resume.md ]] || cmp -s resume.md resume.example.md; then
    if ask "Скачать резюме с hh.ru в resume.md (без ФИО и контактов)?" y; then
        uv run hh-agent resume || warn "Не вышло — вставьте текст резюме в resume.md сами или: uv run hh-agent resume"
    else
        echo "Позже: uv run hh-agent resume — или вставьте текст резюме в resume.md сами."
    fi
else
    echo "resume.md уже заполнен. Обновить с hh.ru: uv run hh-agent resume --force"
fi

say "6/6 Ежедневный autopilot (поиск → оценка → отклики → опросы ботов)"
if [[ $(uname) != Darwin ]]; then
    echo "Расписание через launchd — только на macOS; на Linux добавьте в cron: cd $DIR && $(command -v uv) run hh-agent autopilot"
elif ask "Запускать autopilot каждый день сам?" n; then
    hour=""
    until [[ $hour =~ ^([01]?[0-9]|2[0-3])$ ]]; do
        read -r -p "Во сколько (час, 0–23) [10]: " hour
        hour="${hour:-10}"
    done
    mkdir -p "$(dirname "$AGENT")" logs
    sed -e "s|__UV__|$(command -v uv)|g" -e "s|__DIR__|$DIR|g" -e "s|__HOUR__|$((10#$hour))|g" -e "s|__HOME__|$HOME|g" \
        "launchd/$LABEL.plist" > "$AGENT"
    launchctl unload "$AGENT" 2>/dev/null || true
    launchctl load "$AGENT"
    echo "autopilot будет запускаться каждый день в $((10#$hour)):00 (лог — logs/autopilot.log)."
    echo "Выключить: launchctl unload $AGENT"
else
    echo "Не включаю. Передумаете — запустите ./setup.sh ещё раз: готовые шаги он пропустит."
fi

say "Готово"
echo "Команды: uv run hh-agent help · прогон вручную: uv run hh-agent autopilot · статистика: uv run hh-agent stats"
