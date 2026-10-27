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


def _day_streak(checkins: list[dict], pred) -> int:
    """
    Сколько календарных дней подряд (от последней записи назад) выполняется условие.
    Несколько записей за день — считается последняя. Без дат (старые вызовы) — по записям.
    """
    if not checkins or "created_at" not in checkins[0]:
        return _streak(checkins, pred)
    by_day = {}
    for c in checkins:  # новые первыми — первая запись дня и есть последняя
        by_day.setdefault(timezone.localtime(c["created_at"]).date() if timezone.is_aware(c["created_at"]) else c["created_at"].date(), c)
    days = sorted(by_day, reverse=True)
    n = 0
    for i, d in enumerate(days):
        if (i and (days[i - 1] - d).days != 1) or not pred(by_day[d]):
            break
        n += 1
    return n


def from_checkins(checkins: list[dict], on_aviandr: bool = False, on_serotonergic: bool = False) -> dict:
    """
    checkins — последние ежедневные отметки, НОВЫЕ ПЕРВЫМИ, каждая — словарь с ключами:
      mood (0–10), anxiety (0–10), sleep_hours, left_home (True/False/None),
      ate_normally (True/False/None — «ели хотя бы 2 раза»), alcohol_units, side_effects (список строк),
      side_effect_level (0 нет, 1 слегка, 2 сильно, 3 мешают жизни), serotonin_signs (список), created_at.
    """
    if not checkins:
        return {"on_aviandr": on_aviandr, "on_serotonergic": on_serotonergic}
    newest = checkins[0]
    # «Мне сегодня хуже» и ответ на один вопрос — короткие отметки; поведенческие правила считаем по полным записям
    full = [c for c in checkins if c.get("kind", "full") == "full"]
    worse_since = newest["created_at"] - timedelta(days=3) if "created_at" in newest else None
    worse_marks = sum(1 for c in checkins if c.get("kind") == "worse" and (worse_since is None or c["created_at"] > worse_since))
    with_mood = next((c for c in checkins if c.get("mood") is not None), {})
    with_anx = next((c for c in checkins if c.get("anxiety") is not None), {})
    base = {"last_kind": newest.get("kind", "full"), "worse_marks_3d": worse_marks,
            "mood_last": with_mood.get("mood"), "anxiety_last": with_anx.get("anxiety"),
            "calm_last": None if with_anx.get("anxiety") is None else 10 - with_anx["anxiety"],
            "on_aviandr": on_aviandr, "on_serotonergic": on_serotonergic}
    if not full:
        return base
    checkins = full
    last = checkins[0]
    monitored = set()
    for d in loader.drugs().values():
        monitored |= set(d.get("monitor_side_effects", []))
    week_ago = None
    if "created_at" in last:
        week_ago = last["created_at"] - timedelta(days=7)
    alcohol_days = {timezone.localtime(c["created_at"]).date() if timezone.is_aware(c["created_at"]) else c["created_at"].date()
                    for c in checkins if week_ago and c["created_at"] > week_ago and (c.get("alcohol_units") or 0) > 0}
    other_days = {timezone.localtime(c["created_at"]).date() if timezone.is_aware(c["created_at"]) else c["created_at"].date()
                   for c in checkins if week_ago and c["created_at"] > week_ago and c.get("other_substances")}
    return {
        **base,
        "alcohol_units_last": last.get("alcohol_units") or 0,
        "alcohol_days_7": len(alcohol_days),
        "other_substances_last": bool(last.get("other_substances")),
        "other_substances_days_7": len(other_days),
        "short_sleep_nights": _streak(checkins, lambda c: c.get("sleep_hours") is not None and c["sleep_hours"] < 4),
        "days_not_left_home": _day_streak(checkins, lambda c: c.get("left_home") is False),
        "days_not_eating": _day_streak(checkins, lambda c: c.get("ate_normally") is False),
        "monitored_side_effects": bool(set(last.get("side_effects") or []) & monitored),
        "side_effects_last": ", ".join(last.get("side_effects") or []) or "без уточнения",
        "side_effect_strong_streak": _streak(checkins, lambda c: (c.get("side_effect_level") or 0) >= 2),
        "side_effect_disrupts": (last.get("side_effect_level") or 0) >= 3,
        "serotonin_signs": len(last.get("serotonin_signs") or []),
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
    week = [i for i in past if i["scheduled_at"] > now - timedelta(days=7)]
    # дней без отметок: от последнего отмеченного приёма (или от первого запланированного, если отметок не было)
    marked = [i for i in past if i["taken"] is not None]
    unmarked_days = 0
    if past:
        since = marked[0]["scheduled_at"] if marked else past[-1]["scheduled_at"]
        if not marked or any(i["taken"] is None for i in stale):
            unmarked_days = max(0, (now - since).days)
    return {
        "missed_doses_streak": _streak(past, lambda i: i["taken"] is False),
        "unmarked_doses_streak": _streak(stale, lambda i: i["taken"] is not True),
        "missed_doses_7d": sum(1 for i in week if i["taken"] is False),
        "unmarked_days": unmarked_days,
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
