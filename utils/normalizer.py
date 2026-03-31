"""
utils/normalizer.py
Convert user input to PokeAPI-compatible slugs.
"""

import re

_OVERRIDES: dict[str, str] = {
    "nidoran♂":         "nidoran-m",
    "nidoran♀":         "nidoran-f",
    "nidoran male":     "nidoran-m",
    "nidoran female":   "nidoran-f",
    "farfetch'd":       "farfetchd",
    "sirfetch'd":       "sirfetchd",
    "mr. mime":         "mr-mime",
    "mr mime":          "mr-mime",
    "mr. rime":         "mr-rime",
    "mr rime":          "mr-rime",
    "mime jr":          "mime-jr",
    "mime jr.":         "mime-jr",
    "type: null":       "type-null",
    "type null":        "type-null",
    "flabébé":          "flabebe",
    "porygon z":        "porygon-z",
    "jangmo-o":         "jangmo-o",
    "hakamo-o":         "hakamo-o",
    "kommo-o":          "kommo-o",
    "wo-chien":         "wo-chien",
    "chien-pao":        "chien-pao",
    "ting-lu":          "ting-lu",
    "chi-yu":           "chi-yu",
    "charizard mega x": "charizard-mega-x",
    "charizard mega y": "charizard-mega-y",
    "mewtwo mega x":    "mewtwo-mega-x",
    "mewtwo mega y":    "mewtwo-mega-y",
}


def normalize(name: str) -> str:
    cleaned = name.strip().lower()
    if cleaned in _OVERRIDES:
        return _OVERRIDES[cleaned]
    cleaned = cleaned.replace("'", "").replace(".", "").replace(":", "")
    cleaned = re.sub(r"[\s_]+", "-", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned.strip("-")
