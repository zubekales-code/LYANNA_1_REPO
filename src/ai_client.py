"""Jediná brána k OpenAI.

Nikde jinde v projektu se model nevolá. Volá se Responses API, protože
tak se řada GPT-5.6 dokumentuje a jen ta zpřístupňuje `reasoning.effort`.

Dvě věci, kvůli kterým tenhle modul vypadá složitěji, než by musel:

1. **HTTP 200 neznamená úspěch obsahu.** Reasoning modely mají před
   viditelnou odpovědí neviditelnou přemýšlecí fázi, která se počítá do
   téhož stropu tokenů. U objemného vstupu může model spotřebovat celý
   strop přemýšlením a vrátit formálně platnou odpověď s prázdným textem.
   Proto se prázdný výstup nehlásí jako "nic se nestalo", ale zaloguje se
   i s důvodem ukončení a počtem přemýšlecích tokenů — příčina má být
   vidět z jednoho čtení logu.

2. **Strop tokenů je pojistka, ne rozpočet.** Neplatí se za nevyčerpaný
   strop, jen za skutečně vygenerované tokeny. Držet ho těsně nad
   očekávanou délkou výstupu je proto úspora, která nic nešetří a občas
   stojí celý běh.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from openai import OpenAI

from .config_loader import env
from .log import get

logger = get("ai")

_client: OpenAI | None = None

MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 5


def client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=env("OPENAI_API_KEY"))
    return _client


def _describe_empty(resp: Any, ceiling: int) -> str:
    """Sestaví vysvětlení, proč přišel prázdný text."""
    status = getattr(resp, "status", None)
    reason = getattr(getattr(resp, "incomplete_details", None), "reason", None)
    usage = getattr(resp, "usage", None)
    reasoning_tokens = getattr(
        getattr(usage, "output_tokens_details", None), "reasoning_tokens", None
    )
    output_tokens = getattr(usage, "output_tokens", None)
    return (
        f"status={status}, incomplete_reason={reason}, "
        f"output_tokens={output_tokens}, reasoning_tokens={reasoning_tokens}, "
        f"strop={ceiling}"
    )


def complete(
    *,
    model: str,
    instructions: str,
    user_input: str,
    max_output_tokens: int,
    reasoning_effort: str = "medium",
    label: str = "volání",
) -> str | None:
    """Jedno volání modelu. Vrací text, nebo None při neúspěchu.

    `instructions` je statická část (prompt, taxonomie, styl) a drží se
    stabilní napříč voláními — díky tomu se účtuje jako cachovaný vstup
    z desetiny ceny.
    """
    last_error: str | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = client().responses.create(
                model=model,
                instructions=instructions,
                input=user_input,
                max_output_tokens=max_output_tokens,
                reasoning={"effort": reasoning_effort},
            )
        except Exception as exc:  # noqa: BLE001 — jedno volání nesmí položit běh
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("%s: pokus %d/%d selhal — %s", label, attempt, MAX_ATTEMPTS, last_error)
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        text = (getattr(resp, "output_text", None) or "").strip()
        if text:
            usage = getattr(resp, "usage", None)
            logger.info(
                "%s: OK (model=%s, vstup=%s, výstup=%s tokenů)",
                label,
                model,
                getattr(usage, "input_tokens", "?"),
                getattr(usage, "output_tokens", "?"),
            )
            return text

        # Formálně platná odpověď s prázdným obsahem. Tohle je ta tichá
        # chyba, kvůli které existuje `_describe_empty`.
        detail = _describe_empty(resp, max_output_tokens)
        last_error = f"prázdný text ({detail})"
        logger.warning("%s: pokus %d/%d — %s", label, attempt, MAX_ATTEMPTS, last_error)
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)

    logger.error("%s: neúspěch po %d pokusech — %s", label, MAX_ATTEMPTS, last_error)
    return None


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def parse_json(text: str, label: str = "odpověď") -> Any | None:
    """Rozparsuje JSON z odpovědi modelu.

    Modely mají zlozvyk zabalit JSON do bloku kódu i tam, kde jim to
    prompt zakazuje. Tohle je odolnější než spoléhat na to, že poslechne.
    """
    if not text:
        return None

    cleaned = _FENCE.sub("", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Poslední pokus: vyříznout od první závorky po odpovídající poslední.
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = cleaned.find(opener), cleaned.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue

    logger.error("%s: nešlo rozparsovat jako JSON. Začátek: %s", label, cleaned[:300])
    return None


def ping(model: str) -> tuple[bool, str]:
    """Ověření dostupnosti modelu pro diagnostiku."""
    try:
        resp = client().responses.create(
            model=model,
            instructions="Odpovídej jedním slovem.",
            input="Napiš slovo OK.",
            max_output_tokens=2000,
            reasoning={"effort": "low"},
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:200]}"

    text = (getattr(resp, "output_text", None) or "").strip()
    if not text:
        return False, _describe_empty(resp, 2000)
    return True, f"odpověď {text[:40]!r}"
