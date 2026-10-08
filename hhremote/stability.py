"""Устойчивость замера: тот же рейтинг по контрольному снимку через 30–60 минут.

Сравниваем основной снимок A и контрольный B по одинаковым правилам выборки:
  - профессии: корреляция Спирмена медиан и совпадение топ-10;
  - вакансии: корреляция Спирмена счётчика у вакансий, попавших в оба снимка.
Счётчик «смотрят» — мгновенный, поэтому у отдельной вакансии шум большой; у медиан профессий — малый.
"""
import pandas as pd

from . import analyze


def run(conn, cfg: dict, snap_a: int, snap_b: int) -> pd.DataFrame:
    a = analyze.prepare(conn, cfg, snap_a, None)
    b = analyze.prepare(conn, cfg, snap_b, None)
    pa = a["prof"].set_index("profession_id")
    pb = b["prof"].set_index("profession_id")
    both = pa[["profession", "industry", "n", "median", "mean"]].join(
        pb[["n", "median", "mean"]], rsuffix="_b", how="inner")
    rho_prof = both["median"].corr(both["median_b"], method="spearman")
    top_a = set(pa.sort_values(["median", "mean"], ascending=False).index[:10])
    top_b = set(pb.sort_values(["median", "mean"], ascending=False).index[:10])

    va = a["sample"][["profession_id", "vacancy_id", "viewers"]]
    vb = b["sample"][["profession_id", "vacancy_id", "viewers"]]
    v = va.merge(vb, on=["profession_id", "vacancy_id"], suffixes=("", "_b")).dropna()
    rho_vac = v["viewers"].corr(v["viewers_b"], method="spearman")

    ta, tb = a["snap"]["started_at"][11:16], b["snap"]["started_at"][11:16]
    print(f"Снимок #{snap_a} ({ta}) против #{snap_b} ({tb}):")
    print(f"  профессий в обоих: {len(both)}, Спирмен медиан: {rho_prof:.3f}, общих в топ-10: {len(top_a & top_b)} из 10")
    print(f"  вакансий в обоих: {len(v)}, Спирмен счётчика: {rho_vac:.3f}, "
          f"сумма «смотрят» {int(v['viewers'].sum())} → {int(v['viewers_b'].sum())}")
    out = cfg["paths"]["reports"] / f"stability_s{snap_a}_s{snap_b}.csv"
    both.reset_index().to_csv(out, sep=";", decimal=",", encoding="utf-8-sig", index=False)
    print(f"  по профессиям: {out}")
    return both
