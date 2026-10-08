import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")

SCHEMA = """
CREATE TABLE IF NOT EXISTS industries (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);

-- Уникальные профессиональные роли hh. industry_id — основная отрасль для рейтинга.
CREATE TABLE IF NOT EXISTS professions (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    industry_id    INTEGER REFERENCES industries(id),
    included       INTEGER NOT NULL DEFAULT 0,
    exclude_reason TEXT
);

-- Все связи роли с категориями из справочника hh (роль бывает в нескольких).
CREATE TABLE IF NOT EXISTS profession_industries (
    profession_id INTEGER REFERENCES professions(id),
    industry_id   INTEGER REFERENCES industries(id),
    PRIMARY KEY (profession_id, industry_id)
);

-- Один снимок = один проход по ролям. kind: discovery (отбор ролей) или measure (замер).
CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    area_id     INTEGER NOT NULL,
    work_format TEXT NOT NULL,
    order_by    TEXT NOT NULL,
    per_page    INTEGER NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    notes       TEXT
);

-- Результат запроса выдачи по одной роли внутри снимка.
CREATE TABLE IF NOT EXISTS search_results (
    snapshot_id   INTEGER REFERENCES snapshots(id),
    profession_id INTEGER REFERENCES professions(id),
    fetched_at    TEXT NOT NULL,
    http_status   INTEGER,
    total_found   INTEGER,
    n_items       INTEGER,
    state         TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, profession_id)
);

-- Карточка вакансии: последние увиденные метаданные.
CREATE TABLE IF NOT EXISTS vacancies (
    id            INTEGER PRIMARY KEY,
    name          TEXT,
    employer_id   INTEGER,
    employer_name TEXT,
    area_id       INTEGER,
    area_name     TEXT,
    salary_from   REAL,
    salary_to     REAL,
    currency      TEXT,
    experience    TEXT,
    work_formats  TEXT,
    role_ids      TEXT,
    properties    TEXT,
    published_at  TEXT,
    url           TEXT,
    first_seen_at TEXT,
    last_seen_at  TEXT
);

-- Наблюдение счётчика: вакансия на позиции rank в выдаче роли в момент снимка.
CREATE TABLE IF NOT EXISTS observations (
    snapshot_id   INTEGER REFERENCES snapshots(id),
    profession_id INTEGER REFERENCES professions(id),
    vacancy_id    INTEGER REFERENCES vacancies(id),
    rank          INTEGER NOT NULL,
    viewers       INTEGER,
    observed_at   TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, profession_id, vacancy_id)
);

-- Сырой объект вакансии из выдачи (zlib-JSON без логотипов и адреса): страховка,
-- чтобы позже разобрать любое поле без повторного сбора. Хранится последняя версия.
CREATE TABLE IF NOT EXISTS vacancy_raw (
    id      INTEGER PRIMARY KEY REFERENCES vacancies(id),
    seen_at TEXT NOT NULL,
    data    BLOB NOT NULL
);

-- Справочник регионов hh: город/район → субъект РФ (для фильтра «Регион»).
CREATE TABLE IF NOT EXISTS areas (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    parent_id   INTEGER,
    region_id   INTEGER,
    region_name TEXT
);

-- Отдельные запросы выдачи внутри профессии (большой замер дробит запросы по регионам и опыту).
CREATE TABLE IF NOT EXISTS search_queries (
    snapshot_id   INTEGER REFERENCES snapshots(id),
    profession_id INTEGER REFERENCES professions(id),
    query         TEXT NOT NULL,
    fetched_at    TEXT NOT NULL,
    total_found   INTEGER,
    n_items       INTEGER,
    PRIMARY KEY (snapshot_id, profession_id, query)
);

-- Счётчики фильтров hh (кластеры выдачи) по роли: сколько вакансий в каждом формате, регионе, опыте.
-- Нужны, чтобы спланировать разбиение запросов (hh отдаёт не больше 2000 вакансий на запрос).
CREATE TABLE IF NOT EXISTS search_clusters (
    snapshot_id   INTEGER REFERENCES snapshots(id),
    profession_id INTEGER REFERENCES professions(id),
    cluster       TEXT NOT NULL,
    value_id      TEXT NOT NULL,
    title         TEXT,
    count         INTEGER,
    PRIMARY KEY (snapshot_id, profession_id, cluster, value_id)
);
"""

# Поля, добавленные после первых снимков. У старых строк в них NULL.
MIGRATIONS = {
    "vacancies": {
        "salary_gross": "INTEGER", "salary_mode": "TEXT", "salary_frequency": "TEXT",
        "employment": "TEXT", "schedule": "TEXT", "working_hours": "TEXT",
        "created_at": "TEXT", "changed_at": "TEXT",
        "employer_trusted": "INTEGER", "employer_accredited_it": "INTEGER",
        "employer_rating": "REAL", "employer_reviews": "INTEGER", "employer_category": "TEXT",
        "employer_hr_brand": "TEXT", "employer_has_logo": "INTEGER", "employer_has_site": "INTEGER",
        "branded": "INTEGER", "test_required": "INTEGER", "letter_required": "INTEGER",
        "accept_incomplete": "INTEGER", "accept_temporary": "INTEGER", "internship": "INTEGER",
        "night_shifts": "INTEGER", "side_job": "INTEGER", "labor_contract": "INTEGER",
        "chat": "TEXT", "auto_response": "INTEGER", "has_address": "INTEGER", "insider": "INTEGER",
        "show_contacts": "INTEGER", "labels": "TEXT", "tags": "TEXT", "creation_site": "TEXT",
    },
    "observations": {
        # Меняются со временем, поэтому лежат в наблюдении, а не в карточке вакансии.
        "responses": "INTEGER", "total_responses": "INTEGER", "manager_activity": "TEXT",
        # Запрос выдачи, в котором найдена вакансия (area=…&work_format=…&experience=…); позиция — внутри него.
        "query": "TEXT",
    },
}


def now() -> str:
    return datetime.now(MSK).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    conn.commit()
    return conn
