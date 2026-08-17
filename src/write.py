"""Psaní a překlad — nejužší konec trychtýře.

Sem chodí jednotky položek denně a utrácí se tu skoro celý rozpočet.
To je záměr: je to jediná část celého systému, kterou čtenář posuzuje.

Denní strop (`write.max_articles_per_day`) je tvrdá pojistka. Po rozšíření
na ~70 zdrojů je vstup zhruba čtyřikrát větší než dřív; bez stropu by
jeden bohatý den vygeneroval čtyřicet článků a odpovídající účet.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from . import ai_client, db
from .config_loader import VALID_CATEGORIES, prompt, settings
from .log import get

logger = get("write")


def _today_start_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def _remaining_quota(cfg: dict, pipeline: str) -> int:
    cap = cfg["max_articles_per_day"]
    written_today = db.count_articles_since(_today_start_iso(), pipeline)
    if written_today < 0:
        # Počet se nepodařilo zjistit. Raději konzervativně: povolí se
        # jeden článek, ať se běh nezasekne, ale ani neujede.
        logger.warning("Nepodařilo se zjistit dnešní počet článků — povoluji jen 1.")
        return 1
    remaining = max(0, cap - written_today)
    logger.info("Denní strop: %d, dnes napsáno %d, zbývá %d.", cap, written_today, remaining)
    return remaining


def _collect_sources(item: dict[str, Any], followers: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Zdroje článku: hlavní položka plus všechna další pokrytí téže
    zprávy, která se k ní během triáže připojila.

    Tvar {title, url} je závazný — frontend na něj spoléhá.
    """
    sources = [{"title": item.get("source") or item.get("title") or item["url"], "url": item["url"]}]
    for row in followers:
        if not row.get("url"):
            continue
        sources.append(
            {"title": row.get("source") or row.get("title") or row["url"], "url": row["url"]}
        )
    return sources


def _writer_input(item: dict[str, Any], extra_coverage: list[dict[str, Any]]) -> str:
    payload = {
        "category": item.get("category"),
        "source": item.get("source"),
        "url": item["url"],
        "rss_title": item.get("title"),
        "rss_summary": item.get("description"),
        "full_text": item.get("full_text") or "(nepodařilo se dotáhnout — piš z titulku a perexu)",
    }
    if extra_coverage:
        payload["additional_coverage"] = [
            {"source": c.get("source"), "title": c.get("title")} for c in extra_coverage
        ]
    return json.dumps(payload, ensure_ascii=False, indent=1)


def _validate_article(parsed: Any, label: str) -> tuple[str, str] | None:
    if not isinstance(parsed, dict):
        logger.error("%s: očekáván JSON objekt, přišlo %s", label, type(parsed).__name__)
        return None
    headline = str(parsed.get("headline") or "").strip()
    body = str(parsed.get("body") or "").strip()
    if not headline or not body:
        logger.error("%s: chybí headline nebo body.", label)
        return None
    if len(body) < 400:
        logger.error("%s: tělo má jen %d znaků, to není článek.", label, len(body))
        return None
    return headline, body


def write_one(item: dict[str, Any], cfg: dict, pipeline: str) -> bool:
    """Napíše, přeloží a uloží jeden článek. Vrací skutečný úspěch."""
    label = f"článek [{item.get('source')}] {str(item.get('title'))[:50]}"

    category = item.get("category")
    if category not in VALID_CATEGORIES:
        logger.error("%s: kategorie %r mimo taxonomii — přeskočeno.", label, category)
        db.mark_items([item["id"]], "failed")
        return False

    followers = db.select(
        "raw_items",
        columns="url,source,title",
        filters={"duplicate_of": f"eq.{item['id']}"},
        limit=20,
    )

    # --- anglická verze -------------------------------------------------
    en_text = ai_client.complete(
        model=cfg["model"],
        instructions=prompt("writer"),
        user_input=_writer_input(item, followers),
        max_output_tokens=cfg["max_output_tokens"],
        reasoning_effort=cfg["reasoning_effort"],
        label=f"psaní: {label}",
    )
    if not en_text:
        db.mark_items([item["id"]], "failed")
        return False

    english = _validate_article(ai_client.parse_json(en_text, label="psaní"), f"psaní: {label}")
    if not english:
        db.mark_items([item["id"]], "failed")
        return False
    headline_en, body_en = english

    # --- český překlad ---------------------------------------------------
    tcfg = cfg["translate"]
    cz_text = ai_client.complete(
        model=tcfg["model"],
        instructions=prompt("translator"),
        user_input=json.dumps({"headline": headline_en, "body": body_en}, ensure_ascii=False),
        max_output_tokens=tcfg["max_output_tokens"],
        reasoning_effort=tcfg["reasoning_effort"],
        label=f"překlad: {label}",
    )
    if not cz_text:
        db.mark_items([item["id"]], "failed")
        return False

    czech = _validate_article(ai_client.parse_json(cz_text, label="překlad"), f"překlad: {label}")
    if not czech:
        db.mark_items([item["id"]], "failed")
        return False
    headline_cz, body_cz = czech

    # Kontrola, že překlad nerozbil členění — frontend používá první
    # odstavec jako perex, takže rozdílný počet odstavců je vada.
    par_en = len([p for p in body_en.split("\n\n") if p.strip()])
    par_cz = len([p for p in body_cz.split("\n\n") if p.strip()])
    if par_en != par_cz:
        logger.warning("%s: EN má %d odstavců, CZ %d — ukládám i tak.", label, par_en, par_cz)

    # --- uložení ----------------------------------------------------------
    stored = db.store_article(
        category=category,
        headline_en=headline_en,
        headline_cz=headline_cz,
        body_en=body_en,
        body_cz=body_cz,
        sources=_collect_sources(item, followers),
        raw_item_id=item["id"],
        pipeline=pipeline,
    )
    if not stored:
        logger.error("%s: článek se nepodařilo uložit — položka zůstává ke zpracování.", label)
        return False

    db.mark_items([item["id"]], "used")
    logger.info("Uloženo: [%s] %s", category, headline_en[:70])
    return True


def run(items: list[dict[str, Any]]) -> int:
    """Zpracuje seznam položek do článků. Vrací počet skutečně uložených."""
    cfg = settings()["write"]
    pipeline = settings()["pipeline"]["name"]

    quota = _remaining_quota(cfg, pipeline)
    if quota <= 0:
        logger.info("Denní strop vyčerpán, nic se nepíše.")
        return 0

    written = 0
    for item in items[:quota]:
        if write_one(item, cfg, pipeline):
            written += 1

    logger.info("Psaní hotovo: %d článků uloženo z %d pokusů.", written, min(len(items), quota))
    return written
