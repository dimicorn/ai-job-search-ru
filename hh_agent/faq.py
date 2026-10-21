"""Частые вопросы работодателей и ботов: ответ без вызова LLM (questions.faq_file, по умолчанию faq.yaml).

Запись: match — фрагменты вопроса (без учёта регистра и «ё»), подходит любой; unless — фрагменты,
при которых запись не подходит (вопрос шире записи); answer — ответ.
Вопрос получает ответ из списка, только если в нём один вопросительный знак и все совпавшие
записи дают один и тот же ответ, — иначе вопрос сложнее записи и отвечает LLM.

Агент дополняет список сам: ответ LLM на вопрос, который не зависит от вакансии (зарплата, причина
поиска, срок выхода, документы), записывается с learned: true и отпечатком questions.facts.
Поменяли факты — выученные по старым ответы перестают использоваться (и перезапишутся новыми).
Свои записи без learned не устаревают; править и удалять можно любые.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
from typing import Any

import yaml

from .config import Config

HEADER = """# Частые вопросы: совпадение — ответ без вызова LLM. Подробности — в hh_agent/faq.py.
# match — фрагменты вопроса (регистр не важен), unless — при них запись не подходит, answer — ответ.
# learned: true — запись добавил агент по ответу LLM; правьте или удаляйте как свои.
"""

_LOCK = threading.Lock()  # пишут потоки откликов и Telegram


def _norm(s: str) -> str:
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"[«»\"'“”]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def question_of(text: str) -> str:
    """Сам вопрос из сообщения: последнее предложение со знаком вопроса (без вступления бота)."""
    found = re.findall(r"[^.!?\n]*\?", text or "")
    return found[-1].strip() if found else (text or "").strip()


def _facts_hash(cfg: Config) -> str:
    return hashlib.sha1(cfg.questions.facts.strip().encode()).hexdigest()[:10]


def _load(cfg: Config) -> list[dict[str, Any]]:
    path = cfg.path(cfg.questions.faq_file)
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    out = []
    for e in data:
        if not (isinstance(e, dict) and e.get("match") and e.get("answer")):
            continue
        for k in ("match", "unless"):  # «match: зарплата» строкой — один фрагмент, а не список букв
            if isinstance(e.get(k), str):
                e[k] = [e[k]]
        out.append(e)
    return out


def lookup(cfg: Config, text: str) -> str | None:
    """Ответ из списка или None."""
    if not cfg.questions.faq or (text or "").count("?") > 1:
        return None
    q, facts = _norm(text), _facts_hash(cfg)
    hits = {str(e["answer"]).strip() for e in _load(cfg)
            if (not e.get("learned") or e.get("facts") == facts)
            and any(_norm(m) and _norm(m) in q for m in e["match"])
            and not any(_norm(u) and _norm(u) in q for u in e.get("unless") or [])}
    return hits.pop() if len(hits) == 1 else None


def learn(cfg: Config, text: str, answer: str) -> None:
    """Запомнить ответ LLM на вопрос, не зависящий от вакансии."""
    q, answer = question_of(text), (answer or "").strip()
    if not cfg.questions.faq or not answer or "[уточнить]" in answer or len(q) < 8 or q.count("?") > 1:
        return
    facts = _facts_hash(cfg)
    with _LOCK:
        entries = _load(cfg)
        for e in entries:  # тот же вопрос уже есть: свою запись не трогаем, выученную обновляем
            if any(_norm(m) == _norm(q) for m in e["match"]):
                if not e.get("learned") or (e.get("facts") == facts):
                    return
                e.update(answer=answer, facts=facts)
                break
        else:
            entries.append({"match": [q], "answer": answer, "learned": True, "facts": facts})
        # Через временный файл: lookup из другого потока не прочитает файл наполовину (с обрезанным ответом).
        path = cfg.path(cfg.questions.faq_file)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(HEADER + "\n" + yaml.safe_dump(entries, allow_unicode=True, sort_keys=False, width=100),
                       encoding="utf-8")
        os.replace(tmp, path)
