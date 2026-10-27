"""
Мост между моделями пациента (core) и модулем сигналов (alerts).

Это и есть «интеграция» из docs/role4.md: экраны после сохранения данных пациента вызывают report(...),
мост собирает факты из базы и передаёт их в alerts.service.report_event.
Если Юля сделает свои модели — меняется только этот файл.
"""
from datetime import timedelta

from django.utils import timezone

from alerts import facts as F
from alerts import service
from alerts.keywords import scan
from alerts.models import Alert
from clinical.engine import safety
from clinical.engine import scales as scale_engine
from core.models import DailyCheckin, MedicationIntake, Message, PatientReport, Referral, ScaleResponse

SEROTONERGIC = {"ssri", "tca", "triptan", "serotonergic_other"}


def lead_link(patient):
    return patient.care_links.filter(active=True, is_lead=True).select_related("doctor__user", "doctor__backup_doctor__user").first()


def label(patient):
    return f"П-{patient.id:04d}"


def _scale_facts(patient, code):
    last2 = list(ScaleResponse.objects.filter(patient=patient, scale_code=code).order_by("-created_at")[:2])
    if not last2 or len(last2[0].answers) == 0:
        return {f"{code}_total": last2[0].total} if last2 else {}
    prev = last2[1].total if len(last2) == 2 else None
    f = F.from_scale(code, last2[0].answers, prev)
    f[f"{code}_total"] = last2[0].total
    return f


def serotonergic_drugs(patient):
    """Активные препараты, которые вместе с Авиандром дают риск серотонинового синдрома (инструкция, «Взаимодействие»)."""
    return [rx.drug_name for rx in patient.prescriptions.filter(status="active").exclude(drug_code="aviandr")
            if set(safety.classes_of(rx.drug_name)) & SEROTONERGIC]


def _checkin_facts(patient):
    rows = DailyCheckin.objects.filter(patient=patient).order_by("-created_at")[:21]
    checkins = [{"kind": c.kind, "mood": c.mood, "anxiety": c.anxiety, "sleep_hours": c.sleep_hours, "left_home": c.left_home,
                 "ate_normally": c.ate_normally, "alcohol_units": c.alcohol_units, "side_effects": c.side_effects,
                 "side_effect_level": c.side_effect_level, "serotonin_signs": c.serotonin_signs,
                 "other_substances": c.other_substances, "created_at": c.created_at} for c in rows]
    on_aviandr = patient.prescriptions.filter(status="active", drug_code="aviandr").exists()
    sero = serotonergic_drugs(patient)
    f = F.from_checkins(checkins, on_aviandr=on_aviandr, on_serotonergic=bool(sero))
    f["serotonergic_drugs"] = ", ".join(sero) or "—"
    last_full = next((c for c in rows if c.kind == "full"), None)
    f["other_substances_text"] = (last_full.other_substances_text if last_full else "") or "не указано"
    return f


def _intake_facts(patient):
    now = timezone.now()
    rows = MedicationIntake.objects.filter(prescription__patient=patient, prescription__status="active",
                                           scheduled_at__lte=now).order_by("-scheduled_at")[:80]
    f = F.from_intakes([{"scheduled_at": i.scheduled_at, "taken": i.taken} for i in rows], now)
    month = [i for i in rows if i.scheduled_at >= now - timedelta(days=30)]
    if month:
        f["adherence_30d_pct"] = round(100 * sum(1 for i in month if i.taken) / len(month))
    first = patient.prescriptions.filter(status="active").order_by("start_date").first()
    if first:
        f["therapy_days"] = (timezone.localdate() - first.start_date).days
    return f


def _cssrs_facts(patient):
    """Последняя Колумбийская шкала и была ли она заполнена после последнего «тревожного» ответа или слов."""
    f = {}
    last = ScaleResponse.objects.filter(patient=patient, scale_code="cssrs").order_by("-created_at").first()
    if last:
        # для маршрутизации и сигналов учитываем оценку за последние 30 дней
        f["cssrs_risk"] = last.severity if last.created_at >= timezone.now() - timedelta(days=30) else "outdated"
        f["cssrs_label"] = _cssrs_label(last.answers, last.severity)
        f["cssrs_at"] = last.created_at
    since = timezone.now() - timedelta(days=14)
    flags = []
    p9 = ScaleResponse.objects.filter(patient=patient, scale_code="phq9", created_at__gte=since).order_by("-created_at").first()
    if p9 and len(p9.answers) == 9 and p9.answers[8] >= 1:
        flags.append(p9.created_at)
    if patient.user_id:
        for model, kw in ((DailyCheckin, {"patient": patient}), (Message, {"sender_id": patient.user_id})):
            for t, at in model.objects.filter(created_at__gte=since, **kw).order_by("-created_at").values_list("text", "created_at")[:30]:
                if t and scan(t)["hit"]:
                    flags.append(at)
                    break
    f["suicide_flag_unassessed"] = bool(flags) and (last is None or last.created_at < max(flags))
    return f


def _cssrs_label(answers, severity):
    a = (list(answers) + [0] * 7)[:7]
    if a[4]:
        return "есть план и намерение"
    if a[3]:
        return "есть намерение, плана нет"
    if a[5] and a[6]:
        return "действия или подготовка за последние 3 месяца"
    if a[5]:
        return "действия или подготовка давнее 3 месяцев"
    if a[2]:
        return "мысли о способе без намерения"
    if a[1]:
        return "мысли о самоубийстве без способа и намерения"
    if a[0]:
        return "желание умереть без мыслей о самоубийстве"
    return "риск не выявлен"


def _report_facts(patient, report=None):
    f = {"overdose_now": False, "reported_pregnancy": False}
    if report is not None:
        f["reported_kind"] = report.kind
        f["extra_tablets"] = report.extra_tablets or 0
        f["overdose_now"] = report.kind == "extra_dose" and ((report.extra_tablets or 0) >= 3 or bool(report.feels_bad))
        f["reported_pregnancy"] = report.kind in ("pregnancy", "breastfeeding")
        f["stop_reason"] = report.get_reason_display() if report.reason else "не указана"
    week = timezone.now() - timedelta(days=7)
    f["overdose_recent"] = any(r.kind == "extra_dose" and ((r.extra_tablets or 0) >= 3 or r.feels_bad)
                               for r in PatientReport.objects.filter(patient=patient, created_at__gte=week))
    return f


def collect_facts(patient, report=None):
    """Все факты о пациенте по последним данным (для правил и для панели маршрутизации в карточке)."""
    f = {}
    for code in ("phq9", "gad7"):
        f.update(_scale_facts(patient, code))
    f["scales_worse"] = (f.get("phq9_delta") or 0) > 0 or (f.get("gad7_delta") or 0) > 0
    audit = ScaleResponse.objects.filter(patient=patient, scale_code="auditc").order_by("-created_at").first()
    f["auditc_total"] = audit.total if audit else None
    f["auditc_risk"] = scale_engine.auditc_risk(audit.total if audit else None, patient.sex)
    f.update(_checkin_facts(patient))
    f.update(_intake_facts(patient))
    f.update(_cssrs_facts(patient))
    f.update(_report_facts(patient, report))
    f["pregnant"] = patient.is_pregnant
    return f


def recent_text_hit(patient, days=7):
    """Были ли за неделю высказывания о суициде (для панели маршрутизации, а не для нового сигнала)."""
    since = timezone.now() - timedelta(days=days)
    texts = list(DailyCheckin.objects.filter(patient=patient, created_at__gte=since).values_list("text", flat=True))
    if patient.user_id:
        texts += list(Message.objects.filter(sender_id=patient.user_id, created_at__gte=since).values_list("text", flat=True))
    return scan(" \n".join(t for t in texts if t))["hit"]


URGENCY_RANK = {"emergency": 0, "mandatory": 1, "desirable": 2}


def report(patient, trigger, text="", extra_facts=None, report=None):
    """
    Вызывать сразу после сохранения данных пациента.
    trigger: "scale" | "cssrs" | "checkin" | "message" | "intake" | "schedule" | "report" | "sos".
    Возвращает результат report_event; если у пациента нет ведущего врача — экран помощи всё равно решается по данным.
    """
    link = lead_link(patient)
    f = collect_facts(patient, report)
    f.update(extra_facts or {})
    if link is None:
        crisis = F.needs_crisis_screen(text) or f.get("cssrs_risk") in ("high", "moderate", "low") and trigger == "cssrs"
        return {"alerts": [], "show_crisis_screen": crisis, "open_cssrs": F.needs_crisis_screen(text),
                "patient_actions": [], "doctor_actions": []}
    doctor = link.doctor
    backup = doctor.backup_doctor.user if doctor.backup_doctor_id else None
    result = service.report_event(
        patient_id=patient.id, patient_label=label(patient), doctor=doctor.user, backup_doctor=backup,
        trigger=trigger, facts=f, text=text, card_url=f"/doctor/patient/{patient.public_id}/",
    )
    # Система ничего не делает сама: для врача не-психиатра — только ПРЕДЛОЖЕНИЕ направления (врач подтверждает)
    for action, urgency in (("propose_emergency_referral", "emergency"), ("propose_mandatory_referral", "mandatory")):
        if action not in result["doctor_actions"] or doctor.is_psychiatrist:
            continue
        reasons = [{"alert": a.rule_id, "title": a.title} for a in result["alerts"] if action in a.doctor_actions]
        ref = Referral.objects.filter(patient=patient, status="proposed").first()
        if ref:
            if URGENCY_RANK[urgency] < URGENCY_RANK.get(ref.urgency, 9):
                ref.urgency = urgency
            ref.reasons = ref.reasons + reasons
            ref.save(update_fields=["urgency", "reasons"])
        else:
            Referral.objects.create(patient=patient, urgency=urgency, reasons=reasons)
    return result


def evaluate_current(patient):
    """Проверка по текущему состоянию (для загрузки демо-данных): как будто только что пришло каждое последнее событие."""
    last_text = DailyCheckin.objects.filter(patient=patient).order_by("-created_at").values_list("text", flat=True).first() or ""
    for trigger, text in (("scale", ""), ("cssrs", ""), ("checkin", last_text), ("intake", ""), ("schedule", "")):
        report(patient, trigger, text)
    last_report = PatientReport.objects.filter(patient=patient).order_by("-created_at").first()
    if last_report:
        report(patient, "report", report=last_report)


def open_alerts(patient):
    return Alert.objects.filter(patient_id=patient.id, status__in=Alert.OPEN)


def silent_patients():
    """
    Плановая проверка (ALERTS["SCHEDULE_HOOK"], раз в час из сервиса alerts-worker): пациенты на терапии,
    которые перестали отмечать приём препарата. Отдаёт аргументы для report_event по каждому пациенту.

    Ожидаемые приёмы обычно создаются, когда пациент или врач открывает экран. Замолчавший пациент экран
    не открывает — поэтому здесь расписание сначала достраивается до сегодняшнего дня, иначе пропусков «не видно».
    """
    import logging

    from core.models import PatientProfile

    from . import services as S

    log = logging.getLogger("alerts")
    day_ago = timezone.now() - timedelta(hours=24)
    patients = PatientProfile.objects.filter(prescriptions__status="active").distinct()
    for p in patients:
        try:
            link = lead_link(p)
            if link is None:
                continue
            # Врач уже отметил такой сигнал за последние сутки — не дёргаем его каждый час по тому же поводу
            if Alert.objects.filter(patient_id=p.id, rule_id="diary_abandoned", acknowledged_at__gte=day_ago).exists():
                continue
            for rx in p.prescriptions.filter(status="active"):
                S.ensure_schedule(rx)
            doctor = link.doctor
            yield dict(
                patient_id=p.id, patient_label=label(p), doctor=doctor.user,
                backup_doctor=doctor.backup_doctor.user if doctor.backup_doctor_id else None,
                facts=collect_facts(p), card_url=f"/doctor/patient/{p.public_id}/",
            )
        except Exception:  # ошибка по одному пациенту не должна останавливать проверку остальных
            log.exception("silent_patients: пациент id=%s пропущен из-за ошибки", p.id)
