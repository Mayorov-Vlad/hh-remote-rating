"""Интерактивный дашборд: рейтинг профессий и вакансий по счётчику «смотрят» с фильтрами и графиками.

Дизайн — по скиллу ui-ux-pro-max (стиль Data-Dense Dashboard, Fira Sans/Fira Code, синий + янтарный);
графики — SVG без библиотек, палитры проверены валидатором dataviz.

Данные по каждой вакансии вшиваются в страницу как JSON; все рейтинги пересчитываются в браузере
под выбранные фильтры (отрасль, опыт, зарплата, свежесть, глубина выдачи, копии, поиск).
fragment=True — без <html>/<head>/<body>, для публикации как Artifact.
"""
import base64
import gzip
import json
import math
from datetime import date, datetime

import pandas as pd

from . import analyze
from .db import MSK
from .report_html import MONTHS_FULL

TITLE = "Рейтинг удалённых профессий"
AREA_NAMES = {113: "Вся Россия", 1: "Москва", 2: "Санкт-Петербург"}


def _int(x):
    return None if x is None or pd.isna(x) else int(x)


def _str(x):
    return None if x is None or (isinstance(x, float) and pd.isna(x)) else str(x)


# Тип размещения на hh (из vacancyProperties): 0 Премиум, 1 Стандарт+, 2 Стандарт, 3 только Зарплата.ру, 4 нет данных.
def placement(props) -> int:
    p = set((_str(props) or "").split(","))
    if "HH_PREMIUM" in p:
        return 0
    if "HH_STANDARD_PLUS" in p:
        return 1
    if "HH_STANDARD" in p:
        return 2
    if any(x.startswith("ZP_") for x in p):
        return 3
    return 4


# Битовые флаги вакансии; расшифровка — FLAGS в JS дашборда (тот же порядок).
FLAG_BITS = (
    ("it", lambda v: v.employer_accredited_it == 1),
    ("hr_brand", lambda v: bool(_str(v.employer_hr_brand))),
    ("branded", lambda v: v.branded == 1),
    ("test", lambda v: v.test_required == 1),
    ("letter", lambda v: v.letter_required == 1),
    ("promo", lambda v: bool({"HH_CLICKME_BOOST", "HH_CLICKME_ACTIVE", "HH_AUCTION"} & set((_str(v.properties) or "").split(",")))),
    ("anonymous", lambda v: bool({"HH_ANONYMOUS", "ZP_ANONYMOUS"} & set((_str(v.properties) or "").split(",")))),
    ("antifraud", lambda v: "HH_ANTIFRAUD" in (_str(v.properties) or "")),
    ("trusted", lambda v: v.employer_trusted == 1),
    ("online", lambda v: _str(v.manager_activity) == "online"),
    ("no_resume", lambda v: v.accept_incomplete == 1),
    ("gross", lambda v: v.salary_gross == 1),
    ("agency", lambda v: _str(v.employer_category) in ("AGENCY", "PRIVATE_RECRUITER")),
)


def flags(v) -> int:
    return sum(1 << i for i, (_, test) in enumerate(FLAG_BITS) if test(v))


def _when(ts: str) -> str:
    t = datetime.fromisoformat(ts).astimezone(MSK)
    return f"{t.day} {MONTHS_FULL[t.month - 1]} {t.year}, {t:%H:%M}"


def build_data(conn, cfg: dict, snapshot_id: int | None, top: int | None) -> dict:
    snap_id = analyze._pick_snapshot(conn, snapshot_id)
    full = conn.execute("SELECT work_format FROM snapshots WHERE id = ?", (snap_id,)).fetchone()[0] == ""
    d = analyze.prepare(conn, cfg, snap_id, top, formats="all" if full else "pure")
    sample, dups, snap = d["sample"], d["dups"], d["snap"]
    profs = (sample.groupby(["profession_id", "profession", "industry"])
             .agg(total_found=("total_found", "first")).reset_index())
    industries = sorted(profs["industry"].unique())
    ind_idx = {name: i for i, name in enumerate(industries)}
    prof_idx = {pid: i for i, pid in enumerate(profs["profession_id"])}
    snap_day = date.fromisoformat(snap["started_at"][:10])
    region_of = {int(a): (int(r), n) for a, r, n in conn.execute("SELECT id, region_id, region_name FROM areas") if r}
    region_n: dict[int, list] = {}

    # Повторяющиеся строки (названия, работодатели, регионы…) — словарями, в строках вакансий только номера.
    lookups = {k: [] for k in ("titles", "employers", "currencies", "experience", "areas", "employment", "schedule", "hours")}
    index = {k: {} for k in lookups}

    def idx(kind, value):
        value = _str(value) or ""
        if value not in index[kind]:
            index[kind][value] = len(lookups[kind])
            lookups[kind].append(value)
        return index[kind][value]

    rows = []
    for src, is_dup in ((sample, 0), (dups, 1)):
        for v in src.itertuples():
            if v.profession_id not in prof_idx:
                continue
            # Отбрасываем знаки, а не округляем: границы корзин возраста (< 1 дня и т.д.) не должны сдвигаться.
            age = None if pd.isna(v.age_days) else math.floor(float(v.age_days) * 100) / 100
            rating = None if v.employer_rating is None or pd.isna(v.employer_rating) else round(float(v.employer_rating), 1)
            pub = _str(v.published_at)
            reg = region_of.get(int(v.area_id)) if v.area_id is not None and not pd.isna(v.area_id) else None
            if reg:
                region_n.setdefault(reg[0], [reg[0], reg[1], 0])[2] += 1
            rows.append([
                prof_idx[v.profession_id], _int(v.viewers), int(v.rank),
                0 if is_dup else int(v.sample_rank), idx("titles", v.vacancy), idx("employers", v.employer_name),
                _int(v.salary_from), _int(v.salary_to), idx("currencies", v.currency),
                (snap_day - date.fromisoformat(pub[:10])).days if pub else None, age, idx("experience", v.experience),
                int(v.vacancy_id), is_dup,
                _int(v.responses), idx("areas", v.area_name), placement(v.properties), flags(v), rating,
                _int(v.employer_reviews), idx("employment", v.employment), idx("schedule", v.schedule),
                idx("hours", v.working_hours), int(v.fmt), reg[0] if reg else 0,
            ])
    finished = datetime.fromisoformat(snap["finished_at"] or snap["started_at"]).astimezone(MSK)
    return {
        "meta": {
            "snapshot": d["snap_id"], "kind": snap["kind"], "date": snap["started_at"][:10],
            "when": f"{_when(snap['started_at'])}–{finished:%H:%M} МСК",
            "area": AREA_NAMES.get(snap["area_id"]) or str(snap["area_id"]),
            "top": d["top"] or 0, "min_n": cfg["curation"]["min_pure_remote"],
            "dups": int(len(dups)), "formats": d["formats"],
            "dropped": list(d["check"].loc[d["check"]["status"].str.startswith("выбыла"), "profession"]),
        },
        **lookups,
        # Субъекты РФ по числу вакансий: [id, название, вакансий].
        "regions": sorted(region_n.values(), key=lambda r: -r[2]),
        "industries": industries,
        "professions": [{"n": r.profession, "i": ind_idx[r.industry], "t": _int(r.total_found)}
                        for r in profs.itertuples()],
        "vacancies": rows,
    }


FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600'
         '&family=Fira+Sans:wght@400;500;600;700&display=swap">')

CSS = """
/* Layout: контейнер 1200px по центру. Тёмная шапка (название, снимок, метрика, CSV) → липкая строка фильтров →
   KPI → виджеты в сетке 12 колонок. Только светлая тема. Синий = просмотры, янтарный = доля вакансий / минус. */
:root {
  --bg: #f3f5fa;
  --surface: #ffffff;
  --text: #1b2540;
  --ink: #14204a;
  --muted: #66708a;
  --line: #e4e8f1;
  --grid: #eef1f6;
  --primary: #1e40af;
  --views: #3b82f6;
  --views-soft: rgba(59, 130, 246, .13);
  --vacs: #d97706;
  --sel: #eef4ff;
  --track: #edf1f7;
  --chip: #fef3c7;
  --chip-ink: #92400e;
  --bar-bg: #0f1d45;
  --bar-ink: #ffffff;
  --bar-muted: #b4c0e2;
  --bar-line: rgba(255, 255, 255, .16);
  --shadow: 0 1px 2px rgba(16, 24, 40, .04), 0 2px 10px rgba(20, 32, 74, .04);
  --f-body: "Fira Sans", "Segoe UI", system-ui, -apple-system, sans-serif;
  --f-num: "Fira Code", ui-monospace, "Cascadia Mono", Consolas, monospace;
  --neg: #d97706;
  color-scheme: light;
}
[hidden] { display: none !important; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font: 14.5px/1.45 var(--f-body); }
.container { width: 100%; max-width: 1200px; margin-inline: auto; padding-inline: 24px; }
a { color: inherit; }
button, select, input { font: inherit; color: inherit; }
button, select, summary, [role="button"], a { cursor: pointer; }
:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; border-radius: 4px; }

/* шапка */
.topbar { background: var(--bar-bg); color: var(--bar-ink); border-bottom: 1px solid var(--bar-line); }
.topbar .container { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px 24px; padding-block: 18px; }
.brand { display: grid; gap: 4px; min-width: 0; }
h1 { font: 700 22px/1.2 var(--f-body); margin: 0; letter-spacing: -0.01em; }
.meta { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 14px; color: var(--bar-muted); font-size: 13px; }
.meta b { color: var(--bar-ink); font-weight: 500; }
.live { display: inline-flex; align-items: center; gap: 7px; }
.live::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: #4ade80; animation: pulse 2.4s ease-out infinite; }
@keyframes pulse { 0% { box-shadow: 0 0 0 0 rgba(74, 222, 128, .5); } 70% { box-shadow: 0 0 0 7px transparent; } 100% { box-shadow: 0 0 0 0 transparent; } }
@media (prefers-reduced-motion: reduce) { .live::before { animation: none; } }
.flag { font: 500 11.5px var(--f-body); color: var(--chip-ink); background: var(--chip); border-radius: 999px; padding: 1px 9px; }
.view { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.seg { display: inline-flex; border: 1px solid var(--bar-line); border-radius: 8px; padding: 2px; }
.seg button { background: none; border: 0; border-radius: 6px; color: var(--bar-muted); padding: 5px 12px; min-height: 32px; font-size: 13.5px; transition: background .15s, color .15s; }
.seg button[aria-pressed="true"] { background: rgba(255, 255, 255, .14); color: var(--bar-ink); }
.toggle { display: inline-flex; align-items: center; gap: 8px; font-size: 13.5px; color: var(--bar-muted); min-height: 36px; }
.toggle input { accent-color: var(--views); width: 16px; height: 16px; margin: 0; }
.btn-bar { background: none; border: 1px solid var(--bar-line); border-radius: 8px; color: var(--bar-ink); padding: 5px 12px; min-height: 36px; font-size: 13.5px; }
.btn-bar:hover { border-color: var(--bar-muted); }
.btn-bar.primary { background: var(--views); border-color: var(--views); font-weight: 500; }
.btn-bar.primary:hover { background: #2f74e8; }

/* фильтры */
.filterbar { position: sticky; top: env(safe-area-inset-top, 0px); z-index: 5; background: var(--surface); border-bottom: 1px solid var(--line); box-shadow: var(--shadow); }
.filters { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; padding-block: 10px; }
.filters select, .filters input { width: 100%; min-width: 0; background: var(--bg); border: 1px solid var(--line); border-radius: 8px; padding: 0 10px; height: 38px; font-size: 13.5px; transition: border-color .15s, background .15s; }
.filters select:hover, .filters input:hover { border-color: var(--views); }
.filters select.on { border-color: var(--views); background: var(--sel); color: var(--ink); font-weight: 500; }

/* сетка */
main.container { display: grid; gap: 16px; padding-block: 20px 48px; }
.kpis { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; }
.kpi { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; display: grid; gap: 2px; align-content: start; min-width: 0; box-shadow: var(--shadow); }
.kpi .lab { font-size: 12.5px; color: var(--muted); }
.kpi .val { font: 600 26px/1.2 var(--f-body); color: var(--ink); }
.kpi .val.name { font-size: 15px; line-height: 1.3; overflow-wrap: anywhere; padding-top: 4px; }
.kpi .sub { font-size: 12px; color: var(--muted); overflow-wrap: anywhere; }
.grid { display: grid; grid-template-columns: repeat(12, minmax(0, 1fr)); gap: 16px; align-items: stretch; }
.s3 { grid-column: span 3; } .s4 { grid-column: span 4; } .s5 { grid-column: span 5; } .s6 { grid-column: span 6; } .s7 { grid-column: span 7; } .s8 { grid-column: span 8; } .s12 { grid-column: span 12; }
.card { background: var(--surface); border: 1px solid var(--line); border-radius: 14px; min-width: 0; display: flex; flex-direction: column; box-shadow: var(--shadow); overflow: hidden; }
.card-h { display: flex; flex-wrap: wrap; gap: 6px 12px; align-items: center; justify-content: space-between; padding: 14px 18px 8px; min-height: 52px; }
h2 { font: 600 15px/1.3 var(--f-body); margin: 0; color: var(--ink); }
.tag { font: 500 11.5px var(--f-num); color: var(--muted); background: var(--track); border-radius: 6px; padding: 2px 8px; white-space: nowrap; }
.card-b { padding: 4px 14px 14px; min-width: 0; }
.chart { min-width: 0; }
.chart svg { display: block; }
/* Тело списка не влияет на высоту ряда: высоту задаёт соседний график, список прокручивается. */
.fill { flex: 1 1 auto; height: 0; min-height: 280px; overflow: auto; overscroll-behavior: contain; border-top: 1px solid var(--line); }
.list-fixed { height: 520px; overflow: auto; overscroll-behavior: contain; border-top: 1px solid var(--line); }
.foot { padding: 8px 18px; display: flex; flex-wrap: wrap; gap: 10px; align-items: center; justify-content: space-between; color: var(--muted); font-size: 12.5px; border-top: 1px solid var(--line); min-height: 50px; }
.btn { background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 0 12px; height: 34px; font-size: 13px; transition: border-color .15s; }
.btn:hover { border-color: var(--views); }
.empty { padding: 22px 16px; color: var(--muted); margin: 0; }
.legend { display: flex; flex-wrap: wrap; gap: 6px; }
.legend button { display: inline-flex; align-items: center; gap: 7px; background: none; border: 1px solid var(--line); border-radius: 999px; padding: 2px 10px; height: 28px; font-size: 12.5px; color: var(--text); }
.legend button[aria-pressed="false"] { opacity: .45; }
.legend i { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
.card-h select { background: var(--bg); border: 1px solid var(--line); border-radius: 8px; height: 32px; padding: 0 8px; font-size: 13px; }

/* svg */
svg text { font-family: var(--f-body); }
svg .ax { fill: var(--muted); font-size: 11px; }
svg .ax.num { font-family: var(--f-num); font-size: 10.5px; }
svg .val { fill: var(--text); font: 600 11.5px var(--f-body); font-variant-numeric: tabular-nums; }
svg .gl { stroke: var(--grid); stroke-width: 1; }
svg .base { stroke: var(--line); stroke-width: 1.5; }
svg .c-views { fill: var(--views); }
svg .c-vacs { fill: var(--vacs); }
svg .hit { fill: transparent; }
svg .mk { outline: none; }
svg .mk path { transition: opacity .15s; }
svg .mk:hover .hit, svg .mk:focus-visible .hit { fill: var(--sel); opacity: .6; }
svg .mk:hover path, svg .mk:focus-visible path, svg .mk.on path { stroke: var(--ink); stroke-width: 1.5; }
svg .mk.dim path { opacity: .4; }
svg .dot { fill: var(--views); stroke: var(--surface); stroke-width: 2; }
svg .dot.on { fill: var(--vacs); }

/* списки */
.rows { display: grid; }
.prow { display: grid; grid-template-columns: 2.4em minmax(0, 1fr) minmax(56px, 30%) 3.4em; gap: 10px; align-items: center;
  width: 100%; text-align: left; background: none; border: 0; border-bottom: 1px solid var(--line); padding: 8px 18px; min-height: 44px; transition: background .15s; }
.prow:hover { background: var(--sel); }
.prow[aria-pressed="true"] { background: var(--sel); box-shadow: inset 3px 0 0 var(--views); }
.place { font: 500 12px var(--f-num); color: var(--muted); font-variant-numeric: tabular-nums; }
.nm { min-width: 0; font-weight: 500; overflow-wrap: anywhere; color: var(--text); font-size: 14px; }
.nm small { display: block; font-weight: 400; font-size: 12px; color: var(--muted); }
.bar { height: 8px; background: var(--track); border-radius: 0 4px 4px 0; }
.bar i { display: block; height: 100%; max-width: 100%; background: var(--views); border-radius: 0 4px 4px 0; transition: width .3s ease; }
@media (prefers-reduced-motion: reduce) { .bar i, svg .mk path { transition: none; } }
.v { font: 600 14.5px var(--f-body); text-align: right; font-variant-numeric: tabular-nums; color: var(--ink); }
.chip { display: inline-block; font: 500 11px var(--f-body); color: var(--chip-ink); background: var(--chip); border-radius: 999px; padding: 0 7px; margin-left: 6px; vertical-align: 1px; }
.vac-list { list-style: none; margin: 0; padding: 0; }
.vac { display: grid; grid-template-columns: 3.2em minmax(0, 1fr); gap: 12px; padding: 9px 18px; border-bottom: 1px solid var(--line); transition: background .15s; }
.vac:hover { background: var(--sel); }
.vac .views { font: 600 17px/1.25 var(--f-body); color: var(--views); text-align: right; font-variant-numeric: tabular-nums; }
.vac .views small { display: block; font: 400 10.5px var(--f-body); color: var(--muted); }
.vac a { font-weight: 500; color: var(--text); text-decoration: none; overflow-wrap: anywhere; }
.vac a:hover { color: var(--primary); text-decoration: underline; text-underline-offset: 3px; }
.vac .m { display: block; color: var(--muted); font-size: 12px; overflow-wrap: anywhere; margin-top: 1px; }
.sel { display: inline-flex; align-items: center; gap: 4px; background: var(--sel); color: var(--ink); border-radius: 999px; padding: 2px 2px 2px 10px; font-size: 12.5px; max-width: 100%; }
.sel button { border: 0; background: none; padding: 0; font-size: 15px; line-height: 1; color: var(--muted); width: 28px; height: 28px; }

/* факторы: расходящиеся полосы от нуля, длина — логарифм отношения к медиане профессии */
.fx { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; padding: 4px 18px 18px; align-items: start; }
.fx-g { border: 1px solid var(--line); border-radius: 10px; background: #fafbfd; padding: 0 8px 8px; }
.fx-g h3 { font: 600 12.5px var(--f-body); color: var(--ink); margin: 0 -8px 6px; padding: 9px 12px 8px; border-bottom: 1px solid var(--line); background: var(--surface); border-radius: 10px 10px 0 0; }
.fx-r { display: grid; grid-template-columns: minmax(0, 1fr) 34% 3.4em; gap: 8px; align-items: center; min-height: 26px; padding: 0 4px; border-radius: 6px; }
.fx-r:hover, .fx-r:focus-visible { background: var(--sel); }
.fx-r.dim { opacity: .45; }
.fx-list { grid-template-columns: minmax(0, 1fr); gap: 0; padding: 6px 12px 12px; }
.fx-list .fx-r { grid-template-columns: minmax(0, 1fr) 30% 3.6em; cursor: pointer; min-height: 30px; }
.fx-list .fx-r[aria-pressed="true"] { background: var(--sel); box-shadow: inset 3px 0 0 var(--views); }
.fx-l { font-size: 12.5px; color: var(--text); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fx-b { position: relative; height: 10px; }
.fx-b::before { content: ""; position: absolute; left: 50%; top: -3px; bottom: -3px; width: 1px; background: var(--muted); opacity: .45; }
.fx-b i { position: absolute; top: 0; height: 100%; }
.fx-b i.pos { left: 50%; background: var(--views); border-radius: 0 4px 4px 0; }
.fx-b i.neg { right: 50%; background: var(--neg); border-radius: 4px 0 0 4px; }
.fx-v { font: 600 12.5px var(--f-body); font-variant-numeric: tabular-nums; text-align: right; color: var(--ink); }
.chip.pl { color: #1e3a8a; background: #dbeafe; }
#tip { position: fixed; z-index: 20; pointer-events: none; background: var(--surface); border: 1px solid var(--line); border-radius: 10px;
  box-shadow: 0 10px 30px rgba(15, 23, 42, .18); padding: 10px 12px; font-size: 12.5px; min-width: 200px; max-width: 300px; }
#tip .t { font-weight: 600; margin-bottom: 6px; color: var(--ink); font-size: 13px; }
#tip dl { display: grid; grid-template-columns: auto auto; gap: 3px 14px; margin: 0; }
#tip dt { color: var(--muted); }
#tip dd { margin: 0; text-align: right; font: 600 13px var(--f-body); font-variant-numeric: tabular-nums; color: var(--text); }

@media (max-width: 1120px) {
  .kpis { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .fx { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 960px) {
  .filters { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .s4, .s5, .s6, .s7, .s8 { grid-column: span 12; }
  .fill { height: 520px; flex: none; }
}
@media (max-width: 640px) {
  .container { padding-inline: 16px; }
  .kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .kpi:last-child:nth-child(odd) { grid-column: span 2; }
  .fx { grid-template-columns: minmax(0, 1fr); padding-inline: 12px; }
  .filterbar { position: static; }
  .filters { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .prow { grid-template-columns: 2.2em minmax(0, 1fr) 3.2em; }
  .prow .bar { grid-column: 2 / 4; grid-row: 2; }
}
"""

BODY = """<div class="topbar"><div class="container">
  <div class="brand"><h1>__TITLE__</h1><div class="meta" id="meta"></div></div>
  <div class="view">
    <div class="seg" role="group" aria-label="Метрика"><button type="button" data-m="med" aria-pressed="true">Медиана</button><button type="button" data-m="mean" aria-pressed="false">Среднее</button></div>
    <label class="toggle"><input id="f-dups" type="checkbox"><span id="f-dups-l">Копии</span></label>
    <button class="btn-bar" id="f-reset" type="button">Сбросить</button>
    <button class="btn-bar primary" id="csv" type="button">Скачать CSV</button>
  </div>
</div></div>

<div class="filterbar"><form class="container filters" id="filters" aria-label="Фильтры">
  <input id="f-q" type="search" placeholder="Поиск" aria-label="Поиск" autocomplete="off">
  <select id="f-ind" aria-label="Отрасль"><option value="-1">Все отрасли</option></select>
  <select id="f-reg" aria-label="Регион публикации вакансии"></select>
  <select id="f-fmt" aria-label="Формат работы"><option value="r">Только удалённо</option><option value="rx">Удалённо как вариант</option><option value="nr">Не удалённо</option><option value="h">Гибрид</option><option value="o">Офис</option><option value="f">Разъездной</option><option value="">Все форматы</option></select>
  <select id="f-pl" aria-label="Тариф размещения на hh"><option value="">Тариф: все</option><option value="0">Премиум</option><option value="1">Стандарт+</option><option value="2">Стандарт</option><option value="3">Зарплата.ру</option><option value="np">Без Премиума</option></select>
  <select id="f-exp" aria-label="Опыт"><option value="">Опыт: все</option><option value="noExperience">Без опыта</option><option value="between1And3">Опыт 1–3 года</option><option value="between3And6">Опыт 3–6 лет</option><option value="moreThan6">Опыт более 6 лет</option></select>
  <select id="f-sal" aria-label="Зарплата"><option value="">Зарплата: все</option><option value="any">Зарплата указана</option><option value="50000">до ≥ 50 000 ₽</option><option value="100000">до ≥ 100 000 ₽</option><option value="150000">до ≥ 150 000 ₽</option><option value="200000">до ≥ 200 000 ₽</option><option value="300000">до ≥ 300 000 ₽</option></select>
  <select id="f-fresh" aria-label="Дата публикации"><option value="0">Дата: все</option><option value="3">За 3 дня</option><option value="7">За 7 дней</option><option value="14">За 14 дней</option><option value="30">За 30 дней</option></select>
  <select id="f-depth" aria-label="Глубина выдачи"></select>
  <select id="f-min" aria-label="Минимум вакансий в профессии"></select>
</form></div>

<main class="container">
<section class="kpis" id="overview" aria-label="Итоги"><p class="empty">Загрузка данных…</p></section>

<div class="grid">
  <section class="card s12" aria-labelledby="h-top">
    <div class="card-h"><h2 id="h-top">Топ-15 профессий</h2><span class="tag" id="top-note"></span></div>
    <div class="card-b"><div class="chart" id="ch-top"></div></div>
  </section>

  <section class="card s6" aria-labelledby="h-prof">
    <div class="card-h"><h2 id="h-prof">Рейтинг профессий</h2><span class="tag" id="prof-note"></span></div>
    <div class="list-fixed"><div class="rows" id="prof-list"></div></div>
    <div class="foot"><button class="btn" id="prof-more" type="button"></button><span id="prof-shown"></span></div>
  </section>
  <section class="card s6" id="vac-card" aria-labelledby="h-vac">
    <div class="card-h"><h2 id="h-vac">Рейтинг вакансий</h2><span id="vac-note"></span>
      <select id="vac-sort" aria-label="Сортировка вакансий"><option value="views">Больше смотрят</option><option value="resp">Больше откликов</option><option value="rank">Выше в выдаче</option><option value="fresh">Свежие</option><option value="salary">Выше зарплата</option></select></div>
    <div class="list-fixed"><ol class="vac-list" id="vac-list"></ol></div>
    <div class="foot"><button class="btn" id="vac-more" type="button">Показать ещё 50</button><span id="vac-shown"></span></div>
  </section>

  <section class="card s12" aria-labelledby="h-fac">
    <div class="card-h"><h2 id="h-fac">Что влияет на просмотры</h2><span class="tag" id="fac-note">относительно той же профессии</span></div>
    <div class="fx" id="fx"></div>
  </section>

  <section class="card s5" id="cmp-card" aria-labelledby="h-cmp">
    <div class="card-h"><h2 id="h-cmp">Удалёнка против офиса</h2><span class="tag">удалённо к офису и гибриду</span></div>
    <div class="fill"><div class="fx fx-list" id="cmp"></div></div>
  </section>
  <section class="card s7" aria-labelledby="h-sph">
    <div class="card-h"><h2 id="h-sph">Сферы: доля просмотров и вакансий</h2><div class="legend" id="sph-legend"></div></div>
    <div class="card-b"><div class="chart" id="ch-sph"></div></div>
  </section>

  <section class="card s7" aria-labelledby="h-sc">
    <div class="card-h"><h2 id="h-sc">Предложение и интерес</h2><span class="tag" id="sc-note"></span></div>
    <div class="card-b"><div class="chart" id="ch-sc"></div></div>
  </section>
  <section class="card s5" aria-labelledby="h-emp">
    <div class="card-h"><h2 id="h-emp">Топ работодателей</h2><span class="tag">смотрят, всего</span></div>
    <div class="card-b"><div class="chart" id="ch-emp"></div></div>
  </section>
</div>
</main>
<div id="tip" role="tooltip" hidden></div>
<script type="application/octet-stream" id="dash-gz">__DATA__</script>
<script>__JS__</script>"""

JS = r"""
(async function () {
  'use strict';
  // Данные вшиты сжатыми (gzip + base64): так ~180 тыс. вакансий помещаются в одну страницу.
  async function loadData() {
    var gz = document.getElementById('dash-gz');
    var bin = atob(gz.textContent.trim()), u = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
    var stream = new Blob([u]).stream().pipeThrough(new DecompressionStream('gzip'));
    return JSON.parse(await new Response(stream).text());
  }
  var D = await loadData();
  var META = D.meta, IND = D.industries, PROF = D.professions, VAC = D.vacancies, AREAS = D.areas;
  var P = 0, VW = 1, RK = 2, SR = 3, TI = 4, EM = 5, SF = 6, ST = 7, CU = 8, PD = 9, AG = 10, EX = 11, ID = 12, DU = 13,
      RS = 14, AR = 15, PL = 16, FL = 17, RT = 18, RV = 19, EP = 20, SC = 21, WH = 22, FM = 23, RG = 24;
  var FORMATS = ['Только удалённо', 'Удалённо как вариант', 'Гибрид', 'Офис', 'Разъездной', 'Не указан'];
  // Строки вакансий хранят номера в словарях — разворачиваем один раз при загрузке.
  (function () {
    var base = Date.parse(META.date + 'T00:00:00Z');
    VAC.forEach(function (r) {
      r[TI] = D.titles[r[TI]] || ''; r[EM] = D.employers[r[EM]] || null; r[CU] = D.currencies[r[CU]] || null;
      r[EX] = D.experience[r[EX]] || null; r[PD] = r[PD] == null ? null : new Date(base - r[PD] * 864e5).toISOString().slice(0, 10);
    });
  })();
  // Биты FL — в том же порядке, что FLAG_BITS в dashboard.py.
  var F = {it: 1, hr: 2, branded: 4, test: 8, letter: 16, promo: 32, anon: 64, fraud: 128, trusted: 256, online: 512, noresume: 1024, gross: 2048, agency: 4096};
  var PLACE = ['Премиум', 'Стандарт+', 'Стандарт', 'Зарплата.ру', '—'];
  var EMPL = {FULL: 'Полная', PART: 'Частичная', PROJECT: 'Проект', SIDE_JOB: 'Подработка', PROBATION: 'Стажировка'};
  var SCHED = {FIVE_ON_TWO_OFF: '5/2', TWO_ON_TWO_OFF: '2/2', SIX_ON_ONE_OFF: '6/1', FLEXIBLE: 'Гибкий', WEEKEND: 'Выходные', OTHER: 'Другой'};
  var MONTHS = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
  var CUR = {RUR: '₽', USD: '$', EUR: '€', KZT: '₸', UZS: 'сум', BYR: 'Br'};
  var EXP = {noExperience: 'Без опыта', between1And3: '1–3 года', between3And6: '3–6 лет', moreThan6: 'Более 6 лет'};
  function has(r, bit) { return (r[FL] & bit) !== 0; }
  function area(r) { return AREAS[r[AR]] || ''; }
  var nf = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 1});
  var nf0 = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 0});
  var reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var PROF_PAGE = 30, VAC_PAGE = 50, NS = 'http://www.w3.org/2000/svg';

  // ---------- helpers ----------
  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function S(tag, attrs, parent) { var e = document.createElementNS(NS, tag); for (var k in attrs) e.setAttribute(k, attrs[k]); if (parent) parent.appendChild(e); return e; }
  function T(parent, x, y, s, cls, anchor) { var t = S('text', {x: x, y: y, 'class': cls || 'ax', 'text-anchor': anchor || 'start'}, parent); t.textContent = s; return t; }
  function plural(n, a, b, c) { n = Math.abs(Math.round(n)); var n10 = n % 10, n100 = n % 100; if (n10 === 1 && n100 !== 11) return a; if (n10 >= 2 && n10 <= 4 && (n100 < 12 || n100 > 14)) return b; return c; }
  function fmt(x) { return x == null || isNaN(x) ? '—' : nf.format(x); }
  function pct(x) { return x == null || isNaN(x) ? '—' : nf.format(x * 100) + '%'; }
  function quant(s, q) { if (!s.length) return NaN; var pos = (s.length - 1) * q, lo = Math.floor(pos), hi = Math.ceil(pos); return s[lo] + (s[hi] - s[lo]) * (pos - lo); }
  function asc(a, b) { return a - b; }
  function mean(a) { if (!a.length) return NaN; var s = 0; a.forEach(function (x) { s += x; }); return s / a.length; }
  function trunc(s, n) { return s.length > n ? s.slice(0, n - 1) + '…' : s; }
  function salary(r) {
    var lo = r[SF], hi = r[ST], c = CUR[r[CU]] || r[CU] || '';
    if (lo == null && hi == null) return '';
    if (lo != null && hi != null) return (lo === hi ? fmt(lo) : fmt(lo) + '–' + fmt(hi)) + ' ' + c;
    return (lo != null ? 'от ' + fmt(lo) : 'до ' + fmt(hi)) + ' ' + c;
  }
  function pub(r) {
    if (!r[PD]) return '';
    var p = r[PD].split('-'), s = 'опубл. ' + (+p[2]) + ' ' + MONTHS[+p[1] - 1], a = r[AG];
    if (a != null) { var d = Math.floor(a); s += d < 1 ? ' · сегодня' : ' · ' + d + ' ' + plural(d, 'день', 'дня', 'дней') + ' назад'; }
    return s;
  }
  function niceStep(v) { var p = Math.pow(10, Math.floor(Math.log10(v))), f = v / p; return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p; }
  function scale(max, count) {
    if (!(max > 0)) max = 1;
    var step = niceStep(max / (count || 4)), top = Math.ceil(max / step - 1e-9) * step, list = [];
    for (var v = 0; v <= top + step / 1e6; v += step) list.push(Math.round(v * 1e6) / 1e6);
    return {top: top, list: list};
  }
  function colPath(x, y, w, h, r) { if (h <= 0) return ''; r = Math.min(r, w / 2, h); return 'M' + x + ',' + (y + h) + 'V' + (y + r) + 'Q' + x + ',' + y + ' ' + (x + r) + ',' + y + 'H' + (x + w - r) + 'Q' + (x + w) + ',' + y + ' ' + (x + w) + ',' + (y + r) + 'V' + (y + h) + 'Z'; }
  function barPath(x, y, w, h, r) { if (w <= 0) return ''; r = Math.min(r, h / 2, w); return 'M' + x + ',' + y + 'H' + (x + w - r) + 'Q' + (x + w) + ',' + y + ' ' + (x + w) + ',' + (y + r) + 'V' + (y + h - r) + 'Q' + (x + w) + ',' + (y + h) + ' ' + (x + w - r) + ',' + (y + h) + 'H' + x + 'Z'; }
  // Перенос подписи по словам с замером ширины текста: не больше maxLines строк шириной maxW.
  // Слово длиннее строки переносится с дефисом (после гласной, если можно), остаток сверх maxLines — многоточие.
  var measureCtx = document.createElement('canvas').getContext('2d');
  function textW(s) { measureCtx.font = '11px "Fira Sans", "Segoe UI", system-ui, sans-serif'; return measureCtx.measureText(s).width; }
  function splitWord(w, maxW) {
    var parts = [];
    while (textW(w) > maxW && w.length > 5) {
      var cut = w.length - 3;
      while (cut > 2 && textW(w.slice(0, cut) + '-') > maxW) cut--;
      // Упрощённые правила переноса: в обеих частях есть гласная, вторая не начинается с ь/ъ/й,
      // разрыв после гласной (перед согласной) или между двумя согласными.
      var V = /[аеёиоуыэюяaeiouy]/i, best = -1;
      for (var c = cut; c > Math.max(2, cut - 6); c--) {
        var head = w.slice(0, c), tail = w.slice(c), a = w[c - 1], z = w[c];
        if (!V.test(head) || !V.test(tail) || /[ьъй]/i.test(z) || !/[а-яёa-z]/i.test(a) || !/[а-яёa-z]/i.test(z)) continue;
        if ((V.test(a) && !V.test(z)) || (!V.test(a) && !V.test(z))) { best = c; break; }
      }
      if (best > 0) cut = best;
      parts.push(w.slice(0, cut) + '-'); w = w.slice(cut);
    }
    parts.push(w); return parts;
  }
  function wrapLines(s, maxW, maxLines) {
    var words = [], lines = [], cur = '';
    s.replace(/,/g, ', ').split(/\s+/).filter(Boolean).forEach(function (w) { words = words.concat(splitWord(w, maxW)); });
    words.forEach(function (w) {
      var glue = cur && !/-$/.test(cur) ? cur + ' ' + w : cur + w;
      if (!cur) cur = w; else if (!/-$/.test(cur) && textW(glue) <= maxW) cur = glue; else { lines.push(cur); cur = w; }
    });
    if (cur) lines.push(cur);
    if (lines.length > maxLines) {
      lines = lines.slice(0, maxLines);
      var last = lines[maxLines - 1].replace(/-$/, '');
      while (last.length > 1 && textW(last + '…') > maxW) last = last.slice(0, -1);
      lines[maxLines - 1] = last.replace(/[\s,]+$/, '') + '…';
    }
    return lines;
  }
  function width(box) { return Math.max(Math.floor(box.clientWidth || box.parentNode.clientWidth || 600), 260); }
  function emptyState(box, msg) { box.textContent = ''; box.appendChild(el('p', 'empty', msg || 'Нет данных под эти фильтры. Ослабьте фильтры или нажмите «Сбросить».')); }

  // ---------- tooltip ----------
  var tip = $('tip');
  function showTip(info, x, y) {
    tip.textContent = ''; tip.appendChild(el('div', 't', info.t));
    var dl = el('dl'); info.rows.forEach(function (r) { dl.appendChild(el('dt', null, r[0])); dl.appendChild(el('dd', null, r[1])); });
    tip.appendChild(dl); tip.hidden = false;
    var w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.max(8, Math.min(x + 16, window.innerWidth - w - 8)) + 'px';
    tip.style.top = Math.max(8, Math.min(y + 16, window.innerHeight - h - 8)) + 'px';
  }
  function hideTip() { tip.hidden = true; }
  function bindTip(node, info, onClick) {
    node.setAttribute('tabindex', '0'); node.setAttribute('aria-label', info.t + ': ' + info.rows.map(function (r) { return r[0] + ' ' + r[1]; }).join(', '));
    node.addEventListener('pointermove', function (e) { if (e.pointerType !== 'touch') showTip(info, e.clientX, e.clientY); });
    node.addEventListener('pointerleave', hideTip);
    node.addEventListener('focus', function () { var r = node.getBoundingClientRect(); showTip(info, r.left + r.width / 2, r.top); });
    node.addEventListener('blur', hideTip);
    if (onClick) {
      node.setAttribute('role', 'button');
      node.addEventListener('click', onClick);
      node.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick(); } });
    }
  }
  window.addEventListener('scroll', hideTip, {passive: true});

  // ---------- charts ----------
  // Столбцы: items [{label, value, sub, info, onClick, dim, on}]. На узком контейнере — горизонтальные полосы.
  function columnChart(box, items, o) {
    o = o || {}; box.textContent = '';
    if (!items.length) return emptyState(box);
    var W = width(box);
    if (o.hbarBelow && W < o.hbarBelow) return hbarChart(box, items, o);
    var lines = o.lines || 1, H = o.height || 240, padL = 34, padR = 6, padT = 18, padB = 16 + lines * 13 + (o.sub ? 16 : 0);
    var max = o.max != null ? o.max : Math.max.apply(null, items.map(function (i) { return i.value || 0; }));
    var sc = scale(max, 4), ih = H - padT - padB, iw = W - padL - padR, band = iw / items.length, bw = Math.min(24, band * 0.62);
    var svg = S('svg', {viewBox: '0 0 ' + W + ' ' + H, width: W, height: H, role: 'img', 'aria-label': o.aria || ''}, box);
    sc.list.forEach(function (v) { var y = padT + ih - v / sc.top * ih; S('line', {x1: padL, x2: W - padR, y1: y, y2: y, 'class': 'gl'}, svg); T(svg, padL - 6, y + 3.5, o.tick ? o.tick(v) : fmt(v), 'ax num', 'end'); });
    items.forEach(function (it, i) {
      var cx = padL + band * i + band / 2, h = Math.max(0, (it.value || 0) / sc.top * ih), y = padT + ih - h;
      var g = S('g', {'class': 'mk' + (it.dim ? ' dim' : '') + (it.on ? ' on' : '')}, svg);
      S('rect', {x: padL + band * i, y: padT, width: band, height: ih, 'class': 'hit'}, g);
      S('path', {d: colPath(cx - bw / 2, y, bw, h, 4), 'class': o.cls || 'c-views'}, g);
      T(g, cx, y - 5, o.fmt ? o.fmt(it.value) : fmt(it.value), 'val', 'middle');
      wrapLines(it.label, band - 4, lines).forEach(function (ln, li) { T(g, cx, padT + ih + 15 + li * 13, ln, 'ax', 'middle'); });
      if (o.sub && it.sub) T(g, cx, padT + ih + 15 + lines * 13 + 2, it.sub, 'ax num', 'middle');
      if (it.info) bindTip(g, it.info, it.onClick);
    });
    S('line', {x1: padL, x2: W - padR, y1: padT + ih, y2: padT + ih, 'class': 'base'}, svg);
  }
  // Горизонтальные полосы: подписи слева, значение у конца полосы.
  function hbarChart(box, items, o) {
    o = o || {}; box.textContent = '';
    if (!items.length) return emptyState(box);
    var W = width(box), rowH = 30, padT = 4, labW = Math.min(Math.round(W * 0.42), 260), valW = 52;
    var H = padT + rowH * items.length + 6, iw = W - labW - valW - 8;
    var max = o.max != null ? o.max : Math.max.apply(null, items.map(function (i) { return i.value || 0; })) || 1;
    var svg = S('svg', {viewBox: '0 0 ' + W + ' ' + H, width: W, height: H, role: 'img', 'aria-label': o.aria || ''}, box);
    var chars = Math.floor((labW - 8) / 6.4);
    items.forEach(function (it, i) {
      var y = padT + rowH * i, g = S('g', {'class': 'mk' + (it.dim ? ' dim' : '') + (it.on ? ' on' : '')}, svg);
      S('rect', {x: 0, y: y, width: W, height: rowH, 'class': 'hit'}, g);
      T(g, 0, y + 14, trunc(it.label, chars), 'ax');
      if (it.sub) T(g, 0, y + 26, trunc(it.sub, chars), 'ax num');
      var w = Math.max(0, (it.value || 0) / max * iw);
      S('path', {d: barPath(labW, y + 8, w, 12, 4), 'class': o.cls || 'c-views'}, g);
      T(g, labW + w + 6, y + 18, o.fmt ? o.fmt(it.value) : fmt(it.value), 'val');
      if (it.info) bindTip(g, it.info, it.onClick);
    });
  }
  var sphShow = {views: true, vacs: true};
  function sphereChart(box, rows) {
    box.textContent = '';
    if (!rows.length) return emptyState(box);
    var W = width(box), rowH = 34, labW = Math.min(Math.round(W * 0.36), 240), idxW = 54, iw = W - labW - idxW - 50;
    var H = 22 + rowH * rows.length, svg = S('svg', {viewBox: '0 0 ' + W + ' ' + H, width: W, height: H, role: 'img',
      'aria-label': 'Доля просмотров и доля вакансий по сферам. Больше всего просмотров: ' + IND[rows[0].i]}, box);
    var max = 0; rows.forEach(function (r) { if (sphShow.views) max = Math.max(max, r.sv); if (sphShow.vacs) max = Math.max(max, r.sn); }); max = max || 1;
    T(svg, W - 4, 12, 'индекс', 'ax', 'end');
    var chars = Math.floor((labW - 8) / 6.4);
    rows.forEach(function (r, i) {
      var y = 20 + rowH * i, g = S('g', {'class': 'mk'}, svg);
      S('rect', {x: 0, y: y, width: W, height: rowH, 'class': 'hit'}, g);
      T(g, 0, y + 19, trunc(IND[r.i], chars), 'ax');
      var y1 = y + 7, wv = r.sv / max * iw, wn = r.sn / max * iw;
      if (sphShow.views) { S('path', {d: barPath(labW, y1, wv, 9, 4), 'class': 'c-views'}, g); T(g, labW + wv + 5, y1 + 8, pct(r.sv), 'val'); }
      if (sphShow.vacs) { S('path', {d: barPath(labW, y1 + 11, wn, 9, 4), 'class': 'c-vacs'}, g); T(g, labW + wn + 5, y1 + 19, pct(r.sn), 'ax num'); }
      T(g, W - 4, y + 19, r.sn > 0 ? '×' + nf.format(r.sv / r.sn) : '—', 'val', 'end');
      bindTip(g, {t: IND[r.i], rows: [['Доля просмотров', pct(r.sv)], ['Доля вакансий', pct(r.sn)], ['Индекс внимания', r.sn > 0 ? '×' + nf.format(r.sv / r.sn) : '—'],
        ['Смотрят сейчас, всего', fmt(r.views)], ['Вакансий', fmt(r.n)]]}, function () { setIndustry(r.i); });
    });
  }
  function scatterChart(box, pts) {
    box.textContent = '';
    pts = pts.filter(function (p) { return p.x > 0; });
    if (pts.length < 3) return emptyState(box, 'Слишком мало профессий под эти фильтры для графика.');
    var W = width(box), H = 420, padL = 40, padR = 14, padT = 14, padB = 40, iw = W - padL - padR, ih = H - padT - padB;
    var xs = pts.map(function (p) { return p.x; }), lx0 = Math.log10(Math.min.apply(null, xs) * 0.8), lx1 = Math.log10(Math.max.apply(null, xs) * 1.25);
    var sc = scale(Math.max.apply(null, pts.map(function (p) { return p.y; })), 4);
    var X = function (v) { return padL + (Math.log10(v) - lx0) / (lx1 - lx0) * iw; }, Y = function (v) { return padT + ih - v / sc.top * ih; };
    var svg = S('svg', {viewBox: '0 0 ' + W + ' ' + H, width: W, height: H, role: 'img', 'aria-label': 'Точечный график: число удалённых вакансий профессии и сколько их смотрят'}, box);
    sc.list.forEach(function (v) { S('line', {x1: padL, x2: W - padR, y1: Y(v), y2: Y(v), 'class': 'gl'}, svg); T(svg, padL - 6, Y(v) + 3.5, fmt(v), 'ax num', 'end'); });
    [10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000].forEach(function (v) {
      var lv = Math.log10(v); if (lv < lx0 || lv > lx1) return;
      S('line', {x1: X(v), x2: X(v), y1: padT, y2: padT + ih, 'class': 'gl'}, svg); T(svg, X(v), padT + ih + 15, nf0.format(v), 'ax num', 'middle');
    });
    T(svg, padL + iw / 2, H - 6, 'вакансий профессии (логарифмическая шкала)', 'ax', 'middle');
    var yl = T(svg, 12, padT + ih / 2, (state.metric === 'med' ? 'медиана' : 'среднее') + ' «смотрят»', 'ax', 'middle'); yl.setAttribute('transform', 'rotate(-90 12 ' + (padT + ih / 2) + ')');
    var order = pts.slice().sort(function (a, b) { return b.y - a.y; }), labeled = {};
    order.slice(0, 3).forEach(function (p) { labeled[p.p] = 1; }); if (state.prof >= 0) labeled[state.prof] = 1;
    pts.forEach(function (p) {
      var c = S('circle', {cx: X(p.x), cy: Y(p.y), r: 5, 'class': 'dot' + (p.p === state.prof ? ' on' : '')}, svg);
      p.cx = X(p.x); p.cy = Y(p.y); p.node = c;
    });
    // Подпись ставим справа, при наложении — слева, если и там занято — не ставим (значение есть в подсказке).
    var boxes = [];
    function free(b) { return b.x0 >= padL && b.x1 <= W - 2 && boxes.every(function (o) { return b.x1 < o.x0 || b.x0 > o.x1 || b.y1 < o.y0 || b.y0 > o.y1; }); }
    pts.forEach(function (p) { boxes.push({x0: p.cx - 6, x1: p.cx + 6, y0: p.cy - 6, y1: p.cy + 6}); });
    order.filter(function (p) { return labeled[p.p]; }).forEach(function (p) {
      var s = trunc(PROF[p.p].n, 26), w = s.length * 6, y0 = p.cy - 18, y1 = p.cy - 4;
      var right = {x0: p.cx + 8, x1: p.cx + 8 + w, y0: y0, y1: y1}, left = {x0: p.cx - 8 - w, x1: p.cx - 8, y0: y0, y1: y1};
      if (free(right)) { boxes.push(right); T(svg, right.x0, p.cy - 7, s, 'ax'); }
      else if (free(left)) { boxes.push(left); T(svg, left.x1, p.cy - 7, s, 'ax', 'end'); }
    });
    // Ближайшая точка в радиусе 24px — не нужно целиться в 10-пиксельный кружок.
    var hit = S('rect', {x: padL, y: padT, width: iw, height: ih, 'class': 'hit'}, svg), cur = null;
    function nearest(e) {
      var r = svg.getBoundingClientRect(), mx = (e.clientX - r.left) * W / r.width, my = (e.clientY - r.top) * H / r.height, best = null, bd = 24 * 24;
      pts.forEach(function (p) { var d = (p.cx - mx) * (p.cx - mx) + (p.cy - my) * (p.cy - my); if (d < bd) { bd = d; best = p; } });
      return best;
    }
    function info(p) { return {t: PROF[p.p].n, rows: [[state.metric === 'med' ? 'Медиана «смотрят»' : 'Среднее «смотрят»', fmt(p.y)], ['Вакансий', fmt(p.n)], ['Отрасль', IND[PROF[p.p].i]]]}; }
    hit.addEventListener('pointermove', function (e) {
      var p = nearest(e); if (cur && cur !== p) cur.node.setAttribute('r', 5);
      cur = p; if (p) { p.node.setAttribute('r', 7); showTip(info(p), e.clientX, e.clientY); hit.style.cursor = 'pointer'; } else { hideTip(); hit.style.cursor = 'default'; }
    });
    hit.addEventListener('pointerleave', function () { if (cur) cur.node.setAttribute('r', 5); cur = null; hideTip(); });
    hit.addEventListener('click', function (e) { var p = nearest(e); if (p) selectProf(p.p); });
  }
  // ---------- state & data ----------
  var haystack = VAC.map(function (r) { var pr = PROF[r[P]]; return (r[TI] + ' ' + (r[EM] || '') + ' ' + pr.n + ' ' + IND[pr.i] + ' ' + area(r)).toLowerCase(); });
  var ALL = META.formats === 'all';
  // Москва (1) и Московская область (2019) — разные регионы hh; по умолчанию смотрим их вместе.
  var MSK_REG = [1, 2019];
  var DEF = {ind: -1, reg: 'msk', fmt: ALL ? 'r' : '', exp: '', sal: '', pl: '', fresh: 0, depth: 0, minN: META.min_n, metric: 'med', q: '', dups: false};
  function fmtOk(f, s) { return !s || (s === 'r' ? f === 0 : s === 'rx' ? f === 1 : s === 'nr' ? f >= 2 : s === 'h' ? f === 2 : s === 'o' ? f === 3 : s === 'f' ? f === 4 : true); }
  var state = Object.assign({prof: -1, profAll: false, vacLimit: VAC_PAGE, vacSort: 'views'}, DEF), res = null;

  // anyFormat — без фильтра формата (для сравнения удалёнки с офисом).
  function filtered(anyFormat) {
    var out = [], q = state.q;
    for (var k = 0; k < VAC.length; k++) {
      var r = VAC[k];
      if (r[VW] == null) continue;
      if (r[DU] && !state.dups) continue;
      if (state.reg === 'msk') { if (r[RG] !== 1 && r[RG] !== 2019) continue; } else if (state.reg && r[RG] !== +state.reg) continue;
      if (state.depth && r[RK] > state.depth) continue;
      if (!anyFormat && !fmtOk(r[FM], state.fmt)) continue;
      if (state.ind >= 0 && PROF[r[P]].i !== state.ind) continue;
      if (state.exp && r[EX] !== state.exp) continue;
      if (state.sal === 'any') { if (r[SF] == null && r[ST] == null) continue; }
      else if (state.sal) { if (r[CU] !== 'RUR') continue; var up = r[ST] != null ? r[ST] : r[SF]; if (up == null || up < +state.sal) continue; }
      if (state.pl === 'np') { if (r[PL] === 0) continue; } else if (state.pl !== '' && r[PL] !== +state.pl) continue;
      if (state.fresh && (r[AG] == null || r[AG] > state.fresh)) continue;
      if (q && haystack[k].indexOf(q) === -1) continue;
      out.push(k);
    }
    return out;
  }
  function compute() {
    var rows = filtered(), byP = {}, key = state.metric;
    rows.forEach(function (k) { var p = VAC[k][P]; (byP[p] = byP[p] || []).push(VAC[k][VW]); });
    var profs = [];
    Object.keys(byP).forEach(function (pk) {
      var v = Float64Array.from(byP[pk]).sort(); if (v.length < state.minN) return;
      var sum = 0, zero = 0; v.forEach(function (x) { sum += x; if (x === 0) zero++; });
      profs.push({p: +pk, n: v.length, med: quant(v, .5), mean: sum / v.length, p10: quant(v, .1), p25: quant(v, .25), p75: quant(v, .75), p90: quant(v, .9), max: v[v.length - 1], zero: zero / v.length, sum: sum});
    });
    profs.sort(function (a, b) { return (b[key] - a[key]) || (b.mean - a.mean) || (b.n - a.n); });
    var keep = {}; profs.forEach(function (s, i) { s.place = i + 1; keep[s.p] = s; });
    var vac = rows.filter(function (k) { return keep[VAC[k][P]]; });
    if (state.prof >= 0 && !keep[state.prof]) state.prof = -1;
    return {profs: profs, keep: keep, vac: vac};
  }

  // ---------- render: KPI ----------
  function metricName() { return state.metric === 'med' ? 'медиана' : 'среднее'; }
  function kpi(label, value, sub, isName) {
    var d = el('div', 'kpi'); d.appendChild(el('span', 'lab', label)); d.appendChild(el('span', 'val' + (isName ? ' name' : ''), value));
    if (sub) d.appendChild(el('span', 'sub', sub)); return d;
  }
  function renderKpis() {
    var k = $('overview'); k.textContent = '';
    var all = Float64Array.from(res.vac, function (i) { return VAC[i][VW]; }).sort(), sum = 0, inds = {}, emps = {};
    var resp = Float64Array.from(res.vac.map(function (i) { return VAC[i][RS]; }).filter(function (x) { return x != null; })).sort();
    res.vac.forEach(function (i) { var r = VAC[i]; sum += r[VW]; inds[PROF[r[P]].i] = 1; emps[r[EM] || ''] = 1; });
    var ni = Object.keys(inds).length, ne = Object.keys(emps).length;
    k.appendChild(kpi('Профессий', fmt(res.profs.length), ni + ' ' + plural(ni, 'отрасль', 'отрасли', 'отраслей')));
    k.appendChild(kpi('Вакансий', fmt(res.vac.length), fmt(ne) + ' ' + plural(ne, 'работодатель', 'работодателя', 'работодателей')));
    k.appendChild(kpi('Смотрят сейчас', fmt(sum), 'человек на всех вакансиях'));
    k.appendChild(kpi('Медиана на вакансию', all.length ? fmt(quant(all, .5)) : '—', 'среднее ' + fmt(mean(all))));
    k.appendChild(kpi('Откликов на вакансию', resp.length ? fmt(quant(resp, .5)) : '—', 'медиана · среднее ' + fmt(mean(resp))));
  }

  // ---------- render: charts ----------
  function renderCharts() {
    var key = state.metric;
    // Топ-15
    var top = res.profs.slice(0, 15);
    $('top-note').textContent = metricName() + ' «смотрят сейчас»';
    columnChart($('ch-top'), top.map(function (s) {
      return {label: PROF[s.p].n, value: s[key], on: s.p === state.prof, onClick: function () { selectProf(s.p); },
        info: {t: PROF[s.p].n, rows: [['Место', '#' + s.place], ['Медиана', fmt(s.med)], ['Среднее', fmt(s.mean)], ['Вакансий', fmt(s.n)], ['Отрасль', IND[PROF[s.p].i]]]}};
    }), {height: 300, lines: 3, hbarBelow: 560, aria: top.length ? 'Топ-15 профессий. Первое место: ' + PROF[top[0].p].n + ', ' + fmt(top[0][key]) : ''});

    // Сферы
    var totV = 0, totN = res.vac.length, bySph = {};
    res.vac.forEach(function (k) { var i = PROF[VAC[k][P]].i, v = VAC[k][VW]; var g = bySph[i] = bySph[i] || {i: i, views: 0, n: 0}; g.views += v; g.n++; totV += v; });
    var sph = Object.keys(bySph).map(function (i) { var g = bySph[i]; g.sv = totV ? g.views / totV : 0; g.sn = totN ? g.n / totN : 0; return g; }).sort(function (a, b) { return b.sv - a.sv; });
    sphereChart($('ch-sph'), sph);

    renderFactors();
    renderCompare();
    // Предложение и интерес
    $('sc-note').textContent = metricName() + ' · лог. шкала';
    scatterChart($('ch-sc'), res.profs.map(function (s) { return {p: s.p, x: s.n, y: s[key], n: s.n}; }));
    // Работодатели
    var emp = {};
    res.vac.forEach(function (k) { var r = VAC[k], name = r[EM] || 'Без названия'; var g = emp[name] = emp[name] || {name: name, sum: 0, vals: []}; g.sum += r[VW]; g.vals.push(r[VW]); });
    // Столько строк, чтобы карточка совпала по высоте с соседними «Сферами».
    // Столько строк, чтобы карточка совпала по высоте с соседним точечным графиком (420 px).
    var nEmp = window.innerWidth > 960 ? 14 : 10;
    var emps = Object.keys(emp).map(function (x) { return emp[x]; }).sort(function (a, b) { return b.sum - a.sum; }).slice(0, nEmp);
    hbarChart($('ch-emp'), emps.map(function (g) {
      return {label: g.name, sub: g.vals.length + ' ' + plural(g.vals.length, 'вакансия', 'вакансии', 'вакансий'), value: g.sum,
        info: {t: g.name, rows: [['Смотрят сейчас, всего', fmt(g.sum)], ['Вакансий', fmt(g.vals.length)], ['Медиана на вакансию', fmt(quant(g.vals.slice().sort(asc), .5))]]}};
    }), {aria: 'Работодатели с наибольшим суммарным числом смотрящих'});
  }

  // ---------- факторы ----------
  // Эффект группы = во сколько раз (смотрят + 1) в группе выше, чем в среднем по той же профессии
  // (оба — средние геометрические). По всем вакансиям эффект в среднем равен нулю, а состав профессий
  // в группе не искажает результат.
  function upperRub(r) { return r[CU] === 'RUR' ? (r[ST] != null ? r[ST] : r[SF]) : null; }
  function schedOf(r) { var s = D.schedule[r[SC]] || ''; if (!s) return -1; if (s.indexOf('FLEXIBLE') >= 0) return 1; if (s === 'FIVE_ON_TWO_OFF') return 0; if (/TWO_ON_TWO_OFF|SIX_ON_ONE_OFF|WEEKEND/.test(s)) return 2; return 3; }
  var FACTOR_GROUPS = [
    {t: 'Формат работы', lv: FORMATS.slice(0, 5), f: function (r) { return r[FM] <= 4 ? r[FM] : -1; }},
    {t: 'Тариф размещения', lv: ['Премиум', 'Стандарт+', 'Стандарт', 'Зарплата.ру'], f: function (r) { return r[PL] < 4 ? r[PL] : -1; }},
    {t: 'Вакансия', multi: true, lv: [
      ['Продвижение ClickMe', function (r) { return has(r, F.promo); }],
      ['Брендированная', function (r) { return has(r, F.branded); }],
      ['Зарплата указана', function (r) { return r[SF] != null || r[ST] != null; }],
      ['Зарплата не указана', function (r) { return r[SF] == null && r[ST] == null; }],
      ['Тест при отклике', function (r) { return has(r, F.test); }],
      ['Сопроводительное', function (r) { return has(r, F.letter); }],
      ['Отклик без резюме', function (r) { return has(r, F.noresume); }],
      ['HR сейчас онлайн', function (r) { return has(r, F.online); }]]},
    {t: 'Работодатель', multi: true, lv: [
      ['IT-аккредитация', function (r) { return has(r, F.it); }],
      ['HR-бренд', function (r) { return has(r, F.hr); }],
      ['Рейтинг 4,5 и выше', function (r) { return r[RT] != null && r[RT] >= 4.5; }],
      ['Рейтинг ниже 4', function (r) { return r[RT] != null && r[RT] < 4; }],
      ['Нет отзывов', function (r) { return r[RT] == null; }],
      ['Кадровое агентство', function (r) { return has(r, F.agency); }],
      ['Анонимная', function (r) { return has(r, F.anon); }],
      ['Пометка антифрода', function (r) { return has(r, F.fraud); }]]},
    {t: 'Позиция в выдаче hh', lv: ['1–10', '11–20', '21–50', '51–100', '101–200', '201–500', '500+'],
      f: function (r) { var x = r[RK]; return x <= 10 ? 0 : x <= 20 ? 1 : x <= 50 ? 2 : x <= 100 ? 3 : x <= 200 ? 4 : x <= 500 ? 5 : 6; }},
    {t: 'Возраст вакансии, дней', lv: ['меньше 1', '1–3', '3–7', '7–14', '14–30', '30+'],
      f: function (r) { var a = r[AG]; if (a == null) return -1; return a < 1 ? 0 : a < 3 ? 1 : a < 7 ? 2 : a < 14 ? 3 : a < 30 ? 4 : 5; }},
    {t: 'Зарплата, верх вилки', lv: ['до 50 тыс. ₽', '50–100 тыс.', '100–150 тыс.', '150–250 тыс.', '250 тыс. и выше'],
      f: function (r) { var up = upperRub(r); if (up == null) return -1; return up < 50000 ? 0 : up < 100000 ? 1 : up < 150000 ? 2 : up < 250000 ? 3 : 4; }},
    {t: 'Опыт', lv: ['Без опыта', '1–3 года', '3–6 лет', 'Более 6 лет'],
      f: function (r) { return ['noExperience', 'between1And3', 'between3And6', 'moreThan6'].indexOf(r[EX]); }},
    {t: 'Занятость, график, регион', multi: true, lv: [
      ['Полная занятость', function (r) { return D.employment[r[EP]] === 'FULL'; }],
      ['Частичная', function (r) { return D.employment[r[EP]] === 'PART'; }],
      ['Проект, подработка', function (r) { return /PROJECT|SIDE_JOB/.test(D.employment[r[EP]] || ''); }],
      ['График 5/2', function (r) { return schedOf(r) === 0; }],
      ['Гибкий график', function (r) { return schedOf(r) === 1; }],
      ['Сменный график', function (r) { return schedOf(r) === 2; }],
      ['Москва', function (r) { return area(r) === 'Москва'; }],
      ['Санкт-Петербург', function (r) { return area(r) === 'Санкт-Петербург'; }],
      ['Другие регионы', function (r) { var a = area(r); return !!a && a !== 'Москва' && a !== 'Санкт-Петербург'; }]]}
  ];
  var FX_MIN = 15;
  // Ускорение на ~160 тыс. строк: логарифмы и признаки факторов считаются один раз,
  // медианы для подсказок — по гистограмме (счётчик «смотрят» — небольшие целые числа).
  var LV = new Float64Array(VAC.length);
  VAC.forEach(function (r, k) { LV[k] = Math.log((r[VW] || 0) + 1); });
  var HB = 512;
  function hist() { return new Uint32Array(HB); }
  function hadd(h, v) { h[v >= HB ? HB - 1 : v]++; }
  function hmed(h, n) {
    if (!n) return NaN;
    var lo = (n - 1) >> 1, hi = n >> 1, c = 0, a = -1, b = -1;
    for (var i = 0; i < HB; i++) { c += h[i]; if (a < 0 && c > lo) a = i; if (c > hi) { b = i; break; } }
    return (a + b) / 2;
  }
  var GL = null;
  function groupLevels() {
    if (GL) return GL;
    GL = FACTOR_GROUPS.map(function (g) {
      if (g.multi) { var m = new Uint16Array(VAC.length); VAC.forEach(function (r, k) { var b = 0; g.lv.forEach(function (l, i) { if (l[1](r)) b |= 1 << i; }); m[k] = b; }); return m; }
      var a = new Int8Array(VAC.length); VAC.forEach(function (r, k) { a[k] = g.f(r); }); return a;
    });
    return GL;
  }
  function signed(x, f) { var s = f.format(Math.abs(x) * 100); return (s === '0' ? '' : x > 0 ? '+' : '−') + s + '%'; }
  function renderFactors() {
    var box = $('fx'); box.textContent = '';
    var rows = state.prof >= 0 ? res.vac.filter(function (k) { return VAC[k][P] === state.prof; }) : res.vac;
    $('fac-note').textContent = (state.prof >= 0 ? PROF[state.prof].n + ' · ' : '') + 'относительно той же профессии';
    if (rows.length < FX_MIN * 2) return emptyState(box, 'Слишком мало вакансий под эти фильтры для оценки факторов.');
    var sumP = new Float64Array(PROF.length), nP = new Uint32Array(PROF.length), gl = groupLevels();
    rows.forEach(function (k) { var p = VAC[k][P]; sumP[p] += LV[k]; nP[p]++; });
    var acc = FACTOR_GROUPS.map(function (g) { return g.lv.map(function () { return {s: 0, n: 0, h: hist()}; }); });
    for (var j = 0; j < rows.length; j++) {
      var k = rows[j], r = VAC[k], d = LV[k] - sumP[r[P]] / nP[r[P]], v = r[VW];
      for (var g = 0; g < FACTOR_GROUPS.length; g++) {
        var lv = gl[g][k], A = acc[g];
        if (FACTOR_GROUPS[g].multi) { for (var i = 0; lv; i++, lv >>= 1) if (lv & 1) { A[i].s += d; A[i].n++; hadd(A[i].h, v); } }
        else if (lv >= 0) { A[lv].s += d; A[lv].n++; hadd(A[lv].h, v); }
      }
    }
    var groups = FACTOR_GROUPS.map(function (g, gi) {
      return {t: g.t, items: g.lv.map(function (l, i) { var a = acc[gi][i]; return {label: g.multi ? l[0] : l, n: a.n, lr: a.n ? a.s / a.n : 0, h: a.h}; })};
    });
    var maxAbs = 0.05;
    groups.forEach(function (g) { g.items.forEach(function (it) { if (it.n >= FX_MIN) maxAbs = Math.max(maxAbs, Math.abs(it.lr)); }); });
    groups.forEach(function (g) {
      var gd = el('div', 'fx-g'); gd.appendChild(el('h3', null, g.t));
      g.items.forEach(function (it) {
        if (!it.n) return;
        var eff = Math.exp(it.lr) - 1, row = el('div', 'fx-r' + (it.n < FX_MIN ? ' dim' : ''));
        row.appendChild(el('span', 'fx-l', it.label));
        var bar = el('span', 'fx-b'), i = el('i', it.lr >= 0 ? 'pos' : 'neg');
        i.style.width = (Math.min(1, Math.abs(it.lr) / maxAbs) * 50).toFixed(1) + '%'; bar.appendChild(i); row.appendChild(bar);
        row.appendChild(el('span', 'fx-v', signed(eff, nf0)));
        var med = hmed(it.h, it.n);
        bindTip(row, {t: g.t + ': ' + it.label, rows: [['К той же профессии', signed(eff, nf)],
          ['Медиана «смотрят»', fmt(med)], ['Вакансий', fmt(it.n) + (it.n < FX_MIN ? ' — мало данных' : '')]]});
        gd.appendChild(row);
      });
      box.appendChild(gd);
    });
  }

  // ---------- удалёнка против офиса ----------
  // По каждой профессии: во сколько раз удалённую вакансию смотрят больше, чем неудалённую (гибрид, офис, разъезды).
  // Средние геометрические «смотрят + 1»; фильтр формата не применяется, остальные фильтры — да.
  function renderCompare() {
    var box = $('cmp'); box.textContent = '';
    if (!ALL) { $('cmp-card').hidden = true; return; }
    var acc = {};
    filtered(true).forEach(function (k) {
      var r = VAC[k], f = r[FM]; if (f === 1 || f === 5) return;
      var a = acc[r[P]] = acc[r[P]] || {r: 0, nr: 0, o: 0, no: 0, hr: hist(), ho: hist()}, lg = LV[k];
      if (f === 0) { a.r += lg; a.nr++; hadd(a.hr, r[VW]); } else { a.o += lg; a.no++; hadd(a.ho, r[VW]); }
    });
    var items = Object.keys(acc).map(function (p) { var a = acc[p]; return {p: +p, a: a, lr: a.nr && a.no ? a.r / a.nr - a.o / a.no : 0}; })
      .filter(function (x) { return x.a.nr >= FX_MIN && x.a.no >= FX_MIN; }).sort(function (x, y) { return y.lr - x.lr; });
    if (!items.length) return emptyState(box, 'Нет профессий, где под эти фильтры есть и удалённые, и офисные вакансии.');
    var maxAbs = Math.max.apply(null, items.map(function (x) { return Math.abs(x.lr); }).concat([0.05]));
    items.forEach(function (x) {
      var eff = Math.exp(x.lr) - 1, row = el('div', 'fx-r');
      row.setAttribute('aria-pressed', state.prof === x.p ? 'true' : 'false');
      row.appendChild(el('span', 'fx-l', PROF[x.p].n));
      var bar = el('span', 'fx-b'), i = el('i', x.lr >= 0 ? 'pos' : 'neg');
      i.style.width = (Math.abs(x.lr) / maxAbs * 50).toFixed(1) + '%'; bar.appendChild(i); row.appendChild(bar);
      row.appendChild(el('span', 'fx-v', signed(eff, nf0)));
      bindTip(row, {t: PROF[x.p].n, rows: [['Удалённо к офису и гибриду', signed(eff, nf)],
        ['Медиана «смотрят», удалённо', fmt(hmed(x.a.hr, x.a.nr))], ['Медиана, офис и гибрид', fmt(hmed(x.a.ho, x.a.no))],
        ['Вакансий удалённо / офис', fmt(x.a.nr) + ' / ' + fmt(x.a.no)]]}, function () { selectProf(x.p); });
      box.appendChild(row);
    });
  }

  // ---------- render: lists ----------
  function barCell(value, max) {
    var b = el('span', 'bar'); var i = el('i'); i.style.width = (max > 0 ? Math.max(value / max * 100, value > 0 ? 1.5 : 0) : 0).toFixed(1) + '%'; b.appendChild(i); return b;
  }
  function renderProfs() {
    var list = $('prof-list'); list.textContent = '';
    var key = state.metric, rows = state.profAll ? res.profs : res.profs.slice(0, PROF_PAGE), max = res.profs.length ? res.profs[0][key] : 0;
    $('prof-note').textContent = metricName();
    if (!rows.length) list.appendChild(el('p', 'empty', 'Под фильтры не подходит ни одна профессия. Ослабьте фильтры или уменьшите «Мин. вакансий».'));
    rows.forEach(function (s) {
      var pr = PROF[s.p], b = el('button', 'prow'); b.type = 'button'; b.dataset.p = s.p;
      b.setAttribute('aria-pressed', state.prof === s.p ? 'true' : 'false');
      b.appendChild(el('span', 'place', '#' + s.place));
      var nm = el('span', 'nm', pr.n);
      nm.appendChild(el('small', null, IND[pr.i] + ' · ' + s.n + ' ' + plural(s.n, 'вакансия', 'вакансии', 'вакансий')));
      b.appendChild(nm); b.appendChild(barCell(s[key], max)); b.appendChild(el('span', 'v', fmt(s[key])));
      list.appendChild(b);
    });
    var more = $('prof-more'); more.hidden = res.profs.length <= PROF_PAGE;
    more.textContent = state.profAll ? 'Свернуть' : 'Показать все';
    $('prof-shown').textContent = 'Показано ' + rows.length + ' из ' + res.profs.length;
  }
  function vacItem(k, showProf) {
    var r = VAC[k], li = el('li', 'vac');
    var v = el('span', 'views', fmt(r[VW])); v.title = 'смотрят сейчас'; v.appendChild(el('small', null, 'смотрят')); li.appendChild(v);
    var body = el('div'); var a = el('a', null, r[TI]); a.href = 'https://hh.ru/vacancy/' + r[ID]; a.target = '_blank'; a.rel = 'noopener'; body.appendChild(a);
    if (r[PL] === 0) body.appendChild(el('span', 'chip pl', 'Премиум'));
    if (r[DU]) body.appendChild(el('span', 'chip', 'копия'));
    body.appendChild(el('span', 'm', [r[EM], salary(r), pub(r)].filter(Boolean).join(' · ')));
    body.appendChild(el('span', 'm', [showProf ? PROF[r[P]].n : '', '№' + r[RK] + ' в выдаче', r[RS] != null ? fmt(r[RS]) + ' ' + plural(r[RS], 'отклик', 'отклика', 'откликов') : '',
      area(r), r[EX] && EXP[r[EX]] ? 'опыт: ' + EXP[r[EX]].toLowerCase() : ''].filter(Boolean).join(' · ')));
    li.appendChild(body); return li;
  }
  function upper(r) { return r[CU] === 'RUR' ? (r[ST] != null ? r[ST] : r[SF]) : null; }
  var ORDER = {};
  var SORTS = {
    views: function (a, b) { return (VAC[b][VW] - VAC[a][VW]) || (VAC[a][RK] - VAC[b][RK]); },
    rank: function (a, b) { return (VAC[a][RK] - VAC[b][RK]) || (VAC[b][VW] - VAC[a][VW]); },
    resp: function (a, b) { return ((VAC[b][RS] || 0) - (VAC[a][RS] || 0)) || (VAC[b][VW] - VAC[a][VW]); },
    fresh: function (a, b) { var x = VAC[a][AG], y = VAC[b][AG]; return (x == null) - (y == null) || (x - y) || (VAC[b][VW] - VAC[a][VW]); },
    salary: function (a, b) { var x = upper(VAC[a]), y = upper(VAC[b]); return (x == null) - (y == null) || (y - x) || (VAC[b][VW] - VAC[a][VW]); }
  };
  function renderVacs() {
    var list = $('vac-list'); list.textContent = '';
    var mask = new Uint8Array(VAC.length), total = 0;
    res.vac.forEach(function (k) { if (state.prof < 0 || VAC[k][P] === state.prof) { mask[k] = 1; total++; } });
    var ord = ORDER[state.vacSort] || (ORDER[state.vacSort] = VAC.map(function (_, k) { return k; }).sort(SORTS[state.vacSort]));
    var rows = [];
    for (var j = 0; j < ord.length && rows.length < state.vacLimit; j++) if (mask[ord[j]]) rows.push(ord[j]);
    var note = $('vac-note'); note.textContent = '';
    if (state.prof >= 0) {
      var chip = el('span', 'sel', PROF[state.prof].n); var x = el('button', null, '×'); x.type = 'button'; x.setAttribute('aria-label', 'Показать вакансии всех профессий');
      x.addEventListener('click', function () { selectProf(state.prof); }); chip.appendChild(x); note.appendChild(chip);
    }
    if (!rows.length) list.appendChild(el('li', 'empty', 'Нет вакансий под эти фильтры.'));
    rows.forEach(function (k) { list.appendChild(vacItem(k, state.prof < 0)); });
    $('vac-more').hidden = total <= state.vacLimit;
    $('vac-shown').textContent = 'Показано ' + fmt(Math.min(state.vacLimit, total)) + ' из ' + fmt(total);
  }
  function renderMeta() {
    var m = $('meta'); m.textContent = '';
    var a = el('span', 'live'); a.appendChild(el('b', null, META.when)); m.appendChild(a);
    m.appendChild(el('span', null, 'hh.ru · ' + (ALL ? 'все форматы работы' : 'только удалённо')));
    if (META.kind !== 'measure') { var f = el('span', 'flag', 'пробный замер'); f.title = 'Снимок сделан вне окна замера (будни, 13:00 МСК)'; m.appendChild(f); }
  }
  function render() { hideTip(); res = compute(); renderKpis(); renderCharts(); renderProfs(); renderVacs(); }

  // ---------- CSV ----------
  // Выгружаем вакансии под текущие фильтры (без фильтров — вся база дашборда). Формат под русский Excel.
  var downloads = null;
  try { if (window.claude && typeof window.claude.use === 'function') window.claude.use('downloads').then(function (x) { downloads = x; }, function () {}); } catch (e) { /* локальный файл */ }
  function csvCell(x) { if (x == null) return ''; var s = typeof x === 'number' ? String(x).replace('.', ',') : String(x); return /[;"\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; }
  function buildCsv() {
    var head = ['Отрасль', 'Профессия', 'Вакансия', 'Работодатель', 'Ссылка', 'Смотрят сейчас', 'Откликов', 'Позиция в выдаче', 'Место в выборке', 'Копия',
      'Регион', 'Формат работы', 'Зарплата от', 'Зарплата до', 'Валюта', 'До вычета налогов', 'Опыт', 'Занятость', 'График', 'Часы', 'Размещение', 'Платное продвижение',
      'Брендированное оформление', 'Тест', 'Сопроводительное', 'Отклик без резюме', 'HR онлайн', 'IT-аккредитация', 'HR-бренд', 'Кадровое агентство',
      'Анонимная', 'Пометка антифрода', 'Проверенный работодатель', 'Рейтинг работодателя', 'Отзывов', 'Опубликована', 'Возраст, дней'];
    var b = function (r, bit) { return has(r, bit) ? 1 : 0; };
    var lines = [head.map(csvCell).join(';')];
    res.vac.forEach(function (k) {
      var r = VAC[k], pr = PROF[r[P]];
      lines.push([IND[pr.i], pr.n, r[TI], r[EM], 'https://hh.ru/vacancy/' + r[ID], r[VW], r[RS], r[RK], r[DU] ? '' : r[SR], r[DU],
        area(r), FORMATS[r[FM]], r[SF], r[ST], r[CU], r[SF] == null && r[ST] == null ? '' : b(r, F.gross), EXP[r[EX]] || r[EX],
        EMPL[D.employment[r[EP]]] || D.employment[r[EP]], (D.schedule[r[SC]] || '').split(',').map(function (x) { return SCHED[x] || x; }).join(', '),
        (D.hours[r[WH]] || '').split(',').map(function (x) { return x === 'FLEXIBLE' ? 'гибкие' : x === 'OTHER' ? 'другое' : x.replace('HOURS_', '') + ' ч'; }).filter(function (x) { return x !== ' ч'; }).join(', '), PLACE[r[PL]], b(r, F.promo), b(r, F.branded), b(r, F.test), b(r, F.letter), b(r, F.noresume), b(r, F.online), b(r, F.it),
        b(r, F.hr), b(r, F.agency), b(r, F.anon), b(r, F.fraud), b(r, F.trusted), r[RT], r[RV], r[PD], r[AG] == null ? null : Math.round(r[AG] * 10) / 10].map(csvCell).join(';'));
    });
    return '﻿' + lines.join('\r\n') + '\r\n';
  }
  function saveCsv() {
    var narrowed = state.ind >= 0 || state.reg || state.fmt || state.exp || state.sal || state.pl || state.fresh || state.q || state.depth;
    var name = 'hh_vacancies_s' + META.snapshot + '_' + (narrowed ? 'filtered_' : '') + META.date + '.csv';
    var blob = new Blob([buildCsv()], {type: 'text/csv;charset=utf-8'});
    if (downloads) { downloads.save({filename: name, data: blob}).catch(function () {}); return; }
    var a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = name; document.body.appendChild(a); a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  }
  $('csv').addEventListener('click', saveCsv);

  // ---------- interactions ----------
  function selectProf(p) {
    state.prof = state.prof === p ? -1 : p; state.vacLimit = VAC_PAGE; hideTip();
    renderProfs(); renderVacs(); renderCharts();
    if (state.prof >= 0) $('vac-card').scrollIntoView({behavior: reduceMotion ? 'auto' : 'smooth', block: 'start'});
  }
  function setIndustry(i) { state.ind = state.ind === i ? -1 : i; state.vacLimit = VAC_PAGE; syncControls(); render(); }
  IND.forEach(function (name, i) { var o = el('option', null, name); o.value = i; $('f-ind').appendChild(o); });
  (function () {
    var add = function (v, t) { var o = el('option', null, t); o.value = v; $('f-reg').appendChild(o); };
    add('msk', 'Москва и область'); add('', 'Вся Россия');
    D.regions.forEach(function (g) { add(g[0], g[1]); });
  })();
  // Глубина — позиция вакансии в своей выдаче hh (по России для удалёнки, по своему региону для офиса).
  [0, 20, 50, 100, 200, 500, 1000].forEach(function (n) { var o = el('option', null, n ? 'Топ-' + n + ' выдачи' : 'Глубина'); o.value = n; $('f-depth').appendChild(o); });
  [META.min_n, 20, 50, 100].filter(function (n, i, a) { return n >= META.min_n && a.indexOf(n) === i; }).forEach(function (n) { var o = el('option', null, 'От ' + n + ' вакансий'); o.value = n; $('f-min').appendChild(o); });
  $('f-dups-l').textContent = 'Копии (' + META.dups + ')'; $('f-dups').parentNode.title = 'Учитывать копии одной вакансии';
  function syncControls() {
    $('f-ind').value = state.ind; $('f-reg').value = state.reg; $('f-fmt').value = state.fmt; $('f-exp').value = state.exp; $('f-sal').value = state.sal; $('f-fresh').value = state.fresh; $('f-pl').value = state.pl;
    $('f-depth').value = state.depth; $('f-min').value = state.minN; $('f-q').value = state.q; $('f-dups').checked = state.dups;
    document.querySelectorAll('.seg button').forEach(function (b) { b.setAttribute('aria-pressed', String(b.dataset.m === state.metric)); });
    // Активный фильтр подсвечивается, чтобы было видно, что срез сужен.
    [['f-ind', state.ind !== DEF.ind], ['f-reg', state.reg !== DEF.reg], ['f-fmt', state.fmt !== DEF.fmt], ['f-exp', !!state.exp], ['f-sal', !!state.sal], ['f-fresh', !!state.fresh], ['f-pl', state.pl !== ''],
     ['f-depth', state.depth !== DEF.depth], ['f-min', state.minN !== DEF.minN]].forEach(function (x) { $(x[0]).classList.toggle('on', x[1]); });
  }
  function onFilter() {
    state.ind = +$('f-ind').value; state.reg = $('f-reg').value; state.fmt = $('f-fmt').value; state.exp = $('f-exp').value; state.sal = $('f-sal').value; state.fresh = +$('f-fresh').value; state.pl = $('f-pl').value;
    state.depth = +$('f-depth').value; state.minN = +$('f-min').value; state.dups = $('f-dups').checked;
    state.vacLimit = VAC_PAGE; syncControls(); render();
  }
  document.querySelectorAll('.seg button').forEach(function (b) { b.addEventListener('click', function () { state.metric = b.dataset.m; syncControls(); render(); }); });
  if (!ALL) $('f-fmt').hidden = true;
  ['f-ind', 'f-reg', 'f-fmt', 'f-exp', 'f-sal', 'f-pl', 'f-fresh', 'f-depth', 'f-min', 'f-dups'].forEach(function (id) { $(id).addEventListener('change', onFilter); });
  var qTimer;
  $('f-q').addEventListener('input', function () { clearTimeout(qTimer); qTimer = setTimeout(function () { state.q = $('f-q').value.trim().toLowerCase(); state.vacLimit = VAC_PAGE; render(); }, 180); });
  $('filters').addEventListener('submit', function (e) { e.preventDefault(); });
  $('f-reset').addEventListener('click', function () { Object.assign(state, DEF, {prof: -1, profAll: false, vacLimit: VAC_PAGE}); syncControls(); render(); });
  $('prof-more').addEventListener('click', function () { state.profAll = !state.profAll; renderProfs(); });
  $('vac-more').addEventListener('click', function () { state.vacLimit += VAC_PAGE; renderVacs(); });
  $('vac-sort').addEventListener('change', function () { state.vacSort = $('vac-sort').value; state.vacLimit = VAC_PAGE; renderVacs(); });
  $('prof-list').addEventListener('click', function (e) { var b = e.target.closest('.prow'); if (b) selectProf(+b.dataset.p); });
  // Легенда сфер: переключает ряды.
  [['views', 'Доля просмотров', 'var(--views)'], ['vacs', 'Доля вакансий', 'var(--vacs)']].forEach(function (s) {
    var b = el('button'); b.type = 'button'; b.setAttribute('aria-pressed', 'true'); var sw = el('i'); sw.style.background = s[2]; b.appendChild(sw); b.append(s[1]);
    b.addEventListener('click', function () {
      var other = s[0] === 'views' ? 'vacs' : 'views'; if (sphShow[s[0]] && !sphShow[other]) return;
      sphShow[s[0]] = !sphShow[s[0]]; b.setAttribute('aria-pressed', String(sphShow[s[0]])); renderCharts();
    });
    $('sph-legend').appendChild(b);
  });
  // Перерисовка графиков при изменении ширины.
  var lastW = window.innerWidth, rT;
  window.addEventListener('resize', function () { if (Math.abs(window.innerWidth - lastW) < 8) return; lastW = window.innerWidth; clearTimeout(rT); rT = setTimeout(function () { hideTip(); renderCharts(); }, 150); });

  renderMeta(); syncControls(); render();
  // Подписи меряются по Fira Sans: когда шрифт догрузится, перерисовываем графики.
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () { renderCharts(); });
})();
"""


def render(data: dict, fragment: bool = False) -> str:
    # gzip + base64: ~180 тыс. вакансий занимают ~6–8 МБ вместо ~30 МБ JSON; распаковка — DecompressionStream в браузере.
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    payload = base64.b64encode(gzip.compress(raw, 9)).decode("ascii")
    body = (BODY.replace("__TITLE__", TITLE).replace("__TOP__", str(data["meta"]["top"]))
            .replace("__MIN__", str(data["meta"]["min_n"])).replace("__JS__", JS).replace("__DATA__", payload))
    head = (f'<title>{TITLE}</title>\n<meta name="description" content="Дашборд: рейтинг удалённых профессий '
            f'и вакансий hh.ru по счётчику «смотрят сейчас», с фильтрами.">\n{FONTS}\n<style>{CSS}</style>\n')
    if fragment:
        return head + body
    return ('<!doctype html>\n<html lang="ru">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
            f'{head}</head>\n<body>\n{body}\n</body>\n</html>\n')


def run(conn, cfg: dict, snapshot_id: int | None, top: int | None, out_path=None, fragment: bool = False) -> str:
    data = build_data(conn, cfg, snapshot_id, top)
    if out_path is None:
        stamp = datetime.now(MSK).strftime("%Y-%m-%d")
        out_path = cfg["paths"]["reports"] / f"dashboard_s{data['meta']['snapshot']}_{stamp}.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(data, fragment), encoding="utf-8")
    m = data["meta"]
    print(f"Дашборд: {out_path} (профессий {len(data['professions'])}, строк вакансий {len(data['vacancies'])}, копий {m['dups']})")
    return str(out_path)
