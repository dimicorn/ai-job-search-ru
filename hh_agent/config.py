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
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    letter_backend: str = "codex"  # codex — письма пишет `codex exec`; same — та же модель Claude
    codex_model: str | None = None  # None — модель по умолчанию из ~/.codex/config.toml
    min_score: int = 70
    resume_file: str = "resume.md"
    preferences: str = ""
    letter_style: str = ""


class AutoApply(BaseModel):
    """Полностью автоматические отклики (hh-agent auto). Все паузы случайные в указанных границах."""
    min_score: int = 75                       # откликаться только на вакансии с баллом не ниже
    max_per_run: int = 10                     # максимум откликов за один запуск
    skip_employer_tests: bool = False         # True — вакансии с тестом сразу в manual, без попыток
    active_hours: tuple[int, int] = (9, 21)   # работать только в эти часы (локальное время)
    weekdays_only: bool = False
    read_seconds: tuple[float, float] = (8, 20)       # «читаем» вакансию перед откликом
    action_seconds: tuple[float, float] = (0.8, 2.5)  # пауза между кликами внутри формы
    typing_delay_ms: tuple[int, int] = (30, 90)       # печать письма посимвольно; [0, 0] — вставка целиком
    delay_seconds: tuple[float, float] = (90, 240)    # пауза между откликами
    break_every: int = 5                              # после стольких откликов — длинный перерыв
    break_seconds: tuple[float, float] = (900, 1800)
    max_consecutive_errors: int = 3                   # столько ошибок подряд — остановка (вёрстка поменялась?)


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


class Chats(BaseModel):
    """Ответы работодателям в чатах hh.ru."""
    enabled: bool = True
    auto_reply: bool = True        # False — агент только готовит черновики, отправляете вы
    max_replies_per_run: int = 10
    max_chats_scan: int = 40       # сколько верхних чатов списка просматривать
    history_messages: int = 20     # сколько последних сообщений чата давать модели
    delay_seconds: tuple[float, float] = (20, 60)  # пауза после отправленного ответа
    style: str = "вежливо, коротко, по делу, на «вы»"


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
    chats: Chats = Field(default_factory=Chats)
    browser: Browser = Field(default_factory=Browser)
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
