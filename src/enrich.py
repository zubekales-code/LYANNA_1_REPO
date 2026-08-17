"""Dotažení plného textu u položek, které prošly triáží.

Úzký konec trychtýře: sem chodí desítky položek, ne stovky. Proto se tu
smí utratit víc času na kus.

Jina Reader se používá ve volné variantě (bez klíče). Až se objeví
rate-limiting, stačí založit klíč a nastavit `JINA_API_KEY` — kód ho
použije sám, žádná změna kódu není potřeba.

Selhání dotažení není fatální. Když se plný text nepodaří získat
(paywall, cookie zeď, ochrana proti botům), pipeline pokračuje
z titulku a perexu. Článek bude slabší, ale vznikne — a `writer.md`
má instrukci raději napsat kratší text než doplňovat vatu.
"""

from __future__ import annotations

import os
import time

import requests

from .config_loader import settings
from .log import get

logger = get("enrich")


def _headers() -> dict[str, str]:
    headers = {"Accept": "text/plain"}
    api_key = os.environ.get("JINA_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def fetch_full_text(url: str) -> tuple[str | None, str | None]:
    """Stáhne čitelný text stránky. Vrací (text, chyba)."""
    cfg = settings()["enrich"]
    endpoint = cfg["reader_endpoint"].rstrip("/") + "/" + url

    try:
        resp = requests.get(endpoint, headers=_headers(), timeout=cfg["timeout_seconds"])
    except requests.RequestException as exc:
        return None, f"{type(exc).__name__}: {exc}"

    if resp.status_code >= 400:
        return None, f"HTTP {resp.status_code}"

    text = (resp.text or "").strip()
    if len(text) < cfg["min_chars"]:
        # Krátký výsledek obvykle znamená paywall nebo cookie zeď, ne
        # krátký článek. Hlásí se jako neúspěch, aby se poznalo, které
        # zdroje se systematicky nedaří dotáhnout.
        return None, f"jen {len(text)} znaků (nejspíš paywall)"

    return text[: cfg["max_chars"]], None


def enrich(items: list[dict]) -> list[dict]:
    """Doplní `full_text` u položek, kde to jde. Pořadí zachová."""
    cfg = settings()["enrich"]
    enriched: list[dict] = []
    succeeded = 0

    for index, item in enumerate(items):
        text, error = fetch_full_text(item["url"])
        if error:
            logger.warning("Dotažení selhalo — %s (%s): %s", item.get("title", "?")[:60], item.get("source"), error)
        else:
            succeeded += 1

        enriched.append({**item, "full_text": text})

        if index < len(items) - 1:
            time.sleep(cfg["delay_seconds"])

    logger.info("Dotažení: %d z %d položek má plný text.", succeeded, len(items))
    return enriched
