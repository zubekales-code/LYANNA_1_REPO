"""Triáž: rozhodnout, co stojí za zpracování.

Levný model, vysoký objem, dávkově. Tohle je široký konec trychtýře —
sem chodí stovky položek a odchází desítky.

Pořadí kroků má důvod:

1. Nejdřív se odečtou duplicity NAPŘÍČ BĚHY (otisk titulku už jednou
   prošel do článku). Ty se ani neposílají modelu — je to zbytečné volání.
2. Pak se položky seskupí do shluků téže zprávy v rámci téhle dávky.
   Modelu jde jen zástupce shluku; ostatní zdědí jeho rozhodnutí a
   připojí se jako další odkazy do `sources` výsledného článku.
3. Teprve zbytek se pošle modelu po blocích po třiceti.

Velikost bloku není libovolná. Ve větších dávkách modely přeskakují
položky a komolí ID — což vypadá jako selhání modelu, ale je to selhání
velikosti dávky. Menší blok ten problém odstraní a umožní zůstat
u levného modelu.
"""

from __future__ import annotations

import json
from typing import Any

from . import ai_client, db, dedup
from .config_loader import VALID_CATEGORIES, prompt, settings, taxonomy_as_text
from .log import get

logger = get("triage")


def _build_instructions() -> str:
    """Statická část promptu — drží se stabilní, aby se účtovala jako
    cachovaný vstup z desetiny ceny."""
    return f"{prompt('triage')}\n\n---\n\n# Taxonomie\n\n{taxonomy_as_text()}"


def _batch_payload(items: list[dict[str, Any]]) -> str:
    """Položky pro model. Posílá se jen to, co je k rozhodnutí potřeba."""
    payload = [
        {
            "id": item["id"],
            "title": item.get("title") or "",
            "summary": (item.get("description") or "")[:600],
            "source": item.get("source") or "",
            "default_category": item.get("default_category"),
        }
        for item in items
    ]
    return json.dumps(payload, ensure_ascii=False, indent=1)


def _score_batch(items: list[dict[str, Any]], instructions: str, cfg: dict) -> dict[int, dict]:
    """Ohodnotí jeden blok. Vrací mapu id → {score, category, reason}."""
    text = ai_client.complete(
        model=cfg["model"],
        instructions=instructions,
        user_input=_batch_payload(items),
        max_output_tokens=cfg["max_output_tokens"],
        reasoning_effort=cfg["reasoning_effort"],
        label=f"triáž bloku {len(items)} položek",
    )
    if not text:
        return {}

    parsed = ai_client.parse_json(text, label="triáž")
    if not isinstance(parsed, list):
        logger.error("Triáž: očekáváno JSON pole, přišlo %s", type(parsed).__name__)
        return {}

    valid_ids = {item["id"] for item in items}
    results: dict[int, dict] = {}

    for row in parsed:
        if not isinstance(row, dict):
            continue
        try:
            item_id = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        if item_id not in valid_ids:
            logger.warning("Triáž vrátila neznámé id %s — ignorováno", item_id)
            continue

        try:
            score = int(row.get("score"))
        except (TypeError, ValueError):
            logger.warning("Triáž: id %s bez použitelného skóre — ignorováno", item_id)
            continue
        score = max(0, min(10, score))

        category = str(row.get("category") or "").strip().upper()
        if category not in VALID_CATEGORIES:
            # Neznámá kategorie není důvod položku zahodit, ale sama o sobě
            # je to signál, že model tápe — sráží se skóre pod práh.
            logger.warning("Triáž: id %s vrátilo kategorii %r mimo taxonomii", item_id, category)
            category = None
            score = min(score, 3)

        # Model jako druhá záchytná síť na duplicity: kód porovnává slova
        # v titulku, model pozná i přeformulování ("61 days" / "two months").
        target = row.get("duplicate_of")
        try:
            target = int(target) if target is not None else None
        except (TypeError, ValueError):
            target = None
        if target is not None and (target not in valid_ids or target == item_id):
            logger.warning("Triáž: id %s ukazuje na neplatnou duplicitu %s", item_id, target)
            target = None

        results[item_id] = {
            "score": score,
            "category": category,
            "reason": str(row.get("reason") or "")[:200],
            "duplicate_of": target,
        }

    missing = valid_ids - set(results)
    if missing:
        logger.warning(
            "Triáž: model vynechal %d z %d položek bloku (id: %s)",
            len(missing),
            len(items),
            sorted(missing)[:10],
        )
    return results


def _status_for(score: int, cfg: dict) -> str:
    if score >= cfg["score_min"]:
        return "scored"
    if cfg["waiting_room_min"] <= score <= cfg["waiting_room_max"]:
        return "waiting"
    return "rejected"


def run() -> dict[str, int]:
    """Zhodnotí nezpracované položky. Vrací souhrn pro log a diagnostiku."""
    cfg = settings()["triage"]
    dedup_cfg = settings()["dedup"]
    stats = {"fetched": 0, "duplicates": 0, "scored": 0, "waiting": 0, "rejected": 0, "unscored": 0}

    items = db.fetch_new_items(cfg["max_items_per_run"])
    stats["fetched"] = len(items)
    if not items:
        logger.info("Triáž: žádné nové položky.")
        return stats

    # --- 1. Duplicity napříč běhy -------------------------------------------
    already_used: set[str] = set()
    if dedup_cfg["enabled"]:
        already_used = db.find_used_cluster_keys(
            [i["cluster_key"] for i in items if i.get("cluster_key")]
        )
    fresh = [i for i in items if i.get("cluster_key") not in already_used]
    cross_run_dupes = [i["id"] for i in items if i.get("cluster_key") in already_used]
    if cross_run_dupes:
        db.mark_items(cross_run_dupes, "duplicate")
        stats["duplicates"] += len(cross_run_dupes)
        logger.info("Triáž: %d položek už prošlo dřív z jiného zdroje.", len(cross_run_dupes))

    # --- 2. Shluky téže zprávy v rámci dávky --------------------------------
    if dedup_cfg["enabled"]:
        clusters = dedup.cluster(
            fresh,
            threshold=dedup_cfg["similarity_threshold"],
            min_token_length=dedup_cfg["min_token_length"],
            min_shared_tokens=dedup_cfg["min_shared_tokens"],
        )
    else:
        clusters = [[item] for item in fresh]

    representatives = [group[0] for group in clusters]
    followers = {group[0]["id"]: group[1:] for group in clusters if len(group) > 1}
    in_batch_dupes = sum(len(rest) for rest in followers.values())
    if in_batch_dupes:
        logger.info(
            "Triáž: %d položek sloučeno do %d shluků (táž zpráva z více zdrojů).",
            in_batch_dupes + len(followers),
            len(followers),
        )

    # --- 3. Vlastní hodnocení po blocích ------------------------------------
    instructions = _build_instructions()
    batch_size = cfg["batch_size"]
    all_results: dict[int, dict] = {}

    for start in range(0, len(representatives), batch_size):
        batch = representatives[start : start + batch_size]
        logger.info(
            "Triáž: blok %d–%d z %d",
            start + 1,
            min(start + batch_size, len(representatives)),
            len(representatives),
        )
        all_results.update(_score_batch(batch, instructions, cfg))

    # --- 4. Zápis výsledků ---------------------------------------------------
    updates: list[dict[str, Any]] = []
    by_status: dict[str, list[int]] = {"scored": [], "waiting": [], "rejected": []}

    def resolve_target(item_id: int, depth: int = 0) -> int | None:
        """Rozmotá řetězec A→B→C na A→C. Model občas označí duplicitu
        položky, která je sama označená jako duplicita."""
        result = all_results.get(item_id)
        target = result.get("duplicate_of") if result else None
        if target is None or depth >= 3:
            return None
        deeper = resolve_target(target, depth + 1)
        return deeper or target

    for item in representatives:
        result = all_results.get(item["id"])
        if not result:
            # Nezhodnocená položka zůstává ve stavu 'new' a zkusí se
            # v dalším běhu. Neztrácí se.
            stats["unscored"] += 1
            continue

        model_target = resolve_target(item["id"])
        if model_target is not None:
            # Duplicita odhalená modelem, ne kódem.
            updates.append(
                {
                    "id": item["id"],
                    "url": item["url"],
                    "score": result["score"],
                    "category": result["category"],
                    "reason": "duplicita odhalená modelem",
                    "status": "duplicate",
                    "duplicate_of": model_target,
                }
            )
            stats["duplicates"] += 1
            # Následovníci téhle položky se přepojí na cíl, aby seznam
            # zdrojů výsledného článku zůstal úplný.
            for follower in followers.get(item["id"], []):
                updates.append(
                    {
                        "id": follower["id"],
                        "url": follower["url"],
                        "score": result["score"],
                        "category": result["category"],
                        "reason": "duplicita v rámci dávky",
                        "status": "duplicate",
                        "duplicate_of": model_target,
                    }
                )
                stats["duplicates"] += 1
            continue

        status = _status_for(result["score"], cfg)
        by_status[status].append(item["id"])
        updates.append(
            {
                "id": item["id"],
                "url": item["url"],  # NOT NULL, upsert ho vyžaduje
                "score": result["score"],
                "category": result["category"],
                "reason": result["reason"],
                "status": status,
                # Musí být přítomné u KAŽDÉHO řádku dávky se stejnou sadou
                # klíčů jako u duplicitních řádků níž — Supabase bulk zápis
                # (PostgREST) odmítne dávku, kde se objekty liší v klíčích
                # (PGRST102 "All object keys must match").
                "duplicate_of": None,
            }
        )

        # Následovníci shluku dostanou stejné rozhodnutí, ale status
        # 'duplicate' a odkaz na zástupce — write.py z nich pak poskládá
        # seznam zdrojů výsledného článku.
        for follower in followers.get(item["id"], []):
            updates.append(
                {
                    "id": follower["id"],
                    "url": follower["url"],
                    "score": result["score"],
                    "category": result["category"],
                    "reason": "duplicita v rámci dávky",
                    "status": "duplicate",
                    "duplicate_of": item["id"],
                }
            )
            stats["duplicates"] += 1

    written = db.upsert_merge("raw_items", updates, "id", batch_size=settings()["collect"]["upsert_batch_size"])
    if written < len(updates):
        logger.error(
            "Triáž: zapsáno jen %d z %d rozhodnutí — zbytek se zkusí v dalším běhu.",
            written,
            len(updates),
        )

    stats["scored"] = len(by_status["scored"])
    stats["waiting"] = len(by_status["waiting"])
    stats["rejected"] = len(by_status["rejected"])

    logger.info(
        "Triáž hotova: %d načteno → %d nad prahem, %d čekárna, %d zamítnuto, "
        "%d duplicit, %d nezhodnoceno",
        stats["fetched"],
        stats["scored"],
        stats["waiting"],
        stats["rejected"],
        stats["duplicates"],
        stats["unscored"],
    )
    return stats
