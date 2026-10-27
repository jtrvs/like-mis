"""
Помощники, которые превращают данные пациента в «факты» для правил.

Работают с обычными списками и словарями — не с моделями базы данных.
Поэтому Юля может хранить пациентов как угодно: достаточно передать сюда значения.

Пример (после сохранения ответов PHQ-9):
    from alerts import facts
    f = facts.from_scale("phq9", answers=[1, 2, 0, 1, 1, 0, 2, 1, 1], previous_total=8)
    # -> {"phq9_total": 9, "phq9_item9": 1, "phq9_delta": 1}
"""
from datetime import datetime, timedelta

from django.utils import timezone

from clinical.engine import loader

from .keywords import scan


def _streak(values, pred):
    """Сколько подряд с начала списка (новые — первыми) выполняется условие."""
    n = 0
    for v in values:
        if pred(v):
            n += 1
        else:
            break
    return n


def from_scale(code: str, answers: list[int], previous_total: int | None = None) -> dict:
    """Факты по только что заполненной шкале (phq9 или gad7)."""
    total = sum(answers)
    f = {f"{code}_total": total}
    if code == "phq9" and len(answers) == 9:
        f["phq9_item9"] = answers[8]
    if previous_total is not None:
        f[f"{code}_delta"] = total - previous_total
    return f


def from_checkins(checkins: list[dict], on_aviandr: bool = False) -> dict:
    """
    checkins — последние ежедневные отметки, НОВЫЕ ПЕРВЫМИ, каждая — словарь с ключами:
      mood (0–10), anxiety (0–10), sleep_hours, left_home (True/False/None),
      ate_normally (True/False/None), alcohol_units, side_effects (список строк).
    """
    if not checkins:
        return {"on_aviandr": on_aviandr}
    last = checkins[0]
    monitored = set()
    for d in loader.drugs().values():
        monitored |= set(d.get("monitor_side_effects", []))
    return {
        "mood_last": last.get("mood"),
        "anxiety_last": last.get("anxiety"),
        "alcohol_units_last": last.get("alcohol_units") or 0,
        "short_sleep_nights": _streak(checkins, lambda c: c.get("sleep_hours") is not None and c["sleep_hours"] < 4),
        "days_not_left_home": _streak(checkins, lambda c: c.get("left_home") is False),
        "days_not_eating": _streak(checkins, lambda c: c.get("ate_normally") is False),
        "monitored_side_effects": bool(set(last.get("side_effects") or []) & monitored),
        "on_aviandr": on_aviandr,
    }


def from_intakes(intakes: list[dict], now: datetime | None = None) -> dict:
    """
    intakes — запланированные приёмы препарата, НОВЫЕ ПЕРВЫМИ:
      {"scheduled_at": datetime, "taken": True | False | None}   (None — пациент не отметил)
    """
    now = now or timezone.now()
    aware = lambda dt: timezone.make_aware(dt) if timezone.is_naive(dt) else dt  # noqa: E731
    intakes = [{**i, "scheduled_at": aware(i["scheduled_at"])} for i in intakes]
    now = aware(now)
    past = [i for i in intakes if i["scheduled_at"] <= now]
    stale = [i for i in past if i["scheduled_at"] <= now - timedelta(hours=12)]
    return {
        "missed_doses_streak": _streak(past, lambda i: i["taken"] is False),
        "unmarked_doses_streak": _streak(stale, lambda i: i["taken"] is not True),
    }


def from_text(text: str) -> dict:
    """Высказывания о суициде/самоповреждении в НОВОМ тексте пациента."""
    kw = scan(text or "")
    return {"suicide_keywords": kw["hit"], "suicide_keyword_matches": kw["matches"], "suicide_keywords_negated": kw["negated"]}


def needs_crisis_screen(text: str = "", phq9_answers: list[int] | None = None) -> bool:
    """
    Показывать ли пациенту экран помощи (телефоны доверия, близкие, 112) СРАЗУ после отправки.
    Решается по самим данным — даже если врач ещё не ответил или сигнал уже был.
    """
    if scan(text or "")["hit"]:
        return True
    return bool(phq9_answers and len(phq9_answers) == 9 and phq9_answers[8] >= 1)
