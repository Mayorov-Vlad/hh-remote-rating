"""HTML-отчёт с раскрытием: отрасль → профессия → вакансии выборки со ссылками на hh.ru.

Страница самодостаточная: данные вшиты в разметку (<details>), JS нужен только для поиска
и кнопок «раскрыть/свернуть всё». fragment=True отдаёт страницу без <html>/<head>/<body>
(для публикации как Artifact, где обёртку добавляет платформа).
"""
from datetime import datetime
from html import escape

import pandas as pd

from . import analyze
from .db import MSK

MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
MONTHS_FULL = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
               "сентября", "октября", "ноября", "декабря"]
CURRENCY = {"RUR": "₽", "USD": "$", "EUR": "€", "KZT": "₸", "BYR": "Br", "UZS": "сум"}


def plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


def txt(x) -> str:
    return "" if x is None or (isinstance(x, float) and pd.isna(x)) else str(x)


def num(x, digits: int = 1) -> str:
    if x is None or pd.isna(x):
        return "—"
    if float(x).is_integer():
        return f"{int(x):,}".replace(",", " ")
    return f"{x:.{digits}f}".replace(".", ",")


def salary(row) -> str:
    lo, hi = row.salary_from, row.salary_to
    cur = CURRENCY.get(row.currency or "", row.currency or "")
    if pd.isna(lo) and pd.isna(hi):
        return ""
    if not pd.isna(lo) and not pd.isna(hi):
        return f"{num(lo)}–{num(hi)} {cur}"
    return f"от {num(lo)} {cur}" if not pd.isna(lo) else f"до {num(hi)} {cur}"


def published(row) -> str:
    try:
        t = datetime.fromisoformat(row.published_at)
    except (TypeError, ValueError):
        return ""
    age = row.age_days
    ago = "" if pd.isna(age) else (" · сегодня" if age < 1 else f" · {int(age)} {plural(int(age), 'день', 'дня', 'дней')} назад")
    return f"опубл. {t.day} {MONTHS[t.month - 1]}{ago}"


def when(ts: str) -> str:
    t = datetime.fromisoformat(ts).astimezone(MSK)
    return f"{t.day} {MONTHS_FULL[t.month - 1]} {t.year}, {t:%H:%M}"


CSS = """
/* Layout: одна колонка ~980px; дерево из трёх уровней раскрытия, число «смотрят» всегда справа. */
:root {
  --bg: #f3f5f7;
  --surface: #ffffff;
  --ink: #15202b;
  --muted: #5a6672;
  --line: #dce2e8;
  --accent: #17715b;
  --accent-soft: #dcefe8;
  --bar: #2c8a70;
  --bar-track: #e6ebef;
  --warn: #9a5400;
  --warn-soft: #fbead2;
  --note: #eef2f6;
  --f-display: "Literata", Georgia, "Times New Roman", serif;
  --f-body: "Golos Text", "Segoe UI", system-ui, -apple-system, sans-serif;
  --f-num: "JetBrains Mono", ui-monospace, "Cascadia Mono", Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0e1318; --surface: #151c23; --ink: #e4eaf0; --muted: #94a1ae; --line: #25303a;
    --accent: #5cc9a7; --accent-soft: #163a30; --bar: #3fae8d; --bar-track: #222c35;
    --warn: #f1b45c; --warn-soft: #3a2a13; --note: #1a222a; color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #0e1318; --surface: #151c23; --ink: #e4eaf0; --muted: #94a1ae; --line: #25303a;
  --accent: #5cc9a7; --accent-soft: #163a30; --bar: #3fae8d; --bar-track: #222c35;
  --warn: #f1b45c; --warn-soft: #3a2a13; --note: #1a222a; color-scheme: dark;
}
[hidden] { display: none !important; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.5 var(--f-body); }
.wrap { max-width: 980px; margin: 0 auto; padding-inline: 16px; padding-block: 32px 56px; display: grid; gap: 20px; }
a { color: inherit; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }

header { display: grid; gap: 10px; }
h1 { font: 700 clamp(26px, 4.4vw, 38px)/1.15 var(--f-display); margin: 0; text-wrap: balance; letter-spacing: -0.01em; }
.lead { margin: 0; max-width: 65ch; color: var(--muted); }
.meta { display: flex; flex-wrap: wrap; gap: 6px 18px; color: var(--muted); font-size: 13.5px; }
.meta b { color: var(--ink); font-weight: 600; }
.live { display: inline-flex; align-items: center; gap: 7px; }
.live::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: var(--accent);
  box-shadow: 0 0 0 0 var(--accent); animation: pulse 2.4s ease-out infinite; }
@keyframes pulse { 0% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--accent) 55%, transparent); }
  70% { box-shadow: 0 0 0 7px transparent; } 100% { box-shadow: 0 0 0 0 transparent; } }
@media (prefers-reduced-motion: reduce) { .live::before { animation: none; } }
.banner { background: var(--warn-soft); color: var(--ink); border-radius: 8px; padding: 10px 14px; font-size: 14px; max-width: 75ch; }
.banner b { color: var(--warn); }

.tools { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; position: sticky;
  top: env(safe-area-inset-top, 0px); z-index: 2; background: var(--bg); padding-block: 8px; }
.tools input { flex: 1 1 260px; min-width: 0; font: inherit; color: var(--ink); background: var(--surface);
  border: 1px solid var(--line); border-radius: 8px; padding: 9px 12px; }
.tools button { font: 500 14px var(--f-body); color: var(--ink); background: var(--surface);
  border: 1px solid var(--line); border-radius: 8px; padding: 9px 14px; cursor: pointer; }
.tools button:hover { border-color: var(--accent); }
.found { color: var(--muted); font-size: 13.5px; }
.legend { color: var(--muted); font-size: 13px; margin: 0; }

summary { list-style: none; cursor: pointer; }
summary::-webkit-details-marker { display: none; }
.chev { width: 9px; height: 9px; border-right: 2px solid var(--muted); border-bottom: 2px solid var(--muted);
  transform: rotate(-45deg); transition: transform .15s; justify-self: center; }
details[open] > summary .chev { transform: rotate(45deg); }

.tree { display: grid; gap: 10px; }
details.ind { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; }
details.ind > summary { display: grid; grid-template-columns: 14px 2.2em minmax(0, 1fr) minmax(90px, 170px) 4.2em;
  gap: 12px; align-items: center; padding: 14px 16px; }
.place { font: 500 13px var(--f-num); color: var(--muted); font-variant-numeric: tabular-nums; }
.ind-name { font: 600 18px/1.3 var(--f-display); min-width: 0; }
.sub { display: block; font: 400 13px/1.4 var(--f-body); color: var(--muted); margin-top: 2px; }
.bar { height: 6px; background: var(--bar-track); border-radius: 3px; overflow: hidden; }
.bar i { display: block; height: 100%; width: var(--w); background: var(--bar); border-radius: 3px; }
.val { font: 700 18px var(--f-num); text-align: right; font-variant-numeric: tabular-nums; }

.profs { display: grid; border-top: 1px solid var(--line); }
details.prof + details.prof { border-top: 1px solid var(--line); }
details.prof > summary { display: grid; grid-template-columns: 14px 2.8em minmax(0, 1fr) minmax(70px, 140px) 4.2em;
  gap: 12px; align-items: center; padding: 11px 16px 11px 30px; }
details.prof > summary:hover, details.ind > summary:hover { background: var(--note); }
details.ind > summary:hover { border-radius: 12px; }
.prof-name { font-weight: 600; min-width: 0; }
.prof .val { font-size: 16px; }
.chip { display: inline-block; font: 500 11.5px var(--f-body); letter-spacing: .02em; color: var(--warn);
  background: var(--warn-soft); border-radius: 999px; padding: 1px 8px; margin-left: 6px; vertical-align: 1px; }

ol.vacs { list-style: none; margin: 0; padding: 4px 16px 14px 56px; display: grid; gap: 2px; }
li.vac { display: grid; grid-template-columns: 3.4em minmax(0, 1fr) auto; gap: 12px; align-items: baseline;
  padding: 7px 10px; border-radius: 8px; }
li.vac:hover { background: var(--note); }
.views { font: 700 15px var(--f-num); color: var(--accent); text-align: right; font-variant-numeric: tabular-nums; }
.vac-title a { font-weight: 500; text-decoration: underline; text-decoration-color: var(--line);
  text-underline-offset: 3px; overflow-wrap: anywhere; }
.vac-title a:hover { text-decoration-color: var(--accent); }
.vac-meta { display: block; color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }
.rank { font: 500 12px var(--f-num); color: var(--muted); white-space: nowrap; }

footer { color: var(--muted); font-size: 13px; max-width: 75ch; display: grid; gap: 6px; }
footer p { margin: 0; }

@media (max-width: 640px) {
  details.ind > summary { grid-template-columns: 14px 1.8em minmax(0, 1fr) 3.6em; }
  details.ind > summary .bar { grid-column: 3 / 5; grid-row: 2; }
  details.prof > summary { grid-template-columns: 14px 2.4em minmax(0, 1fr) 3.4em; padding-left: 16px; }
  details.prof > summary .bar { grid-column: 3 / 5; grid-row: 2; }
  ol.vacs { padding: 4px 8px 12px 8px; }
  li.vac { grid-template-columns: 2.8em minmax(0, 1fr); }
  .rank { grid-column: 2; }
}
"""

JS = """
(function () {
  var q = document.getElementById('q');
  var found = document.getElementById('found');
  var inds = Array.prototype.slice.call(document.querySelectorAll('details.ind'));
  function apply() {
    var s = q.value.trim().toLowerCase();
    var shown = 0;
    inds.forEach(function (ind) {
      var indHit = !!s && ind.dataset.name.indexOf(s) !== -1;
      var any = false;
      ind.querySelectorAll('details.prof').forEach(function (p) {
        var nameHit = !s || indHit || p.dataset.name.indexOf(s) !== -1;
        var vacHits = 0;
        p.querySelectorAll('li.vac').forEach(function (li) {
          var hit = nameHit || li.dataset.text.indexOf(s) !== -1;
          li.hidden = !hit;
          if (s && !nameHit && hit) vacHits++;
        });
        var show = nameHit || vacHits > 0;
        p.hidden = !show;
        if (s && vacHits > 0) p.open = true;
        if (show) { any = true; shown++; }
      });
      ind.hidden = !any;
      if (s) ind.open = any;
    });
    found.textContent = s ? 'Профессий найдено: ' + shown : '';
  }
  q.addEventListener('input', apply);
  function setAll(open) {
    document.querySelectorAll('details.ind, details.prof').forEach(function (d) { if (!d.hidden) d.open = open; });
  }
  document.getElementById('expand').addEventListener('click', function () { setAll(true); });
  document.getElementById('collapse').addEventListener('click', function () { setAll(false); });
})();
"""

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Golos+Text:wght@400;500;600'
         '&family=JetBrains+Mono:wght@500;700&family=Literata:opsz,wght@7..72,600;7..72,700&display=swap">')

TITLE = "Рейтинг удалённых профессий"


def build(conn, cfg: dict, snapshot_id: int | None, top: int | None) -> dict:
    d = analyze.prepare(conn, cfg, snapshot_id, top)
    area = conn.execute("SELECT area_name FROM vacancies v JOIN observations o ON o.vacancy_id = v.id "
                        "WHERE o.snapshot_id = ? GROUP BY area_name ORDER BY COUNT(*) DESC LIMIT 1",
                        (d["snap_id"],)).fetchone()
    d["dropped"] = d["check"][d["check"]["status"].str.startswith("выбыла")]
    d["area"] = area[0] if area else str(d["snap"]["area_id"])
    return d


def render(data: dict, fragment: bool = False) -> str:
    snap, top, sample, prof, ind = data["snap"], data["top"], data["sample"], data["prof"], data["ind"]
    max_ind = ind["median_of_medians"].max() or 1
    max_prof = prof["median"].max() or 1
    n_vac = len(sample)

    banner = ""
    if snap["kind"] == "discovery":
        banner = ('<p class="banner"><b>Предварительные данные.</b> Это пробный снимок, сделанный для отбора '
                  'профессий, и он снят не в час пик активности соискателей. Официальный рейтинг появится '
                  'после замера в будний день около 13:00 МСК (обеденное время).</p>')
    elif snap["kind"] == "trial":
        banner = ('<p class="banner"><b>Предварительные данные.</b> Пробный замер вне часа пик активности '
                  'соискателей. Официальный рейтинг появится после замера в будний день около 13:00 МСК (обеденное время).</p>')

    parts = []
    for ir in ind.itertuples():
        profs = prof[prof["industry"] == ir.industry].sort_values("place_in_industry")
        prof_html = []
        for pr in profs.itertuples():
            vacs = sample[sample["profession_id"] == pr.profession_id].sort_values("sample_rank")
            rows = []
            for v in vacs.itertuples():
                meta = " · ".join(x for x in (escape(txt(v.employer_name)), salary(v), published(v)) if x)
                text = f"{txt(v.vacancy)} {txt(v.employer_name)}".lower()
                rows.append(
                    f'<li class="vac" data-text="{escape(text)}">'
                    f'<span class="views" title="смотрят сейчас">{num(v.viewers)}</span>'
                    f'<span class="vac-title"><a href="{escape(v.url)}" target="_blank" rel="noopener">{escape(txt(v.vacancy))}</a>'
                    f'<span class="vac-meta">{meta}</span></span>'
                    f'<span class="rank" title="позиция в выдаче hh.ru">№{v.rank} в выдаче</span></li>'
                )
            chip = f'<span class="chip" title="чисто удалённых вакансий меньше {top}">{pr.n} из {top}</span>' if pr.incomplete else ""
            sub = (f'{pr.n} {plural(pr.n, "вакансия", "вакансии", "вакансий")} в выборке · '
                   f'среднее {num(pr.mean)} · на hh с отметкой «удалённо»: {num(pr.total_found)}')
            prof_html.append(
                f'<details class="prof" data-name="{escape(pr.profession.lower())}"><summary>'
                f'<span class="chev"></span><span class="place" title="место в общем рейтинге">#{pr.place}</span>'
                f'<span class="prof-name">{escape(pr.profession)}{chip}<span class="sub">{sub}</span></span>'
                f'<span class="bar"><i style="--w:{pr.median / max_prof * 100:.1f}%"></i></span>'
                f'<span class="val" title="медиана «смотрят сейчас»">{num(pr.median)}</span>'
                f'</summary><ol class="vacs">{"".join(rows)}</ol></details>'
            )
        k = int(ir.professions)
        sub = (f'{k} {plural(k, "профессия", "профессии", "профессий")} · '
               f'лидер: {escape(ir.leader)} ({num(ir.leader_median)})')
        parts.append(
            f'<details class="ind" data-name="{escape(ir.industry.lower())}"><summary>'
            f'<span class="chev"></span><span class="place">{ir.place}</span>'
            f'<span class="ind-name">{escape(ir.industry)}<span class="sub">{sub}</span></span>'
            f'<span class="bar"><i style="--w:{ir.median_of_medians / max_ind * 100:.1f}%"></i></span>'
            f'<span class="val" title="медиана медиан профессий">{num(ir.median_of_medians)}</span>'
            f'</summary><div class="profs">{"".join(prof_html)}</div></details>'
        )

    dropped = ""
    if len(data["dropped"]):
        names = ", ".join(escape(n) for n in data["dropped"]["profession"])
        dropped = f"<p>Не вошли, потому что на момент замера у них оказалось меньше 10 чисто удалённых вакансий: {names}.</p>"

    n_ind, n_prof = len(ind), len(prof)
    body = f"""<div class="wrap">
<header>
  <h1>{TITLE}</h1>
  <p class="lead">Сколько человек прямо сейчас смотрят вакансии на hh.ru. По каждой профессии взяты первые {top} вакансий выдачи с форматом «только удалённо»; число справа — медиана счётчика «Сейчас эту вакансию смотрят».</p>
  <div class="meta">
    <span class="live">Снимок: <b>{when(snap["started_at"])}–{datetime.fromisoformat(snap["finished_at"]).astimezone(MSK):%H:%M} МСК</b></span>
    <span>Регион: <b>{escape(data["area"])}</b></span>
    <span><b>{n_ind}</b> {plural(n_ind, "отрасль", "отрасли", "отраслей")} · <b>{n_prof}</b> {plural(n_prof, "профессия", "профессии", "профессий")} · <b>{num(n_vac)}</b> {plural(n_vac, "вакансия", "вакансии", "вакансий")}</span>
  </div>
  {banner}
</header>
<div class="tools">
  <input id="q" type="search" placeholder="Поиск: профессия, вакансия или работодатель" aria-label="Поиск">
  <button id="expand" type="button">Раскрыть всё</button>
  <button id="collapse" type="button">Свернуть всё</button>
  <span id="found" class="found" aria-live="polite"></span>
</div>
<p class="legend">Отрасли отсортированы по медиане медиан своих профессий, профессии внутри отрасли — по медиане. #N — место профессии в общем рейтинге. У вакансии слева — сколько человек смотрят её сейчас.</p>
<main class="tree">
{"".join(parts)}
</main>
<footer>
  <p>Отрасли и профессии взяты из справочника профессиональных ролей hh.ru. В исследование вошла профессия, у которой не меньше 10 вакансий с форматом «только удалённо»; если таких меньше {top}, в выборку идут все найденные, а у профессии стоит пометка «N из {top}». Медианы профессий с выборками разного размера сравнимы с оговоркой: в большой выборке больше вакансий из глубины выдачи, а их смотрят реже.</p>
  {dropped}
  <p>Счётчик зависит от свежести вакансии: новые вакансии смотрят чаще. Порядок вакансий — как в выдаче hh.ru по релевантности, без авторизации.</p>
</footer>
</div>
<script>{JS}</script>"""

    head = (f'<title>{TITLE}</title>\n<meta name="description" content="Медиана счётчика «смотрят сейчас» '
            f'по удалённым вакансиям hh.ru, по отраслям и профессиям.">\n{FONTS}\n<style>{CSS}</style>\n')
    if fragment:
        return head + body
    return ('<!doctype html>\n<html lang="ru">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
            f'{head}</head>\n<body>\n{body}\n</body>\n</html>\n')


def run(conn, cfg: dict, snapshot_id: int | None, top: int | None, out_path=None, fragment: bool = False) -> str:
    data = build(conn, cfg, snapshot_id, top)
    if out_path is None:
        stamp = datetime.now(MSK).strftime("%Y-%m-%d")
        out_path = cfg["paths"]["reports"] / f"rating_s{data['snap']['id']}_top{data['top']}_{stamp}.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(data, fragment), encoding="utf-8")
    print(f"HTML: {out_path}")
    return str(out_path)
