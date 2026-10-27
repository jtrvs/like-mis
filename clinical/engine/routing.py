"""Маршрутизация: какие направления к психиатру предложить врачу (он подтверждает)."""
from . import loader, rules

ORDER = {"emergency": 0, "mandatory": 1, "desirable": 2}


def route(facts: dict) -> list[dict]:
    hits = [r for r in loader.routing_rules() if rules.evaluate(r["when"], facts)]
    return sorted(
        [{k: r.get(k) for k in ("id", "urgency", "reason", "action", "source")} for r in hits],
        key=lambda r: ORDER[r["urgency"]],
    )


def summary(facts: dict) -> dict:
    hits = route(facts)
    urgency = hits[0]["urgency"] if hits else None
    return {"urgency": urgency, "reasons": hits}
