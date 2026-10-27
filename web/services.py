"""Прикладная логика кабинетов: риск, приверженность, расписание приёма, контекст для движков."""
from datetime import date, datetime, time, timedelta

from django.utils import timezone

from alerts.models import Alert

from . import alerts_bridge
from clinical.engine import routing, safety
from clinical.engine import scales as scale_engine
from core.models import MedicationIntake, Message, ScaleResponse

SLOTS = {1: [time(9)], 2: [time(9), time(21)], 3: [time(9), time(14), time(21)], 4: [time(8), time(12), time(16), time(21)]}

# Проверено 27.10.2026: МЧС — mchs.gov.ru (Центр экстренной психологической помощи), 051 — Московская служба
# психологической помощи, 8 800 — перечень экстренных служб психологической поддержки (НМИЦ им. Алмазова).
HOTLINES = [
    ("Экстренные службы", "112", "единый номер; скорая — 103"),
    ("Единый круглосуточный телефон доверия", "8 800 100-49-94", "бесплатно, анонимно, для взрослых и детей"),
    ("Кризисная линия доверия", "8 800 333-44-34", "бесплатно, по всей России"),
    ("Психологическая помощь, Москва", "051", "круглосуточно; с мобильного — 8 495 051"),
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


PSY_CLASSES = {"ssri", "tca", "maoi"}
PSY_DRUGS = {"венлафаксин", "дулоксетин", "вортиоксетин", "миртазапин", "агомелатин", "бупропион", "тразодон", "буспирон",
             "кветиапин", "арипипразол", "оланзапин", "сульпирид", "прегабалин", "фабомотизол", "тофизопам", "гидроксизин",
             "лития карбонат", "литий", "вальпроевая кислота", "ламотриджин", "алпразолам", "диазепам", "феназепам",
             "клоназепам", "лоразепам", "бромдигидрохлорфенилбензодиазепин"}


def psy_prescriptions(patient):
    """Активная терапия тревоги/депрессии (по ней считаем недели лечения и ответ) — без амлодипина, трамадола и т. п."""
    out = []
    for rx in patient.prescriptions.filter(status="active").order_by("start_date"):
        name = safety.normalize(rx.drug_name)
        if rx.drug_code == "aviandr" or name in PSY_DRUGS or set(safety.classes_of(rx.drug_name)) & PSY_CLASSES:
            out.append(rx)
    return out


def treatment_response(patient):
    cache = patient.__dict__.setdefault("_req_cache", {})
    if "response" not in cache:
        cache["response"] = _treatment_response(patient)
    return cache["response"]


def _treatment_response(patient):
    """
    Ответ на лечение (таблица правил, раздел 5): снижение балла от исходного на начало терапии.
    Тревога — по GAD-7 (врач может опереться на HAM-A), если нет — по PHQ-9.
    """
    psy = psy_prescriptions(patient)
    first = psy[0] if psy else None
    if not first:
        return None
    for code in ("gad7", "hama", "phq9"):
        base = (ScaleResponse.objects.filter(patient=patient, scale_code=code, created_at__date__lte=first.start_date).order_by("-created_at").first()
                or ScaleResponse.objects.filter(patient=patient, scale_code=code, created_at__date__lte=first.start_date + timedelta(days=3)).order_by("created_at").first())
        last = ScaleResponse.objects.filter(patient=patient, scale_code=code).order_by("-created_at").first()
        if base and last and base.total and last.id != base.id:
            r = scale_engine.response(base.total, last.total)
            r.update(scale=code, weeks=(date.today() - first.start_date).days // 7, title={"gad7": "GAD-7", "hama": "HAM-A", "phq9": "PHQ-9"}[code])
            return r
    return None


def routing_facts(patient, doctor=None):
    f = alerts_bridge.collect_facts(patient)
    f["suicide_keywords"] = alerts_bridge.recent_text_hit(patient)
    conds = set(patient.conditions.values_list("code", flat=True))
    f["substance_dependence"] = "substance_dependence" in conds
    f["manic_symptoms"] = (f.get("short_sleep_nights") or 0) >= 2 and (f.get("mood_last") or 0) >= 9
    if doctor is None:
        link = alerts_bridge.lead_link(patient)
        doctor = link.doctor if link else None
    f["doctor_is_psychiatrist"] = bool(doctor and doctor.is_psychiatrist)
    f["has_f_diagnosis"] = patient.diagnoses.filter(icd10__istartswith="F").exists()
    adh = adherence(patient, 30)
    f["has_concomitant_problems"] = bool(
        conds or patient.prescriptions.filter(status="active").exclude(drug_code="aviandr").exists()
        or (f.get("alcohol_days_7") or 0) > 0 or f.get("auditc_risk") or (adh and adh["pct"] < 80))
    psy = psy_prescriptions(patient)
    if psy:
        f["weeks_on_therapy"] = (date.today() - psy[0].start_date).days // 7
        resp = treatment_response(patient)
        if resp:
            f["score_improvement_pct"] = resp["pct"]
    return f


def routing_panel(patient, doctor=None):
    """Результат кешируется на объекте пациента в пределах одного запроса (карточка, список, сводки)."""
    key = ("routing", getattr(doctor, "pk", None))
    cache = patient.__dict__.setdefault("_req_cache", {})
    if key not in cache:
        cache[key] = routing.summary(routing_facts(patient, doctor))
    return cache[key]


def risk(patient):
    """Сортировка списка пациентов: red — открытый красный/оранжевый сигнал или обязательное направление."""
    open_alerts = Alert.objects.filter(patient_id=patient.id, status__in=Alert.OPEN)
    if open_alerts.filter(severity__in=["critical", "high"]).exists():
        return "red"
    r = routing_panel(patient)
    if r["urgency"] in ("emergency", "mandatory"):
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
    if last is None or (timezone.now() - last.created_at).days >= every_days:
        return True
    # короткая еженедельная проверка: PHQ-2 / GAD-2 ≥ 3 — в тот же день предложить полную шкалу
    short = {"phq9": "phq2", "gad7": "gad2"}.get(code)
    if short:
        s = ScaleResponse.objects.filter(patient=patient, scale_code=short).order_by("-created_at").first()
        return bool(s and s.total >= 3 and s.created_at > last.created_at)
    return False


INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без похожих O/0, I/1


def new_invite_code(n=10):
    """~1,1·10^15 вариантов: перебором не подобрать при ограничении попыток на /join/."""
    import secrets
    return "".join(secrets.choice(INVITE_ALPHABET) for _ in range(n))


def clinic_offer(patient, hidden=()):
    """
    Одна мягкая карточка с услугой клиники — только если есть клинический повод (clinic_services.yaml).
    Уже отправленные и скрытые пациентом услуги не повторяются.
    """
    from clinical.engine import loader, rules as R
    from core.models import ServiceRequest
    asked = set(ServiceRequest.objects.filter(patient=patient, created_at__gte=timezone.now() - timedelta(days=30)).values_list("service", flat=True))
    f = routing_facts(patient)
    f["many_messages"] = needs_extra_visit(patient)
    for s in loader.clinic_services():
        if s["code"] in asked or s["code"] in hidden:
            continue
        if R.evaluate(s["when"], f):
            return s
    return None


def visit_summary(patient, days=14):
    """Сводка перед приёмом: что изменилось за 2 недели — шкалы, приём таблеток, сигналы, побочные эффекты, слова пациента."""
    from core.models import DailyCheckin
    since = timezone.now() - timedelta(days=days)
    out = []
    for code, title in (("phq9", "PHQ-9"), ("gad7", "GAD-7")):
        rows = list(ScaleResponse.objects.filter(patient=patient, scale_code=code).order_by("-created_at")[:2])
        if rows and rows[0].created_at >= since:
            if len(rows) == 2:
                d = rows[0].total - rows[1].total
                trend = "без изменений" if d == 0 else (f"−{-d}, лучше" if d < 0 else f"+{d}, хуже")
                out.append(f"{title}: {rows[0].total} ({trend})")
            else:
                out.append(f"{title}: {rows[0].total}")
    adh = adherence(patient, days)
    if adh:
        out.append(f"Приём препаратов: {adh['pct']}% по отметкам (пропущено {adh['missed']}, не отмечено {adh['unmarked']})")
    alerts = Alert.objects.filter(patient_id=patient.id, created_at__gte=since)
    if alerts.exists():
        out.append(f"Сигналов: {alerts.count()} (открытых {alerts.filter(status__in=Alert.OPEN).count()})")
    cks = list(DailyCheckin.objects.filter(patient=patient, created_at__gte=since))
    if cks:
        se = sorted({x for c in cks for x in (c.side_effects or [])})
        moods = [c.mood for c in cks if c.mood is not None]
        if moods:
            out.append(f"Настроение по дневнику: в среднем {sum(moods) / len(moods):.1f} из 10 ({len(cks)} записей)")
        if se:
            out.append("Отмечал(а): " + ", ".join(se))
        alc = [c for c in cks if c.alcohol_units]
        if alc:
            out.append(f"Алкоголь: {len(alc)} дн., всего {sum(c.alcohol_units for c in alc):.1f} ст. порций")
        worse = sum(1 for c in cks if c.kind == "worse")
        if worse:
            out.append(f"«Мне сегодня хуже»: {worse} раз")
    return out


# Юридические подсказки врачу в типовых ситуациях (кастдев: 5 из 6 психиатров — «обязательно нужно»).
# Справочная информация со ссылками на нормы; не заменяет консультацию юриста клиники.
LEGAL_HINTS = [
    ("Можно ли менять лечение по переписке?",
     "Дистанционно можно наблюдать и корректировать ранее назначенное лечение, если диагноз установлен на очном приёме; "
     "новый диагноз и новое лечение — только после очного приёма.", "323-ФЗ, ст. 36.2; Приказ МЗ № 965н"),
    ("Обязан ли я отвечать пациенту круглосуточно?",
     "Нет. Переписка — не экстренная помощь. При критическом сигнале без вашего ответа система сама показывает пациенту "
     "линии помощи и через 30 минут передаёт сигнал резервному (дежурному) врачу.", "Порядок работы сервиса; 323-ФЗ, ст. 36.2"),
    ("Можно ли позвонить родственнику пациента?",
     "Сведения о здоровье — врачебная тайна. Звонить можно только тем, кого пациент сам отметил «врач может позвонить»; "
     "без согласия — лишь в случаях, прямо указанных в законе.", "323-ФЗ, ст. 13"),
    ("Пациент отказывается от направления к психиатру",
     "Психиатрическая помощь добровольна. Зафиксируйте рекомендацию и отказ в карточке; при непосредственной опасности "
     "для себя или окружающих — экстренная помощь по закону о психиатрической помощи.", "Закон РФ № 3185-1, ст. 4, 29"),
    ("Назначение вне инструкции",
     "Решение принимает врач и фиксирует обоснование в карточке; в спорных случаях — через врачебную комиссию. "
     "Система такое назначение только отмечает, но не предлагает.", "323-ФЗ, ст. 37, 48; инструкция препарата"),
    ("Данные пациента и мессенджеры",
     "Сведения о здоровье — специальная категория персональных данных: отдельное согласие, хранение в РФ. "
     "В мессенджер уходит только метка пациента без ФИО и диагноза.", "152-ФЗ, ст. 10, 18"),
]
