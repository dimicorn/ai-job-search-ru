"""Автоматические отклики без участия человека (hh-agent auto / autopilot)."""
from __future__ import annotations

import random
import time
from datetime import datetime, timedelta

from rich.console import Console

from . import analyst as analyst_mod
from .applier import Applier, Mode
from .browser import SITES, CaptchaError, SiteTabs, open_browser, source_of
from .config import Config
from .db import DB
from .events import log_event
from .learning import Store, learn

console = Console()


def _log(msg: str) -> None:
    console.print(f"[dim]{datetime.now():%H:%M:%S}[/] {msg}")


def _in_window(cfg: Config, now: datetime | None = None) -> bool:
    au = cfg.apply.auto
    now = now or datetime.now()
    start, end = au.active_hours
    if au.weekdays_only and now.weekday() >= 5:
        return False
    return start <= now.hour < end


def _scaled(rng: tuple[float, float], k: float) -> float:
    return random.uniform(rng[0] * k, rng[1] * k)


def _sleep(seconds: float, why: str, cfg: Config, dry_run: bool) -> bool:
    """Пауза; False — если окно рабочих часов закроется раньше, чем она кончится."""
    until = datetime.now() + timedelta(seconds=seconds)
    if not dry_run and not _in_window(cfg, until):
        _log(f"[yellow]Следующий отклик выпал бы за рабочие часы {cfg.apply.auto.active_hours} — заканчиваю.[/]")
        return False
    _log(f"{why}: {seconds / 60:.1f} мин (до {until:%H:%M:%S})")
    time.sleep(seconds)
    return True


def run_auto(cfg: Config, db: DB, dry_run: bool = False, limit: int | None = None,
             min_score: int | None = None) -> None:
    try:
        _run_auto(cfg, db, dry_run, limit, min_score)
    finally:
        learn(cfg, db)  # непрерывное обучение: разбор debug/ и логов этой сессии


def _run_auto(cfg: Config, db: DB, dry_run: bool, limit: int | None, min_score: int | None) -> None:
    au = cfg.apply.auto
    tuning = Store(cfg).tuning
    pace = tuning["pace_multiplier"]
    threshold = au.min_score if min_score is None else min_score
    max_n = limit or (3 if dry_run else au.max_per_run)
    stop_reason = "кандидаты закончились"

    if not dry_run and not _in_window(cfg):
        _log(f"Сейчас не рабочие часы ({au.active_hours[0]}:00–{au.active_hours[1]}:00"
             f"{', только будни' if au.weekdays_only else ''}). Ничего не делаю.")
        return
    left_today = cfg.apply.daily_limit - db.applied_today()
    if not dry_run and left_today <= 0:
        _log(f"Дневной лимит {cfg.apply.daily_limit} уже исчерпан.")
        return

    rows = db.by_status("scored", min_score=threshold)
    if not rows:
        _log(f"Нет оценённых вакансий с баллом ≥ {threshold}.")
        return
    _log(f"{'[bold]DRY-RUN[/] — ' if dry_run else ''}кандидатов: {len(rows)}, "
         f"за этот запуск максимум {max_n}, темп ×{pace}"
         + ("" if dry_run else f", сегодня осталось {left_today} из {cfg.apply.daily_limit}"))

    mode = Mode(interactive=False, dry_run=dry_run, human_like=True, shots_dir=cfg.path("screenshots"))
    analyst = None
    done = errors = 0

    with open_browser(cfg, headless=cfg.browser.headless_apply) as (ctx, page):
        tabs = SiteTabs(ctx, page)  # hh.ru и Хабр Карьера — в разных вкладках
        missing = tabs.missing_logins(r["url"] for r in rows)
        if missing:
            names = ", ".join(SITES[s] for s in missing)
            rows = [r for r in rows if source_of(r["url"]) not in missing]
            if not rows:
                raise SystemExit(f"Нет входа: {names}. Выполните в терминале: uv run hh-agent login")
            _log(f"[yellow]Нет входа: {names} — эти вакансии пропускаю.[/] Выполните: uv run hh-agent login")
        appliers: dict[str, Applier] = {}

        def applier_for(url: str) -> Applier:
            src = source_of(url)
            tab = tabs(src)
            if src not in appliers:
                appliers[src] = Applier(tab, cfg, mode)
            return appliers[src]

        for i, row in enumerate(rows):
            if done >= max_n:
                _log(f"Достигнут лимит запуска ({max_n}).")
                stop_reason = "лимит запуска"
                break
            if not dry_run and db.applied_today() >= cfg.apply.daily_limit:
                _log(f"Достигнут дневной лимит ({cfg.apply.daily_limit}).")
                stop_reason = "дневной лимит"
                break
            if not dry_run and not _in_window(cfg):
                _log("Рабочие часы закончились.")
                stop_reason = "рабочие часы"
                break

            title = f"{row['title']} — {row['company']} ({row['score']})"
            if au.skip_employer_tests and row["has_test"]:
                db.update(row["id"], status="manual", error="у работодателя тест")
                _log(f"[yellow]→ manual[/] {title}: тест работодателя")
                continue

            letter = row["cover_letter"]
            if not letter:
                analyst = analyst or analyst_mod.Analyst(cfg)
                try:
                    letter = analyst.letter(row)
                    db.update(row["id"], cover_letter=letter)
                except (analyst_mod.LimitReached, RuntimeError) as e:
                    _log(f"[red]Не удалось написать письмо[/] для {title}: {e}")
                    continue

            _log(f"Откликаюсь: {title}")
            try:
                status, note = applier_for(row["url"]).apply(row["id"], row["url"], letter,
                                                            analyst_mod._vacancy_prompt(row))
            except CaptchaError as e:
                log_event(cfg, "captcha", where="apply", vid=row["id"], done=done)
                _log(f"[red]{e} — останавливаюсь.[/] Вакансия останется в очереди.")
                stop_reason = "капча"
                break
            except Exception as e:  # вёрстка hh меняется — считаем ошибки подряд
                errors += 1
                db.update(row["id"], status="error", error=f"applier: {e}"[:500])
                _log(f"[red]Ошибка[/] ({errors}/{au.max_consecutive_errors}): {e}")
                if errors >= au.max_consecutive_errors:
                    _log("[red]Слишком много ошибок подряд — останавливаюсь. Проверьте вручную через review.[/]")
                    stop_reason = "ошибки подряд"
                    break
                continue

            now = datetime.now().isoformat(timespec="seconds")
            if status == "dry_run":
                _log(f"[cyan]dry-run[/] {note}")
            elif status == "already_applied":
                # Отклик был раньше: в базе — applied, но без applied_at, чтобы не тратить дневной лимит.
                # Это не новый отклик — не считаем в лимит запуска и не делаем паузу.
                db.update(row["id"], status="applied", applied_at=None, error=note)
                _log("[dim]отклик уже был — пропускаю[/]")
                continue
            elif status == "applied":
                db.update(row["id"], status="applied", applied_at=now, error=note)
                _log(f"[green]✓ отклик отправлен[/]{f' ({note})' if note else ''}")
            elif status == "unconfirmed":
                # Возможно, ушёл — считаем в дневной лимит и больше не трогаем.
                db.update(row["id"], status="unconfirmed", applied_at=now, error=note)
                errors += 1
                _log(f"[yellow]? {note}[/] — проверьте на {SITES[source_of(row['url'])]} вручную")
                if errors >= au.max_consecutive_errors:
                    _log("[red]Отправка не подтверждается несколько раз подряд — останавливаюсь.[/]")
                    stop_reason = "неподтверждённые отправки подряд"
                    break
                continue
            else:
                db.update(row["id"], status="manual", error=note)
                _log(f"[yellow]→ manual[/]: {note}")
                continue

            errors = 0
            done += 1
            if i == len(rows) - 1 or done >= max_n:
                continue
            if dry_run:
                time.sleep(random.uniform(*cfg.browser.delay_seconds))
            elif done % au.break_every == 0:
                if not _sleep(_scaled(au.break_seconds, pace), "Длинный перерыв", cfg, dry_run):
                    stop_reason = "рабочие часы"
                    break
            elif not _sleep(_scaled(au.delay_seconds, pace), "Пауза до следующего отклика", cfg, dry_run):
                stop_reason = "рабочие часы"
                break

    log_event(cfg, "auto_end", dry_run=dry_run, done=done, candidates=len(rows), stop_reason=stop_reason,
              applied_today=db.applied_today(), pace=pace)
    _log(f"Готово: {'заполнено форм' if dry_run else 'откликов'} — {done}. "
         f"Сегодня всего откликов: {db.applied_today()}/{cfg.apply.daily_limit}")
