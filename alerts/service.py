"""
Жизненный цикл сигнала:

  данные пациента сохранены
    -> report_event(...)        проверяем правила, создаём сигнал, сразу доставляем
    -> врач нажал «просмотрено»  acknowledge(...)
    -> нет отметки 15 минут      process_pending(): повтор, после 2 повторов — резервному врачу

Система ничего не назначает сама: doctor_actions — только подсказки, решения принимает врач.
"""
from datetime import timedelta
from functools import lru_cache
from pathlib import Path

import yaml
from django.conf import settings
from django.utils import timezone

from clinical.engine import rules

from . import channels
from . import facts as facts_mod
from .models import Alert, AlertDelivery

RULES_PATH = Path(__file__).resolve().parent / "data" / "alert_rules.yaml"
DEDUP_WINDOW = timedelta(hours=12)
DEFAULTS = {"ACK_TIMEOUT_MIN": 15, "MAX_RETRIES": 2, "CHANNELS": ["inapp", "log"]}


def cfg(key):
    return getattr(settings, "ALERTS", {}).get(key, DEFAULTS[key])


@lru_cache
def load_rules():
    data = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))["rules"]
    for r in data:
        errs = rules.validate(r["when"])
        if errs:
            raise ValueError(f"Правило {r['id']}: {errs}")
    return data


def report_event(*, patient_id, patient_label, doctor, trigger, facts=None, text="", backup_doctor=None,
                 card_url="", received_at=None):
    """
    ГЛАВНАЯ ФУНКЦИЯ ДЛЯ ИНТЕГРАЦИИ. Вызывать сразу после сохранения данных пациента.

    patient_id     — ID пациента в модели проекта
    patient_label  — метка без ФИО (например, «П-0042»): попадёт в мессенджер
    doctor         — User ведущего врача;  backup_doctor — User резервного врача (необязательно)
    trigger        — что произошло: "scale" | "checkin" | "message" | "intake" | "schedule"
    facts          — словарь фактов (см. alerts/facts.py)
    text           — новый текст пациента (сообщение или «своими словами» в дневнике)
    card_url       — ссылка на карточку пациента в кабинете врача

    Возвращает {"alerts": [...], "show_crisis_screen": bool, "patient_actions": [...], "doctor_actions": [...]}
    """
    received_at = received_at or timezone.now()
    f = dict(facts or {})
    f.update(facts_mod.from_text(text))
    created = []
    for r in load_rules():
        if r.get("events") and trigger not in r["events"]:
            continue  # правило проверяется только на новое событие своего типа — старые данные не поднимают сигнал снова
        if not rules.evaluate(r["when"], f):
            continue
        # дубль — только пока такой же сигнал ещё не просмотрен; после отметки врача новое событие снова поднимает сигнал
        if Alert.objects.filter(patient_id=patient_id, rule_id=r["id"], status__in=Alert.OPEN,
                                created_at__gte=timezone.now() - DEDUP_WINDOW).exists():
            continue
        a = Alert.objects.create(
            patient_id=patient_id, patient_label=patient_label, card_url=card_url, doctor=doctor, backup_doctor=backup_doctor,
            rule_id=r["id"], severity=r["severity"], title=r["title"], facts=f,
            patient_actions=r.get("patient_actions", []), doctor_actions=r.get("doctor_actions", []),
            source_received_at=received_at,
        )
        dispatch(a)
        created.append(a)
    pa = sorted({x for a in created for x in a.patient_actions})
    return {
        "alerts": created,
        "show_crisis_screen": facts_mod.needs_crisis_screen(text) or "show_hotlines" in pa,
        "patient_actions": pa,
        "doctor_actions": sorted({x for a in created for x in a.doctor_actions}),
    }


def dispatch(alert, user=None):
    user = user or alert.doctor
    allowed = channels.BY_SEVERITY[alert.severity]
    any_ok = False
    for name in [c for c in cfg("CHANNELS") if allowed is None or c in allowed]:
        ch = channels.REGISTRY[name]()
        try:
            ch.send(alert, user)
            ok, err = True, ""
        except Exception as e:  # упал один канал — остальные продолжают работать
            ok, err = False, str(e)[:500]
        AlertDelivery.objects.create(alert=alert, channel=name, attempt=alert.retries + 1, recipient=ch.recipient(user), ok=ok, error=err)
        any_ok |= ok
    if any_ok and alert.delivered_at is None:
        alert.delivered_at = timezone.now()
        if alert.status == "new":
            alert.status = "delivered"
        alert.save(update_fields=["delivered_at", "status"])
    return any_ok


def visible_to(user):
    """Сигналы, которые видит врач: свои + переданные ему как резервному."""
    from django.db.models import Q
    return Alert.objects.filter(Q(doctor=user) | Q(escalated_to=user))


def acknowledge(alert, user, resolution=""):
    alert.status = "acknowledged"
    alert.acknowledged_at = timezone.now()
    alert.acknowledged_by = user
    alert.resolution = resolution
    alert.save(update_fields=["status", "acknowledged_at", "acknowledged_by", "resolution"])


def process_pending(now=None):
    """Раз в минуту (фоновый процесс alerts-worker): повтор и эскалация неподтверждённых срочных/важных сигналов."""
    now = now or timezone.now()
    timeout = timedelta(minutes=cfg("ACK_TIMEOUT_MIN"))
    stats = {"redelivered": 0, "retried": 0, "escalated": 0}
    for a in Alert.objects.filter(status="new"):  # например, все каналы были недоступны
        stats["redelivered"] += dispatch(a)
    for a in Alert.objects.filter(status__in=["delivered", "escalated"], severity__in=["critical", "high"]):
        last = a.deliveries.order_by("-sent_at").first()
        if last and now - last.sent_at < timeout:
            continue
        if a.retries < cfg("MAX_RETRIES"):
            a.retries += 1
            a.save(update_fields=["retries"])
            dispatch(a, a.escalated_to or a.doctor)
            stats["retried"] += 1
        elif a.escalated_to is None and a.backup_doctor_id:
            a.escalated_to = a.backup_doctor
            a.status = "escalated"
            a.save(update_fields=["escalated_to", "status"])
            dispatch(a, a.escalated_to)
            stats["escalated"] += 1
    return stats
