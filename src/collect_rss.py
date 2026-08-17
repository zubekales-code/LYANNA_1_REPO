"""Sběr: z ~70 RSS feedů udělat položky jednotného tvaru.

Princip fail-soft je tady nejdůležitější z celé pipeline. U sedmdesáti
externích závislostí je jisté, že několik z nich každý den nepojede —
rate limit, výpadek, změněná adresa. Cílem není, aby neselhalo nic, ale
aby selhání jednoho feedu neohrozilo zbylých šedesát devět.
"""

from __future__ import annotations

import concurrent.futures
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import feedparser
import requests

from . import dedup
from .config_loader import settings, sources
from .log import get

logger = get("collect")

# Některé servery odmítají požadavky bez rozumné identifikace klienta.
USER_AGENT = "LyannaAgent/1.0 (+https://github.com/; RSS reader)"


def _parse_date(entry: Any) -> datetime | None:
    for field in ("published_parsed", "updated_parsed"):
        value = getattr(entry, field, None) or entry.get(field) if hasattr(entry, "get") else None
        if value:
            try:
                return datetime(*value[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def _clean(text: str | None, limit: int = 1200) -> str:
    if not text:
        return ""
    # feedparser vrací perexy včetně HTML; hrubé odstranění značek stačí,
    # protože z perexu se nic nevykresluje — jen se posílá do triáže.
    import re

    plain = re.sub(r"<[^>]+>", " ", text)
    plain = re.sub(r"&[a-z]+;", " ", plain)
    plain = re.sub(r"\s+", " ", plain).strip()
    return plain[:limit]


def fetch_source(source: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, list[dict], str | None]:
    """Stáhne a znormalizuje jeden feed.

    Vrací (název zdroje, položky, chyba). Chyba je text, ne výjimka —
    volající ji jen zaloguje a jede dál.
    """
    name = source["name"]
    url = source["url"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=cfg["max_age_days"])

    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml, */*"},
            timeout=cfg["timeout_seconds"],
        )
        if resp.status_code >= 400:
            return name, [], f"HTTP {resp.status_code}"
        parsed = feedparser.parse(resp.content)
    except requests.RequestException as exc:
        return name, [], f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 — jeden feed nesmí položit běh
        return name, [], f"neočekávaná chyba: {type(exc).__name__}: {exc}"

    entries = getattr(parsed, "entries", []) or []
    if not entries:
        # bozo je příznak feedparseru, že vstup nebyl validní XML
        hint = getattr(parsed, "bozo_exception", None)
        return name, [], f"feed bez položek{f' ({hint})' if hint else ''}"

    items: list[dict[str, Any]] = []
    skipped_old = 0

    for entry in entries[: cfg["max_items_per_feed"]]:
        link = (getattr(entry, "link", "") or "").strip()
        title = (getattr(entry, "title", "") or "").strip()
        if not link or not title:
            continue

        published = _parse_date(entry)
        if published and published < cutoff:
            skipped_old += 1
            continue

        summary = getattr(entry, "summary", None) or getattr(entry, "description", None)

        items.append(
            {
                "url": link,
                "title": title[:500],
                "description": _clean(summary),
                "source": name,
                "published_at": (published or datetime.now(timezone.utc)).isoformat(),
                "cluster_key": dedup.cluster_key(title),
                "status": "new",
            }
        )

    note = f"{len(items)} položek" + (f", {skipped_old} starých vynecháno" if skipped_old else "")
    logger.debug("%s: %s", name, note)
    return name, items, None


def collect_all() -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Projde všechny aktivní zdroje. Vrací položky a mapu selhání."""
    cfg = settings()["collect"]
    source_list = sources()
    started = time.time()

    all_items: list[dict[str, Any]] = []
    failures: dict[str, str] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
        futures = {pool.submit(fetch_source, src, cfg): src["name"] for src in source_list}
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                _, items, error = future.result()
            except Exception as exc:  # noqa: BLE001
                failures[name] = f"{type(exc).__name__}: {exc}"
                continue
            if error:
                failures[name] = error
            all_items.extend(items)

    # Deduplikace v rámci běhu: tentýž článek může být ve dvou feedech
    # pod stejnou URL. Databáze by to ustála (unique na URL), ale poslat
    # to tam dvakrát je zbytečná práce.
    seen: set[str] = set()
    unique_items = []
    for item in all_items:
        key = item["url"].rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        unique_items.append(item)

    elapsed = time.time() - started
    logger.info(
        "Sběr hotov za %.1f s: %d zdrojů, %d položek (%d po odečtení duplicitních URL), %d selhání",
        elapsed,
        len(source_list),
        len(all_items),
        len(unique_items),
        len(failures),
    )
    for name, error in sorted(failures.items()):
        logger.warning("  zdroj selhal — %s: %s", name, error)

    return unique_items, failures
