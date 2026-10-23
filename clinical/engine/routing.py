"""Маршрутизация: какие направления к психиатру предложить врачу (он подтверждает)."""
from . import loader, rules


def route(facts: dict) -> list[dict]:
    hits = [r for r in loader.routing_rules() if rules.evaluate(r["when"], facts)]
    order = {"mandatory": 0, "desirable": 1}
    return sorted(
        [{k: r.get(k) for k in ("id", "urgency", "reason", "action", "source")} for r in hits],
        key=lambda r: order[r["urgency"]],
    )


def summary(facts: dict) -> dict:
    hits = route(facts)
    urgency = hits[0]["urgency"] if hits else None
    return {"urgency": urgency, "reasons": hits}
