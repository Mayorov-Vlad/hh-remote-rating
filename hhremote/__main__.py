"""CLI: python -m hhremote <команда>.

  taxonomy            этап 1: отрасли и профессии из справочника hh
  areas               справочник регионов hh (город → субъект РФ) для фильтра «Регион»
  discover            этап 2: снимок по всем ролям, чтобы отобрать удалённые профессии
  curate              этап 2: автофильтр + ручные решения из curation.csv, таблица на проверку
  measure             этап 3: замер счётчиков по отобранным профессиям
  analyze             этап 4: рейтинги и выгрузка в Excel
  html                этап 4: HTML-отчёт с раскрытием отрасль → профессия → вакансии
  dashboard           этап 4: интерактивный дашборд с фильтрами и рейтингом вакансий
  pulse               пульс активности hh по времени суток (каждые N минут), --report — отчёт
  stability           сравнение основного и контрольного снимка (устойчивость рейтинга)
  export              вся база в CSV (reports/csv): таблицы + плоская таблица снимка
  status              что лежит в базе
"""
import argparse
import sys

from . import config, db


def main(argv=None):
    p = argparse.ArgumentParser(prog="hhremote")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("taxonomy")
    sub.add_parser("areas")
    for name in ("discover", "measure"):
        s = sub.add_parser(name)
        s.add_argument("--resume", type=int, help="id прерванного снимка")
        s.add_argument("--area", type=int, help="регион hh (1 — Москва, 113 — вся Россия); по умолчанию из config.toml")
        s.add_argument("--target", type=int, help="сколько уникальных чисто удалённых набирать на роль (по умолчанию sample_size)")
        s.add_argument("--max-pages", type=int, help="предел страниц выдачи на роль (по умолчанию из config.toml)")
        s.add_argument("--any-format", action="store_true", help="все форматы работы (без фильтра «удалённо»)")
        s.add_argument("--roles", help="только эти роли (id через запятую) — для проверки")
        s.add_argument("--delay", help="пауза между запросами, секунд: «мин,макс» (по умолчанию из config.toml)")
        if name == "measure":
            s.add_argument("--full", action="store_true",
                           help="большой замер: все форматы работы по профессиям из отбора, запросы дробятся по регионам и опыту")
            s.add_argument("--topup", type=int, metavar="ID",
                           help="докачать снимок measure --full: только непокрытые части крупных профессий")
            s.add_argument("--all", action="store_true",
                           help="обойти все роли, кроме исключённых вручную в curation.csv; "
                                "отбор >= min_pure_remote делает curate --snapshot по данным этого замера")
            s.add_argument("--control", action="store_true",
                           help="контрольный замер для проверки устойчивости: kind=control, analyze по умолчанию его не берёт")
            s.add_argument("--trial", action="store_true",
                           help="пробный замер вне окна активности: kind=trial, analyze по умолчанию его не берёт")
    c = sub.add_parser("curate")
    c.add_argument("--snapshot", type=int, help="id снимка для отбора (по умолчанию последний discovery)")
    a = sub.add_parser("analyze")
    a.add_argument("--snapshot", type=int, help="id снимка (по умолчанию последний measure)")
    a.add_argument("--top", type=int, help="сколько первых вакансий выдачи брать (по умолчанию sample_size)")
    for name in ("html", "dashboard"):
        h = sub.add_parser(name)
        h.add_argument("--snapshot", type=int, help="id снимка (по умолчанию последний measure)")
        h.add_argument("--top", type=int, help="размер выборки на профессию (по умолчанию sample_size)")
        h.add_argument("--out", help="путь к HTML-файлу (по умолчанию reports/)")
        h.add_argument("--fragment", action="store_true", help="без <html>/<head>/<body> — для публикации как Artifact")
    pu = sub.add_parser("pulse")
    pu.add_argument("--every", type=int, default=20, help="интервал, минут")
    pu.add_argument("--until", help="до какого момента МСК, например 2026-10-09T23:59")
    pu.add_argument("--roles", help="id ролей через запятую (по умолчанию панель из pulse.py)")
    pu.add_argument("--report", action="store_true", help="не собирать, а показать кривую активности")
    st = sub.add_parser("stability")
    st.add_argument("a", type=int, help="id основного снимка")
    st.add_argument("b", type=int, help="id контрольного снимка")
    e = sub.add_parser("export")
    e.add_argument("--snapshot", type=int, help="id снимка для плоской таблицы (по умолчанию последний measure)")
    e.add_argument("--raw", action="store_true", help="ещё и сырые объекты вакансий hh в vacancy_raw.jsonl")
    sub.add_parser("status")
    args = p.parse_args(argv)

    cfg = config.load()
    conn = db.connect(cfg["paths"]["db"])

    if args.cmd in ("taxonomy", "areas", "discover", "measure") or (args.cmd == "pulse" and not args.report):
        from .client import BlockedError, Client
        client = Client(cfg["http"])

    if args.cmd == "taxonomy":
        from . import taxonomy
        n_ind, n_prof = taxonomy.load(conn, client)
        print(f"Отраслей: {n_ind}, уникальных профессий: {n_prof}")

    elif args.cmd == "areas":
        from . import taxonomy
        print(f"Регионов и городов: {taxonomy.load_areas(conn, client)}")

    elif args.cmd in ("discover", "measure"):
        from . import snapshot
        if args.cmd == "discover":
            roles = [r[0] for r in conn.execute("SELECT id FROM professions ORDER BY id")]
        elif args.all:
            from .curate import load_overrides
            manual_off = {pid for pid, o in load_overrides(cfg["paths"]["curation"]).items() if o.get("included") == "0"}
            roles = [r[0] for r in conn.execute("SELECT id FROM professions ORDER BY id") if r[0] not in manual_off]
        else:
            roles = [r[0] for r in conn.execute("SELECT id FROM professions WHERE included = 1 ORDER BY id")]
        if args.roles:
            roles = [int(x) for x in args.roles.split(",")]
        if not roles:
            sys.exit("Нет ролей для обхода: сначала taxonomy (и curate для measure).")
        kind = ("discovery" if args.cmd == "discover" else "trial" if args.trial
                else "control" if args.control else "measure")
        search_cfg = dict(cfg["search"])
        if args.area:
            search_cfg["area"] = args.area
        if args.max_pages:
            search_cfg["max_pages"] = args.max_pages
        if args.any_format:
            search_cfg["work_format"] = ""
        if args.resume:
            # Продолжаем с теми же параметрами, с которыми снимок начат.
            search_cfg["area"] = conn.execute("SELECT area_id FROM snapshots WHERE id = ?", (args.resume,)).fetchone()[0]
        target = args.target or cfg["analysis"]["sample_size"]
        if args.delay:
            client.cfg = dict(client.cfg, delay_min=float(args.delay.split(",")[0]), delay_max=float(args.delay.split(",")[1]))
        if args.cmd == "measure" and (args.full or args.topup):
            search_cfg["work_format"] = ""
        if args.cmd == "measure" and args.topup:
            from . import collect
            try:
                collect.topup(conn, client, search_cfg, args.topup)
            except BlockedError:
                sys.exit(2)
            row = conn.execute("SELECT started_at, finished_at FROM snapshots WHERE id = ?", (args.topup,)).fetchone()
            print(f"Снимок #{args.topup} докачан: {row['started_at']} — {row['finished_at']}")
            return
        snap_id = args.resume or snapshot.create(conn, kind, search_cfg)
        try:
            if args.cmd == "measure" and args.full:
                from . import collect
                collect.run(conn, client, search_cfg, snap_id, roles)
            else:
                snapshot.run(conn, client, search_cfg, snap_id, roles, target)
        except BlockedError:
            sys.exit(2)
        row = conn.execute("SELECT started_at, finished_at FROM snapshots WHERE id = ?", (snap_id,)).fetchone()
        print(f"Снимок #{snap_id} готов: {row['started_at']} — {row['finished_at']}")

    elif args.cmd == "curate":
        from . import curate
        curate.run(conn, cfg, args.snapshot)

    elif args.cmd == "analyze":
        from . import analyze
        analyze.run(conn, cfg, args.snapshot, args.top)

    elif args.cmd in ("html", "dashboard"):
        from pathlib import Path

        from . import dashboard, report_html
        module = report_html if args.cmd == "html" else dashboard
        module.run(conn, cfg, args.snapshot, args.top, Path(args.out) if args.out else None, args.fragment)

    elif args.cmd == "pulse":
        from datetime import datetime, timedelta

        from . import pulse
        from .db import MSK
        if args.report:
            pulse.report(conn, cfg)
        else:
            until = (datetime.fromisoformat(args.until).replace(tzinfo=MSK) if args.until
                     else datetime.now(MSK) + timedelta(hours=24))
            roles = [int(x) for x in args.roles.split(",")] if args.roles else list(pulse.DEFAULT_ROLES)
            pulse.run(conn, client, cfg, args.every, until, roles)

    elif args.cmd == "stability":
        from . import stability
        stability.run(conn, cfg, args.a, args.b)

    elif args.cmd == "export":
        from . import export
        export.run(conn, cfg, args.snapshot, args.raw)

    elif args.cmd == "status":
        for t in ("industries", "professions", "snapshots", "vacancies", "observations"):
            print(f"{t}: {conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]}")
        print("included professions:", conn.execute("SELECT COUNT(*) FROM professions WHERE included = 1").fetchone()[0])
        n_pulse = conn.execute("SELECT COUNT(*) FROM snapshots WHERE kind = 'pulse'").fetchone()[0]
        if n_pulse:
            print(f"  тиков пульса: {n_pulse}")
        for s in conn.execute("SELECT * FROM snapshots WHERE kind <> 'pulse' ORDER BY id"):
            n = conn.execute("SELECT COUNT(*) FROM search_results WHERE snapshot_id = ?", (s["id"],)).fetchone()[0]
            print(f"  #{s['id']} {s['kind']} area={s['area_id']} {s['started_at']} — {s['finished_at']} ролей={n} {s['notes'] or ''}")


if __name__ == "__main__":
    main()
