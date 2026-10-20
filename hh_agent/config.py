from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class Search(BaseModel):
    name: str
    # Сайт: hh — hh.ru, habr — career.habr.com. Если не указан — по url (иначе hh).
    source: Literal["hh", "habr"] | None = None
    # Либо готовый URL поиска с сайта, либо параметры — они превращаются в такой же URL.
    url: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    max_pages: int = 3

    @model_validator(mode="after")
    def _url_or_params(self) -> "Search":
        if not self.url and not self.params:
            raise ValueError(f"search '{self.name}': нужен url или params")
        if self.source is None:
            self.source = "habr" if self.url and "career.habr.com" in self.url else "hh"
        return self


class Filters(BaseModel):
    title_include_any: list[str] = Field(default_factory=list)
    title_exclude: list[str] = Field(default_factory=list)
    description_exclude: list[str] = Field(default_factory=list)
    companies_exclude: list[str] = Field(default_factory=list)
    min_salary: int | None = None
    skip_without_salary: bool = False
    skip_with_employer_test: bool = False


class Analyst(BaseModel):
    backend: str = "codex"  # codex | claude_code (подписка через `claude -p`) | api (ключ)
    model: str = "claude-opus-5-5"  # модель для claude_code / api / openai
    effort: str = "medium"
    base_url: str | None = None   # openai: адрес API, например https://api.openai.com/v1 или http://localhost:11434/v1
    api_key_env: str | None = "OPENAI_API_KEY"  # openai: переменная (или строка в .env) с ключом; null — без ключа
    openai_extra: dict[str, Any] = Field(default_factory=dict)  # openai: доп. поля запроса (temperature, reasoning_effort…)
    command: str | None = None    # command: программа, которая читает запрос из stdin и печатает JSON
    letter_backend: str = "codex"  # same — письмо в запросе оценки; иначе бэкенд для отдельного запроса письма
    codex_model: str | None = None  # None — модель Codex по умолчанию (~/.codex/config.toml не читается)
    codex_effort: str | None = None  # глубина рассуждения Codex: low | medium | high | xhigh | max; None — по умолчанию модели
    batch_size: int = 8          # вакансий на один вызов оценки
    parallel: int = 3            # одновременных запросов к LLM при оценке и письмах
    letter_batch_size: int = 4   # писем на один вызов
    min_score: int = 70
    resume_file: str = "resume.md"
    preferences: str = ""
    letter_style: str = ""


class AutoApply(BaseModel):
    """Полностью автоматические отклики (hh-agent auto). Все паузы случайные в указанных границах."""
    min_score: int = 75                       # откликаться только на вакансии с баллом не ниже
    max_per_run: int = 10                     # максимум откликов за один запуск
    daily_target: int = 0                     # норма в сутки; не хватает вакансий с min_score — добираем ниже; 0 — выкл.
    target_min_score: int = 55                # до какого балла можно опускаться ради нормы
    skip_employer_tests: bool = False         # True — вакансии с тестом сразу в manual, без попыток
    active_hours: tuple[int, int] = (8, 23)   # работать только в эти часы (локальное время)
    weekdays_only: bool = False
    read_seconds: tuple[float, float] = (8, 20)       # «читаем» вакансию перед откликом
    action_seconds: tuple[float, float] = (0.8, 2.5)  # пауза между кликами внутри формы
    typing_delay_ms: tuple[int, int] = (0, 0)         # (0, 0) — письмо вставляется целиком; >0 — печать посимвольно
    delay_seconds: tuple[float, float] = (90, 240)    # пауза между откликами
    break_every: int = 5                              # после стольких откликов — длинный перерыв
    break_seconds: tuple[float, float] = (900, 1800)
    max_consecutive_errors: int = 3                   # столько ошибок подряд — остановка (вёрстка поменялась?)
    captcha_wait_minutes: int = 15                    # капча: уведомить и ждать, пока пройдёте; 0 — сразу стоп


class Apply(BaseModel):
    daily_limit: int = 20
    # auto — после вашего [y] скрипт сам нажимает «Отправить»;
    # manual — скрипт заполняет форму, а «Отправить» в браузере жмёте вы.
    submit_mode: str = "auto"
    # Название резюме, если их несколько (иначе hh подставит последнее использованное).
    resume_title: str | None = None
    delay_seconds: tuple[float, float] = (4, 9)  # пауза после отклика в review
    auto: AutoApply = Field(default_factory=AutoApply)


class Questions(BaseModel):
    """Ответы на вопросы работодателя в форме отклика."""
    enabled: bool = True
    max_fields: int = 30        # больше полей — что-то нестандартное, отдаём человеку
    confirm_in_review: bool = True
    facts: str = ""             # факты о вас, которых нет в резюме (зарплата, срок выхода и т.п.)
    faq: bool = True            # частые вопросы — ответ из faq_file без вызова LLM (и пополнять его)
    faq_file: str = "faq.yaml"


class Telegram(BaseModel):
    """AI-рекрутеры в Telegram (например, у Сбера): после откликов агент отвечает на их вопросы."""
    enabled: bool = False
    api_id: int | None = None                 # с my.telegram.org → API development tools
    api_hash: str | None = None
    session: str = ".telegram"                # файл сессии Telethon (.session), как профиль браузера
    companies: list[str] = Field(default_factory=lambda: ["Сбер", "Sber"])  # компании ботов: новый отклик — повод снова зайти к боту
    bots: list[str] = Field(default_factory=list)  # боты, которые пишут сами (username без @)
    hh_links: bool = False                    # искать ссылку на бота в чатах hh (открывает hh.ru); бот из bots — не нужно
    check_days: int = 7                       # искать ссылку в чатах hh для откликов за столько дней
    max_turns: int = 40                       # ходов в одном диалоге за запуск
    reply_seconds: tuple[float, float] = (6, 20)  # «думаем и печатаем» перед ответом
    wait_reply_seconds: int = 120             # сколько ждать следующего сообщения бота
    share_contact: bool = False               # true — на просьбу «поделиться контактом» отдавать свой номер
    accept_consent: bool = True               # принимать согласие на обработку персональных данных для подбора


class Browser(BaseModel):
    profile_dir: str = ".browser-profile"
    headless_scan: bool = False
    headless_apply: bool = False  # для auto: окно видно — проще следить и меньше похоже на бота
    delay_seconds: tuple[float, float] = (2, 5)


class Config(BaseModel):
    searches: list[Search]
    filters: Filters = Field(default_factory=Filters)
    analyst: Analyst = Field(default_factory=Analyst)
    apply: Apply = Field(default_factory=Apply)
    questions: Questions = Field(default_factory=Questions)
    browser: Browser = Field(default_factory=Browser)
    telegram: Telegram = Field(default_factory=Telegram)
    db_path: str = "hh_agent.db"

    base_dir: Path = Path(".")

    def path(self, p: str) -> Path:
        q = Path(p).expanduser()
        return q if q.is_absolute() else self.base_dir / q

    def sources(self) -> list[str]:
        """Сайты, на которых ищем: hh, habr — в порядке появления в searches."""
        return list(dict.fromkeys(s.source for s in self.searches))

    def resume_text(self) -> str:
        f = self.path(self.analyst.resume_file)
        if not f.exists():
            raise SystemExit(f"Не найдено резюме: {f}. Скопируйте resume.example.md в {f.name} и заполните.")
        return f.read_text(encoding="utf-8")


def load_config(path: str | Path) -> Config:
    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise SystemExit(f"Не найден конфиг: {path}. Скопируйте config.example.yaml в config.yaml.")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = Config.model_validate(data)
    cfg.base_dir = path.parent
    return cfg
