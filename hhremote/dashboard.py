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

TITLE = "Рейтинг вакансий hh.ru"
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


def build_data(conn, cfg: dict, snapshot_id: int | None, top: int | None, best: int | None = None) -> dict:
    snap_id = analyze._pick_snapshot(conn, snapshot_id)
    full = conn.execute("SELECT work_format FROM snapshots WHERE id = ?", (snap_id,)).fetchone()[0] == ""
    d = analyze.prepare(conn, cfg, snap_id, top, formats="all" if full else "pure")
    sample, dups, snap = d["sample"], d["dups"], d["snap"]
    min_n = cfg["curation"]["min_pure_remote"]
    if best:
        # Только best вакансий с наибольшим числом откликов по всем профессиям; одна вакансия — одна строка.
        sample = (sample.sort_values("responses", ascending=False, na_position="last")
                  .drop_duplicates("vacancy_id").head(best))
        keys = set(zip(sample["profession_id"], sample["dup_key"]))
        dups = dups[[k in keys for k in zip(dups["profession_id"], dups["dup_key"])]]
        min_n = 1
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
            cre = _str(v.created_at) or pub
            life = None if pd.isna(v.life_days) else math.floor(float(v.life_days) * 100) / 100
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
                life, (snap_day - date.fromisoformat(cre[:10])).days if cre else None,
            ])
    finished = datetime.fromisoformat(snap["finished_at"] or snap["started_at"]).astimezone(MSK)
    return {
        "meta": {
            "snapshot": d["snap_id"], "kind": snap["kind"], "date": snap["started_at"][:10],
            "when": f"{_when(snap['started_at'])}–{finished:%H:%M} МСК",
            "area": AREA_NAMES.get(snap["area_id"]) or str(snap["area_id"]),
            "top": d["top"] or 0, "min_n": min_n,
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
.filters .range { display: flex; align-items: center; gap: 6px; min-width: 0; height: 38px; padding: 0 6px 0 10px; background: var(--bg);
  border: 1px solid var(--line); border-radius: 8px; font-size: 13.5px; color: var(--text); white-space: nowrap; transition: border-color .15s, background .15s; }
.filters .range:hover, .filters .range:focus-within { border-color: var(--views); }
.filters .range.on { border-color: var(--views); background: var(--sel); color: var(--ink); font-weight: 500; }
.filters .range input { flex: 1 1 0; width: 0; min-width: 0; height: 28px; padding: 0 4px; border: 1px solid var(--line); border-radius: 6px;
  background: var(--surface); text-align: center; font-variant-numeric: tabular-nums; -moz-appearance: textfield; appearance: textfield; }
.filters .range input::-webkit-outer-spin-button, .filters .range input::-webkit-inner-spin-button { -webkit-appearance: none; margin: 0; }

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
.fx-col { display: grid; gap: 12px; align-content: start; min-width: 0; }
.fx-g { border: 1px solid var(--line); border-radius: 10px; background: #fafbfd; padding: 0 8px 8px; }
.fx-g h3 { font: 600 12.5px var(--f-body); color: var(--ink); margin: 0 -8px 6px; padding: 9px 12px 8px; border-bottom: 1px solid var(--line); background: var(--surface); border-radius: 10px 10px 0 0; }
.fx-r { display: grid; grid-template-columns: minmax(0, 1fr) 34% 3.4em; gap: 8px; align-items: center; min-height: 26px; padding: 0 4px; border-radius: 6px; }
.fx-r:hover, .fx-r:focus-visible { background: var(--sel); }
.fx-r.dim { opacity: .45; }
.fx-r { cursor: pointer; }
.fx-r[aria-pressed="true"] { background: var(--sel); box-shadow: inset 3px 0 0 var(--views); }
.fx-r[aria-pressed="true"] .fx-l { font-weight: 600; color: var(--ink); }
.fx-l { font-size: 12.5px; color: var(--text); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fx-b { position: relative; height: 10px; }
.fx-b::before { content: ""; position: absolute; left: 50%; top: -3px; bottom: -3px; width: 1px; background: var(--muted); opacity: .45; }
.fx-b i { position: absolute; top: 0; height: 100%; }
.fx-b i.pos { left: 50%; background: var(--views); border-radius: 0 4px 4px 0; }
.fx-b i.neg { right: 50%; background: var(--neg); border-radius: 4px 0 0 4px; }
.fx-v { font: 600 12.5px var(--f-body); font-variant-numeric: tabular-nums; text-align: right; color: var(--ink); }
.chip.pl { color: #1e3a8a; background: #dbeafe; }
/* рейтинг вакансий: таблица, шапка прилипает при прокрутке */
.vt-wrap { overflow: auto; max-height: 680px; border-top: 1px solid var(--line); overscroll-behavior: contain; }
.vt { width: 100%; min-width: 980px; border-collapse: separate; border-spacing: 0; font-size: 13.5px; }
.vt th { position: sticky; top: 0; z-index: 1; background: var(--surface); text-align: left; font: 600 12px var(--f-body); color: var(--muted);
  padding: 9px 10px; border-bottom: 1px solid var(--line); white-space: nowrap; }
.vt th button { background: none; border: 0; padding: 0; font: inherit; color: inherit; cursor: pointer; }
.vt th button:hover, .vt th.on button { color: var(--ink); }
.vt th.on button::after { content: " ▼"; font-size: 9px; }
.vt td { padding: 8px 9px; border-bottom: 1px solid var(--line); vertical-align: top; }
.vt tbody tr:hover td { background: var(--sel); }
.vt .num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.vt td.idx { color: var(--muted); font-size: 12px; }
.vt td.t { min-width: 240px; }
.vt td.t a { font-weight: 500; color: var(--text); text-decoration: none; }
.vt td.t a:hover { color: var(--primary); text-decoration: underline; text-underline-offset: 3px; }
.vt small { display: block; color: var(--muted); font-size: 12px; margin-top: 1px; }
.vt td.main { font-weight: 700; color: var(--views); font-size: 14px; }
.vt td.cbar { background-image: linear-gradient(var(--views-soft), var(--views-soft)); background-repeat: no-repeat; background-position: right 4px top 7px; }
/* при переходе к таблице её шапка не прячется под закреплённой строкой фильтров */
#vac-card { scroll-margin-top: 118px; }
.vac-count { font: 600 13px var(--f-body); color: var(--muted); font-variant-numeric: tabular-nums; }
#vac-note { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-right: auto; }
.vt td.p { color: var(--text); max-width: 170px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.vt td.f { white-space: nowrap; }
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
    <div class="seg" role="group" aria-label="Главная метрика"><button type="button" data-vm="resp" aria-pressed="true">Отклики</button><button type="button" data-vm="view" aria-pressed="false">Смотрят</button></div>
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
  <div class="range" id="f-life"><span>Живёт, дн.</span><input id="f-lmin" type="number" min="0" step="1" inputmode="numeric" placeholder="от" aria-label="Срок жизни от, дней"><span>–</span><input id="f-lmax" type="number" min="0" step="1" inputmode="numeric" placeholder="до" aria-label="Срок жизни до, дней"></div>
  <select id="f-depth" aria-label="Глубина выдачи"></select>
  <select id="f-min" aria-label="Минимум вакансий в профессии"></select>
</form></div>

<main class="container">
<section class="card" id="vac-card" aria-labelledby="h-vac">
  <div class="card-h"><h2 id="h-vac">Рейтинг вакансий</h2><span id="vac-note"></span>
    <select id="vac-sort" aria-label="Сортировка вакансий"><option value="resp">Больше откликов</option><option value="rpd">Больше откликов в сутки</option>
      <option value="life">Дольше живут</option><option value="views">Больше смотрят</option><option value="fresh">Новые</option>
      <option value="salary">Выше зарплата</option><option value="rank">Выше в выдаче</option></select></div>
  <div class="vt-wrap"><table class="vt"><thead id="vt-head"></thead><tbody id="vac-list"></tbody></table></div>
  <div class="foot"><button class="btn" id="vac-more" type="button">Показать ещё 50</button><span id="vac-shown"></span></div>
</section>
<section class="card" aria-labelledby="h-fac">
  <div class="card-h"><h2 id="h-fac">Что влияет на отклики</h2><span class="tag" id="fac-note">относительно той же профессии</span></div>
  <div class="fx" id="fx"></div>
</section>
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
      RS = 14, AR = 15, PL = 16, FL = 17, RT = 18, RV = 19, EP = 20, SC = 21, WH = 22, FM = 23, RG = 24, LT = 25, CR = 26;
  // Главная метрика — отклики (responsesCount hh, копятся с публикации); вторая — «смотрят сейчас».
  var METRICS = {
    resp: {col: RS, q: 'откликов', gen: 'откликов', total: 'Откликов, всего', short: 'откликов', sort: 'resp', fac: 'отклики'},
    view: {col: VW, q: '«смотрят»', gen: 'просмотров', total: 'Смотрят сейчас, всего', short: 'смотрят', sort: 'views', fac: 'просмотры'}
  };
  function M() { return METRICS[state.vm]; }
  // Срок жизни (LT) — дней с создания вакансии до замера; отклики в сутки — с делителем не меньше суток.
  function rpd(r) { return r[RS] == null || r[LT] == null ? null : r[RS] / Math.max(r[LT], 1); }
  function mv(r) { return r[M().col]; }
  var FORMATS = ['Только удалённо', 'Удалённо как вариант', 'Гибрид', 'Офис', 'Разъездной', 'Не указан'];
  // Строки вакансий хранят номера в словарях — разворачиваем один раз при загрузке.
  (function () {
    var base = Date.parse(META.date + 'T00:00:00Z');
    VAC.forEach(function (r) {
      r[TI] = D.titles[r[TI]] || ''; r[EM] = D.employers[r[EM]] || null; r[CU] = D.currencies[r[CU]] || null;
      r[EX] = D.experience[r[EX]] || null; r[PD] = r[PD] == null ? null : new Date(base - r[PD] * 864e5).toISOString().slice(0, 10);
      r[CR] = r[CR] == null ? null : new Date(base - r[CR] * 864e5).toISOString().slice(0, 10);
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

  // ---------- state & data ----------
  var haystack = VAC.map(function (r) { var pr = PROF[r[P]]; return (r[TI] + ' ' + (r[EM] || '') + ' ' + pr.n + ' ' + IND[pr.i] + ' ' + area(r)).toLowerCase(); });
  var ALL = META.formats === 'all';
  // Москва (1) и Московская область (2019) — разные регионы hh; по умолчанию смотрим их вместе.
  var MSK_REG = [1, 2019];
  var DEF = {fx: '', ind: -1, reg: '', fmt: '', exp: '', sal: '', pl: '', lmin: '', lmax: '', depth: 0, minN: META.min_n, metric: 'med', vm: 'resp', q: '', dups: false, vacSort: 'resp'};
  function fmtOk(f, s) { return !s || (s === 'r' ? f === 0 : s === 'rx' ? f === 1 : s === 'nr' ? f >= 2 : s === 'h' ? f === 2 : s === 'o' ? f === 3 : s === 'f' ? f === 4 : true); }
  var state = Object.assign({prof: -1, profAll: false, vacLimit: VAC_PAGE}, DEF), res = null;
  // Настроенные фильтры живут в браузере зрителя и восстанавливаются при следующем открытии.
  var STORE = 'hh-dash-filters-v1';
  var SAVE_KEYS = ['fx', 'reg', 'fmt', 'exp', 'sal', 'pl', 'lmin', 'lmax', 'depth', 'minN', 'metric', 'vm', 'q', 'dups', 'vacSort'];
  function save() {
    try {
      var o = {ind: state.ind >= 0 ? IND[state.ind] : ''};
      SAVE_KEYS.forEach(function (k) { o[k] = state[k]; });
      localStorage.setItem(STORE, JSON.stringify(o));
    } catch (e) { /* хранилище недоступно — фильтры просто не запомнятся */ }
  }
  function restore() {
    try {
      var o = JSON.parse(localStorage.getItem(STORE) || 'null');
      if (!o) return;
      SAVE_KEYS.forEach(function (k) { if (k in o) state[k] = o[k]; });
      state.ind = o.ind ? IND.indexOf(o.ind) : -1;
      if (!METRICS[state.vm]) state.vm = DEF.vm;
    } catch (e) { /* нет сохранённых фильтров */ }
  }

  // anyFormat — без фильтра формата; noFx — без фильтра по фактору (для самого блока «Что влияет»).
  function filtered(anyFormat, noFx) {
    var out = [], q = state.q, col = M().col, fxG = -1, fxI = -1, gl = null;
    if (state.fx && !noFx) { fxG = +state.fx.split(':')[0]; fxI = +state.fx.split(':')[1]; gl = groupLevels(); }
    for (var k = 0; k < VAC.length; k++) {
      var r = VAC[k];
      if (fxG >= 0) { var lv = gl[fxG][k]; if (FACTOR_GROUPS[fxG].multi ? !((lv >> fxI) & 1) : lv !== fxI) continue; }
      if (r[col] == null) continue;
      if (r[DU] && !state.dups) continue;
      if (state.reg === 'msk') { if (r[RG] !== 1 && r[RG] !== 2019) continue; } else if (state.reg && r[RG] !== +state.reg) continue;
      if (state.depth && r[RK] > state.depth) continue;
      if (!anyFormat && !fmtOk(r[FM], state.fmt)) continue;
      if (state.ind >= 0 && PROF[r[P]].i !== state.ind) continue;
      if (state.exp && r[EX] !== state.exp) continue;
      if (state.sal === 'any') { if (r[SF] == null && r[ST] == null) continue; }
      else if (state.sal) { if (r[CU] !== 'RUR') continue; var up = r[ST] != null ? r[ST] : r[SF]; if (up == null || up < +state.sal) continue; }
      if (state.pl === 'np') { if (r[PL] === 0) continue; } else if (state.pl !== '' && r[PL] !== +state.pl) continue;
      if (state.lmin !== '' || state.lmax !== '') {
        var lt = r[LT]; if (lt == null || (state.lmin !== '' && lt < +state.lmin) || (state.lmax !== '' && lt > +state.lmax)) continue;
      }
      if (q && haystack[k].indexOf(q) === -1) continue;
      out.push(k);
    }
    return out;
  }
  function compute() {
    // Набор профессий («от N вакансий») считаем без фильтра по фактору, чтобы клик по фактору не менял его.
    var all = filtered(false, true), rows = all, byP = {}, key = state.metric, col = M().col;
    rows.forEach(function (k) { var p = VAC[k][P]; (byP[p] = byP[p] || []).push(VAC[k][col]); });
    var profs = [];
    Object.keys(byP).forEach(function (pk) {
      var v = Float64Array.from(byP[pk]).sort(); if (v.length < state.minN) return;
      var sum = 0, zero = 0; v.forEach(function (x) { sum += x; if (x === 0) zero++; });
      profs.push({p: +pk, n: v.length, med: quant(v, .5), mean: sum / v.length, p10: quant(v, .1), p25: quant(v, .25), p75: quant(v, .75), p90: quant(v, .9), max: v[v.length - 1], zero: zero / v.length, sum: sum});
    });
    profs.sort(function (a, b) { return (b[key] - a[key]) || (b.mean - a.mean) || (b.n - a.n); });
    var keep = {}; profs.forEach(function (s, i) { s.place = i + 1; keep[s.p] = s; });
    var inKeep = function (k) { return keep[VAC[k][P]]; };
    var vacAll = all.filter(inKeep), vac = state.fx ? filtered().filter(inKeep) : vacAll;
    if (state.prof >= 0 && !keep[state.prof]) state.prof = -1;
    return {profs: profs, keep: keep, vac: vac, vacAll: vacAll};
  }

  // ---------- render: KPI ----------
  function metricName() { return state.metric === 'med' ? 'медиана' : 'среднее'; }
  function kpi(label, value, sub, isName) {
    var d = el('div', 'kpi'); d.appendChild(el('span', 'lab', label)); d.appendChild(el('span', 'val' + (isName ? ' name' : ''), value));
    if (sub) d.appendChild(el('span', 'sub', sub)); return d;
  }
  // ---------- render: charts ----------
  function renderCharts() { renderFactors(); }

  // ---------- факторы ----------
  // Эффект группы = во сколько раз (смотрят + 1) в группе выше, чем в среднем по той же профессии
  // (оба — средние геометрические). По всем вакансиям эффект в среднем равен нулю, а состав профессий
  // в группе не искажает результат.
  function upperRub(r) { return r[CU] === 'RUR' ? (r[ST] != null ? r[ST] : r[SF]) : null; }
  function schedOf(r) { var s = D.schedule[r[SC]] || ''; if (!s) return -1; if (s.indexOf('FLEXIBLE') >= 0) return 1; if (s === 'FIVE_ON_TWO_OFF') return 0; if (/TWO_ON_TWO_OFF|SIX_ON_ONE_OFF|WEEKEND/.test(s)) return 2; return 3; }
  var FACTOR_GROUPS = [
    {t: 'Формат работы', lv: FORMATS.slice(0, 5), f: function (r) { return r[FM] <= 4 ? r[FM] : -1; }},
    {t: 'Тариф размещения', lv: ['Премиум', 'Стандарт+', 'Стандарт', 'Зарплата.ру'], f: function (r) { return r[PL] < 4 ? r[PL] : -1; }},
    {t: 'Срок жизни, дней', lv: ['меньше 1', '1–3', '3–7', '7–14', '14–30', '30–90', '90+'],
      f: function (r) { var a = r[LT]; if (a == null) return -1; return a < 1 ? 0 : a < 3 ? 1 : a < 7 ? 2 : a < 14 ? 3 : a < 30 ? 4 : a < 90 ? 5 : 6; }},
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
  var LVV = new Float64Array(VAC.length), LVR = new Float64Array(VAC.length);
  VAC.forEach(function (r, k) { LVV[k] = Math.log((r[VW] || 0) + 1); LVR[k] = Math.log((r[RS] || 0) + 1); });
  function LVa() { return state.vm === 'resp' ? LVR : LVV; }
  var HB = 4096;
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
    var rows = state.prof >= 0 ? res.vacAll.filter(function (k) { return VAC[k][P] === state.prof; }) : res.vacAll;
    $('fac-note').textContent = (state.prof >= 0 ? PROF[state.prof].n + ' · ' : '') + 'относительно той же профессии';
    if (rows.length < FX_MIN * 2) return emptyState(box, 'Слишком мало вакансий под эти фильтры для оценки факторов.');
    $('h-fac').textContent = 'Что влияет на ' + M().fac;
    var sumP = new Float64Array(PROF.length), nP = new Uint32Array(PROF.length), gl = groupLevels(), LV = LVa(), col = M().col;
    rows.forEach(function (k) { var p = VAC[k][P]; sumP[p] += LV[k]; nP[p]++; });
    var acc = FACTOR_GROUPS.map(function (g) { return g.lv.map(function () { return {s: 0, n: 0, h: hist()}; }); });
    for (var j = 0; j < rows.length; j++) {
      var k = rows[j], r = VAC[k], d = LV[k] - sumP[r[P]] / nP[r[P]], v = r[col];
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
    // Колонки одной высоты: первые группы — по одной в колонку (верхний ряд),
    // остальные от больших к меньшим — в самую короткую колонку. Высота группы ≈ заголовок + 26 px на строку.
    var ncol = Math.max(1, getComputedStyle(box).gridTemplateColumns.split(' ').length);
    var cols = [], colH = [], blocks = [];
    for (var c = 0; c < ncol; c++) { cols.push(box.appendChild(el('div', 'fx-col'))); colH.push(0); }
    groups.forEach(function (g, gi) {
      var gd = el('div', 'fx-g'); gd.appendChild(el('h3', null, g.t));
      blocks.push({el: gd, h: 44 + 26 * g.items.filter(function (it) { return it.n; }).length});
      g.items.forEach(function (it, ii) {
        if (!it.n) return;
        var eff = Math.exp(it.lr) - 1, row = el('div', 'fx-r' + (it.n < FX_MIN ? ' dim' : '')), key = gi + ':' + ii;
        row.setAttribute('aria-pressed', state.fx === key ? 'true' : 'false');
        row.appendChild(el('span', 'fx-l', it.label));
        var bar = el('span', 'fx-b'), i = el('i', it.lr >= 0 ? 'pos' : 'neg');
        i.style.width = (Math.min(1, Math.abs(it.lr) / maxAbs) * 50).toFixed(1) + '%'; bar.appendChild(i); row.appendChild(bar);
        row.appendChild(el('span', 'fx-v', signed(eff, nf0)));
        var med = hmed(it.h, it.n);
        bindTip(row, {t: g.t + ': ' + it.label, rows: [['К той же профессии', signed(eff, nf)],
          ['Медиана ' + M().q, fmt(med)], ['Вакансий', fmt(it.n) + (it.n < FX_MIN ? ' — мало данных' : '')],
          ['Клик', state.fx === key ? 'снять фильтр' : 'показать в таблице']]}, function () { setFactor(key); });
        gd.appendChild(row);
      });
    });
    function place(b) { var c = colH.indexOf(Math.min.apply(null, colH)); cols[c].appendChild(b.el); colH[c] += b.h + 12; }
    blocks.slice(0, ncol).forEach(place);
    blocks.slice(ncol).sort(function (a, b) { return b.h - a.h; }).forEach(place);
  }

  // ---------- render: lists ----------
  function barCell(value, max) {
    var b = el('span', 'bar'); var i = el('i'); i.style.width = (max > 0 ? Math.max(value / max * 100, value > 0 ? 1.5 : 0) : 0).toFixed(1) + '%'; b.appendChild(i); return b;
  }
  var FMT_SHORT = ['Удалённо', 'Удалённо или офис', 'Гибрид', 'Офис', 'Разъездной', '—'];
  var COLS = [['#', null, 'num'], ['Вакансия', null, ''], ['Профессия', null, ''], ['Формат', null, ''], ['Зарплата', 'salary', 'num'],
    ['Создана', 'fresh', 'num'], ['Живёт, дн.', 'life', 'num'], ['Откликов', 'resp', 'num'], ['В сутки', 'rpd', 'num'], ['Смотрят', 'views', 'num']];
  function dateRu(s) {
    if (!s) return '—';
    var p = s.split('-'); return (+p[2]) + ' ' + MONTHS[+p[1] - 1] + (p[0] !== META.date.slice(0, 4) ? ' ' + p[0] : '');
  }
  function lifeTxt(d) { return d == null ? '—' : d < 1 ? '<1' : nf0.format(Math.floor(d)); }
  function renderVacHead() {
    var tr = el('tr');
    COLS.forEach(function (c) {
      var th = el('th', c[2] + (c[1] && c[1] === state.vacSort ? ' on' : ''));
      if (c[1]) {
        var b = el('button', null, c[0]); b.type = 'button'; b.title = 'Сортировать';
        b.addEventListener('click', function () { state.vacSort = c[1]; state.vacLimit = VAC_PAGE; $('vac-sort').value = c[1]; save(); renderVacs(); });
        th.appendChild(b);
        th.setAttribute('aria-sort', c[1] === state.vacSort ? 'descending' : 'none');
      } else th.textContent = c[0];
      tr.appendChild(th);
    });
    var head = $('vt-head'); head.textContent = ''; head.appendChild(tr);
  }
  function vacRow(k, n, mx) {
    var r = VAC[k], tr = el('tr'), mainCol = state.vm === 'resp' ? RS : VW;
    tr.appendChild(el('td', 'num idx', String(n)));
    var t = el('td', 't'), a = el('a', null, r[TI]); a.href = 'https://hh.ru/vacancy/' + r[ID]; a.target = '_blank'; a.rel = 'noopener'; t.appendChild(a);
    if (r[PL] === 0) t.appendChild(el('span', 'chip pl', 'Премиум'));
    if (r[DU]) t.appendChild(el('span', 'chip', 'копия'));
    t.appendChild(el('small', null, [r[EM], area(r)].filter(Boolean).join(' · ')));
    tr.appendChild(t);
    var pc = el('td', 'p', PROF[r[P]].n); pc.title = PROF[r[P]].n; tr.appendChild(pc);
    tr.appendChild(el('td', 'f', FMT_SHORT[r[FM]]));
    tr.appendChild(el('td', 'num', salary(r) || '—'));
    tr.appendChild(el('td', 'num', dateRu(r[CR])));
    tr.appendChild(el('td', 'num', lifeTxt(r[LT])));
    var d = rpd(r);
    var cr = el('td', 'num cbar' + (mainCol === RS ? ' main' : ''), r[RS] == null ? '—' : fmt(r[RS]));
    if (mx.r > 0 && r[RS]) cr.style.backgroundSize = Math.max(2, r[RS] / mx.r * 92).toFixed(1) + '% 20px';
    tr.appendChild(cr);
    var cd = el('td', 'num cbar', d == null ? '—' : nf.format(d));
    if (mx.d > 0 && d) cd.style.backgroundSize = Math.max(2, d / mx.d * 92).toFixed(1) + '% 20px';
    tr.appendChild(cd);
    tr.appendChild(el('td', 'num' + (mainCol === VW ? ' main' : ''), r[VW] == null ? '—' : fmt(r[VW])));
    return tr;
  }
  function upper(r) { return r[CU] === 'RUR' ? (r[ST] != null ? r[ST] : r[SF]) : null; }
  var ORDER = {};
  var SORTS = {
    views: function (a, b) { return (VAC[b][VW] - VAC[a][VW]) || (VAC[a][RK] - VAC[b][RK]); },
    rank: function (a, b) { return (VAC[a][RK] - VAC[b][RK]) || (VAC[b][VW] - VAC[a][VW]); },
    resp: function (a, b) { return ((VAC[b][RS] || 0) - (VAC[a][RS] || 0)) || (VAC[b][VW] - VAC[a][VW]); },
    fresh: function (a, b) { var x = VAC[a][LT], y = VAC[b][LT]; return (x == null) - (y == null) || (x - y) || ((VAC[b][RS] || 0) - (VAC[a][RS] || 0)); },
    life: function (a, b) { var x = VAC[a][LT], y = VAC[b][LT]; return (x == null) - (y == null) || (y - x) || ((VAC[b][RS] || 0) - (VAC[a][RS] || 0)); },
    rpd: function (a, b) { var x = rpd(VAC[a]), y = rpd(VAC[b]); return (x == null) - (y == null) || (y - x) || ((VAC[b][RS] || 0) - (VAC[a][RS] || 0)); },
    salary: function (a, b) { var x = upper(VAC[a]), y = upper(VAC[b]); return (x == null) - (y == null) || (y - x) || (VAC[b][VW] - VAC[a][VW]); }
  };
  function renderVacs() {
    var list = $('vac-list'); list.textContent = ''; renderVacHead();
    var mask = new Uint8Array(VAC.length), total = 0;
    res.vac.forEach(function (k) { if (state.prof < 0 || VAC[k][P] === state.prof) { mask[k] = 1; total++; } });
    var ord = ORDER[state.vacSort] || (ORDER[state.vacSort] = VAC.map(function (_, k) { return k; }).sort(SORTS[state.vacSort]));
    var rows = [];
    for (var j = 0; j < ord.length && rows.length < state.vacLimit; j++) if (mask[ord[j]]) rows.push(ord[j]);
    var note = $('vac-note'); note.textContent = '';
    note.appendChild(el('span', 'vac-count', fmt(total) + ' ' + plural(total, 'вакансия', 'вакансии', 'вакансий')));
    if (state.fx && fxLabel(state.fx)) {
      var fc = el('span', 'sel', fxLabel(state.fx)), fb = el('button', null, '×'); fb.type = 'button'; fb.setAttribute('aria-label', 'Снять фильтр по фактору');
      fb.addEventListener('click', function () { setFactor(state.fx); }); fc.appendChild(fb); note.appendChild(fc);
    }
    if (state.prof >= 0) {
      var chip = el('span', 'sel', PROF[state.prof].n); var x = el('button', null, '×'); x.type = 'button'; x.setAttribute('aria-label', 'Показать вакансии всех профессий');
      x.addEventListener('click', function () { selectProf(state.prof); }); chip.appendChild(x); note.appendChild(chip);
    }
    if (!rows.length) { var tr = el('tr'), td = el('td', 'empty', 'Нет вакансий под эти фильтры.'); td.colSpan = COLS.length; tr.appendChild(td); list.appendChild(tr); }
    var mx = {r: 0, d: 0};
    rows.forEach(function (k) { var r = VAC[k], d = rpd(r); if (r[RS] > mx.r) mx.r = r[RS]; if (d > mx.d) mx.d = d; });
    rows.forEach(function (k, i) { list.appendChild(vacRow(k, i + 1, mx)); });
    $('vac-more').hidden = total <= state.vacLimit;
    $('vac-shown').textContent = 'Показано ' + fmt(Math.min(state.vacLimit, total)) + ' из ' + fmt(total);
  }
  function renderMeta() {
    var m = $('meta'); m.textContent = '';
    var a = el('span', 'live'); a.appendChild(el('b', null, META.when)); m.appendChild(a);
    m.appendChild(el('span', null, 'hh.ru · ' + (ALL ? 'все форматы работы' : 'только удалённо')));
    if (META.kind !== 'measure') { var f = el('span', 'flag', 'пробный замер'); f.title = 'Снимок сделан вне окна замера (будни, 13:00 МСК)'; m.appendChild(f); }
  }
  function render() { hideTip(); res = compute(); renderCharts(); renderVacs(); document.querySelector('.vt-wrap').scrollTop = 0; }

  // ---------- CSV ----------
  // Выгружаем вакансии под текущие фильтры (без фильтров — вся база дашборда). Формат под русский Excel.
  var downloads = null;
  try { if (window.claude && typeof window.claude.use === 'function') window.claude.use('downloads').then(function (x) { downloads = x; }, function () {}); } catch (e) { /* локальный файл */ }
  function csvCell(x) { if (x == null) return ''; var s = typeof x === 'number' ? String(x).replace('.', ',') : String(x); return /[;"\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; }
  function buildCsv() {
    var head = ['Отрасль', 'Профессия', 'Вакансия', 'Работодатель', 'Ссылка', 'Смотрят сейчас', 'Откликов', 'Позиция в выдаче', 'Место в выборке', 'Копия',
      'Регион', 'Формат работы', 'Зарплата от', 'Зарплата до', 'Валюта', 'До вычета налогов', 'Опыт', 'Занятость', 'График', 'Часы', 'Размещение', 'Платное продвижение',
      'Брендированное оформление', 'Тест', 'Сопроводительное', 'Отклик без резюме', 'HR онлайн', 'IT-аккредитация', 'HR-бренд', 'Кадровое агентство',
      'Анонимная', 'Пометка антифрода', 'Проверенный работодатель', 'Рейтинг работодателя', 'Отзывов', 'Опубликована', 'Создана', 'Живёт, дней', 'Откликов в сутки'];
    var b = function (r, bit) { return has(r, bit) ? 1 : 0; };
    var lines = [head.map(csvCell).join(';')];
    res.vac.forEach(function (k) {
      var r = VAC[k], pr = PROF[r[P]];
      lines.push([IND[pr.i], pr.n, r[TI], r[EM], 'https://hh.ru/vacancy/' + r[ID], r[VW], r[RS], r[RK], r[DU] ? '' : r[SR], r[DU],
        area(r), FORMATS[r[FM]], r[SF], r[ST], r[CU], r[SF] == null && r[ST] == null ? '' : b(r, F.gross), EXP[r[EX]] || r[EX],
        EMPL[D.employment[r[EP]]] || D.employment[r[EP]], (D.schedule[r[SC]] || '').split(',').map(function (x) { return SCHED[x] || x; }).join(', '),
        (D.hours[r[WH]] || '').split(',').map(function (x) { return x === 'FLEXIBLE' ? 'гибкие' : x === 'OTHER' ? 'другое' : x.replace('HOURS_', '') + ' ч'; }).filter(function (x) { return x !== ' ч'; }).join(', '), PLACE[r[PL]], b(r, F.promo), b(r, F.branded), b(r, F.test), b(r, F.letter), b(r, F.noresume), b(r, F.online), b(r, F.it),
        b(r, F.hr), b(r, F.agency), b(r, F.anon), b(r, F.fraud), b(r, F.trusted), r[RT], r[RV], r[PD], r[CR], r[LT] == null ? null : Math.round(r[LT] * 10) / 10, rpd(r) == null ? null : Math.round(rpd(r) * 10) / 10].map(csvCell).join(';'));
    });
    return '﻿' + lines.join('\r\n') + '\r\n';
  }
  function saveCsv() {
    var narrowed = state.fx || state.ind >= 0 || state.reg || state.fmt || state.exp || state.sal || state.pl || state.lmin !== '' || state.lmax !== '' || state.q || state.depth;
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
    renderVacs(); renderCharts();
    if (state.prof >= 0) $('vac-card').scrollIntoView({behavior: reduceMotion ? 'auto' : 'smooth', block: 'start'});
  }
  function fxLabel(key) {
    var p = key.split(':'), g = FACTOR_GROUPS[+p[0]], l = g && g.lv[+p[1]];
    return g && l ? g.t + ': ' + (g.multi ? l[0] : l) : '';
  }
  function setFactor(key) {
    state.fx = state.fx === key ? '' : key; state.vacLimit = VAC_PAGE; hideTip(); save(); render();
    if (state.fx) $('vac-card').scrollIntoView({behavior: reduceMotion ? 'auto' : 'smooth', block: 'start'});
  }
  function setIndustry(i) { state.ind = state.ind === i ? -1 : i; state.vacLimit = VAC_PAGE; syncControls(); save(); render(); }
  IND.forEach(function (name, i) { var o = el('option', null, name); o.value = i; $('f-ind').appendChild(o); });
  (function () {
    var add = function (v, t) { var o = el('option', null, t); o.value = v; $('f-reg').appendChild(o); };
    add('msk', 'Москва и область'); add('', 'Вся Россия');
    D.regions.forEach(function (g) { add(g[0], g[1]); });
  })();
  // Глубина — позиция вакансии в своей выдаче hh (по России для удалёнки, по своему региону для офиса).
  [0, 20, 50, 100, 200, 500, 1000].forEach(function (n) { var o = el('option', null, n ? 'Позиция: топ-' + n : 'Позиция в выдаче: все'); o.value = n; $('f-depth').appendChild(o); });
  [META.min_n, 20, 50, 100].filter(function (n, i, a) { return n >= META.min_n && a.indexOf(n) === i; }).forEach(function (n) { var o = el('option', null, 'Профессии от ' + n + ' вакансий'); o.value = n; $('f-min').appendChild(o); });
  $('f-dups-l').textContent = 'Копии (' + META.dups + ')'; $('f-dups').parentNode.title = 'Учитывать копии одной вакансии';
  function syncControls() {
    $('f-ind').value = state.ind; $('f-reg').value = state.reg; $('f-fmt').value = state.fmt; $('f-exp').value = state.exp; $('f-sal').value = state.sal; $('f-lmin').value = state.lmin; $('f-lmax').value = state.lmax; $('f-pl').value = state.pl;
    $('f-depth').value = state.depth; $('f-min').value = state.minN; $('f-q').value = state.q; $('f-dups').checked = state.dups;
    document.querySelectorAll('.seg button[data-vm]').forEach(function (b) { b.setAttribute('aria-pressed', String(b.dataset.vm === state.vm)); });
    $('vac-sort').value = state.vacSort;
    // Активный фильтр подсвечивается, чтобы было видно, что срез сужен.
    [['f-ind', state.ind !== DEF.ind], ['f-reg', state.reg !== DEF.reg], ['f-fmt', state.fmt !== DEF.fmt], ['f-exp', !!state.exp], ['f-sal', !!state.sal], ['f-life', state.lmin !== '' || state.lmax !== ''], ['f-pl', state.pl !== ''],
     ['f-depth', state.depth !== DEF.depth], ['f-min', state.minN !== DEF.minN]].forEach(function (x) { $(x[0]).classList.toggle('on', x[1]); });
  }
  // Число дней из поля ввода: пусто или не число — без ограничения.
  function days(v) { v = String(v).trim().replace(',', '.'); return v === '' || isNaN(+v) ? '' : String(Math.max(0, Math.round(+v))); }
  function onFilter() {
    state.ind = +$('f-ind').value; state.reg = $('f-reg').value; state.fmt = $('f-fmt').value; state.exp = $('f-exp').value; state.sal = $('f-sal').value; state.lmin = days($('f-lmin').value); state.lmax = days($('f-lmax').value); state.pl = $('f-pl').value;
    state.depth = +$('f-depth').value; state.minN = +$('f-min').value; state.dups = $('f-dups').checked;
    state.vacLimit = VAC_PAGE; syncControls(); save(); render();
  }
  document.querySelectorAll('.seg button[data-vm]').forEach(function (b) { b.addEventListener('click', function () {
    state.vm = b.dataset.vm; state.vacSort = M().sort; state.vacLimit = VAC_PAGE; syncControls(); save(); render();
  }); });
  if (!ALL) $('f-fmt').hidden = true;
  ['f-ind', 'f-reg', 'f-fmt', 'f-exp', 'f-sal', 'f-pl', 'f-depth', 'f-min', 'f-dups'].forEach(function (id) { $(id).addEventListener('change', onFilter); });
  var lTimer;
  ['f-lmin', 'f-lmax'].forEach(function (id) { $(id).addEventListener('input', function () { clearTimeout(lTimer); lTimer = setTimeout(onFilter, 350); }); });
  var qTimer;
  $('f-q').addEventListener('input', function () { clearTimeout(qTimer); qTimer = setTimeout(function () { state.q = $('f-q').value.trim().toLowerCase(); state.vacLimit = VAC_PAGE; save(); render(); }, 180); });
  $('filters').addEventListener('submit', function (e) { e.preventDefault(); });
  $('f-reset').addEventListener('click', function () {
    Object.assign(state, DEF, {prof: -1, profAll: false, vacLimit: VAC_PAGE});
    try { localStorage.removeItem(STORE); } catch (e) { /* нечего чистить */ }
    syncControls(); render();
  });
  $('vac-more').addEventListener('click', function () { state.vacLimit += VAC_PAGE; renderVacs(); });
  $('vac-sort').addEventListener('change', function () { state.vacSort = $('vac-sort').value; state.vacLimit = VAC_PAGE; save(); renderVacs(); });
  // Перерисовка графиков при изменении ширины.
  var lastW = window.innerWidth, rT;
  window.addEventListener('resize', function () { if (Math.abs(window.innerWidth - lastW) < 8) return; lastW = window.innerWidth; clearTimeout(rT); rT = setTimeout(function () { hideTip(); renderCharts(); }, 150); });

  // Восстанавливаем сохранённые фильтры; значение, которого нет в списке (данные обновились), сбрасываем.
  restore(); state.lmin = days(state.lmin); state.lmax = days(state.lmax); if (state.fx && !fxLabel(state.fx)) state.fx = ''; syncControls();
  [['f-reg', 'reg'], ['f-fmt', 'fmt'], ['f-exp', 'exp'], ['f-sal', 'sal'], ['f-pl', 'pl'], ['f-depth', 'depth'], ['f-min', 'minN'], ['vac-sort', 'vacSort']]
    .forEach(function (x) { if ($(x[0]).value !== String(state[x[1]])) state[x[1]] = DEF[x[1]]; });
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


def run(conn, cfg: dict, snapshot_id: int | None, top: int | None, out_path=None, fragment: bool = False,
        best: int | None = None) -> str:
    data = build_data(conn, cfg, snapshot_id, top, best)
    if out_path is None:
        stamp = datetime.now(MSK).strftime("%Y-%m-%d")
        out_path = cfg["paths"]["reports"] / f"dashboard_s{data['meta']['snapshot']}_{stamp}.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(data, fragment), encoding="utf-8")
    m = data["meta"]
    print(f"Дашборд: {out_path} (профессий {len(data['professions'])}, строк вакансий {len(data['vacancies'])}, копий {m['dups']})")
    return str(out_path)
