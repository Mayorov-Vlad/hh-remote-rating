"""Этап 2: отбор удалённых профессий по последнему discovery-снимку.

Автоправило: роль входит в исследование («может работать удалённо»), если в выдаче нашлось
>= min_pure_remote уникальных вакансий с форматом «только удалённо» И они составляют >= min_pure_share
всех вакансий профессии на hh (все форматы, по последней разведке без фильтра формата).
Смешанные форматы (офис/гибрид/удалённо на выбор, разъезды) не считаются удалёнкой.
Ручные решения лежат в curation.csv (разделитель «;», UTF-8) и применяются поверх автоправила:
  profession_id;industry_id;included;reason
Пустое поле = оставить автоматическое значение.
"""
import csv

import pandas as pd

from .client import dup_key, is_pure_remote


def _latest(conn, kind: str) -> int:
    row = conn.execute("SELECT MAX(id) FROM snapshots WHERE kind = ? AND finished_at IS NOT NULL", (kind,)).fetchone()
    if not row[0]:
        raise SystemExit(f"Нет завершённого снимка {kind}.")
    return row[0]


def load_overrides(path) -> dict[int, dict]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        return {int(r["profession_id"]): r for r in csv.DictReader(f, delimiter=";") if r.get("profession_id")}


def review_table(conn, snap_id: int) -> pd.DataFrame:
    obs = pd.read_sql_query(
        "SELECT o.profession_id, o.rank, v.work_formats, v.role_ids, v.employer_id, v.employer_name, v.name "
        "FROM observations o JOIN vacancies v ON v.id = o.vacancy_id WHERE o.snapshot_id = ?",
        conn, params=(snap_id,),
    )
    obs["pure_remote"] = obs["work_formats"].map(is_pure_remote)
    obs["key"] = [dup_key(e, n, t) for e, n, t in zip(obs["employer_id"], obs["employer_name"], obs["name"])]
    obs = obs.sort_values(["profession_id", "rank"])
    obs["pure_unique"] = obs["pure_remote"] & ~obs.duplicated(["profession_id", "key", "pure_remote"])
    obs["role_match"] = [str(p) in r.split(",") for p, r in zip(obs["profession_id"], obs["role_ids"])]
    agg = obs.groupby("profession_id").agg(
        fetched=("rank", "count"),
        pure_count=("pure_unique", "sum"),
        pure_share=("pure_remote", "mean"),
        role_match_share=("role_match", "mean"),
    ).reset_index()

    prof = pd.read_sql_query(
        "SELECT p.id AS profession_id, p.name AS profession, p.industry_id, i.name AS industry, "
        "       sr.total_found, "
        "       (SELECT GROUP_CONCAT(i2.name, ' | ') FROM profession_industries pi "
        "          JOIN industries i2 ON i2.id = pi.industry_id WHERE pi.profession_id = p.id) AS all_industries "
        "FROM professions p JOIN industries i ON i.id = p.industry_id "
        "LEFT JOIN search_results sr ON sr.profession_id = p.id AND sr.snapshot_id = ?",
        conn, params=(snap_id,),
    )
    df = prof.merge(agg, on="profession_id", how="left")
    # Всего вакансий профессии во всех форматах — из последней разведки без фильтра формата.
    row = conn.execute("SELECT MAX(id) FROM snapshots WHERE kind = 'discovery' AND work_format = '' "
                       "AND finished_at IS NOT NULL").fetchone()
    if row[0]:
        allf = pd.read_sql_query("SELECT profession_id, total_found AS total_all FROM search_results WHERE snapshot_id = ?",
                                 conn, params=(row[0],))
        df = df.merge(allf, on="profession_id", how="left")
    else:
        df["total_all"] = None
    df[["fetched", "pure_count"]] = df[["fetched", "pure_count"]].fillna(0).astype(int)
    df["total_found"] = df["total_found"].fillna(0).astype(int)
    return df


def run(conn, cfg: dict, snapshot_id: int | None = None) -> None:
    snap_id = snapshot_id or _latest(conn, "discovery")
    min_pure = cfg["curation"]["min_pure_remote"]
    sample_size = cfg["analysis"]["sample_size"]
    overrides = load_overrides(cfg["paths"]["curation"])
    names = dict(conn.execute("SELECT id, name FROM industries").fetchall())

    min_share = cfg["curation"].get("min_pure_share", 0)
    df = review_table(conn, snap_id)
    for row in df.itertuples():
        share = row.pure_count / row.total_all if row.total_all and row.total_all == row.total_all else None
        included = int(row.pure_count >= min_pure and (share is None or share >= min_share))
        if row.pure_count < min_pure:
            reason = f"мало чисто удалённых вакансий ({row.pure_count} < {min_pure}; всего с отметкой REMOTE {row.total_found})"
        elif not included:
            reason = (f"удалёнка — редкое исключение: {row.pure_count} из {int(row.total_all)} вакансий "
                      f"({share:.1%} < {min_share:.0%})")
        else:
            reason = None
        industry_id = row.industry_id
        o = overrides.get(row.profession_id)
        if o:
            if o.get("industry_id"):
                industry_id = int(o["industry_id"])
            if o.get("included") not in (None, ""):
                included = int(o["included"])
                reason = None if included else (o.get("reason") or reason)
        conn.execute(
            "UPDATE professions SET included = ?, exclude_reason = ?, industry_id = ? WHERE id = ?",
            (included, reason, int(industry_id), row.profession_id),
        )
    conn.commit()

    df = review_table(conn, snap_id)
    flags = pd.read_sql_query("SELECT id AS profession_id, included, exclude_reason FROM professions", conn)
    df = df.merge(flags, on="profession_id").sort_values(["industry", "pure_count"], ascending=[True, False])
    df["incomplete"] = (df["included"] == 1) & (df["pure_count"] < sample_size)
    df["pure_share_all"] = df["pure_count"] / df["total_all"]

    out = cfg["paths"]["reports"]
    out.mkdir(parents=True, exist_ok=True)
    cols = {
        "industry": "Отрасль", "profession_id": "ID роли", "profession": "Профессия",
        "pure_count": "Чисто удалённых (найдено)", "total_found": "Всего с отметкой REMOTE",
        "total_all": "Всего вакансий (все форматы)", "pure_share_all": "Доля чисто удалённых от всех",
        "fetched": "Просмотрено в выдаче", "pure_share": "Доля чисто удалённых",
        "incomplete": f"Неполная выборка (<{sample_size})", "role_match_share": "Доля вакансий с этой ролью",
        "all_industries": "Все категории hh", "included": "В исследовании", "exclude_reason": "Причина исключения",
    }
    view = df[list(cols)].rename(columns=cols)
    path = out / "professions_review.xlsx"
    with pd.ExcelWriter(path) as xw:
        view[view["В исследовании"] == 1].to_excel(xw, sheet_name="Включены", index=False)
        view[view["В исследовании"] == 0].to_excel(xw, sheet_name="Исключены", index=False)

    inc = df[df["included"] == 1]
    print(f"Снимок #{snap_id}. В исследовании: {len(inc)} из {len(df)} профессий, "
          f"{inc['industry'].nunique()} отраслей из {len(names)}. "
          f"Неполная выборка (<{sample_size} чисто удалённых): {int(inc['incomplete'].sum())}.")
    for ind, g in inc.groupby("industry"):
        print(f"  {ind}: {len(g)}")
    print(f"Таблица на проверку: {path}")
