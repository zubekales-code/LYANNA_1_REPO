"""Odhalení téže zprávy přicházející z různých zdrojů.

Jedna studie dorazí dnes z phys.org, ScienceDaily, New Scientist
i Guardianu — čtyři různé URL, jedna zpráva. Unikát na URL tohle
nezachytí, protože URL jsou skutečně různé.

Při pěti zdrojích to nevadilo. Při sedmdesáti je to nejviditelnější vada,
kterou by feed mohl mít: čtenář uvidí čtyřikrát totéž.

Řešení je záměrně jednoduché — porovnání množin slov v titulku. Žádné
embeddingy, žádný další model. Vektorové vyhledávání má smysl až tam, kde
prosté porovnání přestane stačit, a tady zdaleka nepřestalo.

Tři věci, na kterých to stojí, každá z měření na reálných titulcích:

1. **Slova se zkracují na kmen.** Bez toho se „functioned" a „functions"
   považují za různá slova a dvojice o téže studii propadne.

2. **Míra podobnosti je překryv, ne Jaccard.** Jaccard dělí velikostí
   sjednocení, takže trestá rozdílnou délku titulků — a různě dlouhé
   titulky jsou přesně to, co se u téže zprávy z různých zdrojů děje.
   Překryv dělí velikostí menší množiny a tenhle problém nemá.

3. **Musí být sdíleno aspoň několik slov v absolutním počtu.** Samotný
   poměr nestačí: „CRISPR therapy restores vision" a „CRISPR therapy
   lowers cholesterol" mají vysoký překryv na třech slovech, a přitom
   jsou to dvě různé zprávy. Podmínka minimálního počtu je odfiltruje.

I tak tohle nechytí všechno — přeformulování typu „61 days" versus
„two months" je za hranicí toho, co jde poznat z porovnání slov. Druhou
záchytnou sítí je triáž: model vidí celý blok třiceti titulků najednou
a má v promptu instrukci dvojice označit.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

# Slova, která o obsahu nic neříkají a jen zvyšují zdánlivou podobnost
# dvou nesouvisejících titulků. Uvedeno v tom tvaru, který vyjde
# ze `stem()` — porovnání probíhá až po zkrácení na kmen.
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "can",
    "could", "did", "do", "find", "first", "for", "from", "had", "has",
    "have", "how", "in", "into", "is", "it", "its", "may", "might", "more",
    "most", "new", "not", "of", "on", "one", "or", "our", "out", "over",
    "research", "researcher", "reveal", "say", "scientist", "show", "st",
    "study", "suggest", "than", "that", "the", "their", "them", "then",
    "these", "they", "this", "to", "two", "up", "use", "was", "way", "we",
    "were", "what", "when", "which", "who", "why", "will", "with", "you",
    "your",
}

# Britský a americký pravopis u slov, která se ve vědeckých titulcích
# objevují dost často na to, aby na nich shoda padala.
SPELLING = {
    "vapour": "vapor",
    "colour": "color",
    "behaviour": "behavior",
    "modelling": "modeling",
    "ageing": "aging",
    "fibre": "fiber",
    "sulphur": "sulfur",
    "analyse": "analyze",
}

_SUFFIXES = ("ations", "ation", "ingly", "edly", "ings", "ing", "ies", "ied", "es", "ed", "s")

_NON_WORD = re.compile(r"[^a-z0-9\s]+")
_WHITESPACE = re.compile(r"\s+")


def stem(word: str) -> str:
    """Hrubé zkrácení na kmen.

    Není to lingvisticky správné a nemá být — jediný úkol je, aby
    „detects", „detected" a „detecting" daly stejný řetězec.
    """
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            base = word[: -len(suffix)]
            return base + "y" if suffix == "ies" else base
    return word


def tokens(title: str, min_length: int = 3) -> frozenset[str]:
    """Titulek na množinu významových kmenů."""
    if not title:
        return frozenset()

    text = unicodedata.normalize("NFKD", title)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = _NON_WORD.sub(" ", text.lower())
    text = _WHITESPACE.sub(" ", text).strip()

    result: set[str] = set()
    for word in text.split():
        word = SPELLING.get(word, word)
        if len(word) < min_length or word in STOPWORDS:
            continue
        root = stem(word)
        if root in STOPWORDS:
            continue
        result.add(root)
    return frozenset(result)


def similarity(a: frozenset[str], b: frozenset[str]) -> float:
    """Překryv: velikost průniku ku velikosti MENŠÍ z množin."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def is_same_story(
    a: frozenset[str],
    b: frozenset[str],
    threshold: float,
    min_shared: int,
) -> bool:
    return len(a & b) >= min_shared and similarity(a, b) >= threshold


def cluster_key(title: str, min_length: int = 3) -> str:
    """Stabilní otisk titulku, který přežije mezi běhy.

    Kmeny se seřadí abecedně, takže přeházené pořadí slov dá stejný
    otisk. Nezachytí přeformulování — od toho je `cluster()` uvnitř
    běhu — ale zachytí přetisk téhož titulku jinde a jindy.
    """
    key_source = " ".join(sorted(tokens(title, min_length)))
    if not key_source:
        # Titulek bez použitelných slov: klíč z celého řetězce, aby se
        # všechny takové nezhroutily do jednoho společného otisku.
        key_source = (title or "").strip().lower()
    return hashlib.sha1(key_source.encode("utf-8")).hexdigest()[:16]


def cluster(
    items: list[dict],
    threshold: float = 0.5,
    min_token_length: int = 3,
    min_shared_tokens: int = 4,
) -> list[list[dict]]:
    """Rozdělí položky do shluků téže zprávy.

    Zástupcem shluku (prvním prvkem) je položka, která přišla první —
    pořadí vstupu se respektuje, takže když volající předá seznam
    seřazený podle preference zdrojů, vyhraje preferovaný zdroj.
    """
    clusters: list[list[dict]] = []
    signatures: list[frozenset[str]] = []

    for item in items:
        sig = tokens(item.get("title") or "", min_token_length)

        best_index, best_score = -1, 0.0
        for index, existing in enumerate(signatures):
            if not is_same_story(sig, existing, threshold, min_shared_tokens):
                continue
            score = similarity(sig, existing)
            if score > best_score:
                best_index, best_score = index, score

        if best_index >= 0:
            clusters[best_index].append(item)
        else:
            clusters.append([item])
            signatures.append(sig)

    return clusters
