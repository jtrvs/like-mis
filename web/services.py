"""Прикладная логика кабинетов: риск, приверженность, расписание приёма, контекст для движков."""
from datetime import date, datetime, time, timedelta

from django.utils import timezone

from alerts.models import Alert

from . import alerts_bridge
from clinical.engine import routing, safety
from core.models import MedicationIntake, Message, ScaleResponse

SLOTS = {1: [time(9)], 2: [time(9), time(21)], 3: [time(9), time(14), time(21)], 4: [time(8), time(12), time(16), time(21)]}

HOTLINES = [
    ("Скорая помощь / единый номер экстренных служб", "112", "103 — скорая с мобильного"),
    ("Единый круглосуточный телефон доверия", "8 800 100-49-94", "бесплатно, анонимно"),
    ("Кризисная линия доверия", "8 800 333-44-34", "бесплатно, круглосуточно"),
    ("Экстренная психологическая помощь МЧС", "8 495 989-50-50", "круглосуточно"),
]


def age(p):
    t = date.today()
    return t.year - p.birth_date.year - ((t.month, t.day) < (p.birth_date.month, p.birth_date.day))


def ensure_schedule(prescription, until=None):
    """Создаёт ожидаемые приёмы по кратности назначения (для дневника и приверженности)."""
    until = until or date.today()
    start = prescription.start_date
    end = min(until, prescription.end_date or until)
    last = prescription.intakes.order_by("-scheduled_at").values_list("scheduled_at", flat=True).first()
    if last is not None:  # дописываем только хвост, а не перечитываем всю историю
        start = max(start, timezone.localtime(last).date())
    existing = set(prescription.intakes.filter(scheduled_at__date__gte=start).values_list("scheduled_at", flat=True))
    new = []
    d = start
    tz = timezone.get_current_timezone()
    created = prescription.created_at
    if created and prescription.comment.startswith("перенесено из выписки"):
        # Историческая дата нужна для терапии, но старые приёмы мы не наблюдали.
        # Не создаём задним числом неотмеченные дозы и ложные пропуски.
        d = max(d, timezone.localtime(created).date())
    while d <= end:
        for t in SLOTS.get(prescription.times_per_day, SLOTS[1]):
            dt = timezone.make_aware(datetime.combine(d, t), tz)
            if created and dt < created and d == timezone.localtime(created).date():
                continue  # в день назначения не считаем пропущенными утренние дозы, назначенные днём
            if dt not in existing:
                new.append(MedicationIntake(prescription=prescription, scheduled_at=dt))
        d += timedelta(days=1)
    MedicationIntake.objects.bulk_create(new, ignore_conflicts=True)  # гонка врач/пациент не создаст дублей


def adherence(patient, days=14):
    since = timezone.now() - timedelta(days=days)
    qs = MedicationIntake.objects.filter(
        prescription__patient=patient, prescription__status="active", scheduled_at__gte=since, scheduled_at__lte=timezone.now()
    )
    total = qs.count()
    if not total:
        return None
    taken = qs.filter(taken=True).count()
    return {"pct": round(100 * taken / total), "taken": taken, "total": total,
            "missed": qs.filter(taken=False).count(), "unmarked": qs.filter(taken__isnull=True).count()}


def safety_ctx(patient):
    active = patient.prescriptions.filter(status="active")
    stopped = patient.prescriptions.filter(status="stopped", end_date__isnull=False)
    return {
        "age": age(patient),
        "pregnant": patient.is_pregnant,
        "breastfeeding": patient.is_breastfeeding,
        "egfr": patient.egfr,
        "child_pugh": patient.child_pugh_class,
        "conditions": list(patient.conditions.values_list("code", flat=True)),
        "icd10": list(patient.diagnoses.values_list("icd10", flat=True)),
        "current_drugs": [p.drug_name for p in active.exclude(drug_code="aviandr")],
        "stopped_drugs": [{"name": p.drug_name, "days_ago": (date.today() - p.end_date).days} for p in stopped],
        "post_covid": patient.post_covid,
        "weeks_on_drug": next(((date.today() - p.start_date).days // 7 + 1 for p in active.filter(drug_code="aviandr")), None),
    }


def aviandr_panel(patient):
    findings = safety.check("aviandr", safety_ctx(patient))
    return {
        "verdict": safety.verdict(findings),
        "findings": [f for f in findings if f.level != "info"],
        "warnings": [f for f in findings if f.level == "info"],
        "dosing": safety.dosing_hint("aviandr"),
        "on_aviandr": patient.prescriptions.filter(status="active", drug_code="aviandr").exists(),
    }


def routing_facts(patient):
    f = alerts_bridge.collect_facts(patient)
    f["suicide_keywords"] = alerts_bridge.recent_text_hit(patient)
    conds = set(patient.conditions.values_list("code", flat=True))
    f["substance_dependence"] = "substance_dependence" in conds
    first = patient.prescriptions.filter(status="active").order_by("start_date").first()
    if first:
        f["weeks_on_therapy"] = (date.today() - first.start_date).days // 7
        base = ScaleResponse.objects.filter(patient=patient, scale_code="phq9", created_at__date__lte=first.start_date).order_by("-created_at").first()
        last = ScaleResponse.objects.filter(patient=patient, scale_code="phq9").order_by("-created_at").first()
        if base and last and base.total:
            f["score_improvement_pct"] = round(100 * (base.total - last.total) / base.total)
    return f


def routing_panel(patient):
    return routing.summary(routing_facts(patient))


def risk(patient):
    """Сортировка списка пациентов: red — открытый critical/high алерт или обязательное направление."""
    open_alerts = Alert.objects.filter(patient_id=patient.id, status__in=Alert.OPEN)
    if open_alerts.filter(severity__in=["critical", "high"]).exists():
        return "red"
    r = routing_panel(patient)
    if r["urgency"] == "mandatory":
        return "red"
    if open_alerts.exists() or r["urgency"] == "desirable":
        return "amber"
    return "green"


def series(patient, code, days=90):
    qs = ScaleResponse.objects.filter(patient=patient, scale_code=code, created_at__gte=timezone.now() - timedelta(days=days)).order_by("created_at")
    return [{"x": timezone.localtime(r.created_at).strftime("%d.%m"), "y": r.total} for r in qs]


def needs_extra_visit(patient):
    """Кастдев п.4: много обращений за неделю — предложить дополнительный приём."""
    if not patient.user_id:
        return False
    week = Message.objects.filter(sender_id=patient.user_id, created_at__gte=timezone.now() - timedelta(days=7))
    return week.count() >= 6 or sum(len(m.text) for m in week) > 2500


def scale_due(patient, code, every_days):
    last = ScaleResponse.objects.filter(patient=patient, scale_code=code).order_by("-created_at").first()
    return last is None or (timezone.now() - last.created_at).days >= every_days


INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без похожих O/0, I/1


def new_invite_code(n=10):
    """~1,1·10^15 вариантов: перебором не подобрать при ограничении попыток на /join/."""
    import secrets
    return "".join(secrets.choice(INVITE_ALPHABET) for _ in range(n))
