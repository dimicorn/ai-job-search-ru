"""Оркестратор: login → scan (разведчик + аналитик) → review (вы + исполнитель)."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import analyst as analyst_mod
from .applier import apply_one
from .autopilot import run_auto
from .learning import learn
from . import chats as chats_mod
from .browser import LOGIN_URL, SITES, is_logged_in, missing_logins, open_browser, pause, source_of
from .config import Config, load_config
from .db import DB, dec_list
from .scout import scan

console = Console()


LOGIN_HINT = {"hh": "как соискатель", "habr": "через Хабр Аккаунт"}


def cmd_login(cfg: Config, db: DB) -> None:
    """Вход на каждый сайт из searches (hh.ru, Хабр Карьера); сессии хранятся в профиле браузера."""
    with open_browser(cfg) as (_, page):
        for src in cfg.sources():
            site = SITES[src]
            if is_logged_in(page, src):
                console.print(f"[green]Вы уже вошли: {site}.[/]")
                continue
            page.goto(LOGIN_URL[src], wait_until="domcontentloaded")
            input(f"Войдите на {site} в открывшемся окне ({LOGIN_HINT[src]}) и нажмите Enter… ")
            if is_logged_in(page, src):
                console.print(f"[green]{site}: готово, сессия сохранена.[/]")
            else:
                console.print(f"[red]{site}: вход не обнаружен.[/] Войдите {LOGIN_HINT[src]} и повторите hh-agent login.")


def cmd_scan(cfg: Config, db: DB, analyze: bool = True) -> None:
    with open_browser(cfg, headless=cfg.browser.headless_scan) as (_, page):
        n = scan(cfg, db, page)
    console.print(f"\nНовых вакансий после фильтров: [bold]{n}[/]")
    if analyze:
        cmd_analyze(cfg, db)


def cmd_analyze(cfg: Config, db: DB) -> None:
    console.rule("[bold]Оценка (Claude)")
    analyst_mod.analyze_new(cfg, db)


def _show(row, idx: int, total: int) -> None:
    sal = "—"
    if row["salary_from"] or row["salary_to"]:
        sal = f"{row['salary_from'] or '…'}–{row['salary_to'] or '…'} {row['currency'] or ''}"
    flags = dec_list(row["red_flags"])
    body = (
        f"[bold]{row['title']}[/] — {row['company']}\n"
        f"{row['area']} · {sal} · {', '.join(dec_list(row['work_formats'])) or '—'} · {row['url']}\n\n"
        f"[bold]Оценка {row['score']}[/] ({row['verdict']})\n"
        + "\n".join(f"  • {r}" for r in dec_list(row["reasons"]))
        + ("\n[red]Настораживает:[/]\n" + "\n".join(f"  • {r}" for r in flags) if flags else "")
        + (f"\n[yellow]У работодателя тест — возможно, понадобится ручной ввод.[/]" if row["has_test"] else "")
        + (f"\n[yellow]Отложено: {row['error']}[/]" if row["status"] == "manual" and row["error"] else "")
        + f"\n\n[bold]Письмо:[/]\n{row['cover_letter']}"
    )
    console.print(Panel(body, title=f"{idx}/{total}", expand=False))


def _edit(text: str) -> str:
    with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(text)
        path = f.name
    subprocess.run([os.environ.get("EDITOR", "nano"), path])
    with open(path, encoding="utf-8") as f:
        new = f.read().strip()
    os.unlink(path)
    return new or text


def cmd_review(cfg: Config, db: DB, min_score: int | None = None, manual: bool = False) -> None:
    if manual:  # отложенные: тесты, анкеты, нераспознанные формы — их проходите вы
        threshold = 0 if min_score is None else min_score
        rows = db.by_status("manual", min_score=threshold)
        if not rows:
            console.print("В manual пусто.")
            return
        console.print("[yellow]Режим manual:[/] агент откроет вакансию и попробует отклик; если нужен тест "
                      "или анкета — письмо окажется в буфере обмена, а закончите вы в окне браузера.")
    else:
        threshold = cfg.analyst.min_score if min_score is None else min_score
        rows = db.by_status("scored", min_score=threshold)
    if not rows:
        console.print(f"Нет оценённых вакансий с баллом ≥ {threshold}. Запустите [bold]hh-agent scan[/].")
        return
    console.print(f"К просмотру: {len(rows)}. Сегодня уже откликов: {db.applied_today()}/{cfg.apply.daily_limit}")
    keys = "[y] откликнуться  [e] править письмо  [r] переписать с указанием  [o] открыть  [s] пропустить  [l] позже  [q] выход"
    analyst = None
    with open_browser(cfg) as (_, page):
        missing = missing_logins(page, (r["url"] for r in rows))
        if missing:
            names = ", ".join(SITES[s] for s in missing)
            rows = [r for r in rows if source_of(r["url"]) not in missing]
            if not rows:
                raise SystemExit(f"Нет входа: {names}. Сначала выполните: hh-agent login")
            console.print(f"[yellow]Нет входа: {names} — эти вакансии пропускаю (hh-agent login).[/]")
        for i, row in enumerate(rows, 1):
            if db.applied_today() >= cfg.apply.daily_limit:
                console.print(f"[yellow]Достигнут дневной лимит ({cfg.apply.daily_limit}).[/]")
                break
            letter = row["cover_letter"]
            if not letter:  # вакансия ниже порога: письмо ещё не писалось
                analyst = analyst or analyst_mod.Analyst(cfg)
                try:
                    with console.status("Пишу письмо…"):
                        letter = analyst.letter(row)
                    db.update(row["id"], cover_letter=letter)
                except (analyst_mod.LimitReached, RuntimeError) as e:
                    console.print(f"[red]Не удалось написать письмо: {e}[/]")
            while True:
                _show(db.get(row["id"]), i, len(rows))
                ch = input(f"{keys}\n> ").strip().lower()
                if ch == "e":
                    letter = _edit(letter)
                    db.update(row["id"], cover_letter=letter)
                elif ch == "r":
                    hint = input("Что изменить в письме? ")
                    analyst = analyst or analyst_mod.Analyst(cfg)
                    try:
                        with console.status("Переписываю…"):
                            letter = analyst.letter(db.get(row["id"]), extra=hint)
                    except (analyst_mod.LimitReached, RuntimeError) as e:
                        console.print(f"[red]Не удалось переписать: {e}[/]")
                        continue
                    db.update(row["id"], cover_letter=letter)
                elif ch == "o":
                    page.goto(row["url"], wait_until="domcontentloaded")
                elif ch == "s":
                    db.update(row["id"], status="skipped")
                    break
                elif ch == "l":
                    break
                elif ch == "q":
                    return
                elif ch == "y":
                    try:
                        status, note = apply_one(page, row["url"], letter, cfg, row["id"],
                                                 analyst_mod._vacancy_prompt(db.get(row["id"])))
                    except Exception as e:  # вёрстка hh меняется — не роняем всю сессию
                        status, note = "error", f"applier: {e}"
                    db.update(
                        row["id"], status=status, error=note,
                        applied_at=datetime.now().isoformat(timespec="seconds") if status == "applied" else None,
                    )
                    color = {"applied": "green", "manual": "yellow"}.get(status, "red")
                    console.print(f"[{color}]{status}[/] {note or ''}")
                    pause(cfg.apply.delay_seconds)
                    break


def cmd_chats(cfg: Config, db: DB, dry_run: bool = False, review: bool = False) -> None:
    from .browser import CaptchaError
    with open_browser(cfg, headless=cfg.browser.headless_apply) as (_, page):
        if not is_logged_in(page):
            raise SystemExit("Вы не вошли в hh.ru. Выполните в терминале: uv run hh-agent login")
        try:
            if review:
                chats_mod.review_drafts(cfg, db, page)
            else:
                chats_mod.process_chats(cfg, db, page, dry_run=dry_run)
        except CaptchaError:
            console.print("[red]hh.ru показал капчу — чаты отложены.[/]")
    drafts = len(db.chat_drafts())
    if drafts and not review:
        console.print(f"[yellow]Черновиков ждут вас: {drafts}[/] — uv run hh-agent chats --review")


def cmd_requeue(cfg: Config, db: DB, include_tests: bool) -> None:
    """manual → scored. Тесты и анкеты по умолчанию не трогаем: автомат их всё равно не пройдёт."""
    n = 0
    for r in db.by_status("manual"):
        err = (r["error"] or "").lower()
        if not include_tests and ("тест" in err or "анкет" in err or "вопрос" in err):
            continue
        db.update(r["id"], status="scored", error=None)
        n += 1
        console.print(f"  ↺ {r['title']} — {r['company']}")
    console.print(f"Возвращено в очередь: {n}. Остальное в manual — для hh-agent review --manual.")


def cmd_stats(cfg: Config, db: DB) -> None:
    t = Table("статус", "кол-во")
    for s, n in db.stats():
        t.add_row(s, str(n))
    console.print(t)
    console.print(f"Откликов сегодня: {db.applied_today()}/{cfg.apply.daily_limit}")
    cs = dict(db.chat_stats())
    if cs:
        console.print("Чаты: " + ", ".join(f"{k} {v}" for k, v in cs.items()))


def cmd_list(cfg: Config, db: DB, status: str) -> None:
    t = Table("id", "сайт", "балл", "вакансия", "компания", "причина/ошибка")
    for r in db.by_status(status):
        t.add_row(str(r["id"]), source_of(r["url"]), str(r["score"] or ""), r["title"], r["company"] or "",
                  r["filter_reason"] or r["error"] or "")
    console.print(t)


def main() -> None:
    p = argparse.ArgumentParser(prog="hh-agent", description="Полуавтоматические отклики на hh.ru и Хабр Карьере")
    p.add_argument("-c", "--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="войти в hh.ru / Хабр Карьеру (один раз, сессия сохраняется)")
    s = sub.add_parser("scan", help="найти вакансии по фильтрам и оценить их")
    s.add_argument("--no-analyze", action="store_true")
    sub.add_parser("analyze", help="оценить ещё не оценённые вакансии")
    lt = sub.add_parser("letters", help="переписать письма для оценённых вакансий")
    lt.add_argument("--min-score", type=int)
    r = sub.add_parser("review", help="просмотреть подходящие вакансии и откликнуться")
    r.add_argument("--min-score", type=int)
    r.add_argument("--manual", action="store_true", help="пройти отложенные (manual): тесты, анкеты, нераспознанные формы")
    rq = sub.add_parser("requeue", help="вернуть manual-вакансии в очередь (для review/auto)")
    rq.add_argument("--all", action="store_true", help="включая вакансии с тестами работодателя")
    sub.add_parser("run", help="scan + review")
    au = sub.add_parser("auto", help="автоматические отклики (без подтверждения) с паузами и лимитами")
    au.add_argument("--dry-run", action="store_true", help="заполнить формы, но не отправлять (скриншоты в screenshots/)")
    au.add_argument("--limit", type=int, help="максимум откликов за запуск")
    au.add_argument("--min-score", type=int)
    sub.add_parser("autopilot", help="чаты + scan + оценка + auto + чаты — для запуска по расписанию")
    ch = sub.add_parser("chats", help="ответить работодателям в чатах")
    ch.add_argument("--dry-run", action="store_true", help="решить и показать ответы, ничего не отправлять")
    ch.add_argument("--review", action="store_true", help="пройти черновики, которые агент не отправил сам")
    sub.add_parser("learn", help="разобрать debug/ и логи и подстроить агента (обычно — само в конце сессии)")
    sub.add_parser("stats", help="статистика")
    ls = sub.add_parser("list", help="список вакансий по статусу")
    ls.add_argument("status", choices=["new", "filtered", "scored", "skipped", "applied", "unconfirmed", "manual", "error"])
    a = p.parse_args()

    if (a.cmd in ("login", "review", "run") or (a.cmd == "chats" and a.review)) and not sys.stdin.isatty():
        raise SystemExit(
            f"Команда {a.cmd} интерактивная: запустите её в обычном окне терминала "
            "(не через `!` в Claude Code и не из cron)."
        )
    cfg = load_config(a.config)
    db = DB(cfg.path(cfg.db_path))
    if a.cmd == "login":
        cmd_login(cfg, db)
    elif a.cmd == "scan":
        cmd_scan(cfg, db, analyze=not a.no_analyze)
    elif a.cmd == "analyze":
        cmd_analyze(cfg, db)
    elif a.cmd == "letters":
        analyst_mod.rewrite_letters(cfg, db, a.min_score)
    elif a.cmd == "review":
        try:
            cmd_review(cfg, db, a.min_score, manual=a.manual)
        finally:
            learn(cfg, db)
    elif a.cmd == "requeue":
        cmd_requeue(cfg, db, a.all)
    elif a.cmd == "learn":
        learn(cfg, db)
    elif a.cmd == "auto":
        run_auto(cfg, db, dry_run=a.dry_run, limit=a.limit, min_score=a.min_score)
    elif a.cmd == "chats":
        cmd_chats(cfg, db, dry_run=a.dry_run, review=a.review)
    elif a.cmd == "autopilot":
        cmd_chats(cfg, db)  # сначала ответить тем, кто уже пишет
        cmd_scan(cfg, db)
        run_auto(cfg, db)
        cmd_chats(cfg, db)
    elif a.cmd == "run":
        cmd_scan(cfg, db)
        try:
            cmd_review(cfg, db)
        finally:
            learn(cfg, db)
    elif a.cmd == "stats":
        cmd_stats(cfg, db)
    elif a.cmd == "list":
        cmd_list(cfg, db, a.status)


if __name__ == "__main__":
    main()
