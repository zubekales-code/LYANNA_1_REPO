"""Denní běh: sběr → triáž → dotažení → psaní.

Spouští se jako `python -m src.main` (nebo s `--only <fáze>` pro
samostatný krok při ladění).

Každá fáze hlásí, co se SKUTEČNĚ povedlo, ne kolik se pokusila udělat.
Rozdíl mezi "odesláno" a "uloženo" je přesně to místo, kde se dřív dalo
přehlédnout, že se neuložilo nic.
"""

from __future__ import annotations

import argparse
import sys

from . import collect_rss, db, enrich, log, triage, write
from .config_loader import ConfigError, settings, sources


def phase_collect() -> int:
    logger_ = log.get("main")
    items, failures = collect_rss.collect_all()
    if not items:
        logger_.error("Sběr nevrátil ani jednu položku — nic se neukládá.")
        return 0

    stored = db.store_raw_items(items, settings()["collect"]["upsert_batch_size"])
    logger_.info(
        "Sběr: %d položek nasbíráno, %d nových uloženo (%d už v databázi bylo).",
        len(items),
        stored,
        len(items) - stored,
    )
    if failures and len(failures) > len(sources()) // 3:
        logger_.error(
            "Selhala víc než třetina zdrojů (%d z %d) — stojí za kontrolu.",
            len(failures),
            len(sources()),
        )
    return stored


def phase_triage() -> dict[str, int]:
    return triage.run()


def phase_write() -> int:
    logger_ = log.get("main")
    cfg = settings()["write"]
    tcfg = settings()["triage"]

    # Bere se s rezervou nad denní strop: část položek může při dotažení
    # nebo psaní odpadnout a je lepší mít z čeho brát.
    candidates = db.fetch_scored_items(tcfg["score_min"], cfg["max_articles_per_day"] * 2)
    if not candidates:
        logger_.info("Žádné položky nad prahem ke zpracování.")
        return 0

    logger_.info("Ke zpracování: %d položek nad prahem %d.", len(candidates), tcfg["score_min"])
    enriched = enrich.enrich(candidates)
    return write.run(enriched)


def main() -> int:
    parser = argparse.ArgumentParser(description="Lyanna — denní běh")
    parser.add_argument(
        "--only",
        choices=["collect", "triage", "write"],
        help="Spustit jen jednu fázi (pro ladění).",
    )
    args = parser.parse_args()

    try:
        log.setup(settings().get("logging", {}).get("level", "INFO"))
    except ConfigError as exc:
        print(f"Chyba konfigurace: {exc}", file=sys.stderr)
        return 2

    logger_ = log.get("main")

    try:
        if args.only in (None, "collect"):
            phase_collect()
        if args.only in (None, "triage"):
            phase_triage()
        if args.only in (None, "write"):
            written = phase_write()
            if args.only is None and written == 0:
                # Není to nutně chyba (slabý den, vyčerpaný strop), ale
                # nemá to projít bez povšimnutí.
                logger_.warning("Běh skončil bez jediného nového článku.")
    except ConfigError as exc:
        logger_.error("Chyba konfigurace: %s", exc)
        return 2
    except Exception as exc:  # noqa: BLE001
        logger_.exception("Běh spadl na neošetřené chybě: %s", exc)
        return 1

    logger_.info("Hotovo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
