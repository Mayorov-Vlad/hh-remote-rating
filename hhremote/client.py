"""HTTP-доступ к hh.ru: вежливый троттлинг, ретраи, детект капчи, разбор HH-Lux-InitialState."""
import html
import json
import random
import re
import time
import zlib

import truststore

# hh.ru использует цепочку сертификатов, которой нет в certifi; берём хранилище Windows.
truststore.inject_into_ssl()

import requests  # noqa: E402

STATE_RE = re.compile(r'<template[^>]*id="HH-Lux-InitialState"[^>]*>(.*?)</template>', re.S)
SEARCH_URL = "https://hh.ru/search/vacancy"
ROLES_URL = "https://api.hh.ru/professional_roles"
AREAS_URL = "https://api.hh.ru/areas/113"


class BlockedError(RuntimeError):
    """hh.ru показал капчу или заблокировал запросы. Капчу не обходим: сбор останавливается."""


class Client:
    def __init__(self, http_cfg: dict):
        self.cfg = http_cfg
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": http_cfg["user_agent"],
            "Accept-Language": "ru-RU,ru;q=0.9",
        })
        self._last = 0.0

    def _throttle(self):
        delay = random.uniform(self.cfg["delay_min"], self.cfg["delay_max"])
        wait = self._last + delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def get(self, url: str, params: dict | None = None, headers: dict | None = None) -> requests.Response:
        for attempt in range(self.cfg["retries"] + 1):
            self._throttle()
            try:
                r = self.session.get(url, params=params, headers=headers, timeout=self.cfg["timeout"])
            except requests.RequestException:
                if attempt == self.cfg["retries"]:
                    raise
                time.sleep(2 ** attempt * 3)
                continue
            if "captcha" in r.url:
                raise BlockedError(f"hh.ru запросил капчу: {r.url}")
            if r.status_code == 403:
                raise BlockedError(f"hh.ru вернул 403 на {r.url}")
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == self.cfg["retries"]:
                    r.raise_for_status()
                time.sleep(2 ** attempt * 5)
                continue
            return r
        raise RuntimeError("unreachable")

    def page_state(self, url: str, params: dict | None = None) -> tuple[int, dict]:
        r = self.get(url, params)
        m = STATE_RE.search(r.text)
        if not m:
            raise BlockedError(f"На странице нет HH-Lux-InitialState (возможно, анти-бот): {r.url}")
        return r.status_code, json.loads(html.unescape(m.group(1)))

    def professional_roles(self) -> dict:
        r = self.get(ROLES_URL, headers={"HH-User-Agent": self.cfg["user_agent"]})
        r.raise_for_status()
        return r.json()

    def areas(self) -> dict:
        """Дерево регионов России из открытого справочника hh."""
        r = self.get(AREAS_URL, headers={"HH-User-Agent": self.cfg["user_agent"]})
        r.raise_for_status()
        return r.json()

    def search(self, role_id: int, search_cfg: dict, page: int = 0) -> tuple[int, int, list[dict]]:
        """Одна страница выдачи по роли. Возвращает (http_status, totalResults, вакансии).
        Без search.work_format — все форматы работы. Счётчики фильтров hh — в self.last_clusters."""
        params = {
            "professional_role": role_id,
            "area": search_cfg["area"],
            "items_on_page": search_cfg["per_page"],
            "order_by": search_cfg["order_by"],
            "page": page,
        }
        for key in SPLIT_KEYS:
            if search_cfg.get(key):
                params[key] = search_cfg[key]
        status, state = self.page_state(SEARCH_URL, params)
        result = state.get("vacancySearchResult") or {}
        items = [parse_vacancy(v) for v in result.get("vacancies", [])]
        # positionInRequest у hh начинается с 1 на каждой странице; считаем сквозную позицию сами.
        for i, item in enumerate(items):
            item["rank"] = page * search_cfg["per_page"] + i + 1
        self.last_clusters = parse_clusters(state.get("searchClusters") or {})
        return status, result.get("totalResults", 0), items


CLUSTERS = ("work_format", "area", "experience", "employment_form", "work_schedule_by_days", "industry")
# Параметры выдачи, которыми дробятся запросы (hh отдаёт не больше 2000 вакансий на запрос).
SPLIT_KEYS = ("work_format", "experience", "employment_form", "industry", "work_schedule_by_days")


def parse_clusters(raw: dict) -> list[tuple[str, str, str, int]]:
    out = []
    for name in CLUSTERS:
        for vid, g in ((raw.get(name) or {}).get("groups") or {}).items():
            out.append((name, str(vid), g.get("title"), g.get("count")))
    return out


def is_pure_remote(work_formats: str) -> bool:
    """«Реально удалённая» вакансия: формат только REMOTE, без офиса, гибрида и разъездов."""
    return work_formats == "REMOTE"


# Класс формата работы: 0 только удалённо, 1 удалённо как один из вариантов, 2 гибрид,
# 3 офис, 4 разъездной, 5 формат не указан.
FORMAT_CLASSES = ("Только удалённо", "Удалённо как вариант", "Гибрид", "Офис", "Разъездной", "Не указан")


def format_class(work_formats: str) -> int:
    s = {x for x in (work_formats or "").split(",") if x}
    if not s:
        return 5
    if s == {"REMOTE"}:
        return 0
    if "REMOTE" in s:
        return 1
    if "HYBRID" in s:
        return 2
    if "FIELD_WORK" in s:
        return 4
    return 3


_NON_WORD = re.compile(r"[^\w]+")


def dup_key(employer_id, employer_name, title) -> str:
    """Ключ копии: один работодатель + одно и то же название (без регистра и пунктуации).
    Работодатели часто публикуют одну вакансию несколько раз под разными id."""
    who = employer_id if employer_id not in (None, "") and str(employer_id) != "nan" else employer_name
    what = _NON_WORD.sub(" ", str(title or "").lower()).strip()
    return f"{who}|{what}"


def _flat(listish, key: str) -> list:
    """hh отдаёт списки как [{"key": [..]}]; разворачиваем в плоский список."""
    out = []
    for item in listish or []:
        out.extend(item.get(key, []))
    return out


def _flag(x) -> int | None:
    return None if x is None else int(bool(x))


def _ts(x) -> str | None:
    return x.get("$") if isinstance(x, dict) else x


def raw_blob(v: dict) -> bytes:
    """Сырой объект вакансии без адреса и логотипов (тяжёлые и для анализа не нужны)."""
    v = dict(v)
    v.pop("address", None)
    if isinstance(v.get("company"), dict):
        v["company"] = {k: x for k, x in v["company"].items() if k != "logos"}
    return zlib.compress(json.dumps(v, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6)


def parse_vacancy(v: dict) -> dict:
    comp = v.get("compensation") or {}
    company = v.get("company") or {}
    area = v.get("area") or {}
    props = []
    for block in (v.get("vacancyProperties") or {}).get("properties", []):
        props.extend(p["propertyType"] for p in block.get("property", []))
    reviews = company.get("employerReviews") or {}
    badges = (company.get("badges") or {}).get("badge") or []
    hr_brand = (v.get("hr-brand") or {}).get("type") or next(
        (b.get("position") for b in badges if b.get("type") == "hrbrand"), None)
    try:
        rating = float(reviews["totalRating"]) if reviews.get("totalRating") else None
    except (TypeError, ValueError):
        rating = None
    return {
        "id": v["vacancyId"],
        "name": v.get("name"),
        "employer_id": company.get("id"),
        "employer_name": company.get("visibleName") or company.get("name"),
        "area_id": area.get("@id"),
        "area_name": area.get("name"),
        "salary_from": comp.get("from"),
        "salary_to": comp.get("to"),
        "currency": comp.get("currencyCode"),
        "experience": v.get("workExperience"),
        "work_formats": ",".join(_flat(v.get("workFormats"), "workFormatsElement")),
        "role_ids": ",".join(str(x) for x in _flat(v.get("professionalRoleIds"), "professionalRoleId")),
        "properties": ",".join(props),
        "published_at": (v.get("publicationTime") or {}).get("$"),
        "url": (v.get("links") or {}).get("desktop") or f"https://hh.ru/vacancy/{v['vacancyId']}",
        "salary_gross": _flag(comp.get("gross")) if comp.get("from") or comp.get("to") else None,
        "salary_mode": comp.get("mode"),
        "salary_frequency": comp.get("frequency"),
        "employment": v.get("employmentForm") or (v.get("employment") or {}).get("@type"),
        "schedule": ",".join(_flat(v.get("workScheduleByDays"), "workScheduleByDaysElement")),
        "working_hours": ",".join(_flat(v.get("workingHours"), "workingHoursElement")),
        "created_at": _ts(v.get("creationTime")),
        "changed_at": _ts(v.get("lastChangeTime")),
        "employer_trusted": _flag(company.get("@trusted")),
        "employer_accredited_it": _flag(company.get("accreditedITEmployer")),
        "employer_rating": rating,
        "employer_reviews": reviews.get("reviewsCount"),
        "employer_category": company.get("@category"),
        "employer_hr_brand": hr_brand,
        "employer_has_logo": int(bool((company.get("logos") or {}).get("logo"))),
        "employer_has_site": int(bool(company.get("companySiteUrl"))),
        # Брендированный шаблон вакансии — платная опция оформления.
        "branded": int(bool((v.get("branding") or {}).get("type") or v.get("template"))),
        "test_required": _flag(v.get("userTestPresent")),
        "letter_required": _flag(v.get("@responseLetterRequired")),
        "accept_incomplete": _flag(v.get("acceptIncompleteResumes")),
        "accept_temporary": _flag(v.get("acceptTemporary")),
        "internship": _flag(v.get("internship")),
        "night_shifts": _flag(v.get("nightShifts")),
        "side_job": _flag(v.get("sideJob")),
        "labor_contract": _flag(v.get("acceptLaborContract")),
        "chat": v.get("chatWritePossibility"),
        "auto_response": _flag((v.get("autoResponse") or {}).get("acceptAutoResponse")),
        "has_address": int(bool(v.get("address"))),
        "insider": int(bool(v.get("insider"))),
        "show_contacts": _flag(v.get("@showContact")),
        "labels": ",".join(str(x.get("id") if isinstance(x, dict) else x) for x in v.get("labels") or []),
        "tags": ",".join(t.get("id", "") for t in v.get("tags") or [] if isinstance(t, dict)),
        "creation_site": v.get("creationSite"),
        # Тот же счётчик, что «Сейчас эту вакансию смотрят» на странице (= excludingCurrent).
        "viewers": v.get("online_users_count"),
        "responses": v.get("responsesCount"),
        "total_responses": v.get("totalResponsesCount"),
        "manager_activity": (v.get("employerManager") or {}).get("latestActivity"),
        "raw": raw_blob(v),
    }
