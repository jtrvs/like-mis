"""
Мини-движок декларативных правил: условия описываются в YAML, без eval().
Используется маршрутизацией (clinical) и алертами (alerts).

Условие:
  {fact: phq9_total, op: gte, value: 20}
  {all: [...]} | {any: [...]} | {not: {...}}
Операторы: eq ne gt gte lt lte in contains_any is_true is_false exists
"""
from typing import Any

OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: a is not None and a > b,
    "gte": lambda a, b: a is not None and a >= b,
    "lt": lambda a, b: a is not None and a < b,
    "lte": lambda a, b: a is not None and a <= b,
    "in": lambda a, b: a in b,
    "contains_any": lambda a, b: bool(a) and any(x in a for x in b),
    "is_true": lambda a, b: a is True,
    "is_false": lambda a, b: a is False,
    "exists": lambda a, b: a is not None,
}


def evaluate(cond: dict, facts: dict[str, Any]) -> bool:
    if "all" in cond:
        return all(evaluate(c, facts) for c in cond["all"])
    if "any" in cond:
        return any(evaluate(c, facts) for c in cond["any"])
    if "not" in cond:
        return not evaluate(cond["not"], facts)
    op = cond["op"]
    if op not in OPS:
        raise ValueError(f"Неизвестный оператор {op}")
    return OPS[op](facts.get(cond["fact"]), cond.get("value"))


def validate(cond: dict, known_facts: set[str] | None = None) -> list[str]:
    """Проверка правила при загрузке (чтобы опечатка в YAML не ломала прод)."""
    errs = []
    if "all" in cond or "any" in cond:
        for c in cond.get("all", cond.get("any")):
            errs += validate(c, known_facts)
    elif "not" in cond:
        errs += validate(cond["not"], known_facts)
    else:
        if cond.get("op") not in OPS:
            errs.append(f"оператор {cond.get('op')!r}")
        if known_facts is not None and cond.get("fact") not in known_facts:
            errs.append(f"неизвестный факт {cond.get('fact')!r}")
        if cond.get("op") in ("gte", "gt", "lte", "lt") and not isinstance(cond.get("value"), (int, float)):
            errs.append(f"для {cond.get('fact')!r} нужно число в value")
    return errs
