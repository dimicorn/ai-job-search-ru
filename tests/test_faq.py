import yaml

from hh_agent import faq


def _write(cfg, entries):
    cfg.path(cfg.questions.faq_file).write_text(yaml.safe_dump(entries, allow_unicode=True), encoding="utf-8")


def test_lookup_match_and_unless(cfg):
    _write(cfg, [{"match": ["зарплат"], "unless": ["вилк"], "answer": "150 000 ₽ на руки"}])
    assert faq.lookup(cfg, "Какие у вас ожидания по Зарплате?") == "150 000 ₽ на руки"
    assert faq.lookup(cfg, "Устраивает ли вас вилка зарплаты 100–120?") is None  # вопрос шире записи


def test_lookup_needs_one_question_and_one_answer(cfg):
    _write(cfg, [{"match": ["зарплат"], "answer": "150 000"}, {"match": ["переезд"], "answer": "Нет"}])
    assert faq.lookup(cfg, "Зарплата? И готовы к переезду?") is None  # два вопроса
    assert faq.lookup(cfg, "Зарплата и переезд — что скажете") is None  # две записи — два разных ответа


def test_learn_then_expire_when_facts_change(cfg):
    q = "Здравствуйте! Когда сможете выйти на работу?"
    faq.learn(cfg, q, "Через 1–2 недели после предложения.")
    assert faq.lookup(cfg, "Когда сможете выйти на работу?") == "Через 1–2 недели после предложения."
    cfg.questions.facts += "\nСрок выхода: сразу."
    assert faq.lookup(cfg, "Когда сможете выйти на работу?") is None  # выучено по старым фактам


def test_learn_skips_unknowns_and_keeps_own_entries(cfg):
    _write(cfg, [{"match": ["Когда сможете выйти на работу?"], "answer": "Свой ответ"}])
    faq.learn(cfg, "Когда сможете выйти на работу?", "Ответ LLM")
    faq.learn(cfg, "Сколько лет опыта с Kubernetes?", "[уточнить]")
    entries = yaml.safe_load(cfg.path(cfg.questions.faq_file).read_text(encoding="utf-8"))
    assert entries == [{"match": ["Когда сможете выйти на работу?"], "answer": "Свой ответ"}]


def test_question_of_drops_bot_intro():
    assert faq.question_of("Спасибо за ответ! Следующий вопрос. Есть ли опыт с Docker?") == "Есть ли опыт с Docker?"


def test_match_written_as_string_is_one_fragment(cfg):
    # «match: зарплат» строкой раньше разбиралась по буквам — и подходила к любому вопросу
    cfg.path(cfg.questions.faq_file).write_text("- match: зарплат\n  unless: миним\n  answer: 150 000\n",
                                                encoding="utf-8")
    assert faq.lookup(cfg, "Какой у вас опыт с PyTorch?") is None
    assert faq.lookup(cfg, "Ваши ожидания по зарплате?") == "150 000"
    assert faq.lookup(cfg, "Минимальная зарплата?") is None


def test_learn_replaces_file_atomically(cfg, monkeypatch):
    # lookup из другого потока не должен увидеть файл наполовину записанным
    seen = []
    real_replace = faq.os.replace
    monkeypatch.setattr(faq.os, "replace", lambda a, b: (seen.append(cfg.path(cfg.questions.faq_file).exists()),
                                                         real_replace(a, b)))
    faq.learn(cfg, "Когда сможете выйти на работу?", "Через неделю.")
    assert seen == [False]  # пока файл писался, на месте faq.yaml его не было — только готовый целиком
    assert faq.lookup(cfg, "Когда сможете выйти на работу?") == "Через неделю."
    assert not list(cfg.base_dir.glob(".faq.yaml.*"))
