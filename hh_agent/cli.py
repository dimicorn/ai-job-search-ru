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
from . import recruiters
from .learning import learn, report
from .browser import LOGIN_URL, SITES, SiteTabs, is_logged_in, open_browser, pause, source_of
from .config import Config, load_config
from .db import DB, dec_list
from .scout import scan

console = Console()


LOGIN_HINT = {"hh": "как соискатель", "habr": "через Хабр Аккаунт"}


def cmd_login(cfg: Config, db: DB) -> None:
    """Вход на каждый сайт из searches (hh.ru, Хабр Карьера); сессии хранятся в профиле браузера."""
    with open_browser(cfg) as (ctx, first):
        tabs = SiteTabs(ctx, first)
        for src in cfg.sources():
            site = SITES[src]
            page = tabs(src)
            if is_logged_in(page, src):
                console.print(f"[green]Вы уже вошли: {site}.[/]")
                continue
            page.goto(LOGIN_URL[src], wait_until="domcontentloaded")
            input(f"Войдите на {site} в открывшемся окне ({LOGIN_HINT[src]}) и нажмите Enter… ")
            if is_logged_in(page, src):
                console.print(f"[green]{site}: готово, сессия сохранена.[/]")
            else:
                console.print(f"[red]{site}: вход не обнаружен.[/] Войдите {LOGIN_HINT[src]} и повторите hh-agent login.")
    if cfg.telegram.enabled:
        recruiters.login(cfg)


def cmd_scan(cfg: Config, db: DB, analyze: bool = True) -> None:
    with open_browser(cfg, headless=cfg.browser.headless_scan) as (_, page):
        n = scan(cfg, db, page)
    console.print(f"\nНовых вакансий после фильтров: [bold]{n}[/]")
    if analyze:
        cmd_analyze(cfg, db)


def cmd_analyze(cfg: Config, db: DB) -> None:
    console.rule("[bold]Оценка вакансий")
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
        + ("\n[yellow]У работодателя тест — возможно, понадобится ручной ввод.[/]" if row["has_test"] else "")
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
    with open_browser(cfg) as (ctx, first):
        tabs = SiteTabs(ctx, first)  # hh.ru и Хабр Карьера — в разных вкладках
        missing = tabs.missing_logins(r["url"] for r in rows)
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
                    tabs(source_of(row["url"])).goto(row["url"], wait_until="domcontentloaded")
                elif ch == "s":
                    db.update(row["id"], status="skipped")
                    break
                elif ch == "l":
                    break
                elif ch == "q":
                    return
                elif ch == "y":
                    try:
                        status, note = apply_one(tabs(source_of(row["url"])), row["url"], letter, cfg, row["id"],
                                                 analyst_mod._vacancy_prompt(db.get(row["id"])))
                    except Exception as e:  # вёрстка hh меняется — не роняем всю сессию
                        status, note = "error", f"applier: {e}"
                    # «Отклик уже был» — applied без applied_at: в дневной лимит не идёт.
                    db.update(
                        row["id"], status="applied" if status == "already_applied" else status, error=note,
                        applied_at=datetime.now().isoformat(timespec="seconds") if status == "applied" else None,
                    )
                    color = {"applied": "green", "already_applied": "dim", "manual": "yellow"}.get(status, "red")
                    console.print(f"[{color}]{status}[/] {note or ''}")
                    pause(cfg.apply.delay_seconds)
                    break


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
    """Сводка за сегодня, 7 и 30 дней (включая сегодня) и текущая очередь."""
    from datetime import date, timedelta
    from .events import read_events

    today = date.today()
    periods = {"сегодня": today.isoformat(),
               "7 дней": (today - timedelta(days=6)).isoformat(),
               "30 дней": (today - timedelta(days=29)).isoformat()}
    rows = db.conn.execute("SELECT url, status, score, found_at, applied_at FROM vacancies").fetchall()
    events = read_events(cfg)
    sites = sorted({source_of(r["url"]) for r in rows}) or ["hh"]
    passing = cfg.analyst.min_score

    def count(since: str, pred) -> int:
        return sum(1 for r in rows if pred(r, since))

    found = lambda r, d: (r["found_at"] or "") >= d
    sent = lambda src: (lambda r, d: r["status"] == "applied" and (r["applied_at"] or "") >= d
                        and source_of(r["url"]) == src)
    metrics = [
        ("Найдено вакансий", lambda d: count(d, found)),
        ("  прошли фильтры", lambda d: count(d, lambda r, d: found(r, d) and r["status"] != "filtered")),
        (f"  подходящих (балл ≥ {passing})", lambda d: count(d, lambda r, d: found(r, d) and (r["score"] or 0) >= passing)),
        ("Откликов отправлено", lambda d: sum(count(d, sent(src)) for src in sites)),
        *[(f"  {SITES.get(src, src)}", (lambda src: lambda d: count(d, sent(src)))(src)) for src in sites],
        ("Отправка не подтвердилась", lambda d: count(d, lambda r, d: r["status"] == "unconfirmed"
                                                     and (r["applied_at"] or "") >= d)),
        ("Отложено в manual", lambda d: count(d, lambda r, d: found(r, d) and r["status"] == "manual")),
        # Каждая капча — одна запись: появление (без solved) или, при ожидании без id вакансии, «пройдена».
        ("Капч", lambda d: sum(1 for e in events if e["kind"] == "captcha" and e["ts"] >= d
                               and ("solved" not in e or (e.get("solved") and "vid" not in e)))),
    ]
    # Расход LLM (события `llm`, пишутся с версии с учётом токенов).
    llm = [e for e in events if e["kind"] == "llm"]
    tok = lambda e: (e.get("input") or 0) + (e.get("output") or 0)
    k = lambda n: f"{n / 1000:.0f}k" if n >= 10_000 else f"{n / 1000:.1f}k" if n else "0"
    def per_apply(d: str) -> str:
        n = sum(count(d, sent(src)) for src in sites)
        used = sum(tok(e) for e in llm if e["ts"] >= d)
        return k(used // n) if n and used else "—"
    purposes = {"score": "оценка", "letter": "письма", "questions": "вопросы", "recruiter": "рекрутеры в Telegram", "report": "отчёт", "other": "прочее"}
    metrics += [
        ("LLM-вызовов", lambda d: sum(1 for e in llm if e["ts"] >= d)),
        ("Токенов (вход + выход)", lambda d: k(sum(tok(e) for e in llm if e["ts"] >= d))),
        *[(f"  {name}", (lambda p: lambda d: k(sum(tok(e) for e in llm if e["ts"] >= d and e.get("purpose") == p)))(p))
          for p, name in purposes.items() if any(e.get("purpose") == p for e in llm)],
        ("  из них из кеша", lambda d: k(sum(e.get("cached") or 0 for e in llm if e["ts"] >= d))),
        ("Токенов на отклик", per_apply),
    ]
    if any(e.get("cost_usd") for e in llm):
        metrics.append(("Стоимость, $", lambda d: f"{sum(e.get('cost_usd') or 0 for e in llm if e['ts'] >= d):.2f}"))

    t = Table("", *periods)
    for name, fn in metrics:
        t.add_row(name, *(str(fn(d)) for d in periods.values()),
                  style="bold" if not name.startswith(" ") else None)
    console.print(t)

    queue = db.by_status("scored", min_score=cfg.apply.auto.min_score)
    by_site = {src: sum(source_of(r["url"]) == src for r in queue) for src in sites}
    console.print(f"Сегодня откликов: {db.applied_today()}/{cfg.apply.daily_limit}. "
                  f"В очереди на автоотклик (балл ≥ {cfg.apply.auto.min_score}): {len(queue)}"
                  + (" — " + ", ".join(f"{SITES.get(s, s)} {n}" for s, n in by_site.items()) if len(sites) > 1 else ""))
    manual = len(db.by_status("manual"))
    if manual:
        console.print(f"Ждут вас в manual: {manual} (uv run hh-agent review --manual)")


def cmd_list(cfg: Config, db: DB, status: str) -> None:
    t = Table("id", "сайт", "балл", "вакансия", "компания", "причина/ошибка")
    for r in db.by_status(status):
        t.add_row(str(r["id"]), source_of(r["url"]), str(r["score"] or ""), r["title"], r["company"] or "",
                  r["filter_reason"] or r["error"] or "")
    console.print(t)


def _keep_awake() -> None:
    """Не давать Mac засыпать от бездействия, пока идёт запуск (закрытую крышку это не отменяет).
    caffeinate сам завершится вместе с этим процессом (-w)."""
    try:
        subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(os.getpid())],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass  # не macOS — просто работаем дальше


def cmd_help(sub: argparse._SubParsersAction) -> None:
    """Список команд с описаниями и опциями — из тех же определений, что и разбор аргументов."""
    t = Table("команда", "что делает", "опции", box=None, show_header=False, padding=(0, 2, 0, 0))
    for act in sub._choices_actions:
        opts = [x.option_strings[-1] if x.option_strings else f"<{x.dest}>"
                for x in sub.choices[act.dest]._actions if x.dest != "help"]
        t.add_row(f"[bold]{act.dest}[/]", act.help or "", " ".join(opts))
    console.print("Использование: [bold]uv run hh-agent <команда> [опции][/]\n")
    console.print(t)
    console.print("\nПодробнее о команде: uv run hh-agent <команда> -h")


def main() -> None:
    p = argparse.ArgumentParser(prog="hh-agent", description="Агент откликов на hh.ru и Хабр Карьере")
    p.add_argument("-c", "--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("help", help="показать этот список команд")
    sub.add_parser("login", help="войти в hh.ru, Хабр Карьеру и Telegram (один раз, сессия сохраняется)")
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
    sub.add_parser("autopilot", help="scan + оценка + auto — для запуска по расписанию")
    rc = sub.add_parser("recruiters", help="пройти опросы AI-рекрутеров в Telegram (сами идут в конце auto)")
    rc.add_argument("--dry-run", action="store_true", help="показать, что агент ответил бы, ничего не отправляя")
    rp = sub.add_parser("report", help="сводка и рекомендации по журналу событий за N дней (через LLM)")
    rp.add_argument("--days", type=int, default=7)
    sub.add_parser("stats", help="статистика за сегодня, 7 и 30 дней")
    ls = sub.add_parser("list", help="список вакансий по статусу")
    ls.add_argument("status", choices=["new", "filtered", "scored", "skipped", "applied", "unconfirmed", "manual", "error"])
    a = p.parse_args()
    if a.cmd in (None, "help"):
        cmd_help(sub)
        return

    if a.cmd in ("login", "review", "run") and not sys.stdin.isatty():
        raise SystemExit(
            f"Команда {a.cmd} интерактивная: запустите её в обычном окне терминала "
            "(не через `!` в Claude Code и не из cron)."
        )
    if a.cmd in ("scan", "analyze", "letters", "review", "run", "auto", "autopilot", "recruiters"):
        _keep_awake()
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
    elif a.cmd == "report":
        report(cfg, a.days)
    elif a.cmd == "auto":
        run_auto(cfg, db, dry_run=a.dry_run, limit=a.limit, min_score=a.min_score)
    elif a.cmd == "autopilot":
        cmd_scan(cfg, db)
        run_auto(cfg, db)
    elif a.cmd == "run":
        cmd_scan(cfg, db)
        try:
            cmd_review(cfg, db)
        finally:
            learn(cfg, db)
    elif a.cmd == "recruiters":
        if not cfg.telegram.enabled:
            raise SystemExit("Включите telegram.enabled в config.yaml.")
        recruiters.run(cfg, db, dry_run=a.dry_run)
    elif a.cmd == "stats":
        cmd_stats(cfg, db)
    elif a.cmd == "list":
        cmd_list(cfg, db, a.status)


if __name__ == "__main__":
    main()
