"""Пульс активности hh.ru: каждые N минут одна и та же панель запросов (первая страница выдачи
по нескольким крупным ролям, вся Россия, все форматы). Сумма счётчиков «смотрят» по выдаче —
индекс того, сколько людей сейчас сидит на hh. По нему выбираем время основного замера.

Каждый тик — снимок kind='pulse'. Отчёт: python -m hhremote pulse --report.
"""
import time
from datetime import datetime, timedelta

import pandas as pd

from . import snapshot
from .client import BlockedError
from .db import MSK

# Крупные профессии из отбора «могут работать удалённо», разные отрасли (id справочника hh). Меняются через --roles.
DEFAULT_ROLES = (70, 18, 132, 83, 48, 67, 66, 96, 68, 121, 110, 34)


def tick(conn, client, cfg: dict, roles) -> int:
    search_cfg = dict(cfg["search"], area=113, work_format="", max_pages=1)
    snap_id = snapshot.create(conn, "pulse", search_cfg)
    snapshot.run(conn, client, search_cfg, snap_id, list(roles), 1, quiet=True)
    return snap_id


def run(conn, client, cfg: dict, every_min: int, until: datetime, roles) -> None:
    print(f"Пульс каждые {every_min} мин до {until:%d.%m %H:%M} МСК, ролей {len(roles)}", flush=True)
    while datetime.now(MSK) < until:
        t0 = datetime.now(MSK)
        try:
            snap_id = tick(conn, client, cfg, roles)
        except BlockedError as e:
            print(f"СТОП: {e}", flush=True)
            return
        r = conn.execute("SELECT COUNT(*), SUM(viewers) FROM observations WHERE snapshot_id = ?", (snap_id,)).fetchone()
        print(f"{t0:%d.%m %H:%M} #{snap_id}: вакансий {r[0]}, смотрят {r[1]}", flush=True)
        # Следующий тик — на ближайшей отметке, кратной every_min.
        nxt = t0.replace(second=0, microsecond=0) + timedelta(minutes=every_min - t0.minute % every_min)
        time.sleep(max(1.0, (nxt - datetime.now(MSK)).total_seconds()))


def report(conn, cfg: dict) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT s.id, s.started_at, o.profession_id, o.vacancy_id, o.viewers FROM snapshots s "
        "JOIN observations o ON o.snapshot_id = s.id WHERE s.kind = 'pulse' AND s.finished_at IS NOT NULL",
        conn)
    if df.empty:
        raise SystemExit("Нет тиков пульса.")
    t = pd.to_datetime(df["started_at"]).dt.tz_convert(MSK)
    df["t"] = t.dt.floor("20min")
    # Индекс тика: сумма «смотрят» по одинаковой панели запросов, нормированная на среднее по всем тикам.
    g = df.groupby(["id", "t"]).agg(n=("viewers", "size"), viewers=("viewers", "sum")).reset_index()
    g["per_vacancy"] = g["viewers"] / g["n"]
    g["index"] = (g["per_vacancy"] / g["per_vacancy"].mean() * 100).round(1)
    g["weekday"] = g["t"].dt.day_name()
    g["hour"] = g["t"].dt.strftime("%H:%M")
    out = cfg["paths"]["reports"] / "pulse.csv"
    g.to_csv(out, sep=";", decimal=",", encoding="utf-8-sig", index=False)
    for r in g.itertuples():
        print(f"{r.t:%d.%m %a %H:%M}  индекс {r.index:6.1f}  {'█' * int(r.index / 4)}")
    top = g.sort_values("index", ascending=False).head(5)
    print("Пик:", ", ".join(f"{r.t:%d.%m %H:%M} ({r.index})" for r in top.itertuples()))
    print(f"Таблица: {out}")
    return g
