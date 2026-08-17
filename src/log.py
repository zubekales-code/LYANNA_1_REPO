"""Jednotné logování.

Cíl je ten z doplňkového dokumentu: příčinu chyby poznat z jednoho čtení
logu, ne dalším kolem ladění. Proto jde do logu i úspěch s čísly, ne jen
selhání.
"""

from __future__ import annotations

import logging
import sys


def setup(level: str = "INFO") -> None:
    root = logging.getLogger()
    if root.handlers:  # při opakovaném volání nezdvojovat výstup
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s %(name)-14s %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root.addHandler(handler)
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    # feedparser a httpx si jinak povídají do logu víc, než je užitečné
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)
