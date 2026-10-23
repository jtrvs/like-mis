import time
from datetime import date, timedelta

from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from alerts import service as alert_service
from alerts.models import Alert
from clinical.engine import loader
from core.models import (CareLink, DailyCheckin, Diagnosis, DoctorDecision, Message, PatientProfile,
                         Prescription, Referral, ScaleResponse)
from extraction.engine.llm import extract as extract_text

from . import services as S
from .access import audit, doctor_required, patient_for_doctor, safe_next
from .forms import ImportForm, NewPatientForm, PrescribeForm, add_conditions
from .import_review import medication_code, medication_name, prescription_name, review_rows

RISK_ORDER = {"red": 0, "amber": 1, "green": 2}
COND_LABELS = dict(__import__("web.forms", fromlist=["CONDITION_CHOICES"]).CONDITION_CHOICES)


def _with_names(alerts):
    """В кабинете врача рядом с сигналом показываем ФИО (в мессенджер уходит только метка П-0001)."""
    alerts = list(alerts)
    names = {p.id: p for p in PatientProfile.objects.filter(id__in={a.patient_id for a in alerts})}
    for a in alerts:
        a.patient = names.get(a.patient_id)
    return alerts


def my_alerts(request):
    """Сигналы ведущего врача + эскалированные этому врачу как резервному."""
    return alert_service.visible_to(request.user)


def _nav(request):
    return {"open_alerts": my_alerts(request).filter(status__in=Alert.OPEN).count()}


def _link(patient, doctor):
    """Связь врача с пациентом; резервный врач по эскалации работает через связь ведущего врача."""
    return patient.care_links.filter(doctor=doctor).first() or patient.care_links.filter(is_lead=True, active=True).first()


@doctor_required
def doctor_home(request):
    rows = []
    links = CareLink.objects.filter(doctor=request.doctor, active=True).select_related("patient")
    for link in links:
        p = link.patient
        last_phq = ScaleResponse.objects.filter(patient=p, scale_code="phq9").order_by("-created_at").first()
        last_gad = ScaleResponse.objects.filter(patient=p, scale_code="gad7").order_by("-created_at").first()
        last_seen = DailyCheckin.objects.filter(patient=p).order_by("-created_at").values_list("created_at", flat=True).first()
        rows.append({
            "p": p, "age": S.age(p), "risk": S.risk(p), "adh": S.adherence(p),
            "dx": p.diagnoses.filter(is_primary=True).first(),
            "phq": last_phq, "gad": last_gad, "last_seen": last_seen, "joined": bool(p.user_id), "code": link.invite_code,
            "alerts": Alert.objects.filter(patient_id=p.id, status__in=Alert.OPEN).count(),
            "has_observations": bool(last_seen or last_phq or last_gad),
        })
    rows.sort(key=lambda r: (RISK_ORDER[r["risk"]], -r["alerts"], r["p"].last_name))
    summary = {"total": len(rows), "attention": sum(r["risk"] == "red" for r in rows)}
    q = request.GET.get("q", "").strip()[:120]
    status_filter = request.GET.get("status", "")
    if status_filter not in ("attention", "watch", "unjoined"):
        status_filter = ""
    if q:
        search = q.casefold()
        rows = [r for r in rows if search in " ".join((
            r["p"].last_name, r["p"].first_name, r["p"].middle_name,
            r["dx"].icd10 if r["dx"] else "",
            r["dx"].title if r["dx"] else "",
        )).casefold()]
    if status_filter == "attention":
        rows = [r for r in rows if r["risk"] == "red"]
    elif status_filter == "watch":
        rows = [r for r in rows if r["risk"] == "amber"]
    elif status_filter == "unjoined":
        rows = [r for r in rows if not r["joined"]]
    alerts = _with_names(my_alerts(request).filter(status__in=Alert.OPEN).order_by("-created_at")[:20])
    return render(request, "web/doctor/home.html", {
        "rows": rows, "alerts": _sorted_alerts(alerts), "nav": "home",
        "summary": summary, "q": q, "status_filter": status_filter, **_nav(request),
    })


def _sorted_alerts(qs):
    sev = {"critical": 0, "high": 1, "medium": 2}
    return sorted(qs, key=lambda a: (sev[a.severity], -a.created_at.timestamp()))


@doctor_required
def doctor_alerts(request):
    qs = _with_names(my_alerts(request).order_by("-created_at")[:100])
    return render(request, "web/doctor/alerts.html", {"alerts": qs, "nav": "alerts", **_nav(request)})


@doctor_required
def patient_new(request):
    form = NewPatientForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            p = form.save()
            if form.cleaned_data["icd10"]:
                Diagnosis.objects.create(patient=p, icd10=form.cleaned_data["icd10"].upper(), title=form.cleaned_data["dx_title"] or "",
                                         established_by=request.doctor)
            add_conditions(p, form.cleaned_data["conditions"])
            link = CareLink.objects.create(doctor=request.doctor, patient=p, invite_code=S.new_invite_code(),
                                           invite_expires_at=timezone.now() + timedelta(days=14))
        audit(request, "patient_created", p)
        messages.success(request, f"Карточка создана. Код для пациента: {link.invite_code}")
        return redirect("doctor_patient", public_id=p.public_id)
    return render(request, "web/doctor/patient_new.html", {"form": form, "nav": "new", **_nav(request)})


@doctor_required
def patient_card(request, public_id):
    t0 = time.perf_counter()
    p = patient_for_doctor(request, public_id)
    for rx in p.prescriptions.filter(status="active"):
        S.ensure_schedule(rx)
    link = _link(p, request.doctor)
    alerts = Alert.objects.filter(patient_id=p.id, status__in=Alert.OPEN)
    checkins = list(DailyCheckin.objects.filter(patient=p).order_by("-created_at")[:30])[::-1]
    scales = list(ScaleResponse.objects.filter(
        patient=p, scale_code__in=["phq9", "gad7"],
        created_at__gte=timezone.now() - timedelta(days=90),
    ).order_by("created_at"))
    chart = {
        code: [{"x": timezone.localtime(r.created_at).isoformat(), "y": r.total}
               for r in scales if r.scale_code == code]
        for code in ("phq9", "gad7")
    }
    for code in ("mood", "anxiety"):
        chart[code] = [{"x": timezone.localtime(c.created_at).isoformat(), "y": getattr(c, code)}
                       for c in checkins]
    ctx = {
        "p": p, "age": S.age(p), "link": link, "nav": "home",
        "via_escalation": link.doctor_id != request.doctor.id,
        "dx": p.diagnoses.all(),
        "conds": [COND_LABELS.get(c.code, c.code) for c in p.conditions.all()],
        "rx": p.prescriptions.filter(status="active"),
        "rx_stopped": p.prescriptions.filter(status="stopped").order_by("-end_date")[:5],
        "adh": S.adherence(p),
        "alerts": _sorted_alerts(_with_names(alerts)),
        "referrals": p.referrals.filter(status="proposed").order_by("urgency"),
        "referral_done": p.referrals.filter(status="confirmed", created_at__gte=timezone.now() - timedelta(days=30)).order_by("-created_at").first(),
        "routing": S.routing_panel(p),
        "aviandr": S.aviandr_panel(p),
        "aviandr_drug": loader.drugs()["aviandr"],
        "is_psychiatrist": request.doctor.is_psychiatrist,
        "chart": chart,
        "latest_phq": next((r for r in reversed(scales) if r.scale_code == "phq9"), None),
        "latest_gad": next((r for r in reversed(scales) if r.scale_code == "gad7"), None),
        "last_checkin": checkins[-1] if checkins else None,
        "thread": Message.objects.filter(care_link=link).order_by("-created_at")[:30][::-1],
        "extra_visit": S.needs_extra_visit(p),
        "contacts": p.trusted_contacts.filter(doctor_may_call=True),
        "decisions": p.decisions.order_by("-created_at")[:8],
        "prescribe_form": PrescribeForm(initial={"start_date": date.today()}),
        "today": date.today(),
    }
    ctx["render_ms"] = round((time.perf_counter() - t0) * 1000)
    ctx.update(_nav(request))
    return render(request, "web/doctor/patient.html", ctx)


@doctor_required
@require_POST
def prescribe_aviandr(request, public_id):
    p = patient_for_doctor(request, public_id, "prescribe_attempt")
    form = PrescribeForm(request.POST)
    panel = S.aviandr_panel(p)
    if not form.is_valid():
        messages.error(request, "Проверьте дату начала приёма.")
        return redirect("doctor_patient", public_id=p.public_id)
    if panel["on_aviandr"]:
        messages.error(request, "Авиандр уже назначен этому пациенту.")
        return redirect("doctor_patient", public_id=p.public_id)
    off_label = any(f.level in ("contraindication", "off_label") for f in panel["findings"])
    if panel["verdict"] == "red" and not form.cleaned_data["override"]:
        messages.error(request, "Есть противопоказания по инструкции. Назначение возможно только как решение врача вне инструкции — отметьте это и укажите обоснование.")
        return redirect("doctor_patient", public_id=p.public_id)
    if panel["verdict"] != "green" and not form.cleaned_data["rationale"].strip():
        messages.error(request, "Есть предупреждения по инструкции — укажите комментарий врача.")
        return redirect("doctor_patient", public_id=p.public_id)
    d = loader.drugs()["aviandr"]["dosing"]
    with transaction.atomic():
        p = PatientProfile.objects.select_for_update().get(pk=p.pk)  # повторный клик не создаст второе назначение
        if Prescription.objects.filter(patient=p, drug_code="aviandr", status="active").exists():
            messages.error(request, "Авиандр уже назначен этому пациенту.")
            return redirect("doctor_patient", public_id=p.public_id)
        rx = Prescription.objects.create(
            patient=p, drug_code="aviandr", drug_name="Авиандр (маритупирдин)", dose_mg=d["dose_mg"], times_per_day=d["times_per_day"],
            start_date=form.cleaned_data["start_date"], prescribed_by=request.doctor, off_label=off_label, comment=form.cleaned_data["rationale"])
        DoctorDecision.objects.create(doctor=request.doctor, patient=p, kind="prescribe", rationale=form.cleaned_data["rationale"],
                                      payload={"drug": "aviandr", "verdict": panel["verdict"], "off_label": off_label,
                                               "findings": [f.code for f in panel["findings"]]})
    S.ensure_schedule(rx)
    audit(request, "prescribed", p, drug="aviandr", off_label=off_label)
    messages.success(request, "Авиандр назначен. Схема приёма появилась в дневнике пациента.")
    return redirect("doctor_patient", public_id=p.public_id)


@doctor_required
@require_POST
def stop_prescription(request, public_id, rx_id):
    p = patient_for_doctor(request, public_id, "stop_attempt")
    rx = get_object_or_404(Prescription, id=rx_id, patient=p, status="active")
    rx.status, rx.end_date = "stopped", date.today()
    rx.save(update_fields=["status", "end_date"])
    DoctorDecision.objects.create(doctor=request.doctor, patient=p, kind="stop", payload={"drug": rx.drug_name}, rationale=request.POST.get("rationale", ""))
    messages.success(request, f"{rx.drug_name} отменён.")
    return redirect("doctor_patient", public_id=p.public_id)


@doctor_required
@require_POST
def referral_decide(request, public_id, ref_id):
    p = patient_for_doctor(request, public_id, "referral_decision")
    ref = get_object_or_404(Referral, id=ref_id, patient=p, status="proposed")  # принятое решение не перезаписывается
    ok = request.POST.get("decision") == "confirm"
    if not ok and ref.urgency == "mandatory" and not request.POST.get("rationale", "").strip():
        messages.error(request, "Отказ от обязательного направления требует обоснования.")
        return redirect("doctor_patient", public_id=p.public_id)
    ref.status = "confirmed" if ok else "rejected"
    ref.save(update_fields=["status"])
    DoctorDecision.objects.create(doctor=request.doctor, patient=p, kind="refer" if ok else "reject_recommendation",
                                  payload={"referral": ref.id, "urgency": ref.urgency}, rationale=request.POST.get("rationale", ""))
    messages.success(request, "Направление к психиатру подтверждено." if ok else "Рекомендация о направлении отклонена.")
    return redirect("doctor_patient", public_id=p.public_id)


@doctor_required
@require_POST
def routing_confirm(request, public_id):
    """Врач подтверждает направление, предложенное панелью маршрутизации."""
    p = patient_for_doctor(request, public_id, "referral_decision")
    r = S.routing_panel(p)
    if r["urgency"] and not p.referrals.filter(status="proposed").exists():
        ref = Referral.objects.create(patient=p, urgency=r["urgency"], reasons=r["reasons"], status="confirmed")
        DoctorDecision.objects.create(doctor=request.doctor, patient=p, kind="refer", payload={"referral": ref.id, "urgency": r["urgency"]})
        messages.success(request, "Направление к психиатру оформлено.")
    return redirect("doctor_patient", public_id=p.public_id)


@doctor_required
@require_POST
def alert_ack(request, alert_id):
    a = get_object_or_404(my_alerts(request), id=alert_id)
    alert_service.acknowledge(a, request.user, request.POST.get("resolution", ""))
    DoctorDecision.objects.create(doctor=request.doctor, patient=PatientProfile.objects.get(id=a.patient_id), kind="alert_response",
                                  payload={"alert": a.id, "rule": a.rule_id}, rationale=request.POST.get("resolution", ""))
    messages.success(request, "Сигнал отмечен как просмотренный.")
    return redirect(safe_next(request, "doctor_home"))


@doctor_required
@require_POST
def send_message(request, public_id):
    p = patient_for_doctor(request, public_id, "message")
    text = request.POST.get("text", "").strip()
    if text:
        Message.objects.create(care_link=_link(p, request.doctor), sender=request.user, text=text[:3000])
    return redirect(f"/doctor/patient/{p.public_id}/#messages")


@doctor_required
def import_history(request, public_id):
    """Импорт выписки: система извлекает сведения, врач отмечает, что перенести в карточку."""
    p = patient_for_doctor(request, public_id, "import_view")
    form = ImportForm(request.POST or None)
    data = ms = None
    key = f"import:{p.public_id}"
    current_rows, stopped_rows = [], []
    apply_error = False
    if request.method == "POST" and "apply" in request.POST:
        payload = request.session.get(key)
        if not payload:
            messages.error(request, "Сессия разбора устарела — вставьте текст ещё раз.")
            return redirect("import_history", public_id=p.public_id)
        data = payload
        form = ImportForm(initial={"text": payload.get("_source_text", "")})
        current_rows, stopped_rows = review_rows(payload, request.POST)
        selected = [row for row in current_rows if row["selected"] and row["enabled"]]
        selected_stopped = [row for row in stopped_rows if row["selected"]]
        # Проверяем все выбранные строки до любой записи в карточку.
        valid = [row["form"].is_valid() for row in selected + selected_stopped]
        apply_error = not all(valid)
        skipped = 0
        if not apply_error:
            allowed_cond = set(COND_LABELS) | {"hepatic_failure", "renal_failure"}
            with transaction.atomic():
                # Сериализуем переносы одного пациента, в том числе из разных вкладок.
                p = PatientProfile.objects.select_for_update().get(pk=p.pk)
                active = list(p.prescriptions.filter(status="active"))
                planned = []
                for row in selected:
                    m = row["medication"]
                    name = medication_name(m["inn"])
                    start = row["form"].cleaned_data["start_date"]
                    matches = [rx for rx in active if prescription_name(rx) == name]
                    if matches:
                        if all(rx.dose_mg == m["dose_mg"] and rx.times_per_day == m["times_per_day"] and rx.start_date == start for rx in matches):
                            skipped += 1
                            continue
                        row["form"].add_error(None, "Этот препарат уже есть в действующих назначениях с другой датой или схемой. Сверьте карточку; импорт не заменяет назначение автоматически.")
                        apply_error = True
                    elif any(item[0] == name for item in planned):
                        row["form"].add_error(None, "Препарат повторяется среди выбранных строк. Оставьте одну подтверждённую схему.")
                        apply_error = True
                    else:
                        planned.append((name, row))
                if not apply_error:
                    for code in request.POST.getlist("dx"):
                        if code in payload["diagnoses"]:
                            Diagnosis.objects.get_or_create(patient=p, icd10=code, defaults={
                                "title": "из выписки", "is_primary": code.startswith("F"), "established_by": request.doctor,
                                "established_on_visit": False})
                    add_conditions(p, [c for c in request.POST.getlist("cond") if c in allowed_cond and c in payload["conditions"]])
                    for name, row in planned:
                        m = row["medication"]
                        Prescription.objects.create(patient=p, drug_code=medication_code(name), drug_name=m["inn"],
                            dose_mg=m["dose_mg"], times_per_day=m["times_per_day"],
                            start_date=row["form"].cleaned_data["start_date"], prescribed_by=request.doctor,
                            comment="перенесено из выписки; дата подтверждена врачом")
                    for row in selected_stopped:
                        name = medication_name(row["name"])
                        dates = row["form"].cleaned_data
                        exists = any(prescription_name(rx) == name for rx in p.prescriptions.filter(
                            status="stopped", start_date=dates["start_date"], end_date=dates["end_date"]))
                        if exists:
                            skipped += 1
                            continue
                        Prescription.objects.create(patient=p, drug_code=medication_code(name), drug_name=row["name"],
                            dose_mg=0, times_per_day=1, start_date=dates["start_date"], end_date=dates["end_date"],
                            status="stopped", prescribed_by=request.doctor,
                            comment="отменён до обращения (из выписки); даты подтверждены врачом, доза не установлена")
                    if request.POST.get("egfr") and isinstance(payload.get("egfr"), (int, float)):
                        p.egfr = payload["egfr"]
                    if request.POST.get("child_pugh") and payload.get("child_pugh") in ("A", "B", "C"):
                        p.child_pugh_class = payload["child_pugh"]
                    if request.POST.get("pregnant") and payload.get("pregnant"):
                        p.is_pregnant = True
                    p.save()
                    DoctorDecision.objects.create(doctor=request.doctor, patient=p, kind="accept_recommendation",
                        payload={"import": True, "existing_prescriptions_skipped": skipped})
            if not apply_error:
                request.session.pop(key, None)
                messages.success(request, f"Сведения из выписки перенесены в карточку. Уже существующих назначений пропущено: {skipped}.")
                return redirect("doctor_patient", public_id=p.public_id)
    if request.method == "POST" and "apply" not in request.POST and form.is_valid():
        t0 = time.perf_counter()
        data = extract_text(form.cleaned_data["text"])
        ms = round((time.perf_counter() - t0) * 1000, 1)
        request.session[key] = {k: v for k, v in data.items() if not k.startswith("_")}
        request.session[key]["_source_text"] = form.cleaned_data["text"]
        current_rows, stopped_rows = review_rows(data)
    labels = {**COND_LABELS, "hepatic_failure": "Печёночная недостаточность", "renal_failure": "Почечная недостаточность"}
    conds = [(c, labels.get(c, c)) for c in (data or {}).get("conditions", [])]
    return render(request, "web/doctor/import.html", {"p": p, "form": form, "data": data, "ms": ms, "conds": conds,
                                                      "current_rows": current_rows, "stopped_rows": stopped_rows, "apply_error": apply_error,
                                                      "selected_dx": request.POST.getlist("dx"), "selected_cond": request.POST.getlist("cond"),
                                                      "selected_egfr": request.POST.get("egfr"), "selected_child_pugh": request.POST.get("child_pugh"), "selected_pregnant": request.POST.get("pregnant"),
                                                      "cond_labels": COND_LABELS, "nav": "home", **_nav(request)})
