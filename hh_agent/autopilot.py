"""Автоматические отклики без участия человека (hh-agent auto / autopilot)."""
from __future__ import annotations

import random
import time
from datetime import datetime, timedelta

from rich.console import Console

from . import analyst as analyst_mod
from .applier import Applier, Mode
from . import browser
from .browser import SITES, CaptchaError, CaptchaPending, SiteTabs, captcha_visible, open_browser, source_of
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


class Site:
    """Очередь, пауза и счётчики одного сайта: сайты чередуются, пауза у каждого своя."""

    def __init__(self, src: str, queue: list, applier: Applier):
        self.src, self.queue, self.applier = src, queue, applier
        self.next_ready = 0.0          # time.monotonic(), раньше которого на сайт не идём
        self.on_break = False
        self.done = self.errors = 0
        self.blocked: str | None = None
        self.pending = None            # вакансия, на которой капча ждёт человека
        self.pending_since = 0.0

    def schedule_pause(self, cfg: Config, pace: float, dry_run: bool) -> None:
        au = cfg.apply.auto
        self.errors = 0
        if dry_run:
            secs, self.on_break = random.uniform(*cfg.browser.delay_seconds), False
        elif self.done % au.break_every == 0:
            secs, self.on_break = _scaled(au.break_seconds, pace), True
        else:
            secs, self.on_break = _scaled(au.delay_seconds, pace), False
        self.next_ready = time.monotonic() + secs
        _log(f"[dim]{SITES[self.src]}: следующий отклик не раньше "
             f"{(datetime.now() + timedelta(seconds=secs)):%H:%M:%S}{' (перерыв)' if self.on_break else ''}[/]")


def _record(db: DB, st: Site, row, status: str, note: str | None) -> bool:
    """Записать итог отклика. True — новый отклик (считается в лимиты, дальше пауза)."""
    now = datetime.now().isoformat(timespec="seconds")
    if status == "dry_run":
        _log(f"[cyan]dry-run[/] {note}")
        return True
    if status == "already_applied":
        # Отклик был раньше: applied без applied_at — не тратит дневной лимит, паузы не нужно.
        db.update(row["id"], status="applied", applied_at=None, error=note)
        _log("[dim]отклик уже был — пропускаю[/]")
        return False
    if status == "applied":
        db.update(row["id"], status="applied", applied_at=now, error=note)
        _log(f"[green]✓ отклик отправлен[/]{f' ({note})' if note else ''}")
        return True
    if status == "unconfirmed":
        # Возможно, ушёл — считаем в дневной лимит и больше не трогаем.
        db.update(row["id"], status="unconfirmed", applied_at=now, error=note)
        st.errors += 1
        _log(f"[yellow]? {note}[/] — проверьте на {SITES[st.src]} вручную")
        return False
    db.update(row["id"], status="manual", error=note)
    _log(f"[yellow]→ manual[/]: {note}")
    return False


def _check_pending(cfg: Config, db: DB, st: Site, tabs: SiteTabs) -> str | None:
    """Капча, оставленная человеку: пройдена — проверить отклик; истекло время — выключить сайт."""
    page = tabs.pages[st.src]
    row = st.pending
    if captcha_visible(page):
        if time.monotonic() - st.pending_since > cfg.apply.auto.captcha_wait_minutes * 60:
            st.pending, st.blocked = None, "капчу не прошли вовремя"
            st.queue.insert(0, row)
            log_event(cfg, "captcha", where="apply", vid=row["id"], solved=False)
            _log(f"[red]{SITES[st.src]}: капчу не прошли вовремя — на этом сайте больше не откликаюсь.[/]")
        return None
    st.pending = None
    log_event(cfg, "captcha", where="apply", vid=row["id"], solved=True,
              waited=round(time.monotonic() - st.pending_since))
    _log(f"{SITES[st.src]}: капча пройдена — проверяю отклик.")
    try:
        ok = st.applier._wait_confirmed(10_000)
    except CaptchaError:  # капча появилась снова
        st.pending, st.pending_since = row, time.monotonic()
        return None
    status = "applied" if ok else "unconfirmed"
    note = None if ok else "отправка после капчи не подтвердилась"
    log_event(cfg, "apply", vid=row["id"], mode="auto", status=status, note=note)
    return "applied" if _record(db, st, row, status, note) else None


def run_auto(cfg: Config, db: DB, dry_run: bool = False, limit: int | None = None,
             min_score: int | None = None) -> None:
    try:
        _run_auto(cfg, db, dry_run, limit, min_score)
    finally:
        learn(cfg, db)  # подстройка пауз и таймаутов по итогам сессии (правила, без LLM)


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
    done = 0

    with open_browser(cfg, headless=cfg.browser.headless_apply) as (ctx, page):
        tabs = SiteTabs(ctx, page)  # hh.ru и Хабр Карьера — в разных вкладках
        missing = tabs.missing_logins(r["url"] for r in rows)
        if missing:
            names = ", ".join(SITES[s] for s in missing)
            rows = [r for r in rows if source_of(r["url"]) not in missing]
            if not rows:
                raise SystemExit(f"Нет входа: {names}. Выполните в терминале: uv run hh-agent login")
            _log(f"[yellow]Нет входа: {names} — эти вакансии пропускаю.[/] Выполните: uv run hh-agent login")
        by_site: dict[str, list] = {}
        for r in rows:  # внутри сайта — по убыванию балла (как отдала база)
            by_site.setdefault(source_of(r["url"]), []).append(r)
        sites = {src: Site(src, q, Applier(tabs(src), cfg, mode)) for src, q in by_site.items()}
        browser.DEFER_CAPTCHA = len(sites) > 1 and not dry_run
        if len(sites) > 1:
            _log("Чередую сайты: " + ", ".join(f"{SITES[s.src]} — {len(s.queue)}" for s in sites.values())
                 + ". Пауза идёт у каждого сайта своя.")
        last_src = None

        while True:
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

            # Капчи, оставленные человеку: пройдена — проверяем отклик; не дождались — сайт выключаем.
            for st in sites.values():
                if st.pending is not None:
                    res = _check_pending(cfg, db, st, tabs)
                    if res == "applied":
                        done += 1
                        st.done += 1
                        st.schedule_pause(cfg, pace, dry_run)

            ready = [st for st in sites.values() if st.queue and st.blocked is None and st.pending is None]
            if not ready:
                if any(st.pending is not None for st in sites.values()):
                    time.sleep(5)  # ждём человека на капче, других вакансий нет
                    continue
                blocked = [f"{SITES[st.src]}: {st.blocked}" for st in sites.values() if st.blocked]
                if blocked:
                    stop_reason = "; ".join(blocked)
                break
            # Сайт, у которого пауза кончается раньше; при равенстве — не тот, что был только что.
            st = min(ready, key=lambda x: (x.next_ready, x.src == last_src))
            wait = st.next_ready - time.monotonic()
            if wait > 0:
                if any(x.pending is not None for x in sites.values()):
                    time.sleep(min(wait, 5))  # короткими шагами — чтобы заметить пройденную капчу
                    continue
                what = "Длинный перерыв" if st.on_break else "Пауза до следующего отклика"
                if not _sleep(wait, f"{what} ({SITES[st.src]})", cfg, dry_run):
                    stop_reason = "рабочие часы"
                    break
            row = st.queue.pop(0)
            last_src = st.src

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

            prefix = f"[{SITES[st.src]}] " if len(sites) > 1 else ""
            _log(f"{prefix}Откликаюсь: {title}")
            tabs(st.src)
            try:
                status, note = st.applier.apply(row["id"], row["url"], letter, analyst_mod._vacancy_prompt(row))
            except CaptchaPending:
                st.pending, st.pending_since = row, time.monotonic()
                log_event(cfg, "captcha", where="apply", vid=row["id"], done=done, deferred=True)
                others = [SITES[x.src] for x in sites.values() if x is not st and x.queue and x.blocked is None]
                _log(f"[yellow]{SITES[st.src]} показал капчу[/] — пройдите её во вкладке; "
                     + (f"пока работаю на: {', '.join(others)}" if others else "жду"))
                continue
            except CaptchaError as e:
                log_event(cfg, "captcha", where="apply", vid=row["id"], done=done)
                st.queue.insert(0, row)
                st.blocked = "капча"
                _log(f"[red]{SITES[st.src]}: {e}[/] — на этом сайте больше не откликаюсь, вакансия осталась в очереди.")
                continue
            except Exception as e:  # вёрстка меняется — считаем ошибки подряд (на каждом сайте свои)
                st.errors += 1
                db.update(row["id"], status="error", error=f"applier: {e}"[:500])
                _log(f"[red]Ошибка[/] ({st.errors}/{au.max_consecutive_errors}): {e}")
                if st.errors >= au.max_consecutive_errors:
                    st.blocked = "ошибки подряд"
                    _log(f"[red]{SITES[st.src]}: слишком много ошибок подряд — на этом сайте останавливаюсь.[/]")
                continue

            if _record(db, st, row, status, note):
                done += 1
                st.done += 1
                st.schedule_pause(cfg, pace, dry_run)
            elif status == "unconfirmed" and st.errors >= au.max_consecutive_errors:
                st.blocked = "неподтверждённые отправки подряд"
                _log(f"[red]{SITES[st.src]}: отправка не подтверждается несколько раз подряд — останавливаюсь.[/]")

        browser.DEFER_CAPTCHA = False
        for st in sites.values():  # капчу так и не прошли до конца запуска
            if st.pending is not None:
                _log(f"[yellow]{SITES[st.src]}: капча не пройдена — вакансия осталась в очереди.[/]")

    log_event(cfg, "auto_end", dry_run=dry_run, done=done, candidates=len(rows), stop_reason=stop_reason,
              applied_today=db.applied_today(), pace=pace)
    _log(f"Готово: {'заполнено форм' if dry_run else 'откликов'} — {done}. "
         f"Сегодня всего откликов: {db.applied_today()}/{cfg.apply.daily_limit}")
