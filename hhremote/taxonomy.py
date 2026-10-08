"""Этап 1: отрасли и профессии из справочника профессиональных ролей hh."""
from .client import Client


def load_areas(conn, client: Client) -> int:
    """Регионы hh: каждому городу и району — его субъект РФ (первый уровень под «Россией»)."""
    tree = client.areas()
    rows = []

    def walk(node, parent, region):
        rows.append((int(node["id"]), node["name"], parent, region[0] if region else None, region[1] if region else None))
        for child in node.get("areas") or []:
            walk(child, int(node["id"]), region or (int(child["id"]), child["name"]))

    for reg in tree["areas"]:
        walk(reg, int(tree["id"]), (int(reg["id"]), reg["name"]))
    conn.executemany("INSERT OR REPLACE INTO areas(id, name, parent_id, region_id, region_name) VALUES (?, ?, ?, ?, ?)", rows)
    conn.commit()
    return len(rows)


def load(conn, client: Client) -> tuple[int, int]:
    data = client.professional_roles()
    for cat in data["categories"]:
        conn.execute(
            "INSERT INTO industries(id, name) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET name = excluded.name",
            (int(cat["id"]), cat["name"]),
        )
        for role in cat["roles"]:
            # Основная отрасль по умолчанию — первая категория, где встретилась роль.
            # Ручные решения (curation.csv) её потом переопределяют, повторный запуск их не трогает.
            conn.execute(
                "INSERT INTO professions(id, name, industry_id) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET name = excluded.name",
                (int(role["id"]), role["name"], int(cat["id"])),
            )
            conn.execute(
                "INSERT OR IGNORE INTO profession_industries(profession_id, industry_id) VALUES (?, ?)",
                (int(role["id"]), int(cat["id"])),
            )
    conn.commit()
    n_ind = conn.execute("SELECT COUNT(*) FROM industries").fetchone()[0]
    n_prof = conn.execute("SELECT COUNT(*) FROM professions").fetchone()[0]
    return n_ind, n_prof
