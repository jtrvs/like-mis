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

import logging

import yaml
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from clinical.engine import rules

from . import channels
from . import facts as facts_mod
from .models import Alert, AlertDelivery

RULES_PATH = Path(__file__).resolve().parent / "data" / "alert_rules.yaml"
TRIGGERS = {"scale", "checkin", "message", "intake", "schedule"}
log = logging.getLogger("alerts")
DEFAULTS = {"ACK_TIMEOUT_MIN": 15, "MAX_RETRIES": 2, "CHANNELS": ["inapp", "log"]}


def cfg(key):
    return getattr(settings, "ALERTS", {}).get(key, DEFAULTS[key])


@lru_cache
def load_rules():
    active_version = RuleVersion.objects.filter(file_type='alerts', is_active=True).first()
    if active_version:
        try:
            data = yaml.safe_load(active_version.content)["rules"]
        except Exception as e:
            log.error("Ошибка чтения правил из базы, падаем в файл: %s", e)
            data = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))["rules"]
    else:
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
    import re
    if re.search(r"[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+", patient_label or ""):
        raise ValueError("patient_label похож на ФИО — передайте метку без персональных данных, например «П-0042»")
    if trigger not in TRIGGERS:  # опечатка в trigger молча отключила бы все правила
        raise ValueError(f"trigger должен быть одним из {sorted(TRIGGERS)}, получено {trigger!r}")
    received_at = received_at or timezone.now()
    f = dict(facts or {})
    f.update(facts_mod.from_text(text))
    created = []
    for r in load_rules():
        if r.get("events") and trigger not in r["events"]:
            continue  # правило проверяется только на новое событие своего типа — старые данные не поднимают сигнал снова
        if not rules.evaluate(r["when"], f):
            continue
        # Дубль — пока такой же сигнал по пациенту ещё не просмотрен врачом. После отметки врача новое событие
        # снова поднимает сигнал. Защита от гонки — частичный уникальный индекс в базе (см. models.Alert.Meta).
        if Alert.objects.filter(patient_id=patient_id, rule_id=r["id"], status__in=Alert.OPEN).exists():
            continue
        try:
            with transaction.atomic():
                a = Alert.objects.create(
                    patient_id=patient_id, patient_label=patient_label, card_url=card_url, doctor=doctor, backup_doctor=backup_doctor,
                    rule_id=r["id"], severity=r["severity"], title=r["title"], facts=_jsonable(f),
                    patient_actions=r.get("patient_actions", []), doctor_actions=r.get("doctor_actions", []),
                    source_received_at=received_at,
                )
        except IntegrityError:
            continue  # параллельный запрос уже создал такой же сигнал
        dispatch(a)
        created.append(a)
    pa = sorted({x for a in created for x in a.patient_actions})
    return {
        "alerts": created,
        "show_crisis_screen": facts_mod.needs_crisis_screen(text) or "show_hotlines" in pa,
        "patient_actions": pa,
        "doctor_actions": sorted({x for a in created for x in a.doctor_actions}),
    }


def _jsonable(d):
    """В JSONField нельзя положить datetime и т. п. — приводим к строкам."""
    import json
    return json.loads(json.dumps(d, default=str, ensure_ascii=False))


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
    """Отметка врача. Уже просмотренный сигнал повторно не перезаписывается (сохраняем, кто и когда отреагировал)."""
    now = timezone.now()
    n = Alert.objects.filter(id=alert.id, status__in=Alert.OPEN).update(
        status="acknowledged", acknowledged_at=now, acknowledged_by=user, resolution=resolution)
    alert.refresh_from_db()
    return bool(n)


def process_pending(now=None):
    """Раз в минуту (фоновый процесс alerts-worker): повтор и эскалация неподтверждённых срочных/важных сигналов."""
    now = now or timezone.now()
    timeout = timedelta(minutes=cfg("ACK_TIMEOUT_MIN"))
    stats = {"redelivered": 0, "retried": 0, "escalated": 0}
    for a in Alert.objects.filter(status="new"):  # например, все каналы были недоступны
        stats["redelivered"] += dispatch(a)
    stats["unanswered"] = 0
    for a in Alert.objects.filter(status__in=["delivered", "escalated"], severity__in=["critical", "high"], exhausted_at__isnull=True):
        last = a.deliveries.order_by("-sent_at").first()
        if last and now - last.sent_at < timeout:
            continue
        open_now = Alert.objects.filter(id=a.id, status__in=["delivered", "escalated"])  # врач мог отметить секунду назад
        if a.retries < cfg("MAX_RETRIES"):
            if open_now.update(retries=a.retries + 1):
                a.refresh_from_db()
                dispatch(a, a.escalated_to or a.doctor)
                stats["retried"] += 1
        elif a.escalated_to_id is None and a.backup_doctor_id:
            # передаём резервному врачу и даём ему тот же цикл повторов
            if open_now.update(escalated_to=a.backup_doctor_id, status="escalated", retries=0):
                a.refresh_from_db()
                dispatch(a, a.escalated_to)
                stats["escalated"] += 1
        else:
            # ни ведущий, ни резервный не ответили (или резервного нет) — громко в журнал, один раз
            if open_now.update(exhausted_at=now):
                log.critical("СИГНАЛ БЕЗ ОТВЕТА: alert %s, пациент %s, правило %s", a.id, a.patient_label, a.rule_id)
                stats["unanswered"] += 1
    return stats
