"""
Жизненный цикл сигнала:

  данные пациента сохранены
    -> report_event(...)        проверяем правила, создаём сигнал, сразу доставляем
    -> врач нажал «просмотрено»  acknowledge(...)
    -> красный без отметки       process_pending(): через 15 мин повтор, дальше каждые 5 мин,
                                 через 30 мин — резервному (дежурному) врачу, у него тот же цикл

Система ничего не назначает сама: doctor_actions — только подсказки, решения принимает врач.
"""
from datetime import timedelta
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
TRIGGERS = {"scale", "cssrs", "checkin", "message", "intake", "schedule", "report", "sos"}
log = logging.getLogger("alerts")
DEFAULTS = {"ACK_TIMEOUT_MIN": 15, "REPEAT_EVERY_MIN": 5, "ESCALATE_AFTER_MIN": 30, "SUPPRESS_HOURS": 72,
            "CHANNELS": ["inapp", "log"]}


def cfg(key):
    return getattr(settings, "ALERTS", {}).get(key, DEFAULTS[key])


def _checked(data):
    """Проверка правил при загрузке: опечатка в YAML не должна молча отключить сигнал."""
    for r in data:
        errs = rules.validate(r["when"])
        if r.get("severity") not in SEVERITIES:
            errs.append(f"категория {r.get('severity')!r}")
        if errs:
            raise ValueError(f"Правило {r['id']}: {errs}")
    return data


def load_rules():
    """
    Правила сигналов: активная версия из админки («Версии правил») или файл alert_rules.yaml.
    Каждый процесс (web, alerts-worker) сам замечает новую активную версию. Если версия в базе
    оказалась некорректной, работаем по файлу из репозитория и пишем ошибку в журнал — сигналы не отключаются.
    """
    from clinical.engine import loader
    try:
        return _checked(loader.versioned("alerts")["rules"])
    except Exception as e:  # noqa: BLE001
        if loader.active_version("alerts") is None:
            raise
        log.error("Активная версия правил сигналов некорректна, работаю по файлу из репозитория: %s", e)
        return _checked(yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))["rules"])


def _clear():
    from clinical.engine import loader
    loader.reload()


load_rules.cache_clear = _clear  # совместимость с тестами и админкой


SEVERITIES = {code for code, _ in Alert.SEVERITY}


class _Facts(dict):
    def __missing__(self, key):
        return "—"


def render_title(template, facts):
    """Подставляет факты в текст сигнала: «PHQ-9, вопрос 9 = {phq9_item9}» → «… = 2»."""
    try:
        return template.format_map(_Facts({k: ("—" if v is None else v) for k, v in facts.items()}))[:255]
    except (ValueError, IndexError):
        return template[:255]


def _suppressed(patient_id, rule, f, now):
    """
    Антиспам: некрасный сигнал по тому же правилу не повторяется 72 ч после отметки врача, если не стало хуже.
    Красные (critical) не подавляются никогда.
    """
    if rule["severity"] == "critical" or rule.get("never_suppress"):
        return False
    since = now - timedelta(hours=cfg("SUPPRESS_HOURS"))
    last = (Alert.objects.filter(patient_id=patient_id, rule_id=rule["id"], acknowledged_at__gte=since)
            .order_by("-acknowledged_at").first())
    if last is None:
        return False
    for key in rule.get("worse_if_greater", []):
        new, old = f.get(key), last.facts.get(key)
        if isinstance(new, (int, float)) and isinstance(old, (int, float)) and new > old:
            return False
    return True


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
        if _suppressed(patient_id, r, f, received_at):
            continue
        try:
            with transaction.atomic():
                a = Alert.objects.create(
                    patient_id=patient_id, patient_label=patient_label, card_url=card_url, doctor=doctor, backup_doctor=backup_doctor,
                    rule_id=r["id"], severity=r["severity"], title=render_title(r["title"], f), facts=_jsonable(f),
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
        "open_cssrs": "open_cssrs" in pa or facts_mod.needs_crisis_screen(text),
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


def acknowledge(alert, user, resolution="", false_positive=False):
    """
    Отметка врача. Уже просмотренный сигнал повторно не перезаписывается (сохраняем, кто и когда отреагировал).
    false_positive — врач отметил «ложное срабатывание» (для разбора правил).
    """
    now = timezone.now()
    n = Alert.objects.filter(id=alert.id, status__in=Alert.OPEN).update(
        status="acknowledged", acknowledged_at=now, acknowledged_by=user, resolution=resolution,
        false_positive=bool(false_positive))
    alert.refresh_from_db()
    return bool(n)


def process_pending(now=None):
    """
    Раз в минуту (фоновый процесс alerts-worker). Повторы и эскалация — только для красных (ЭКСТРЕННО):
    нет отметки 15 минут — повтор, дальше каждые 5 минут; через 30 минут — резервному (дежурному) врачу,
    у него тот же цикл; если не ответил и он — громко в журнал.
    Оранжевые («СРОЧНО», ответ в течение суток) и жёлтые не повторяются.
    """
    now = now or timezone.now()
    first = timedelta(minutes=cfg("ACK_TIMEOUT_MIN"))
    every = timedelta(minutes=cfg("REPEAT_EVERY_MIN"))
    escalate_after = timedelta(minutes=cfg("ESCALATE_AFTER_MIN"))
    stats = {"redelivered": 0, "retried": 0, "escalated": 0, "unanswered": 0}
    for a in Alert.objects.filter(status="new"):  # например, все каналы были недоступны
        stats["redelivered"] += dispatch(a)
    for a in Alert.objects.filter(status__in=["delivered", "escalated"], severity="critical", exhausted_at__isnull=True):
        open_now = Alert.objects.filter(id=a.id, status__in=["delivered", "escalated"])  # врач мог отметить секунду назад
        started = a.escalated_at if a.escalated_to_id else (a.delivered_at or a.created_at)
        if now - started >= escalate_after:
            if a.escalated_to_id is None and a.backup_doctor_id:
                # передаём резервному врачу и даём ему тот же цикл повторов
                if open_now.update(escalated_to=a.backup_doctor_id, escalated_at=now, status="escalated", retries=0):
                    a.refresh_from_db()
                    dispatch(a, a.escalated_to)
                    stats["escalated"] += 1
            elif open_now.update(exhausted_at=now):
                # ни ведущий, ни резервный не ответили (или резервного нет) — громко в журнал, один раз
                log.critical("СИГНАЛ БЕЗ ОТВЕТА: alert %s, пациент %s, правило %s", a.id, a.patient_label, a.rule_id)
                stats["unanswered"] += 1
            continue
        # повтор №k — через 15 мин + 5 мин × (k − 1) от начала цикла (у ведущего — от доставки, у резервного — от передачи)
        if now - started < first + every * a.retries:
            continue
        if open_now.update(retries=a.retries + 1):
            a.refresh_from_db()
            dispatch(a, a.escalated_to or a.doctor)
            stats["retried"] += 1
    return stats
