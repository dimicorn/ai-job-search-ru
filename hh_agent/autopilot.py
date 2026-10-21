"""Автоматические отклики без участия человека (hh-agent auto / autopilot).

Всё идёт параллельно, в одном браузере с общим профилем, у каждого потока своя вкладка:
  поиск-оценка — поиск на hh и Хабре (каждый сайт в своём потоке), затем оценка и письма
                 (только autopilot): новые вакансии сразу попадают в очередь откликов;
  hh.ru, Хабр  — отклики, у каждого сайта свои паузы и счётчик ошибок; вакансии берутся из базы
                 по одной, пока поиск-оценка ещё идёт — ждут новых;
  Telegram     — опросы AI-рекрутеров; повторяется, когда появляются новые отклики в их компании.
Дневной лимит и лимит запуска общие для сайтов. Ctrl+C или SIGTERM — остановка с итогом (stop.py).
"""
from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timedelta

from . import analyst as analyst_mod
from . import recruiters, stop
from .applier import Applier, Mode
from .browser import SITES, CaptchaError, SiteTabs, attach, free_port, is_logged_in, open_browser, source_of
from .config import Config
from .db import DB
from .events import log_event
from .learning import Store, learn
from .log import console
from .scout import scan

POLL_SECONDS = 5  # как часто поток откликов смотрит в базу, пока поиск и оценка ещё идут


def _log(msg: str) -> None:
    """Строка со временем: в рабочих потоках время и имя потока добавляет сама консоль (log.py)."""
    main = threading.current_thread() is threading.main_thread()
    console.print(f"[dim]{datetime.now():%H:%M:%S}[/] {msg}" if main else msg)


def _thread(target, *args, name: str) -> threading.Thread:
    """Поток, ошибка которого попадает в журнал, а не обрывает запуск молча. SystemExit (нет входа в Codex,
    неверный ключ API) — тоже: в потоке Python гасит его без следа. После остановки ошибки не сбой
    (браузер закрыт по Ctrl+C); второй Ctrl+C завершает процесс, не дожидаясь потока (daemon)."""
    def body():
        try:
            target(*args)
        except (Exception, SystemExit) as e:
            if stop.requested():
                return
            msg = str(e) if isinstance(e, SystemExit) else repr(e)
            _log(f"[red]Сбой потока: {msg}[/]")
            log_event(args[0], "error", where=f"thread {name}", message=msg[:500])
    return threading.Thread(target=body, name=name, daemon=True)


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
    """Пауза (её обрывает остановка); False — если окно рабочих часов закроется раньше, чем она кончится."""
    until = datetime.now() + timedelta(seconds=seconds)
    if not dry_run and not _in_window(cfg, until):
        _log(f"[yellow]Следующий отклик выпал бы за рабочие часы {cfg.apply.auto.active_hours} — заканчиваю.[/]")
        return False
    _log(f"{why}: {seconds / 60:.1f} мин (до {until:%H:%M:%S})")
    stop.wait(seconds)
    return True


class Site:
    """Пауза и счётчики одного сайта."""

    def __init__(self, src: str, applier: Applier):
        self.src, self.applier = src, applier
        self.next_ready = 0.0          # time.monotonic(), раньше которого не откликаемся
        self.on_break = False
        self.done = self.errors = 0
        self.blocked: str | None = None

    def schedule_pause(self, cfg: Config, pace: float, dry_run: bool) -> None:
        au = cfg.apply.auto
        self.errors = 0
        if dry_run:
            secs, self.on_break = random.uniform(*cfg.browser.delay_seconds), False
        elif au.break_every and self.done % au.break_every == 0:  # 0 — без длинных перерывов
            secs, self.on_break = _scaled(au.break_seconds, pace), True
        else:
            secs, self.on_break = _scaled(au.delay_seconds, pace), False
        self.next_ready = time.monotonic() + secs
        _log(f"[dim]следующий отклик не раньше "
             f"{(datetime.now() + timedelta(seconds=secs)):%H:%M:%S}{' (перерыв)' if self.on_break else ''}[/]")


class Run:
    """Общее для потоков: какие вакансии уже взяты, счётчики, лимиты и причина остановки."""

    def __init__(self, cfg: Config, dry_run: bool, max_n: int, threshold: int, fill_to_target: bool):
        self.cfg, self.dry_run, self.max_n = cfg, dry_run, max_n
        self.threshold, self.fill_to_target = threshold, fill_to_target
        self.lock = threading.Lock()
        self.taken: set[int] = set()
        self.done = self.inflight = 0
        self.stop_reason: str | None = None
        self.producing = threading.Event()  # идёт поиск и оценка — новые вакансии ещё появятся
        self.filling_logged = False

    def stop(self, reason: str) -> None:
        with self.lock:
            if self.stop_reason is None:
                self.stop_reason = reason
                _log(f"Останавливаю отклики: {reason}.")

    def next_row(self, db: DB, src: str):
        """Следующая вакансия сайта: сначала с баллом ≥ порога; добор до нормы (daily_target) —
        с баллом от target_min_score, когда поиск закончен и сильных вакансий на сайте не осталось."""
        au = self.cfg.apply.auto
        with self.lock:
            mine = lambda rows: [r for r in rows if source_of(r["url"]) == src and r["id"] not in self.taken]
            rows = mine(db.by_status("scored", min_score=self.threshold))
            if not rows and self.fill_to_target and not self.producing.is_set():
                need = au.daily_target - db.applied_today() - self.inflight
                if need > 0:
                    rows = [r for r in mine(db.by_status("scored", min_score=au.target_min_score))
                            if r["score"] < self.threshold]
                    if rows and not self.filling_logged:
                        self.filling_logged = True
                        _log(f"До нормы {au.daily_target} не хватает {need} — добираю вакансиями "
                             f"с баллом от {au.target_min_score}.")
            if not rows:
                return None
            self.taken.add(rows[0]["id"])
            return rows[0]

    def reserve(self, db: DB) -> bool:
        """Занять место под отклик в общих лимитах (запуска и дневном); False — лимит исчерпан."""
        with self.lock:
            if self.stop_reason or stop.requested():
                return False
            if self.done + self.inflight >= self.max_n:
                reason = "лимит запуска"
            elif not self.dry_run and db.applied_today() + self.inflight >= self.cfg.apply.daily_limit:
                reason = "дневной лимит"
            else:
                self.inflight += 1
                return True
        self.stop(reason)
        return False

    def release(self, counted: bool) -> None:
        with self.lock:
            self.inflight -= 1
            self.done += counted


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


def _apply_worker(cfg: Config, run: Run, src: str, mode: Mode, pace: float, cdp_port: int) -> None:
    """Отклики на одном сайте в своей вкладке; пока идёт поиск-оценка, ждёт новых вакансий."""
    au, dry_run = cfg.apply.auto, run.dry_run
    db = DB(cfg.path(cfg.db_path))
    analyst = None
    with attach(cdp_port) as (_, page):
        st = Site(src, Applier(page, cfg, mode))
        while run.stop_reason is None and not stop.requested():
            if not dry_run and not _in_window(cfg):
                run.stop("рабочие часы")
                break
            row = run.next_row(db, src)
            if row is None:
                if run.producing.is_set():
                    stop.wait(POLL_SECONDS)  # поиск и оценка ещё идут — новые вакансии появятся
                    continue
                break
            wait = st.next_ready - time.monotonic()
            if wait > 0:
                what = "Длинный перерыв" if st.on_break else "Пауза до следующего отклика"
                if not _sleep(wait, what, cfg, dry_run):
                    run.stop("рабочие часы")
                    break
                if stop.requested():
                    break
            title = f"{row['title']} — {row['company']} ({row['score']})"
            if au.skip_employer_tests and row["has_test"]:
                db.update(row["id"], status="manual", error="у работодателя тест")
                _log(f"[yellow]→ manual[/] {title}: тест работодателя")
                continue
            if not run.reserve(db):
                break
            counted = False
            try:
                letter = row["cover_letter"]
                if not letter:
                    analyst = analyst or analyst_mod.Analyst(cfg)
                    try:
                        letter = analyst.letter(row)
                        db.update(row["id"], cover_letter=letter)
                    except (analyst_mod.LimitReached, RuntimeError) as e:
                        if stop.requested():
                            break
                        _log(f"[red]Не удалось написать письмо[/] для {title}: {e}")
                        continue
                _log(f"Откликаюсь: {title}")
                try:
                    status, note = st.applier.apply(row["id"], row["url"], letter, analyst_mod._vacancy_prompt(row))
                except CaptchaError as e:  # капчу ждали (captcha_wait_minutes), но её не прошли
                    log_event(cfg, "captcha", where="apply", vid=row["id"], done=st.done)
                    st.blocked = "капча"
                    _log(f"[red]{e}[/] — на {SITES[src]} больше не откликаюсь, вакансия осталась в очереди.")
                    break
                except Exception as e:  # вёрстка меняется — считаем ошибки подряд
                    if stop.requested():  # браузер закрыт по Ctrl+C — отклик не сбой, вакансия в очереди
                        _log(f"[yellow]Отклик прерван[/] — {title} осталась в очереди")
                        break
                    st.errors += 1
                    db.update(row["id"], status="error", error=f"applier: {e}"[:500])
                    _log(f"[red]Ошибка[/] ({st.errors}/{au.max_consecutive_errors}): {e}")
                    if st.errors >= au.max_consecutive_errors:
                        st.blocked = "ошибки подряд"
                        _log(f"[red]Слишком много ошибок подряд — на {SITES[src]} останавливаюсь.[/]")
                        break
                    continue
                counted = _record(db, st, row, status, note)
            finally:
                run.release(counted)
            if counted:
                st.done += 1
                st.schedule_pause(cfg, pace, dry_run)
            elif status == "unconfirmed" and st.errors >= au.max_consecutive_errors:
                st.blocked = "неподтверждённые отправки подряд"
                _log(f"[red]Отправка не подтверждается несколько раз подряд — на {SITES[src]} останавливаюсь.[/]")
                break
    if st.blocked:
        _log(f"[yellow]{SITES[src]}: {st.blocked}[/]")
    _log(f"Откликов на {SITES[src]}: {st.done}")


def _scan_worker(cfg: Config, src: str, cdp_port: int, found: list[int]) -> None:
    db = DB(cfg.path(cfg.db_path))
    with attach(cdp_port) as (_, page):
        found.append(scan(cfg, db, page, {src}))


def _section(thread: str, sites: list[str]) -> str | None:
    """Блок вывода по имени потока: поиск и отклики сайта — в блок сайта, Telegram — в свой,
    оценка (общая для сайтов, её потоки LLM_*) — в первый блок."""
    if thread == "Telegram":
        return "tg"
    for s in sites:
        if SITES[s] in thread:
            return s
    if thread == "поиск-оценка" or thread.startswith("LLM"):
        return sites[0] if sites else None
    return None


def _produce(cfg: Config, run: Run, cdp_port: int) -> None:
    """Поиск на всех сайтах параллельно, затем оценка и письма (Codex — несколькими запросами сразу)."""
    try:
        found: list[int] = []
        threads = [_thread(_scan_worker, cfg, src, cdp_port, found, name=f"поиск {SITES[src]}")
                   for src in cfg.sources()]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if stop.requested():
            return
        _log(f"Новых вакансий после фильтров: {sum(found)}")
        analyst_mod.analyze_new(cfg, DB(cfg.path(cfg.db_path)))
    except Exception as e:
        if not stop.requested():
            _log(f"[red]Поиск и оценка: {e}[/]")
    finally:
        run.producing.clear()


def _telegram(cfg: Config, dry_run: bool, cdp_port: int, others: list[threading.Thread]) -> None:
    """Опросы AI-рекрутеров; заново — когда появились новые отклики в их компании, и в конце."""
    db = DB(cfg.path(cfg.db_path))
    seen = None
    while not stop.requested():
        alive = any(t.is_alive() for t in others)
        applies = len(recruiters._company_applies(cfg, db))
        if applies != seen:
            seen = applies
            try:
                recruiters.run(cfg, db, dry_run=dry_run, cdp_port=cdp_port)
            except Exception as e:
                if not stop.requested():
                    _log(f"[red]Telegram: {e}[/]")
        if not alive:
            return
        for _ in range(30):  # через 30 с проверить новые отклики, но закончить сразу, как закончат остальные
            if not any(t.is_alive() for t in others) or stop.wait(1):
                break


def run_auto(cfg: Config, db: DB, dry_run: bool = False, limit: int | None = None,
             min_score: int | None = None, produce: bool = False) -> None:
    """produce — сначала (параллельно с откликами) поиск и оценка: так работает autopilot."""
    try:
        _run_auto(cfg, db, dry_run, limit, min_score, produce)
    finally:
        learn(cfg, db)  # подстройка пауз и таймаутов по итогам сессии (правила, без LLM)


def _run_auto(cfg: Config, db: DB, dry_run: bool, limit: int | None, min_score: int | None, produce: bool) -> None:
    au = cfg.apply.auto
    pace = Store(cfg).tuning["pace_multiplier"]
    threshold = au.min_score if min_score is None else min_score
    max_n = limit or (3 if dry_run else au.max_per_run)
    run = Run(cfg, dry_run, max_n, threshold,
              fill_to_target=min_score is None and not dry_run and au.target_min_score < threshold)

    can_apply = True
    if not dry_run and not _in_window(cfg):
        _log(f"Сейчас не рабочие часы ({au.active_hours[0]}:00–{au.active_hours[1]}:00"
             f"{', только будни' if au.weekdays_only else ''}) — без откликов.")
        can_apply = False
    elif not dry_run and db.applied_today() >= cfg.apply.daily_limit:
        _log(f"Дневной лимит {cfg.apply.daily_limit} уже исчерпан — без откликов.")
        can_apply = False
    elif not produce and not db.by_status("scored", min_score=min(threshold, au.target_min_score)):
        _log(f"Нет оценённых вакансий с баллом ≥ {threshold}.")
        can_apply = False
    if can_apply:
        _log(f"{'[bold]DRY-RUN[/] — ' if dry_run else ''}в очереди: "
             f"{len(db.by_status('scored', min_score=threshold))}, за этот запуск максимум {max_n}, темп ×{pace}"
             + ("" if dry_run else f", сегодня {db.applied_today()} из {cfg.apply.daily_limit}"))

    mode = Mode(interactive=False, dry_run=dry_run, human_like=True, shots_dir=cfg.path("screenshots"))
    port = free_port()
    with open_browser(cfg, headless=cfg.browser.headless_apply, cdp_port=port) as (ctx, page):
        threads: list[threading.Thread] = []
        if produce:
            run.producing.set()
            threads.append(_thread(_produce, cfg, run, port, name="поиск-оценка"))
        if can_apply:
            tabs = SiteTabs(ctx, page)
            for src in cfg.sources():
                if is_logged_in(tabs(src), src):
                    threads.append(_thread(_apply_worker, cfg, run, src, mode, pace, port, name=SITES[src]))
                else:
                    _log(f"[yellow]Нет входа на {SITES[src]} — там не откликаюсь.[/] Выполните: uv run hh-agent login")
        workers = list(threads)
        if cfg.telegram.enabled:
            threads.append(_thread(_telegram, cfg, dry_run, port, workers, name="Telegram"))
        # Работа — параллельно, вывод — блоками: hh.ru, потом Хабр Карьера, потом Telegram.
        sites = sorted(cfg.sources(), key=lambda s: list(SITES).index(s) if s in SITES else len(SITES))
        order = [(s, SITES[s]) for s in sites] + ([("tg", "AI-рекрутеры в Telegram")] if cfg.telegram.enabled else [])
        with console.sequential(order, lambda name: _section(name, sites)):
            for t in threads:
                t.start()
            try:
                for key, _ in order:
                    for t in threads:
                        if _section(t.name, sites) == key:
                            t.join()
                    console.section_done(key)
            except KeyboardInterrupt:  # Ctrl+C или SIGTERM — потоки заканчивают текущий шаг
                _log(f"[yellow]{stop.reason()} — останавливаюсь: новых откликов и опросов не начинаю. "
                     f"Ещё раз {stop.reason()} — выйти сразу.[/]")
            for t in threads:
                t.join()

    stop_reason = (f"остановлено ({stop.reason()})" if stop.requested()
                   else run.stop_reason or "кандидаты закончились")
    log_event(cfg, "auto_end", dry_run=dry_run, done=run.done, stop_reason=stop_reason,
              applied_today=db.applied_today(), pace=pace, produce=produce)
    _log(f"Готово: {'заполнено форм' if dry_run else 'откликов'} — {run.done}. "
         f"Сегодня всего откликов: {db.applied_today()}/{cfg.apply.daily_limit}")
    if can_apply and not dry_run and db.applied_today() < au.daily_target:
        _log(f"[yellow]Норма {au.daily_target} не выполнена ({db.applied_today()}): {stop_reason}.[/]")
