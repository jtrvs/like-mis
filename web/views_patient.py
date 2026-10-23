import json
from datetime import timedelta

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from alerts.keywords import scan

from . import alerts_bridge
from clinical.engine import loader, scales
from core.models import DailyCheckin, MedicationIntake, Message, ScaleResponse, TrustedContact

from . import services as S
from .access import patient_required, safe_next
from .forms import CheckinForm, TrustedContactForm

SCALE_EVERY = {"phq9": 14, "gad7": 14}


def _report(patient, trigger, text=""):
    """Сообщаем модулю сигналов о новом событии; врач получает сигнал, если сработало правило."""
    return alerts_bridge.report(patient, trigger, text)


def _crisis_redirect(text="", phq9_answers=None, result=None):
    """
    Экран помощи показываем по самим данным, а не по факту создания алерта:
    даже если врач уже видел прошлый сигнал или у пациента нет ведущего врача, поддержка нужна сейчас.
    """
    if (result and result["show_crisis_screen"]) or scan(text)["hit"] or (phq9_answers and len(phq9_answers) == 9 and phq9_answers[8] >= 1):
        return redirect("/me/help/?crisis=1")
    return None


@patient_required
def patient_home(request):
    p = request.patient
    for rx in p.prescriptions.filter(status="active"):
        S.ensure_schedule(rx)
    today = timezone.localdate()
    doses = MedicationIntake.objects.filter(prescription__patient=p, prescription__status="active", scheduled_at__date=today).select_related("prescription").order_by("scheduled_at")
    checked_today = DailyCheckin.objects.filter(patient=p, created_at__date=today).exists()
    ctx = {
        "p": p, "tab": "home", "doses": doses, "checked_today": checked_today,
        "due": [(c, loader.scales()[c]["title"]) for c, d in SCALE_EVERY.items() if S.scale_due(p, c, d)],
        "unread": Message.objects.filter(care_link__patient=p, read_at__isnull=True).exclude(sender=request.user).count(),
        "adh": S.adherence(p, 7),
        "extra_visit": S.needs_extra_visit(p),
    }
    return render(request, "web/patient/home.html", ctx)


@patient_required
def checkin(request):
    p = request.patient
    form = CheckinForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        DailyCheckin.objects.create(patient=p, mood=d["mood"], anxiety=d["anxiety"], sleep_hours=d["sleep_hours"],
                                    left_home=d["left_home"], ate_normally=d["ate_normally"], alcohol_units=d["alcohol_units"] or 0,
                                    side_effects=d["side_effects"], text=d["text"])
        res = _report(p, "checkin", d["text"])
        r = _crisis_redirect(d["text"], result=res)
        if r:
            return r
        if d["alcohol_units"] and p.prescriptions.filter(status="active", drug_code="aviandr").exists():
            messages.warning(request, "Запись сохранена. Напоминаем: во время приёма Авиандра алкоголь противопоказан — он усиливает действие препарата. Врач увидит эту запись.")
        else:
            messages.success(request, "Запись сохранена. Врач увидит её в вашей карточке.")
        return redirect("patient_home")
    return render(request, "web/patient/checkin.html", {"form": form, "tab": "home", "range": range(11)})


@patient_required
def scale_fill(request, code):
    p = request.patient
    s = loader.scales().get(code)
    if not s:
        return redirect("patient_home")
    error = None
    if request.method == "POST":
        try:
            answers = [int(request.POST[f"q{i}"]) for i in range(s["items"])]
            res = scales.score(code, answers)
        except (KeyError, ValueError):
            error = "Ответьте, пожалуйста, на все вопросы."
        else:
            ScaleResponse.objects.create(patient=p, scale_code=code, answers=answers, total=res.total, severity=res.severity, filled_by="patient")
            alert_res = _report(p, "scale")
            r = _crisis_redirect(phq9_answers=answers if code == "phq9" else None, result=alert_res)
            if r:
                return r
            messages.success(request, f"Спасибо. Результат: {res.total} баллов. Врач увидит его в карточке.")
            return redirect("patient_progress")
    items = list(enumerate(s["item_labels"]))
    return render(request, "web/patient/scale.html", {"s": s, "items": items, "labels": list(enumerate(s["answer_labels"])), "error": error, "tab": "home"})


@patient_required
def meds(request):
    p = request.patient
    for rx in p.prescriptions.filter(status="active"):
        S.ensure_schedule(rx)
    since = timezone.localdate() - timedelta(days=6)
    doses = MedicationIntake.objects.filter(prescription__patient=p, prescription__status="active", scheduled_at__date__gte=since,
                                            scheduled_at__lte=timezone.now() + timedelta(hours=12)).select_related("prescription").order_by("-scheduled_at")
    return render(request, "web/patient/meds.html", {"doses": doses, "rx": p.prescriptions.filter(status="active"), "adh": S.adherence(p, 14), "tab": "meds"})


@patient_required
@require_POST
def dose_mark(request, intake_id):
    i = get_object_or_404(MedicationIntake, id=intake_id, prescription__patient=request.patient)
    i.taken = request.POST.get("taken") == "1"
    i.marked_at = timezone.now()
    i.actual_dose_mg = i.prescription.dose_mg if i.taken else None
    i.save()
    _report(request.patient, "intake")
    if request.headers.get("HX-Request"):
        return render(request, "web/patient/_dose.html", {"d": i})
    return redirect(safe_next(request, "patient_meds"))


@patient_required
def progress(request):
    p = request.patient
    checkins = list(DailyCheckin.objects.filter(patient=p).order_by("-created_at")[:30])[::-1]
    chart = {"phq9": S.series(p, "phq9"), "gad7": S.series(p, "gad7"),
             "mood": [{"x": timezone.localtime(c.created_at).strftime("%d.%m"), "y": c.mood} for c in checkins],
             "anxiety": [{"x": timezone.localtime(c.created_at).strftime("%d.%m"), "y": c.anxiety} for c in checkins]}
    last = {c: ScaleResponse.objects.filter(patient=p, scale_code=c).order_by("-created_at").first() for c in ("phq9", "gad7")}
    return render(request, "web/patient/progress.html", {"chart": json.dumps(chart), "last": last, "tab": "progress"})


@patient_required
def chat(request):
    p = request.patient
    link = p.care_links.filter(active=True, is_lead=True).select_related("doctor__user").first()
    if request.method == "POST":
        text = request.POST.get("text", "").strip()
        if text and link:
            Message.objects.create(care_link=link, sender=request.user, text=text[:3000])
            res = _report(p, "message", text)
            r = _crisis_redirect(text, result=res)
            if r:
                return r
        return redirect("patient_chat")
    Message.objects.filter(care_link=link, read_at__isnull=True).exclude(sender=request.user).update(read_at=timezone.now())
    thread = Message.objects.filter(care_link=link).order_by("-created_at")[:50][::-1] if link else []
    return render(request, "web/patient/chat.html", {"thread": thread, "link": link, "tab": "chat", "extra_visit": S.needs_extra_visit(p)})


@patient_required
def help_page(request):
    p = request.patient
    form = TrustedContactForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        c = form.save(commit=False)
        c.patient = p
        c.save()
        messages.success(request, "Контакт добавлен.")
        return redirect("patient_help")
    return render(request, "web/patient/help.html", {"hotlines": S.HOTLINES, "contacts": p.trusted_contacts.all(), "form": form,
                                                     "crisis": request.GET.get("crisis"), "tab": "help"})


@patient_required
@require_POST
def contact_delete(request, cid):
    get_object_or_404(TrustedContact, id=cid, patient=request.patient).delete()
    return redirect("patient_help")
