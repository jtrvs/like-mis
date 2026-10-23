"""Подсчёт шкал по описанию из YAML."""
from dataclasses import dataclass, field

from . import loader


@dataclass
class ScaleResult:
    scale: str
    total: int
    severity: str
    severity_label: str
    flags: list = field(default_factory=list)
    source: str = ""


def score(scale_code: str, answers: list[int]) -> ScaleResult:
    s = loader.scales()[scale_code]
    if len(answers) != s["items"]:
        raise ValueError(f"{scale_code}: ожидалось {s['items']} ответов, получено {len(answers)}")
    for a in answers:
        if not (s["min_item"] <= a <= s["max_item"]):
            raise ValueError(f"{scale_code}: недопустимый балл {a}")
    total = sum(answers)
    band = next(b for b in s["severity"] if b["from"] <= total <= b["to"])
    flags = [c["flag"] for c in s.get("critical_items", []) if answers[c["index"]] >= c["min_score"]]
    return ScaleResult(scale_code, total, band["code"], band["label"], flags, s["source"]["original"])
