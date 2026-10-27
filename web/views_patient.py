from datetime import timedelta

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from alerts.keywords import scan

from . import alerts_bridge
from clinical.engine import loader, scales
from core.models import DailyCheckin, MedicationIntake, Message, PatientReport, ScaleResponse, ServiceRequest, TrustedContact

from . import services as S
from .access import patient_required, safe_next
from .forms import CheckinForm, PatientReportForm, TrustedContactForm
from .learn import PAGES as LEARN

# Когда что заполняется (таблица «Шкалы»): PHQ-9 и GAD-7 — раз в 2 недели, PHQ-2 + GAD-2 — каждую неделю,
# AUDIT-C — на старте. Пациент заполняет только шкалы из этого списка (HAM-A и CGI — врач, C-SSRS — по триггеру).
SCALE_EVERY = {"phq9": 14, "gad7": 14}
PATIENT_SCALES = {"phq9", "gad7", "auditc"}
SCALE_NAMES = {"phq9": "Опросник о настроении (PHQ-9)", "gad7": "Опросник о тревоге (GAD-7)", "auditc": "Вопросы об алкоголе (AUDIT-C)"}


def _report(patient, trigger, text="", **kw):
    """Сообщаем модулю сигналов о новом событии; врач получает сигнал, если сработало правило."""
    return alerts_bridge.report(patient, trigger, text, **kw)


def _crisis_redirect(text="", phq9_answers=None, result=None):
    """
    Мысли о смерти в PHQ-9 (вопрос 9) или в словах пациента → сразу Колумбийская шкала с контактами поддержки
    (таблица правил, 1.2). Решается по самим данным, а не по факту создания сигнала: даже если врач уже видел
    прошлый сигнал или у пациента нет ведущего врача, оценка и поддержка нужны сейчас.
    """
    if scan(text)["hit"] or (phq9_answers and len(phq9_answers) == 9 and phq9_answers[8] >= 1) or (result and result.get("open_cssrs")):
        return redirect("/me/cssrs/")
    if result and result["show_crisis_screen"]:
        return redirect("/me/help/?crisis=1")
    return None


def _days_without_marks(p):
    """Сколько дней пациент ничего не отмечал (ни самочувствие, ни таблетки) — для напоминания 4.1."""
    last_c = DailyCheckin.objects.filter(patient=p).order_by("-created_at").values_list("created_at", flat=True).first()
    last_i = MedicationIntake.objects.filter(prescription__patient=p, marked_at__isnull=False).order_by("-marked_at").values_list("marked_at", flat=True).first()
    last = max([t for t in (last_c, last_i) if t], default=None)
    return (timezone.now() - last).days if last else None


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
        "unread": Message.objects.filter(care_link__patient=p, read_at__isnull=True).exclude(sender=request.user).count(),
        "adh": S.adherence(p, 7),
        "extra_visit": S.needs_extra_visit(p),
        # короткая проверка — раз в неделю, если за неделю не было ни её, ни полного опросника
        "short_due": not ScaleResponse.objects.filter(patient=p, scale_code__in=["phq2", "phq9"],
                                                      created_at__gte=timezone.now() - timedelta(days=7)).exists(),
        "auditc_due": not ScaleResponse.objects.filter(patient=p, scale_code="auditc").exists(),
        "quiet_days": _days_without_marks(p),
        "response": S.treatment_response(p),
    }
    ctx["due"] = [(c, SCALE_NAMES[c]) for c, d in SCALE_EVERY.items() if S.scale_due(p, c, d)]
    ctx["offer"] = S.clinic_offer(p, hidden=request.session.get("hidden_offers", []))
    ctx["first_visit"] = not DailyCheckin.objects.filter(patient=p).exists()
    ctx["one_range"] = range(11)
    ctx["on_aviandr"] = p.prescriptions.filter(status="active", drug_code="aviandr").exists()
    ctx["short_due"] = ctx["short_due"] and not ctx["due"]
    return render(request, "web/patient/home.html", ctx)


@patient_required
def checkin(request):
    p = request.patient
    form = CheckinForm(request.POST or None)
    on_aviandr = p.prescriptions.filter(status="active", drug_code="aviandr").exists()
    ask_serotonin = on_aviandr and bool(alerts_bridge.serotonergic_drugs(p))  # правило 2.4: вопросы о симптомах
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        drinks = dict(form.fields["alcohol_drink"].choices)
        DailyCheckin.objects.create(
            patient=p, mood=d["mood"], anxiety=10 - d["calm"], sleep_hours=d["sleep_hours"],
            left_home=d["left_home"], ate_normally=d["ate_normally"], alcohol_units=d["alcohol_units"],
            alcohol_drink=drinks.get(d["alcohol_drink"], "") if d["alcohol_drink"] else "", alcohol_ml=d["alcohol_ml"] if d["alcohol_drink"] else None,
            alcohol_abv=d["alcohol_abv"] if d["alcohol_drink"] else None,
            other_substances=d["other_substances"], other_substances_text=d["other_substances_text"] if d["other_substances"] else "",
            side_effects=d["side_effects"], side_effect_level=d["side_effect_level"] if d["side_effects"] else 0,
            serotonin_signs=d["serotonin_signs"] if ask_serotonin else [], joy=d["joy"], text=d["text"], private_text=d["private_text"])
        res = _report(p, "checkin", d["text"])
        r = _crisis_redirect(d["text"], result=res)
        if r:
            return r
        # личная заметка врачу не уходит, но если в ней тревожные слова — поддержка нужна сейчас
        if scan(d["private_text"])["hit"]:
            return redirect("/me/help/?support=1&private=1")
        pa = set(res["patient_actions"])
        if "serotonin_call_103" in pa:
            return redirect("/me/help/?reason=serotonin")
        if "ask_wellbeing" in pa:
            messages.warning(request, "Похоже, вы почти не ели последние дни. Как вы себя чувствуете? Можно написать врачу во вкладке «Врач».")
        elif d["alcohol_units"] and on_aviandr:
            messages.warning(request, "Запись сохранена. Во время лечения лучше не употреблять алкоголь: он усиливает действие лекарства на нервную систему. "
                                      "Если появятся сердцебиение, сильная сонливость или головокружение — позвоните врачу.")
        elif "no_driving_if_dizzy" in pa or (on_aviandr and set(d["side_effects"]) & {"головокружение", "сонливость"}):
            messages.warning(request, "Запись сохранена. Если кружится голова или клонит в сон — не садитесь за руль. Ваш врач увидит ваши отметки.")
        else:
            messages.success(request, "Спасибо, что поделились. Запись сохранена — врач увидит её в вашей карточке.")
        return redirect("patient_home")
    return render(request, "web/patient/checkin.html", {"form": form, "tab": "home", "range": range(11), "ask_serotonin": ask_serotonin})


def scale_items(s, posted=None):
    """Вопросы шкалы с вариантами ответа (у AUDIT-C — свои у каждого вопроса)."""
    posted = posted or {}
    return [(i, label, [(s["min_item"] + k, opt) for k, opt in enumerate(scales.options(s, i))], posted.get(f"q{i}", ""))
            for i, label in enumerate(s["item_labels"])]


def _change_message(code, prev, total):
    """Тексты для интерфейса: «стало лучше» / «стало хуже» — без баллов-ярлыков и без диагнозов."""
    what = {"phq9": "настроения", "gad7": "тревоги"}.get(code)
    if prev is None or what is None:
        return "Спасибо. Ваши ответы сохранены, врач увидит их в карточке."
    if total <= prev - 3:
        return f"Уровень {what} снизился: {prev} → {total}. Это заметное изменение."
    if total > prev:
        return "Спасибо, что отметили. Ваш врач увидит эти ответы и, если нужно, свяжется с вами."
    return "Спасибо. Ваши ответы сохранены, врач увидит их в карточке."


@patient_required
def scale_fill(request, code):
    p = request.patient
    s = loader.scales().get(code)
    if not s or code not in PATIENT_SCALES:
        return redirect("patient_home")
    error = None
    if request.method == "POST":
        try:
            answers = [int(request.POST[f"q{i}"]) for i in range(s["items"])]
            res = scales.score(code, answers)
        except (KeyError, ValueError):
            error = "Ответьте, пожалуйста, на все вопросы."
        else:
            prev = ScaleResponse.objects.filter(patient=p, scale_code=code).order_by("-created_at").values_list("total", flat=True).first()
            ScaleResponse.objects.create(patient=p, scale_code=code, answers=answers, total=res.total, severity=res.severity, filled_by="patient")
            alert_res = _report(p, "scale")
            r = _crisis_redirect(phq9_answers=answers if code == "phq9" else None, result=alert_res)
            if r:
                return r
            if "remind_no_alcohol" in alert_res["patient_actions"]:
                messages.warning(request, "Спасибо. Во время лечения лучше не употреблять алкоголь: он усиливает действие лекарства на нервную систему.")
            else:
                messages.success(request, _change_message(code, prev, res.total))
            return redirect("patient_progress" if code in ("phq9", "gad7") else "patient_home")
    return render(request, "web/patient/scale.html", {"s": s, "title": SCALE_NAMES[code], "items": scale_items(s, request.POST),
                                                      "error": error, "tab": "home"})


@patient_required
def short_check(request):
    """Короткая еженедельная проверка: PHQ-2 + GAD-2. 3 и больше — в тот же день предложить полную шкалу."""
    p = request.patient
    parts = [(code, loader.scales()[code]) for code in ("phq2", "gad2")]
    error = None
    if request.method == "POST":
        try:
            results = []
            for code, s in parts:
                answers = [int(request.POST[f"{code}_q{i}"]) for i in range(s["items"])]
                results.append((code, answers, scales.score(code, answers)))
        except (KeyError, ValueError):
            error = "Ответьте, пожалуйста, на все вопросы."
        else:
            for code, answers, res in results:
                ScaleResponse.objects.create(patient=p, scale_code=code, answers=answers, total=res.total, severity=res.severity, filled_by="patient")
            _report(p, "scale")
            above = [code for code, _, res in results if res.total >= 3]
            if above:
                messages.info(request, "Спасибо. Предлагаем сегодня ответить на ещё несколько вопросов — это поможет врачу точнее увидеть изменения.")
            else:
                messages.success(request, "Спасибо. Ответы сохранены, врач увидит их в карточке.")
            return redirect("patient_home")
    items = [(code, i, label, [(k, opt) for k, opt in enumerate(s["answer_labels"])], request.POST.get(f"{code}_q{i}", ""))
             for code, s in parts for i, label in enumerate(s["item_labels"])]
    return render(request, "web/patient/short.html", {"items": items, "error": error, "tab": "home", "period": parts[0][1].get("period")})


@patient_required
def cssrs(request):
    """
    Колумбийская шкала (скрининг). Открывается после «тревожного» ответа или слов пациента.
    Если на вопрос 2 «нет» — сразу к вопросу 6. Контакты поддержки видны всё время.
    """
    p = request.patient
    s = loader.scales()["cssrs"]
    error = None
    posted = {}
    if request.method == "POST":
        raw = [request.POST.get(f"q{i}") for i in range(s["items"])]
        answers = [1 if v == "1" else 0 for v in raw]
        skip = scales.skipped(s, answers)
        missing = [i for i, v in enumerate(raw) if i not in skip and v not in ("0", "1")]
        answers = [0 if i in skip else a for i, a in enumerate(answers)]
        posted = {f"q{i}": v for i, v in enumerate(raw) if v}
        if missing:
            error = "Ответьте, пожалуйста, на вопросы, отмеченные ниже."
        else:
            res = scales.score("cssrs", answers)
            ScaleResponse.objects.create(patient=p, scale_code="cssrs", answers=answers, total=res.total, severity=res.severity, filled_by="patient")
            _report(p, "cssrs")
            if res.severity == "high":
                return redirect("/me/help/?crisis=1")
            if res.severity in ("moderate", "low"):
                return redirect("/me/help/?support=1")
            messages.success(request, "Спасибо, что ответили. Если станет тяжело, раздел «Мне плохо» всегда открыт.")
            return redirect("patient_home")
    items = [(i, label, posted.get(f"q{i}", "")) for i, label in enumerate(s["item_labels"])]
    return render(request, "web/patient/cssrs.html", {"items": items, "error": error, "hotlines": S.HOTLINES[:3],
                                                      "contacts": p.trusted_contacts.filter(patient_may_call=True), "tab": "help"})


@patient_required
def report_to_doctor(request):
    """Сообщить врачу: лишние таблетки, перестал принимать, уменьшил дозу, беременность (правила 1.4, 1.5, 4.4–4.6)."""
    p = request.patient
    form = PatientReportForm(request.POST or None, initial={"kind": request.GET.get("kind", "")})
    if request.method == "POST" and form.is_valid():
        rep = form.save(commit=False)
        rep.patient = p
        rep.save()
        res = alerts_bridge.report(p, "report", report=rep)
        pa = set(res["patient_actions"])
        if "call_ambulance_take_package" in pa or (rep.kind == "extra_dose" and ((rep.extra_tablets or 0) >= 3 or rep.feels_bad)):
            return redirect("/me/help/?reason=overdose")
        if rep.kind in ("pregnancy", "breastfeeding"):
            messages.warning(request, "Пожалуйста, свяжитесь с вашим врачом как можно скорее, чтобы обсудить лечение. Мы уже сообщили ему.")
        elif rep.kind == "extra_dose":
            messages.warning(request, "Спасибо, что сообщили. Пожалуйста, не превышайте назначенную дозу. Врач получил уведомление.")
        else:
            messages.success(request, "Спасибо, что сообщили. Пожалуйста, обсудите это с врачом — он поможет решить, как быть дальше.")
        return redirect("patient_meds")
    return render(request, "web/patient/report.html", {"form": form, "tab": "meds"})


@patient_required
def meds(request):
    p = request.patient
    for rx in p.prescriptions.filter(status="active"):
        S.ensure_schedule(rx)
    since = timezone.localdate() - timedelta(days=6)
    doses = MedicationIntake.objects.filter(prescription__patient=p, prescription__status="active", scheduled_at__date__gte=since,
                                            scheduled_at__lte=timezone.now() + timedelta(hours=12)).select_related("prescription").order_by("-scheduled_at")
    today = timezone.localdate()
    doses = list(doses)
    return render(request, "web/patient/meds.html", {
        "today_doses": sorted((d for d in doses if timezone.localdate(d.scheduled_at) == today), key=lambda d: d.scheduled_at),
        "upcoming_doses": sorted((d for d in doses if timezone.localdate(d.scheduled_at) > today), key=lambda d: d.scheduled_at),
        "past_doses": [d for d in doses if timezone.localdate(d.scheduled_at) < today],
        "rx": p.prescriptions.filter(status="active"), "adh": S.adherence(p, 14), "tab": "meds",
    })


@patient_required
@require_POST
def dose_mark(request, intake_id):
    i = get_object_or_404(MedicationIntake, id=intake_id, prescription__patient=request.patient)
    i.taken = request.POST.get("taken") == "1"
    i.marked_at = timezone.now()
    i.actual_dose_mg = i.prescription.dose_mg if i.taken else None
    i.save()
    _report(request.patient, "intake")
    if not i.taken and not request.headers.get("HX-Request"):
        messages.info(request, "Ничего страшного. Не принимайте пропущенную — следующую примите в обычное время.")
    if request.headers.get("HX-Request"):
        return render(request, "web/patient/_dose.html", {"d": i})
    return redirect(safe_next(request, "patient_meds"))


@patient_required
def progress(request):
    p = request.patient
    checkins = list(DailyCheckin.objects.filter(patient=p).order_by("-created_at")[:30])[::-1]
    responses = list(ScaleResponse.objects.filter(
        patient=p, scale_code__in=["phq9", "gad7"],
        created_at__gte=timezone.now() - timedelta(days=90),
    ).order_by("created_at"))
    chart = {
        code: [{"x": timezone.localtime(r.created_at).isoformat(), "y": r.total}
               for r in responses if r.scale_code == code]
        for code in ("phq9", "gad7")
    }
    # обе линии дневника направлены одинаково: 10 — хорошо (спокойствие = 10 − тревога)
    chart["mood"] = [{"x": timezone.localtime(c.created_at).isoformat(), "y": c.mood} for c in checkins if c.mood is not None]
    chart["calm"] = [{"x": timezone.localtime(c.created_at).isoformat(), "y": 10 - c.anxiety} for c in checkins if c.anxiety is not None]
    last = {c: ScaleResponse.objects.filter(patient=p, scale_code=c).order_by("-created_at").first() for c in ("phq9", "gad7")}
    return render(request, "web/patient/progress.html", {
        "chart": chart, "last": last, "recent_checkins": list(reversed(checkins))[:5], "tab": "progress",
    })


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
            messages.success(request, "Сообщение отправлено.")
        elif not link:
            messages.error(request, "Сообщение не отправлено: ведущий врач пока не подключён.")
        else:
            messages.error(request, "Введите текст сообщения.")
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
    if request.method == "GET" and request.GET.get("sos"):
        # кнопка «Мне плохо»: врачу — «СРОЧНО · Пациент открыл экран помощи» (повторно не дублируется, пока открыт)
        _report(p, "sos", extra_facts={"sos": True})
    from alerts.models import Alert
    notified = Alert.objects.filter(patient_id=p.id, status__in=Alert.OPEN, created_at__gte=timezone.now() - timedelta(hours=1)).exists()
    return render(request, "web/patient/help.html", {"hotlines": S.HOTLINES, "contacts": p.trusted_contacts.all(), "form": form, "notified": notified,
                                                     "call_contacts": p.trusted_contacts.filter(patient_may_call=True),
                                                     "crisis": request.GET.get("crisis"), "support": request.GET.get("support"),
                                                     "reason": request.GET.get("reason", ""), "tab": "help"})


@patient_required
@require_POST
def contact_delete(request, cid):
    get_object_or_404(TrustedContact, id=cid, patient=request.patient).delete()
    return redirect("patient_help")


@patient_required
@require_POST
def worse(request):
    """Кнопка «Мне сегодня хуже»: одна отметка без вопросов — на тяжёлый день чаще всего хватает сил только на неё."""
    p = request.patient
    DailyCheckin.objects.create(patient=p, kind="worse")
    _report(p, "checkin")
    messages.info(request, "Спасибо, что отметили. Врач увидит это. Если хочется — ответьте ниже на один вопрос или откройте «Мне плохо».")
    return redirect("/me/?one=1")


@patient_required
@require_POST
def one_question(request):
    """После паузы: ответ на один вопрос — настроение 0–10."""
    p = request.patient
    try:
        mood = int(request.POST["mood"])
        assert 0 <= mood <= 10
    except (KeyError, ValueError, AssertionError):
        return redirect("patient_home")
    DailyCheckin.objects.create(patient=p, kind="one", mood=mood)
    _report(p, "checkin")
    messages.success(request, "Спасибо! Отметка сохранена.")
    return redirect("patient_home")


@patient_required
@require_POST
def service_request(request, code):
    """Заявка на услугу клиники: уходит врачу, без навязывания — карточку можно скрыть."""
    from clinical.engine import loader
    p = request.patient
    s = next((x for x in loader.clinic_services() if x["code"] == code), None)
    if s is None:
        return redirect("patient_home")
    if request.POST.get("hide"):
        hidden = set(request.session.get("hidden_offers", [])) | {code}
        request.session["hidden_offers"] = sorted(hidden)
        return redirect("patient_home")
    ServiceRequest.objects.get_or_create(patient=p, service=code, status="new", defaults={"title": s["title"], "reason": s.get("text", "")})
    messages.success(request, "Заявка отправлена. С вами свяжутся, чтобы договориться о времени.")
    return redirect("patient_home")


@patient_required
def learn(request, slug=None):
    if slug:
        page = LEARN.get(slug)
        if not page:
            return redirect("patient_learn")
        return render(request, "web/patient/learn_page.html", {"page": page, "tab": "home"})
    return render(request, "web/patient/learn.html", {"pages": LEARN.items(), "tab": "home"})
