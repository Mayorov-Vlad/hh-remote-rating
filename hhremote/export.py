"""Выгрузка базы в CSV: все таблицы как есть + одна плоская таблица по снимку.

Формат под русский Excel: разделитель «;», десятичная запятая, UTF-8 с BOM.
Плоская таблица base_s{id}.csv — одна строка на наблюдение (вакансия в выдаче профессии)
со всеми полями вакансии, профессией, отраслью и пометками анализа: чисто удалённая, копия, в рейтинге.
"""
import json
import zlib

import pandas as pd

from . import analyze
from .client import dup_key, is_pure_remote

CSV = {"sep": ";", "decimal": ",", "encoding": "utf-8-sig", "index": False}
TABLES = ("industries", "professions", "profession_industries", "snapshots", "search_results", "vacancies", "observations")


def flat(conn, cfg: dict, snap_id: int) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT o.snapshot_id, o.observed_at, i.name AS industry, o.profession_id, p.name AS profession, "
        "       p.included AS profession_included, o.rank, o.viewers, o.responses, o.total_responses, "
        "       o.manager_activity, sr.total_found, v.* "
        "FROM observations o "
        "JOIN professions p ON p.id = o.profession_id "
        "LEFT JOIN industries i ON i.id = p.industry_id "
        "JOIN vacancies v ON v.id = o.vacancy_id "
        "JOIN search_results sr ON sr.snapshot_id = o.snapshot_id AND sr.profession_id = o.profession_id "
        "WHERE o.snapshot_id = ? ORDER BY i.name, p.name, o.rank",
        conn, params=(snap_id,),
    )
    df = df.rename(columns={"id": "vacancy_id", "name": "vacancy"})
    df["pure_remote"] = df["work_formats"].map(is_pure_remote).astype(int)
    keys = pd.Series([dup_key(e, n, t) for e, n, t in zip(df["employer_id"], df["employer_name"], df["vacancy"])],
                     index=df.index)
    df["is_dup"] = (df["pure_remote"].astype(bool)
                    & pd.DataFrame({"p": df["profession_id"], "k": keys, "r": df["pure_remote"]}).duplicated()).astype(int)
    # Та же выборка, что в рейтинге и дашборде.
    d = analyze.prepare(conn, cfg, snap_id, None)
    rated = set(zip(d["sample"]["profession_id"], d["sample"]["vacancy_id"]))
    df["in_rating"] = [int((p, v) in rated) for p, v in zip(df["profession_id"], df["vacancy_id"])]
    return df


def run(conn, cfg: dict, snapshot_id: int | None, raw: bool = False) -> None:
    out = cfg["paths"]["reports"] / "csv"
    out.mkdir(parents=True, exist_ok=True)
    for t in TABLES:
        pd.read_sql_query(f"SELECT * FROM {t}", conn).to_csv(out / f"{t}.csv", **CSV)
    snap_id = analyze._pick_snapshot(conn, snapshot_id)
    base = flat(conn, cfg, snap_id)
    path = out / f"base_s{snap_id}.csv"
    base.to_csv(path, **CSV)
    print(f"Таблицы: {', '.join(TABLES)} → {out}")
    print(f"Плоская база снимка #{snap_id}: {path} ({len(base)} строк, {base.shape[1]} столбцов, "
          f"в рейтинге {int(base['in_rating'].sum())})")
    if raw:
        rpath = out / "vacancy_raw.jsonl"
        with open(rpath, "w", encoding="utf-8") as f:
            for vid, seen, blob in conn.execute("SELECT id, seen_at, data FROM vacancy_raw ORDER BY id"):
                f.write(json.dumps({"id": vid, "seen_at": seen, "data": json.loads(zlib.decompress(blob))},
                                   ensure_ascii=False) + "\n")
        print(f"Сырые объекты вакансий: {rpath}")
