#!/usr/bin/env python3
"""Diagnostika — otestuje všechno najednou a vypíše tabulku.

Ladit naostro (spustit celou pipeline, čekat, najít jednu chybu, opakovat)
je nejdražší způsob práce. Tenhle skript ověří konfiguraci, Supabase, obě
úrovně modelu, Jina Reader a všech ~70 feedů — a řekne, co je špatně,
z jednoho výpisu.

Spouští se ručně:  python diagnose.py
Zkrácená verze:    python diagnose.py --skip-feeds

Testuje se schválně i na reálném objemu (všechny feedy, ne dva vzorky).
Právě objem odhalil chyby, které jednotlivé testovací volání minulo.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import sys
import time

from src import ai_client, collect_rss, db, enrich, log
from src.config_loader import ConfigError, prompt, settings, sources, taxonomy

OK = "OK"
FAIL = "CHYBA"
WARN = "POZOR"

results: list[tuple[str, str, str, str]] = []  # (oblast, co, stav, detail)


def record(area: str, what: str, status: str, detail: str = "") -> None:
    results.append((area, what, status, detail))


# ---------------------------------------------------------------------------


def check_config() -> None:
    try:
        s = settings()
        record("Konfigurace", "settings.yaml", OK, f"{len(s)} sekcí")
    except ConfigError as exc:
        record("Konfigurace", "settings.yaml", FAIL, str(exc))
        return

    try:
        cats = taxonomy()["categories"]
        record("Konfigurace", "taxonomy.yaml", OK, f"{len(cats)} kategorií")
    except ConfigError as exc:
        record("Konfigurace", "taxonomy.yaml", FAIL, str(exc))

    try:
        src = sources()
        tiers: dict[str, int] = {}
        for item in src:
            tiers[item["tier"]] = tiers.get(item["tier"], 0) + 1
        record(
            "Konfigurace",
            "sources.yaml",
            OK,
            f"{len(src)} aktivních ({', '.join(f'{k}={v}' for k, v in sorted(tiers.items()))})",
        )
    except ConfigError as exc:
        record("Konfigurace", "sources.yaml", FAIL, str(exc))

    for name in ("triage", "writer", "translator"):
        try:
            text = prompt(name)
            record("Konfigurace", f"prompts/{name}.md", OK, f"{len(text)} znaků")
        except ConfigError as exc:
            record("Konfigurace", f"prompts/{name}.md", FAIL, str(exc))


def check_env() -> bool:
    ok = True
    for key in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "OPENAI_API_KEY"):
        import os

        value = os.environ.get(key, "").strip()
        if value:
            record("Prostředí", key, OK, f"nastaveno ({len(value)} znaků)")
        else:
            record("Prostředí", key, FAIL, "chybí")
            ok = False
    import os

    if os.environ.get("JINA_API_KEY", "").strip():
        record("Prostředí", "JINA_API_KEY", OK, "nastaveno (volitelné)")
    else:
        record("Prostředí", "JINA_API_KEY", OK, "nenastaveno — používá se volný endpoint")
    return ok


def check_supabase() -> None:
    ok, detail = db.ping()
    record("Supabase", "čtení + zápis", OK if ok else FAIL, detail)

    for table in ("raw_items", "articles"):
        total = db.count(table)
        if total < 0:
            record("Supabase", f"tabulka {table}", FAIL, "nedostupná nebo neexistuje")
        else:
            record("Supabase", f"tabulka {table}", OK, f"{total} řádků")

    # Ověření, že migrace proběhla: sloupec pipeline musí existovat.
    rows = db.select("articles", columns="pipeline", limit=1)
    if rows and "pipeline" in rows[0]:
        record("Supabase", "articles.pipeline", OK, "sloupec existuje")
    elif db.count("articles") == 0:
        record("Supabase", "articles.pipeline", WARN, "tabulka prázdná, nelze ověřit")
    else:
        record("Supabase", "articles.pipeline", FAIL, "chybí — spusť sql/001_init.sql")

    # Rozpad podle stavu, ať je vidět, kde se položky hromadí.
    for status in ("new", "scored", "waiting", "rejected", "duplicate", "used", "failed"):
        n = db.count("raw_items", {"status": f"eq.{status}"})
        if n > 0:
            record("Supabase", f"raw_items[{status}]", OK, f"{n}")


def check_models() -> None:
    for role, key in (("triáž", "triage"), ("psaní", "write")):
        model = settings()[key]["model"]
        started = time.time()
        ok, detail = ai_client.ping(model)
        elapsed = time.time() - started
        record(
            "OpenAI",
            f"{model} ({role})",
            OK if ok else FAIL,
            f"{detail} — {elapsed:.1f} s",
        )


def check_reader() -> None:
    # Stabilní veřejná stránka, na které se pozná, jestli Reader vůbec jede.
    text, error = enrich.fetch_full_text("https://example.com")
    if error and "znaků" in error:
        # example.com je krátká; krátký výsledek tady znamená, že služba
        # odpověděla — což je to, co se testuje.
        record("Jina Reader", "dostupnost", OK, "odpovídá (testovací stránka je krátká)")
    elif error:
        record("Jina Reader", "dostupnost", FAIL, error)
    else:
        record("Jina Reader", "dostupnost", OK, f"{len(text or '')} znaků")


def check_feeds() -> None:
    cfg = settings()["collect"]
    src = sources()
    started = time.time()

    ok_count, failures = 0, []
    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
        futures = [pool.submit(collect_rss.fetch_source, s, cfg) for s in src]
        for future in concurrent.futures.as_completed(futures):
            try:
                name, items, error = future.result()
            except Exception as exc:  # noqa: BLE001
                failures.append(("?", f"{type(exc).__name__}: {exc}"))
                continue
            if error:
                failures.append((name, error))
            else:
                ok_count += 1

    elapsed = time.time() - started
    record(
        "RSS zdroje",
        "souhrn",
        OK if not failures else WARN,
        f"{ok_count}/{len(src)} funkčních za {elapsed:.1f} s",
    )
    for name, error in sorted(failures):
        record("RSS zdroje", name, FAIL, error[:80])


# ---------------------------------------------------------------------------


def print_table() -> int:
    width_area = max(len(r[0]) for r in results) + 2
    width_what = max(len(r[1]) for r in results) + 2
    width_status = 7

    print()
    print("=" * (width_area + width_what + width_status + 40))
    print("LYANNA — DIAGNOSTIKA")
    print("=" * (width_area + width_what + width_status + 40))

    current_area = None
    for area, what, status, detail in results:
        if area != current_area:
            print()
            current_area = area
        marker = {OK: "  ", FAIL: "->", WARN: " !"}[status]
        print(f"{marker} {area:<{width_area}}{what:<{width_what}}{status:<{width_status}}{detail}")

    failures = sum(1 for r in results if r[2] == FAIL)
    warnings = sum(1 for r in results if r[2] == WARN)

    print()
    print("-" * (width_area + width_what + width_status + 40))
    if failures:
        print(f"VÝSLEDEK: {failures} chyb, {warnings} varování. Pipeline zatím nespouštěj.")
    elif warnings:
        print(f"VÝSLEDEK: vše podstatné OK, {warnings} varování ke kontrole.")
    else:
        print("VÝSLEDEK: všechno OK.")
    print()
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnostika všech služeb")
    parser.add_argument("--skip-feeds", action="store_true", help="Vynechat test ~70 RSS zdrojů")
    parser.add_argument("--skip-ai", action="store_true", help="Vynechat volání OpenAI")
    args = parser.parse_args()

    log.setup("WARNING")  # tabulka má být čitelná, ne zaplavená logem

    check_config()
    env_ok = check_env()

    if env_ok:
        check_supabase()
        if not args.skip_ai:
            check_models()
    else:
        record("Supabase", "přeskočeno", WARN, "chybí proměnné prostředí")
        record("OpenAI", "přeskočeno", WARN, "chybí proměnné prostředí")

    check_reader()

    if not args.skip_feeds:
        check_feeds()
    else:
        record("RSS zdroje", "přeskočeno", WARN, "spuštěno s --skip-feeds")

    return print_table()


if __name__ == "__main__":
    sys.exit(main())
