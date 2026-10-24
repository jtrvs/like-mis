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
from core.models import DailyCheckin, MedicationIntake, Message, Referral, ScaleResponse


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


def _checkin_facts(patient):
    rows = DailyCheckin.objects.filter(patient=patient).order_by("-created_at")[:7]
    checkins = [{"mood": c.mood, "anxiety": c.anxiety, "sleep_hours": c.sleep_hours, "left_home": c.left_home,
                 "ate_normally": c.ate_normally, "alcohol_units": c.alcohol_units, "side_effects": c.side_effects} for c in rows]
    on_aviandr = patient.prescriptions.filter(status="active", drug_code="aviandr").exists()
    return F.from_checkins(checkins, on_aviandr=on_aviandr)


def _intake_facts(patient):
    rows = MedicationIntake.objects.filter(prescription__patient=patient, prescription__status="active",
                                           scheduled_at__lte=timezone.now()).order_by("-scheduled_at")[:10]
    return F.from_intakes([{"scheduled_at": i.scheduled_at, "taken": i.taken} for i in rows])


def collect_facts(patient):
    """Все факты о пациенте по последним данным (для правил и для панели маршрутизации в карточке)."""
    f = {}
    for code in ("phq9", "gad7"):
        f.update(_scale_facts(patient, code))
    f.update(_checkin_facts(patient))
    f.update(_intake_facts(patient))
    f["pregnant"] = patient.is_pregnant
    return f


def recent_text_hit(patient, days=7):
    """Были ли за неделю высказывания о суициде (для панели маршрутизации, а не для нового сигнала)."""
    since = timezone.now() - timedelta(days=days)
    texts = list(DailyCheckin.objects.filter(patient=patient, created_at__gte=since).values_list("text", flat=True))
    if patient.user_id:
        texts += list(Message.objects.filter(sender_id=patient.user_id, created_at__gte=since).values_list("text", flat=True))
    return scan(" \n".join(t for t in texts if t))["hit"]


def report(patient, trigger, text=""):
    """
    Вызывать сразу после сохранения данных пациента.
    trigger: "scale" | "checkin" | "message" | "intake" | "schedule".
    Возвращает результат report_event; если у пациента нет ведущего врача — экран помощи всё равно решается по данным.
    """
    link = lead_link(patient)
    if link is None:
        return {"alerts": [], "show_crisis_screen": F.needs_crisis_screen(text), "patient_actions": [], "doctor_actions": []}
    doctor = link.doctor
    backup = doctor.backup_doctor.user if doctor.backup_doctor_id else None
    result = service.report_event(
        patient_id=patient.id, patient_label=label(patient), doctor=doctor.user, backup_doctor=backup,
        trigger=trigger, facts=collect_facts(patient), text=text, card_url=f"/doctor/patient/{patient.public_id}/",
    )
    # Система ничего не делает сама: для врача не-психиатра — только ПРЕДЛОЖЕНИЕ направления (врач подтверждает)
    if "propose_mandatory_referral" in result["doctor_actions"] and not doctor.is_psychiatrist:
        reasons = [{"alert": a.rule_id, "title": a.title} for a in result["alerts"] if "propose_mandatory_referral" in a.doctor_actions]
        ref = Referral.objects.filter(patient=patient, status="proposed").first()
        if ref:
            ref.urgency = "mandatory"
            ref.reasons = ref.reasons + reasons
            ref.save(update_fields=["urgency", "reasons"])
        else:
            Referral.objects.create(patient=patient, urgency="mandatory", reasons=reasons)
    return result


def evaluate_current(patient):
    """Проверка по текущему состоянию (для загрузки демо-данных): как будто только что пришло каждое последнее событие."""
    last_text = DailyCheckin.objects.filter(patient=patient).order_by("-created_at").values_list("text", flat=True).first() or ""
    for trigger, text in (("scale", ""), ("checkin", last_text), ("intake", ""), ("schedule", "")):
        report(patient, trigger, text)


def open_alerts(patient):
    return Alert.objects.filter(patient_id=patient.id, status__in=Alert.OPEN)
