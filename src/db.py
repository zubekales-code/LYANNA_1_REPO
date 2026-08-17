"""Jediná brána k Supabase.

Nikde jinde v projektu se na databázi nesahá. Když se bude měnit způsob
zápisu nebo přidávat logování, mění se to tady.

Volá se přímo PostgREST přes `requests`, ne přes supabase-py. Důvod je
konkrétní: hromadný zápis potřebuje přesnou kontrolu nad hlavičkou
`Prefer` a parametrem `on_conflict`, a to je právě to místo, kde
v předchozím projektu tiše selhalo ošetření duplicit.

Tři pravidla, která se tu drží a jsou tvrdě vykoupená zkušeností:

1. `on_conflict=<sloupec>` MUSÍ doprovázet `resolution=ignore-duplicates`.
   Bez něj se hlavička chová, jako by šlo o čistý INSERT, a jediná
   kolidující položka shodí celou dávku (409 / 23505).
2. Zapisuje se po malých blocích. Shozený blok o dvaceti položkách je
   levnější ztráta než shozená dávka o dvou stech.
3. Funkce hlásí, co se SKUTEČNĚ povedlo, ne kolik se odeslalo. Proto
   se používá `return=representation` a počítají se vrácené řádky.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Sequence

import requests

from .config_loader import env
from .log import get

logger = get("db")

TIMEOUT = 30


def _base_url() -> str:
    return env("SUPABASE_URL").rstrip("/") + "/rest/v1"


def _headers(prefer: str | None = None, extra: dict[str, str] | None = None) -> dict[str, str]:
    key = env("SUPABASE_SERVICE_KEY")
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    if extra:
        headers.update(extra)
    return headers


def _chunks(seq: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


# ---------------------------------------------------------------------------
# Čtení
# ---------------------------------------------------------------------------


def select(
    table: str,
    columns: str = "*",
    filters: dict[str, str] | None = None,
    order: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Čtení z tabulky. `filters` jsou PostgREST výrazy, např. {"status": "eq.new"}."""
    params: dict[str, Any] = {"select": columns}
    if filters:
        params.update(filters)
    if order:
        params["order"] = order
    if limit is not None:
        params["limit"] = limit

    try:
        resp = requests.get(
            f"{_base_url()}/{table}", headers=_headers(), params=params, timeout=TIMEOUT
        )
    except requests.RequestException as exc:
        logger.error("select %s: síťová chyba: %s", table, exc)
        return []

    if resp.status_code >= 300:
        logger.error("select %s: status %s: %s", table, resp.status_code, resp.text[:400])
        return []
    try:
        return resp.json()
    except ValueError:
        logger.error("select %s: odpověď není JSON: %s", table, resp.text[:200])
        return []


def count(table: str, filters: dict[str, str] | None = None) -> int:
    """Počet řádků. Vrací -1, když se počet nepodařilo zjistit — což je
    jiná informace než nula a volající se podle toho může zařídit."""
    params: dict[str, Any] = {"select": "id", "limit": 1}
    if filters:
        params.update(filters)
    try:
        resp = requests.get(
            f"{_base_url()}/{table}",
            headers=_headers(prefer="count=exact", extra={"Range-Unit": "items", "Range": "0-0"}),
            params=params,
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.error("count %s: síťová chyba: %s", table, exc)
        return -1

    if resp.status_code >= 300:
        logger.error("count %s: status %s: %s", table, resp.status_code, resp.text[:300])
        return -1

    # Content-Range má tvar "0-0/137" nebo "*/137"
    content_range = resp.headers.get("Content-Range", "")
    if "/" in content_range:
        total = content_range.split("/")[-1]
        if total.isdigit():
            return int(total)
    logger.warning("count %s: nečitelná hlavička Content-Range %r", table, content_range)
    return -1


# ---------------------------------------------------------------------------
# Zápis
# ---------------------------------------------------------------------------


def insert_ignore_duplicates(
    table: str,
    rows: list[dict[str, Any]],
    conflict_column: str,
    batch_size: int = 20,
) -> int:
    """Hromadný insert, který přeskočí kolize na `conflict_column`.

    Vrací počet SKUTEČNĚ vložených řádků — ne počet odeslaných. Když
    server odpoví chybou, blok se počítá jako nula a chyba jde do logu
    i s tělem odpovědi.
    """
    if not rows:
        return 0

    inserted = 0
    failed_batches = 0

    for batch in _chunks(rows, batch_size):
        try:
            resp = requests.post(
                f"{_base_url()}/{table}",
                headers=_headers(prefer="resolution=ignore-duplicates,return=representation"),
                # BEZ tohohle parametru hlavička výše tiše nefunguje a jediná
                # duplicita shodí celý blok. Viz hlavička modulu.
                params={"on_conflict": conflict_column},
                json=list(batch),
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            failed_batches += 1
            logger.error("insert %s: síťová chyba u bloku %d položek: %s", table, len(batch), exc)
            continue

        if resp.status_code >= 300:
            failed_batches += 1
            logger.error(
                "insert %s: status %s u bloku %d položek: %s",
                table,
                resp.status_code,
                len(batch),
                resp.text[:400],
            )
            continue

        try:
            returned = resp.json()
            inserted += len(returned) if isinstance(returned, list) else 0
        except ValueError:
            # Zápis prošel, jen odpověď nešla přečíst. Nelže se do plusu:
            # počítá se konzervativně nula a fakt se zaloguje.
            logger.warning("insert %s: blok prošel, ale odpověď nešlo přečíst", table)

    if failed_batches:
        logger.error(
            "insert %s: selhalo %d bloků z %d — vloženo %d z %d položek",
            table,
            failed_batches,
            (len(rows) + batch_size - 1) // batch_size,
            inserted,
            len(rows),
        )
    return inserted


def upsert_merge(
    table: str,
    rows: list[dict[str, Any]],
    conflict_column: str,
    batch_size: int = 20,
) -> int:
    """Hromadná aktualizace přes upsert (kolize se sloučí, ne přeskočí).

    Používá se k zápisu výsledků triáže: každý řádek musí obsahovat
    `conflict_column` a všechny NOT NULL sloupce tabulky.
    """
    if not rows:
        return 0

    updated = 0
    for batch in _chunks(rows, batch_size):
        try:
            resp = requests.post(
                f"{_base_url()}/{table}",
                headers=_headers(prefer="resolution=merge-duplicates,return=representation"),
                params={"on_conflict": conflict_column},
                json=list(batch),
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            logger.error("upsert %s: síťová chyba u bloku %d: %s", table, len(batch), exc)
            continue

        if resp.status_code >= 300:
            logger.error(
                "upsert %s: status %s u bloku %d: %s",
                table,
                resp.status_code,
                len(batch),
                resp.text[:400],
            )
            continue

        try:
            returned = resp.json()
            updated += len(returned) if isinstance(returned, list) else 0
        except ValueError:
            logger.warning("upsert %s: blok prošel, ale odpověď nešlo přečíst", table)

    return updated


def update_where(table: str, filters: dict[str, str], values: dict[str, Any]) -> bool:
    """PATCH nad množinou řádků. Vrací, jestli operace opravdu prošla."""
    try:
        resp = requests.patch(
            f"{_base_url()}/{table}",
            headers=_headers(prefer="return=minimal"),
            params=filters,
            json=values,
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.error("update %s: síťová chyba: %s", table, exc)
        return False

    if resp.status_code >= 300:
        logger.error("update %s: status %s: %s", table, resp.status_code, resp.text[:400])
        return False
    return True


def insert_one(table: str, row: dict[str, Any]) -> dict[str, Any] | None:
    """Vloží jeden řádek a vrátí ho i s vygenerovaným id, nebo None."""
    try:
        resp = requests.post(
            f"{_base_url()}/{table}",
            headers=_headers(prefer="return=representation"),
            json=[row],
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.error("insert_one %s: síťová chyba: %s", table, exc)
        return None

    if resp.status_code >= 300:
        logger.error("insert_one %s: status %s: %s", table, resp.status_code, resp.text[:400])
        return None

    try:
        data = resp.json()
    except ValueError:
        logger.error("insert_one %s: odpověď není JSON", table)
        return None
    return data[0] if isinstance(data, list) and data else None


# ---------------------------------------------------------------------------
# Operace specifické pro Lyannu
# ---------------------------------------------------------------------------


def store_raw_items(items: list[dict[str, Any]], batch_size: int = 20) -> int:
    """Uloží nasbírané položky. Kolize na URL se tiše přeskakují —
    přesně to dělá běh idempotentním."""
    return insert_ignore_duplicates("raw_items", items, "url", batch_size)


def fetch_new_items(limit: int) -> list[dict[str, Any]]:
    return select(
        "raw_items",
        columns="id,url,title,description,source,published_at,cluster_key",
        filters={"status": "eq.new"},
        order="collected_at.desc",
        limit=limit,
    )


def fetch_scored_items(score_min: int, limit: int) -> list[dict[str, Any]]:
    """Položky nad prahem, seřazené od nejlepší. Pořadí je důležité:
    denní strop uřízne konec seznamu, takže na začátku musí být to nejlepší."""
    return select(
        "raw_items",
        columns="id,url,title,description,source,published_at,score,category,cluster_key",
        filters={"status": "eq.scored", "score": f"gte.{score_min}"},
        order="score.desc,published_at.desc",
        limit=limit,
    )


def find_used_cluster_keys(keys: list[str]) -> set[str]:
    """Které z těchhle otisků už jednou prošly do článku.

    Tím se zachytí duplicita napříč BĚHY — zpráva, o které se psalo včera
    a která se dnes objevila znovu z jiného zdroje.
    """
    found: set[str] = set()
    if not keys:
        return found
    for batch in _chunks(sorted(set(keys)), 50):
        quoted = ",".join(f'"{k}"' for k in batch)
        rows = select(
            "raw_items",
            columns="cluster_key",
            filters={"cluster_key": f"in.({quoted})", "status": "in.(used,duplicate)"},
            limit=1000,
        )
        found.update(r["cluster_key"] for r in rows if r.get("cluster_key"))
    return found


def mark_items(ids: list[int], status: str, **extra: Any) -> bool:
    if not ids:
        return True
    id_list = ",".join(str(i) for i in ids)
    return update_where("raw_items", {"id": f"in.({id_list})"}, {"status": status, **extra})


def store_article(
    category: str,
    headline_en: str,
    headline_cz: str,
    body_en: str,
    body_cz: str,
    sources: list[dict[str, str]],
    raw_item_id: int | None,
    pipeline: str,
) -> dict[str, Any] | None:
    """Zápis hotového článku.

    `sources` musí být pole objektů {title, url} — frontend na tenhle tvar
    spoléhá (main.jsx, vykreslení stránky se zdroji).
    """
    return insert_one(
        "articles",
        {
            "category": category,
            "headline_en": headline_en,
            "headline_cz": headline_cz,
            "body_en": body_en,
            "body_cz": body_cz,
            "sources": sources,
            "raw_item_id": raw_item_id,
            "pipeline": pipeline,
        },
    )


def count_articles_since(iso_timestamp: str, pipeline: str) -> int:
    return count(
        "articles",
        {"published_at": f"gte.{iso_timestamp}", "pipeline": f"eq.{pipeline}"},
    )


def ping() -> tuple[bool, str]:
    """Ověření spojení pro diagnostiku: čtení i zápis."""
    try:
        read_ok = count("raw_items") >= 0
    except Exception as exc:  # noqa: BLE001 — diagnostika nesmí spadnout
        return False, f"čtení selhalo: {exc}"
    if not read_ok:
        return False, "čtení selhalo (viz log výše)"

    probe = {
        "url": "https://lyanna.local/diagnostics-probe",
        "title": "diagnostics probe",
        "source": "diagnose.py",
        "status": "rejected",
    }
    written = insert_ignore_duplicates("raw_items", [probe], "url", batch_size=1)
    # Druhý běh diagnostiky vrátí 0, protože řádek už existuje — to je
    # správné chování (idempotence), ne chyba.
    if written == 0:
        existing = select("raw_items", columns="id", filters={"url": f"eq.{probe['url']}"}, limit=1)
        if not existing:
            return False, "zápis selhal (viz log výše)"
        return True, "čtení i zápis OK (testovací řádek už existoval)"
    return True, "čtení i zápis OK"


def json_preview(obj: Any, limit: int = 300) -> str:
    """Pomůcka pro logování — zkrácený JSON."""
    text = json.dumps(obj, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"
