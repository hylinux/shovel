"""Bounded literal terms shared by SQLite keyword readers."""

import re


def keyword_terms(query: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"\w+", query.casefold())))[:32]
