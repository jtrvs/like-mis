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


def options(s: dict, i: int) -> list[str]:
    """Варианты ответа на вопрос i: свои у вопроса (AUDIT-C) или общие для шкалы."""
    per_item = s.get("item_options")
    return per_item[i] if per_item else s["answer_labels"]


def item_max(s: dict, i: int) -> int:
    return s["min_item"] + len(options(s, i)) - 1 if s.get("item_options") else s["max_item"]


def skipped(s: dict, answers: list) -> set[int]:
    """Вопросы, которые пропускаются по ответам на предыдущие (ветвление C-SSRS)."""
    out = set()
    for r in s.get("skip_rules", []):
        if r["if_item"] < len(answers) and answers[r["if_item"]] == r["equals"]:
            out |= set(r["skip"])
    return out


def cssrs_risk(a: list[int]) -> int:
    """C-SSRS (скрининг): 0 — нет, 1 — низкий, 2 — умеренный, 3 — высокий (таблица «Шкалы»)."""
    q = {i + 1: bool(v) for i, v in enumerate(a[:6])}
    recent6 = q[6] and len(a) > 6 and bool(a[6])
    if q[4] or q[5] or recent6:
        return 3
    if q[3] or q[6]:
        return 2
    if q[1] or q[2]:
        return 1
    return 0


def score(scale_code: str, answers: list[int]) -> ScaleResult:
    s = loader.scales()[scale_code]
    if len(answers) != s["items"]:
        raise ValueError(f"{scale_code}: ожидалось {s['items']} ответов, получено {len(answers)}")
    skip = skipped(s, answers)
    for i, a in enumerate(answers):
        if i in skip:
            if a != s["min_item"]:
                raise ValueError(f"{scale_code}: на пропущенный вопрос {i + 1} не должно быть ответа")
            continue
        if not (s["min_item"] <= a <= item_max(s, i)):
            raise ValueError(f"{scale_code}: недопустимый балл {a}")
    total = cssrs_risk(answers) if scale_code == "cssrs" else sum(answers)
    band = next(b for b in s["severity"] if b["from"] <= total <= b["to"])
    flags = [c["flag"] for c in s.get("critical_items", []) if answers[c["index"]] >= c["min_score"]]
    return ScaleResult(scale_code, total, band["code"], band["label"], flags, s["source"]["original"])


def auditc_risk(total: int | None, sex: str) -> bool:
    """AUDIT-C выше порога: 4+ у мужчин, 3+ у женщин."""
    if total is None:
        return False
    thr = loader.scales()["auditc"]["risk_threshold"]
    return total >= thr.get(sex, min(thr.values()))


def response(baseline: int | None, current: int | None) -> dict | None:
    """Ответ на лечение по снижению балла от исходного (таблица правил, раздел 5)."""
    if not baseline or current is None:
        return None
    pct = round(100 * (baseline - current) / baseline)
    if pct >= 50:
        code, label = "response", "Ответ на терапию"
    elif pct >= 25:
        code, label = "partial", "Частичный ответ"
    else:
        code, label = "none", "Нет ответа"
    return {"pct": pct, "code": code, "label": label, "baseline": baseline, "current": current}
