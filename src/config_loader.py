"""Načtení a validace konfigurace z config/.

Jediné místo, kde se čtou YAML soubory a prompty. Validace je záměrně
přísná a probíhá při startu: špatný řádek v sources.yaml má spadnout
hned, ne uprostřed noční dávky.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
PROMPTS_DIR = CONFIG_DIR / "prompts"

# Musí přesně odpovídat konstantě CATEGORIES ve frontendu (main.jsx),
# mínus "ALL", což je jen zobrazovací režim webu, ne hodnota v databázi.
VALID_CATEGORIES = {
    "SOCIETY",
    "PHYSICS",
    "CHEMISTRY",
    "SPACE",
    "BIOLOGY",
    "ENVIRONMENT",
    "TECHNOLOGY",
}


class ConfigError(RuntimeError):
    """Konfigurace je nekonzistentní. Běh nemá smysl začínat."""


def _load_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    if not path.exists():
        raise ConfigError(f"Chybí konfigurační soubor: {path}")
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ConfigError(f"{name}: očekáván YAML objekt, přišlo {type(data).__name__}")
    return data


@lru_cache(maxsize=1)
def settings() -> dict[str, Any]:
    return _load_yaml("settings.yaml")


@lru_cache(maxsize=1)
def taxonomy() -> dict[str, Any]:
    data = _load_yaml("taxonomy.yaml")
    cats = data.get("categories") or {}
    unknown = set(cats) - VALID_CATEGORIES
    if unknown:
        raise ConfigError(
            f"taxonomy.yaml obsahuje kategorie, které frontend nezná: {sorted(unknown)}"
        )
    missing = VALID_CATEGORIES - set(cats)
    if missing:
        raise ConfigError(f"taxonomy.yaml postrádá kategorie: {sorted(missing)}")
    return data


@lru_cache(maxsize=1)
def sources() -> list[dict[str, Any]]:
    """Zdroje po filtraci podle `enabled` a povolených tierů."""
    data = _load_yaml("sources.yaml")
    raw = data.get("sources")
    if not isinstance(raw, list) or not raw:
        raise ConfigError("sources.yaml: klíč `sources` musí být neprázdný seznam")

    allowed_tiers = set(settings().get("sources", {}).get("enabled_tiers", []))
    seen_urls: set[str] = set()
    out: list[dict[str, Any]] = []

    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigError(f"sources.yaml: položka #{i} není objekt")
        name, url = item.get("name"), item.get("url")
        if not name or not url:
            raise ConfigError(f"sources.yaml: položka #{i} nemá `name` nebo `url`")
        if not str(url).startswith(("http://", "https://")):
            raise ConfigError(f"sources.yaml: {name} — URL musí začínat http(s)://")

        # Duplicitní URL v konfiguraci by znamenala, že se stejný feed
        # stahuje dvakrát za běh. Unique v databázi by to sice ustálo,
        # ale je to zbytečná práce a nejspíš překlep.
        key = str(url).rstrip("/").lower()
        if key in seen_urls:
            raise ConfigError(f"sources.yaml: duplicitní URL u zdroje {name}")
        seen_urls.add(key)

        cat = item.get("default_category")
        if cat is not None and cat not in VALID_CATEGORIES:
            raise ConfigError(f"sources.yaml: {name} — neznámá default_category {cat!r}")

        if item.get("enabled") is False:
            continue
        tier = item.get("tier", "wide")
        if allowed_tiers and tier not in allowed_tiers:
            continue

        out.append({**item, "tier": tier})

    if not out:
        raise ConfigError("sources.yaml: po filtraci nezbyl ani jeden aktivní zdroj")
    return out


@lru_cache(maxsize=8)
def prompt(name: str) -> str:
    """Načte prompt z config/prompts/<name>.md."""
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise ConfigError(f"Chybí prompt: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ConfigError(f"Prompt {name}.md je prázdný")
    return text


def env(key: str, required: bool = True) -> str:
    """Přečte proměnnou prostředí. Chybějící povinná = tvrdá chyba."""
    value = os.environ.get(key, "").strip()
    if required and not value:
        raise ConfigError(
            f"Chybí proměnná prostředí {key}. "
            "Lokálně ji nastav v .env, v GitHubu v Settings → Secrets → Actions."
        )
    return value


def taxonomy_as_text() -> str:
    """Taxonomie jako čitelný text pro vložení do promptu."""
    data = taxonomy()
    lines: list[str] = []
    for name, spec in data.get("categories", {}).items():
        lines.append(f"### {name} ({spec.get('label_cz', '')})")
        lines.append(f"Patří sem: {(spec.get('includes') or '').strip()}")
        lines.append(f"Nepatří sem: {(spec.get('excludes') or '').strip()}")
        lines.append("")
    rules = data.get("rules") or []
    if rules:
        lines.append("### Pravidla pro hraniční případy")
        lines.extend(f"- {r.strip()}" for r in rules)
    return "\n".join(lines).strip()
