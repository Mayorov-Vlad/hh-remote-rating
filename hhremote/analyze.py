"""Этап 4: рейтинги профессий и отраслей по счётчику «смотрят» и выгрузка в Excel/CSV.

Рейтинг в Excel — всегда по чисто удалённым вакансиям. Дашборд по снимку всех форматов
(measure --full) берёт formats="all": все вакансии профессии, формат — фильтром.

Выборка профессии — первые N вакансий выдачи hh (N = sample_size); при pure_remote_only
берутся только вакансии с форматом «только удалённо», в порядке выдачи.
Основная метрика профессии — медиана счётчика по выборке.
Профессия с выборкой меньше min_pure_remote на момент замера в рейтинг не идёт (лист «Проверка удалёнки»).
Одна вакансия может стоять в выдаче нескольких ролей; в каждой роли она считается отдельно.
"""
from datetime import datetime

import pandas as pd

from .client import dup_key, format_class, is_pure_remote
from .db import MSK


def _pick_snapshot(conn, snapshot_id: int | None) -> int:
    if snapshot_id:
        return snapshot_id
    row = conn.execute("SELECT MAX(id) FROM snapshots WHERE kind = 'measure' AND finished_at IS NOT NULL").fetchone()
    if not row[0]:
        raise SystemExit("Нет завершённого measure-снимка. Укажи --snapshot явно.")
    return row[0]


def load(conn, snap_id: int) -> pd.DataFrame:
    """Все наблюдения снимка по включённым профессиям, в порядке выдачи."""
    df = pd.read_sql_query(
        "SELECT o.profession_id, p.name AS profession, i.name AS industry, o.rank, o.viewers, o.observed_at, "
        "       v.id AS vacancy_id, v.name AS vacancy, v.employer_id, v.employer_name, v.work_formats, v.properties, "
        "       v.experience, v.area_id, v.area_name, o.query, v.salary_gross, v.employment, v.schedule, v.working_hours, "
        "       v.employer_trusted, v.employer_accredited_it, v.employer_rating, v.employer_reviews, "
        "       v.employer_hr_brand, v.employer_category, v.branded, v.test_required, v.letter_required, v.accept_incomplete, "
        "       o.responses, o.total_responses, o.manager_activity, "
        "       v.published_at, v.url, v.salary_from, v.salary_to, v.currency, sr.total_found "
        "FROM observations o "
        "JOIN professions p ON p.id = o.profession_id "
        "JOIN industries i ON i.id = p.industry_id "
        "JOIN vacancies v ON v.id = o.vacancy_id "
        "JOIN search_results sr ON sr.snapshot_id = o.snapshot_id AND sr.profession_id = o.profession_id "
        "WHERE o.snapshot_id = ? AND p.included = 1 "
        "ORDER BY o.profession_id, o.rank",
        conn, params=(snap_id,),
    )
    df["has_remote"] = df["work_formats"].str.split(",").map(lambda f: "REMOTE" in f)
    df["pure_remote"] = df["work_formats"].map(is_pure_remote)
    df["fmt"] = df["work_formats"].map(format_class)
    # Копия удалённой вакансии — тот же работодатель и название (город не важен).
    # У офисных и гибридных добавляем регион: одна вакансия сети в 50 городах — это 50 рабочих мест.
    df["dup_key"] = [dup_key(e, n, t) + ("" if pure else f"|{a}")
                     for e, n, t, a, pure in zip(df["employer_id"], df["employer_name"], df["vacancy"],
                                                 df["area_id"], df["pure_remote"])]
    obs_t = pd.to_datetime(df["observed_at"], utc=True)
    pub_t = pd.to_datetime(df["published_at"], utc=True, errors="coerce")
    df["age_days"] = (obs_t - pub_t).dt.total_seconds() / 86400
    return df


def mark_dups(pool: pd.DataFrame) -> pd.DataFrame:
    """is_dup = копия: тот же работодатель и название уже встречались выше в выдаче этой профессии."""
    pool = pool.sort_values(["profession_id", "rank"]).copy()
    pool["is_dup"] = pool.duplicated(["profession_id", "dup_key"], keep="first")
    return pool


def take_sample(df: pd.DataFrame, top: int | None, pure_only: bool, dedupe: bool = True,
                all_formats: bool = False) -> pd.DataFrame:
    # Страховка от сбоя фильтра hh: в удалённый рейтинг вакансия без REMOTE не идёт никогда.
    pool = mark_dups(df if all_formats else df[df["pure_remote"]] if pure_only else df[df["has_remote"]])
    if dedupe:
        pool = pool[~pool["is_dup"]].copy()
    pool["sample_rank"] = pool.groupby("profession_id").cumcount() + 1
    return pool if top is None else pool[pool["sample_rank"] <= top]


def dropped_dups(df: pd.DataFrame, sample: pd.DataFrame, pure_only: bool) -> pd.DataFrame:
    """Копии, отброшенные из выборки: в пределах той же глубины выдачи, что и выборка профессии."""
    pool = mark_dups(df[df["pure_remote"]] if pure_only else df[df["has_remote"]])
    depth = sample.groupby("profession_id")["rank"].max().rename("depth")
    d = pool[pool["is_dup"]].merge(depth, on="profession_id")
    return d[d["rank"] <= d["depth"]].drop(columns="depth")


def remote_check(df: pd.DataFrame, sample: pd.DataFrame, top: int, min_n: int) -> pd.DataFrame:
    g = df.groupby(["industry", "profession_id", "profession"])
    r = g.agg(
        total_found=("total_found", "first"),
        fetched=("vacancy_id", "count"),
        with_remote=("has_remote", "sum"),
        pure=("pure_remote", "sum"),
    ).reset_index()
    n = sample.groupby("profession_id").size().rename("n")
    r = r.merge(n, on="profession_id", how="left").fillna({"n": 0})
    r["n"] = r["n"].astype(int)
    r["status"] = r["n"].map(
        lambda k: "полная выборка" if k >= top else (f"неполная выборка ({k})" if k >= min_n else f"выбыла: {k} < {min_n}"))
    return r.sort_values(["industry", "pure"], ascending=[True, False])


def profession_rating(sample: pd.DataFrame, top: int) -> pd.DataFrame:
    g = sample.groupby(["industry", "profession_id", "profession"])
    r = g["viewers"].agg(
        n="count", median="median", mean="mean",
        p25=lambda s: s.quantile(0.25), p75=lambda s: s.quantile(0.75), max="max",
        zero_share=lambda s: (s == 0).mean(),
    ).reset_index()
    r["total_found"] = g["total_found"].first().values
    r["incomplete"] = (r["n"] < top) if top else False
    r = r.sort_values(["median", "mean"], ascending=False).reset_index(drop=True)
    r.insert(0, "place", r.index + 1)
    r["place_in_industry"] = r.groupby("industry")["median"].rank(method="min", ascending=False).astype(int)
    return r


def industry_rating(sample: pd.DataFrame, prof: pd.DataFrame) -> pd.DataFrame:
    by_prof = prof.groupby("industry").agg(
        professions=("profession_id", "count"),
        median_of_medians=("median", "median"),
    )
    pooled = sample.groupby("industry")["viewers"].agg(pooled_median="median", pooled_mean="mean", vacancies="count")
    leader = prof.sort_values("median", ascending=False).groupby("industry").first()[["profession", "median"]]
    leader.columns = ["leader", "leader_median"]
    r = by_prof.join(pooled).join(leader).reset_index()
    r = r.sort_values(["median_of_medians", "pooled_median"], ascending=False).reset_index(drop=True)
    r.insert(0, "place", r.index + 1)
    return r


def prepare(conn, cfg: dict, snapshot_id: int | None, top: int | None, formats: str = "pure") -> dict:
    """Общая подготовка для Excel, HTML и дашборда: выборка, копии, проверка удалёнки, рейтинги.
    formats="all" — выборка из всех форматов работы (для снимка measure --full); проверка удалёнки
    и отсев профессий всё равно по чисто удалённым."""
    snap_id = _pick_snapshot(conn, snapshot_id)
    top = top or cfg["analysis"]["sample_size"]
    pure_only = cfg["analysis"]["pure_remote_only"]
    dedupe = cfg["analysis"].get("dedupe", True)
    df = load(conn, snap_id)
    if df.empty:
        raise SystemExit(f"В снимке #{snap_id} нет наблюдений по включённым профессиям.")
    sample = take_sample(df, top, pure_only, dedupe)
    check = remote_check(df, sample, top, cfg["curation"]["min_pure_remote"])
    kept = check.loc[~check["status"].str.startswith("выбыла"), "profession_id"]
    sample = sample[sample["profession_id"].isin(kept)]
    dups = dropped_dups(df, sample, pure_only) if dedupe else df.iloc[0:0]
    if formats == "all":
        sample = take_sample(df, None, False, dedupe, all_formats=True)
        sample = sample[sample["profession_id"].isin(kept)]
        dups = mark_dups(df)
        dups = dups[dups["is_dup"] & dups["profession_id"].isin(kept)] if dedupe else df.iloc[0:0]
        top = None
    prof = profession_rating(sample, top)
    ind = industry_rating(sample, prof)
    snap = dict(conn.execute("SELECT * FROM snapshots WHERE id = ?", (snap_id,)).fetchone())
    return {"snap_id": snap_id, "snap": snap, "top": top, "pure_only": pure_only, "dedupe": dedupe, "formats": formats,
            "df": df, "sample": sample, "dups": dups, "check": check, "prof": prof, "ind": ind}


def diagnostics(conn, df: pd.DataFrame, sample: pd.DataFrame, check: pd.DataFrame,
                snap_id: int, top: int, pure_only: bool, dups: pd.DataFrame) -> pd.DataFrame:
    s = conn.execute("SELECT * FROM snapshots WHERE id = ?", (snap_id,)).fetchone()
    rows = [
        ("Снимок", f"#{snap_id} ({s['kind']})"),
        ("Начало", s["started_at"]),
        ("Конец", s["finished_at"]),
        ("Регион (area)", s["area_id"]),
        ("Выборка на профессию", f"первые {top} " + ("чисто удалённых" if pure_only else "с отметкой REMOTE")),
        ("Профессий в рейтинге", sample["profession_id"].nunique()),
        ("Профессий с неполной выборкой", int(check["status"].str.startswith("неполная").sum())),
        ("Профессий выбыло (мало удалённых на момент замера)", int(check["status"].str.startswith("выбыла").sum())),
        ("Вакансий в выдаче без REMOTE (сбой фильтра hh)", int((~df["has_remote"]).sum())),
        ("Копий отброшено (тот же работодатель и название в профессии)", len(dups)),
        ("Одна вакансия (id) в нескольких профессиях", int((sample.groupby("vacancy_id")["profession_id"].nunique() > 1).sum())),
        ("Наблюдений в рейтинге", len(sample)),
        ("Уникальных вакансий в рейтинге", sample["vacancy_id"].nunique()),
        ("Корреляция Спирмена: позиция в выдаче ~ смотрят", round(sample["rank"].corr(sample["viewers"], method="spearman"), 3)),
        ("Корреляция Спирмена: возраст вакансии ~ смотрят", round(sample["age_days"].corr(sample["viewers"], method="spearman"), 3)),
        ("Медиана возраста вакансии, дней", round(sample["age_days"].median(), 1)),
    ]
    return pd.DataFrame(rows, columns=["Показатель", "Значение"])


PROF_COLS = {
    "place": "Место", "industry": "Отрасль", "profession": "Профессия", "median": "Медиана смотрят",
    "mean": "Среднее", "p25": "P25", "p75": "P75", "max": "Максимум", "zero_share": "Доля нулей",
    "n": "Вакансий в выборке", "incomplete": "Неполная выборка", "total_found": "Всего с отметкой REMOTE",
    "place_in_industry": "Место в отрасли", "profession_id": "ID роли",
}
IND_COLS = {
    "place": "Место", "industry": "Отрасль", "median_of_medians": "Медиана медиан профессий",
    "pooled_median": "Медиана по всем вакансиям", "pooled_mean": "Среднее по всем вакансиям",
    "professions": "Профессий", "vacancies": "Вакансий", "leader": "Лидер отрасли",
    "leader_median": "Медиана лидера",
}
CHECK_COLS = {
    "industry": "Отрасль", "profession": "Профессия", "status": "Статус",
    "n": "Вакансий в выборке", "pure": "Чисто удалённых в просмотренной выдаче",
    "with_remote": "С отметкой REMOTE в просмотренной выдаче", "fetched": "Просмотрено вакансий",
    "total_found": "Всего с отметкой REMOTE", "profession_id": "ID роли",
}
RAW_COLS = {
    "industry": "Отрасль", "profession": "Профессия", "sample_rank": "Номер в выборке",
    "rank": "Позиция в выдаче hh", "viewers": "Смотрят", "vacancy": "Вакансия", "employer_name": "Работодатель",
    "work_formats": "Формат", "published_at": "Опубликована", "age_days": "Возраст, дней",
    "properties": "Тип размещения", "observed_at": "Время замера", "url": "Ссылка",
}


def run(conn, cfg: dict, snapshot_id: int | None, top: int | None) -> None:
    d = prepare(conn, cfg, snapshot_id, top)
    snap_id, top, pure_only = d["snap_id"], d["top"], d["pure_only"]
    df, sample, check, prof, ind, dups = d["df"], d["sample"], d["check"], d["prof"], d["ind"], d["dups"]
    diag = diagnostics(conn, df, sample, check, snap_id, top, pure_only, dups)

    out = cfg["paths"]["reports"]
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(MSK).strftime("%Y-%m-%d")
    base = out / f"rating_s{snap_id}_top{top}_{stamp}"
    prof_view = prof[list(PROF_COLS)].rename(columns=PROF_COLS)
    ind_view = ind[list(IND_COLS)].rename(columns=IND_COLS)
    by_ind = prof.sort_values(["industry", "place_in_industry"])[list(PROF_COLS)].rename(columns=PROF_COLS)
    check_view = check[list(CHECK_COLS)].rename(columns=CHECK_COLS)
    raw = sample.sort_values(["industry", "profession", "sample_rank"])[list(RAW_COLS)].rename(columns=RAW_COLS)
    raw["Опубликована"] = raw["Опубликована"].str[:16].str.replace("T", " ")
    with pd.ExcelWriter(f"{base}.xlsx") as xw:
        prof_view.to_excel(xw, sheet_name="Профессии", index=False)
        ind_view.to_excel(xw, sheet_name="Отрасли", index=False)
        by_ind.to_excel(xw, sheet_name="Профессии по отраслям", index=False)
        check_view.to_excel(xw, sheet_name="Проверка удалёнки", index=False)
        raw.to_excel(xw, sheet_name="Сырые данные", index=False)
        dup_cols = {k: v for k, v in RAW_COLS.items() if k != "sample_rank"}
        dups.sort_values(["industry", "profession", "rank"])[list(dup_cols)].rename(columns=dup_cols)             .to_excel(xw, sheet_name="Копии (исключены)", index=False)
        diag.to_excel(xw, sheet_name="О снимке", index=False)
    prof_view.to_csv(f"{base}_professions.csv", index=False, sep=";", encoding="utf-8-sig")
    ind_view.to_csv(f"{base}_industries.csv", index=False, sep=";", encoding="utf-8-sig")

    print(diag.to_string(index=False))
    dropped = check[check["status"].str.startswith("выбыла")]
    if len(dropped):
        print("\nВыбыли (мало чисто удалённых на момент замера):")
        print(dropped[["profession", "status"]].to_string(index=False))
    print("\nТоп-20 профессий по медиане «смотрят»:")
    print(prof.head(20)[["place", "profession", "industry", "median", "mean", "n"]].to_string(index=False))
    print("\nОтрасли:")
    print(ind[["place", "industry", "median_of_medians", "pooled_median", "professions"]].to_string(index=False))
    print(f"\nФайлы: {base}.xlsx и CSV рядом")
