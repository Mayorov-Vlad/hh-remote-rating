"""Большой замер: все форматы работы по профессиям из отбора «могут работать удалённо».

hh отдаёт не больше 2000 вакансий на запрос, поэтому запросы по крупной профессии дробятся.
План на профессию строится по последней разведке без фильтра формата (discovery, work_format=''):
  1. «Только удалённо» по всей России — вся удалёнка, как в основном замере.
  2. Все форматы: если у профессии всего не больше 2000 вакансий — один запрос по России.
     Иначе, если по России каждая группа опыта не больше 2000, — 4 запроса по опыту (полный охват).
     Иначе — запрос по каждому региону из счётчиков hh (топ-19 регионов профессии);
     регион больше 2000 дробится по опыту (4 запроса); в конце запрос по всей России —
     первые 2000 по релевантности, это добирает часть вакансий из прочих регионов.
Вакансия, найденная несколькими запросами, пишется один раз — из первого запроса в порядке плана.
Позиция в выдаче считается внутри своего запроса, сам запрос пишется в observations.query.
Порядок профессий перемешивается; прерванный замер продолжается с --resume (по профессиям).
"""
import random
import sys

from .client import BlockedError, Client
from .db import now
from .snapshot import UPSERT_VACANCY

LIMIT = 2000
EXPERIENCE = ("noExperience", "between1And3", "between3And6", "moreThan6")


def discovery_id(conn) -> int:
    row = conn.execute("SELECT MAX(id) FROM snapshots WHERE kind = 'discovery' AND work_format = '' "
                       "AND finished_at IS NOT NULL").fetchone()
    if not row[0]:
        raise SystemExit("Нет разведки без фильтра формата: python -m hhremote discover --any-format --target 1 --max-pages 1")
    return row[0]


def plan(conn, role_id: int, disc_id: int) -> list[dict]:
    qs = [{"area": 113, "work_format": "REMOTE"}]
    total = conn.execute("SELECT total_found FROM search_results WHERE snapshot_id = ? AND profession_id = ?",
                         (disc_id, role_id)).fetchone()
    total = total[0] if total else LIMIT + 1
    if total <= LIMIT:
        return qs + [{"area": 113}]
    # Если по России каждая группа опыта не больше 2000 — дробим по опыту: полный охват и меньше запросов.
    exp = dict(conn.execute(
        "SELECT value_id, count FROM search_clusters WHERE snapshot_id = ? AND profession_id = ? AND cluster = 'experience'",
        (disc_id, role_id)).fetchall())
    if exp and max(exp.values()) <= LIMIT:
        return qs + [{"area": 113, "experience": e} for e in EXPERIENCE if exp.get(e)]
    regions = conn.execute(
        "SELECT value_id, count FROM search_clusters WHERE snapshot_id = ? AND profession_id = ? "
        "AND cluster = 'area' AND value_id <> '113' ORDER BY count DESC", (disc_id, role_id)).fetchall()
    for rid, cnt in regions:
        if cnt > LIMIT:
            qs += [{"area": int(rid), "experience": e} for e in EXPERIENCE]
        else:
            qs.append({"area": int(rid)})
    return qs + [{"area": 113}]


KEYS = ("area", "work_format", "experience", "employment_form", "industry", "work_schedule_by_days", "order_by")
# Порядок дробления запроса, который упёрся в 2000: опыт → формат → занятость → отрасль работодателя → график.
SPLIT = ("experience", "work_format", "employment_form", "industry", "work_schedule_by_days")


def qkey(q: dict) -> str:
    return "&".join(f"{k}={q[k]}" for k in KEYS if q.get(k))


def query_cfg(search_cfg: dict, q: dict) -> dict:
    cfg = dict(search_cfg)
    for k in KEYS:
        if k != "order_by":
            cfg[k] = q.get(k)
    cfg["work_format"] = q.get("work_format") or ""
    if q.get("order_by"):
        cfg["order_by"] = q["order_by"]
    return cfg


def parse_qkey(s: str) -> dict:
    q = dict(x.split("=", 1) for x in s.split("&") if x)
    q["area"] = int(q["area"])
    return q


def fetch_query(client: Client, role_id: int, search_cfg: dict, q: dict) -> tuple[int, int, list[dict]]:
    cfg = query_cfg(search_cfg, q)
    items: list[dict] = []
    page = 0
    while True:
        status, found, batch = client.search(role_id, cfg, page)
        items.extend(batch)
        page += 1
        if not batch or len(items) >= min(found, LIMIT) or page >= LIMIT // search_cfg["per_page"]:
            return status, found, items


def run(conn, client: Client, search_cfg: dict, snapshot_id: int, role_ids: list[int]) -> None:
    disc = discovery_id(conn)
    done = {r[0] for r in conn.execute("SELECT profession_id FROM search_results WHERE snapshot_id = ?", (snapshot_id,))}
    todo = [r for r in role_ids if r not in done]
    random.shuffle(todo)
    print(f"Снимок #{snapshot_id} (все форматы, разведка #{disc}): профессий к обходу {len(todo)}, уже готово {len(done)}",
          flush=True)
    for i, role_id in enumerate(todo, 1):
        seen: set[int] = set()
        n_req = 0
        # Профессию, прерванную на середине, собираем заново целиком.
        conn.execute("DELETE FROM observations WHERE snapshot_id = ? AND profession_id = ?", (snapshot_id, role_id))
        conn.execute("DELETE FROM search_queries WHERE snapshot_id = ? AND profession_id = ?", (snapshot_id, role_id))
        try:
            for q in plan(conn, role_id, disc):
                status, found, items = fetch_query(client, role_id, search_cfg, q)
                n_req += max(1, -(-len(items) // search_cfg["per_page"]))
                ts = now()
                for v in items:
                    if v["id"] in seen:
                        continue
                    seen.add(v["id"])
                    conn.execute(UPSERT_VACANCY, {**v, "seen": ts})
                    conn.execute("INSERT OR REPLACE INTO vacancy_raw(id, seen_at, data) VALUES (?, ?, ?)", (v["id"], ts, v["raw"]))
                    conn.execute(
                        "INSERT OR IGNORE INTO observations(snapshot_id, profession_id, vacancy_id, rank, viewers, observed_at, "
                        "responses, total_responses, manager_activity, query) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (snapshot_id, role_id, v["id"], v["rank"], v["viewers"], ts,
                         v["responses"], v["total_responses"], v["manager_activity"], qkey(q)))
                conn.execute(
                    "INSERT OR REPLACE INTO search_queries(snapshot_id, profession_id, query, fetched_at, total_found, n_items) "
                    "VALUES (?, ?, ?, ?, ?, ?)", (snapshot_id, role_id, qkey(q), ts, found, len(items)))
                conn.commit()
        except BlockedError as e:
            conn.rollback()
            conn.execute("UPDATE snapshots SET notes = ? WHERE id = ?", (f"остановлен: {e}", snapshot_id))
            conn.commit()
            print(f"\nСТОП: {e}\nПродолжить позже: python -m hhremote measure --full --resume {snapshot_id}", file=sys.stderr)
            raise
        total = conn.execute("SELECT total_found FROM search_results WHERE snapshot_id = ? AND profession_id = ?",
                             (disc, role_id)).fetchone()
        conn.execute(
            "INSERT OR REPLACE INTO search_results(snapshot_id, profession_id, fetched_at, http_status, total_found, n_items, state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", (snapshot_id, role_id, now(), 200, total[0] if total else None, len(seen), "ok"))
        conn.commit()
        print(f"  {i}/{len(todo)} роль {role_id}: вакансий {len(seen)}, запросов ~{n_req}", flush=True)

    conn.execute("UPDATE snapshots SET finished_at = ? WHERE id = ?", (now(), snapshot_id))
    conn.commit()


def _save(conn, snapshot_id, role_id, q, found, items, seen) -> int:
    ts = now()
    new = 0
    for v in items:
        if v["id"] in seen:
            continue
        seen.add(v["id"])
        new += 1
        conn.execute(UPSERT_VACANCY, {**v, "seen": ts})
        conn.execute("INSERT OR REPLACE INTO vacancy_raw(id, seen_at, data) VALUES (?, ?, ?)", (v["id"], ts, v["raw"]))
        conn.execute(
            "INSERT OR IGNORE INTO observations(snapshot_id, profession_id, vacancy_id, rank, viewers, observed_at, "
            "responses, total_responses, manager_activity, query) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (snapshot_id, role_id, v["id"], v["rank"], v["viewers"], ts,
             v["responses"], v["total_responses"], v["manager_activity"], qkey(q)))
    conn.execute(
        "INSERT OR REPLACE INTO search_queries(snapshot_id, profession_id, query, fetched_at, total_found, n_items) "
        "VALUES (?, ?, ?, ?, ?, ?)", (snapshot_id, role_id, qkey(q), ts, found, len(items)))
    conn.commit()
    return new


def _deep(conn, client, search_cfg, snapshot_id, role_id, q, seen, stats, done_keys) -> None:
    """Запрос; если он больше 2000 — дробим по счётчикам hh этого же запроса, рекурсивно."""
    if qkey(q) in done_keys:
        return
    done_keys.add(qkey(q))
    cfg = query_cfg(search_cfg, q)
    status, found, first = client.search(role_id, cfg, 0)
    clusters = client.last_clusters
    stats["req"] += 1
    if found <= LIMIT:
        items = first
        page = 1
        while len(items) < found and page < LIMIT // search_cfg["per_page"]:
            _, _, batch = client.search(role_id, cfg, page)
            stats["req"] += 1
            if not batch:
                break
            items += batch
            page += 1
        stats["new"] += _save(conn, snapshot_id, role_id, q, found, items, seen)
        return
    _save(conn, snapshot_id, role_id, q, found, first, seen)
    dim = next((d for d in SPLIT if d not in q), None)
    if dim is None:
        # Дробить больше нечем: берём ту же выдачу ещё и по дате — это другие 2000 вакансий.
        for order in ("relevance", "publication_time"):
            qq = dict(q, order_by=order)
            if qkey(qq) in done_keys:
                continue
            done_keys.add(qkey(qq))
            status, f2, items = fetch_query(client, role_id, search_cfg, qq)
            stats["req"] += -(-len(items) // search_cfg["per_page"])
            stats["new"] += _save(conn, snapshot_id, role_id, qq, f2, items, seen)
        return
    for name, vid, _title, cnt in clusters:
        if name == dim and cnt:
            _deep(conn, client, search_cfg, snapshot_id, role_id, dict(q, **{dim: vid}), seen, stats, done_keys)


def topup(conn, client: Client, search_cfg: dict, snapshot_id: int, min_cover: float = 0.98) -> None:
    """Докачка снимка all-format: только непокрытые части профессий с охватом меньше min_cover —
    запросы, упёршиеся в 2000 (дробим глубже), и регионы, которые ещё не запрашивались."""
    disc = discovery_id(conn)
    roles = conn.execute(
        "SELECT s.profession_id, d.total_found, s.n_items FROM search_results s JOIN search_results d "
        "ON d.profession_id = s.profession_id AND d.snapshot_id = ? WHERE s.snapshot_id = ? AND s.n_items < d.total_found * ?",
        (disc, snapshot_id, min_cover)).fetchall()
    regions = [r[0] for r in conn.execute("SELECT id FROM areas WHERE parent_id = 113 ORDER BY id")]
    print(f"Докачка снимка #{snapshot_id}: профессий {len(roles)}, не хватает ~{sum(t - n for _, t, n in roles)} вакансий",
          flush=True)
    random.shuffle(roles)
    for i, (role_id, total, n0) in enumerate(roles, 1):
        seen = {r[0] for r in conn.execute("SELECT vacancy_id FROM observations WHERE snapshot_id = ? AND profession_id = ?",
                                           (snapshot_id, role_id))}
        rows = conn.execute("SELECT query, total_found, n_items FROM search_queries WHERE snapshot_id = ? AND profession_id = ?",
                            (snapshot_id, role_id)).fetchall()
        done_keys = {q for q, _, _ in rows}
        queried_areas = {parse_qkey(q)["area"] for q, _, _ in rows}
        stats = {"req": 0, "new": 0}
        try:
            # 1) запросы, упёршиеся в лимит (кроме общего по России — его покрывают регионы)
            for q, found, n in rows:
                pq = parse_qkey(q)
                if found and n < found and not (pq["area"] == 113 and len(pq) == 1) and "order_by" not in pq:
                    done_keys.discard(q)
                    _deep(conn, client, search_cfg, snapshot_id, role_id, pq, seen, stats, done_keys)
            # 2) регионы, которые ещё не запрашивались (только если профессию уже собирали по регионам)
            for rid in regions if queried_areas - {113} else []:
                if rid not in queried_areas:
                    _deep(conn, client, search_cfg, snapshot_id, role_id, {"area": rid}, seen, stats, done_keys)
        except BlockedError as e:
            conn.execute("UPDATE snapshots SET notes = ? WHERE id = ?", (f"докачка остановлена: {e}", snapshot_id))
            conn.commit()
            print(f"\nСТОП: {e}\nПродолжить: python -m hhremote measure --topup {snapshot_id}", file=sys.stderr)
            raise
        conn.execute("UPDATE search_results SET n_items = ?, fetched_at = ? WHERE snapshot_id = ? AND profession_id = ?",
                     (len(seen), now(), snapshot_id, role_id))
        conn.commit()
        print(f"  {i}/{len(roles)} роль {role_id}: +{stats['new']} вакансий (было {n0}, стало {len(seen)} из {total}), "
              f"запросов {stats['req']}", flush=True)
    conn.execute("UPDATE snapshots SET finished_at = ?, notes = ? WHERE id = ?",
                 (now(), f"докачка крупных профессий до {now()[11:16]}", snapshot_id))
    conn.commit()
