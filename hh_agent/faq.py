"""Частые вопросы работодателей и ботов: ответ без вызова LLM (questions.faq_file, по умолчанию faq.yaml).

Запись: match — фрагменты вопроса (без учёта регистра и «ё»), подходит любой; unless — фрагменты,
при которых запись не подходит (вопрос шире записи); answer — ответ.
Вопрос получает ответ из списка, только если в нём один вопросительный знак и все совпавшие
записи дают один и тот же ответ, — иначе вопрос сложнее записи и отвечает LLM.

Агент дополняет список сам: ответ LLM на вопрос, который не зависит от вакансии (зарплата, причина
поиска, срок выхода, документы), записывается с learned: true и отпечатком questions.facts.
Поменяли факты — выученные по старым ответы перестают использоваться (новый ответ допишется рядом).
Свои записи без learned не устаревают; править и удалять можно любые. Агент файл не переписывает,
а только дописывает в конец: ваши комментарии и записи остаются.
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
    """Ответ из списка или None. Есть вопрос — фрагменты ищутся только в нём: бот часто сначала повторяет
    тему прошлого ответа («Спасибо, про приписное понял. Расскажите об опыте с PyTorch?»)."""
    if not cfg.questions.faq or (text or "").count("?") > 1:
        return None
    q, facts = _norm(question_of(text) if "?" in (text or "") else text), _facts_hash(cfg)
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
        for e in _load(cfg):  # тот же вопрос уже есть — своя запись или выученная по нынешним фактам
            if any(_norm(m) == _norm(q) for m in e["match"]) and (not e.get("learned") or e.get("facts") == facts):
                return
        # Дописываем в конец, а не переписываем файл: ваши комментарии и записи, которые агент не разбирает
        # (без answer, черновики), остаются как есть. Выученная по старым фактам запись тоже остаётся,
        # но lookup её уже не использует.
        path = cfg.path(cfg.questions.faq_file)
        old = path.read_text(encoding="utf-8") if path.exists() else HEADER + "\n"
        entry = yaml.safe_dump([{"match": [q], "answer": answer, "learned": True, "facts": facts}],
                               allow_unicode=True, sort_keys=False, width=100)
        text = old + ("\n" if old and not old.endswith("\n") else "") + entry
        try:
            ok = isinstance(yaml.safe_load(text), list)
        except yaml.YAMLError:
            ok = False
        if not ok:  # файл не в виде списка «- match: …» — дописать нельзя, не портим его
            return
        # Через временный файл: lookup из другого потока не прочитает файл наполовину (с обрезанным ответом).
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
