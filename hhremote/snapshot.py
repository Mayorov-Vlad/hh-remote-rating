"""Снимок: выдача по каждой роли, счётчик «смотрят» берём прямо из выдачи.

Обычно хватает одной страницы (50 вакансий). Если в ней меньше sample_size чисто удалённых,
догружаются следующие страницы (до search.max_pages).

Порядок ролей перемешивается, чтобы дрейф активности за время снимка ложился на все профессии
равномерно. Прерванный снимок продолжается командой с --resume: готовые роли пропускаются.
"""
import random
import sys

from .client import BlockedError, Client, dup_key, is_pure_remote
from .db import now

VACANCY_FIELDS = (
    "name", "employer_id", "employer_name", "area_id", "area_name", "salary_from", "salary_to",
    "currency", "experience", "work_formats", "role_ids", "properties", "published_at", "url",
    "salary_gross", "salary_mode", "salary_frequency", "employment", "schedule", "working_hours",
    "created_at", "changed_at", "employer_trusted", "employer_accredited_it", "employer_rating",
    "employer_reviews", "employer_category", "employer_hr_brand", "employer_has_logo", "employer_has_site",
    "branded", "test_required", "letter_required", "accept_incomplete", "accept_temporary", "internship",
    "night_shifts", "side_job", "labor_contract", "chat", "auto_response", "has_address", "insider",
    "show_contacts", "labels", "tags", "creation_site",
)

UPSERT_VACANCY = (
    f"INSERT INTO vacancies(id, {', '.join(VACANCY_FIELDS)}, first_seen_at, last_seen_at) "
    f"VALUES (:id, {', '.join(':' + f for f in VACANCY_FIELDS)}, :seen, :seen) "
    f"ON CONFLICT(id) DO UPDATE SET {', '.join(f'{f} = excluded.{f}' for f in VACANCY_FIELDS)}, "
    "last_seen_at = excluded.last_seen_at"
)


def create(conn, kind: str, search_cfg: dict, notes: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO snapshots(kind, area_id, work_format, order_by, per_page, started_at, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (kind, search_cfg["area"], search_cfg["work_format"], search_cfg["order_by"],
         search_cfg["per_page"], now(), notes),
    )
    conn.commit()
    return cur.lastrowid


def fetch_role(client: Client, role_id: int, search_cfg: dict, target_pure: int) -> tuple[int, int, list[dict], list]:
    """Страницы выдачи по роли, пока не наберётся target_pure чисто удалённых вакансий
    (или не кончится выдача / лимит max_pages). Последний элемент — счётчики фильтров с первой страницы."""
    items: list[dict] = []
    page = 0
    clusters: list = []
    while True:
        status, found, batch = client.search(role_id, search_cfg, page)
        if page == 0:
            clusters = client.last_clusters
        items.extend(batch)
        page += 1
        # Считаем только уникальные (без копий одного работодателя), как и в анализе.
        n_pure = len({dup_key(v["employer_id"], v["employer_name"], v["name"])
                      for v in items if is_pure_remote(v["work_formats"])})
        if (n_pure >= target_pure or not batch or len(items) >= found
                or page >= search_cfg["max_pages"]):
            return status, found, items, clusters


def run(conn, client: Client, search_cfg: dict, snapshot_id: int, role_ids: list[int], target_pure: int,
        quiet: bool = False) -> None:
    done = {r[0] for r in conn.execute(
        "SELECT profession_id FROM search_results WHERE snapshot_id = ?", (snapshot_id,))}
    todo = [r for r in role_ids if r not in done]
    random.shuffle(todo)
    total = len(todo)
    if not quiet:
        print(f"Снимок #{snapshot_id}: ролей к обходу {total}, уже готово {len(done)}", flush=True)

    for i, role_id in enumerate(todo, 1):
        try:
            status, found, items, clusters = fetch_role(client, role_id, search_cfg, target_pure)
        except BlockedError as e:
            conn.execute("UPDATE snapshots SET notes = ? WHERE id = ?", (f"остановлен: {e}", snapshot_id))
            conn.commit()
            print(f"\nСТОП: {e}\nПродолжить позже: python -m hhremote <команда> --resume {snapshot_id}",
                  file=sys.stderr)
            raise
        ts = now()
        for v in items:
            conn.execute(UPSERT_VACANCY, {**v, "seen": ts})
            conn.execute(
                "INSERT OR REPLACE INTO vacancy_raw(id, seen_at, data) VALUES (?, ?, ?)", (v["id"], ts, v["raw"]))
            conn.execute(
                "INSERT OR REPLACE INTO observations(snapshot_id, profession_id, vacancy_id, rank, viewers, observed_at, "
                "responses, total_responses, manager_activity) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (snapshot_id, role_id, v["id"], v["rank"], v["viewers"], ts,
                 v["responses"], v["total_responses"], v["manager_activity"]),
            )
        conn.executemany(
            "INSERT OR REPLACE INTO search_clusters(snapshot_id, profession_id, cluster, value_id, title, count) "
            "VALUES (?, ?, ?, ?, ?, ?)", [(snapshot_id, role_id, *c) for c in clusters])
        conn.execute(
            "INSERT OR REPLACE INTO search_results(snapshot_id, profession_id, fetched_at, http_status, total_found, n_items, state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (snapshot_id, role_id, ts, status, found, len(items), "ok"),
        )
        conn.commit()
        if not quiet and (i % 10 == 0 or i == total):
            print(f"  {i}/{total}", flush=True)

    conn.execute("UPDATE snapshots SET finished_at = ? WHERE id = ?", (now(), snapshot_id))
    conn.commit()
