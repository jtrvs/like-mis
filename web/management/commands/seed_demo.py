"""
Демо-данные для показа жюри и тестов с врачами — клинические сценарии клинического психолога команды:
  А (anna)  — ГТР, Авиандр показан, 8 недель: частичный ответ к 4-й неделе, ответ на терапию к 8-й;
  Б (maria) — ГТР у пожилой пациентки: СКФ 52, сертралин + трамадол — Авиандр противопоказан, 4 предупреждения;
  В (oleg)  — пациент терапевта без диагноза: PHQ-9 = 21, вопрос 9 = 2, AUDIT-C = 5, C-SSRS — намерение без плана;
  Г (darya) — паническое расстройство: 5 дней не выходит из дома, Авиандр — только вне инструкции;
  дополнительно: igor (депрессия на сертралине без ответа), elena (алкоголь и пропуски), Павлов (ещё не подключился).
Все пациенты вымышленные. Пароль у всех: Demo-2026!
  python manage.py seed_demo --reset
"""
import random
from datetime import date, datetime, time, timedelta

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from alerts.models import Alert
from web import alerts_bridge
from clinical.engine import scales
from core.models import (CareLink, CareTeamMember, Condition, DailyCheckin, Diagnosis, DoctorDecision, DoctorProfile, Message,
                         PatientProfile, PatientReport, Prescription, Profile, Referral, ScaleResponse, TrustedContact)
from web import services as S

PWD = "Demo-2026!"
TODAY = date.today()


def ago(days, hour=10):
    dt = timezone.make_aware(datetime.combine(TODAY - timedelta(days=days), time(hour)))
    return min(dt, timezone.now() - timedelta(minutes=30))


def phq(total, item9=0):
    """Раскладка суммы по 9 пунктам (для демонстрации), пункт 9 задаётся явно."""
    rest, a = total - item9, [0] * 8
    i = 0
    while rest > 0:
        if a[i % 8] < 3:
            a[i % 8] += 1
            rest -= 1
        i += 1
    return a + [item9]


def gad(total):
    a, i = [0] * 7, 0
    while total > 0:
        if a[i % 7] < 3:
            a[i % 7] += 1
            total -= 1
        i += 1
    return a


class Command(BaseCommand):
    def add_arguments(self, p):
        p.add_argument("--reset", action="store_true")

    def user(self, username, first, last, role, email=""):
        u, _ = User.objects.get_or_create(username=username, defaults={"first_name": first, "last_name": last, "email": email})
        u.set_password(PWD)
        u.save()
        Profile.objects.get_or_create(user=u, defaults={"role": role})
        return u

    def scale(self, p, code, answers, days, by="patient", hour=19):
        r = scales.score(code, answers)
        s = ScaleResponse.objects.create(patient=p, scale_code=code, answers=answers, total=r.total, severity=r.severity, filled_by=by)
        ScaleResponse.objects.filter(id=s.id).update(created_at=ago(days, hour))

    def checkins(self, p, days, mood, anx, **kw):
        """anx — тревога 0–10 (в кабинете пациент отмечает «спокойствие» = 10 − тревога); alc — стандартные порции."""
        for i, d in enumerate(days):
            alc = kw.get("alc", [0] * len(days))[i]
            drink = kw.get("drink", "Вино")
            abv = {"Вино": 12, "Пиво": 5, "Крепкий напиток (водка, коньяк, виски)": 40}.get(drink, 12)
            c = DailyCheckin.objects.create(patient=p, mood=mood[i], anxiety=anx[i], sleep_hours=kw.get("sleep", [7] * len(days))[i],
                                            left_home=kw.get("home", [True] * len(days))[i], ate_normally=kw.get("ate", [True] * len(days))[i],
                                            alcohol_units=alc, alcohol_drink=drink if alc else "", alcohol_abv=abv if alc else None,
                                            alcohol_ml=round(alc * 10 / 0.789 / abv * 100) if alc else None,
                                            joy=kw.get("joy", [""] * len(days))[i],
                                            side_effects=kw.get("se", [[]] * len(days))[i],
                                            side_effect_level=kw.get("sel", [1 if (kw.get("se") or [[]] * len(days))[i] else 0 for _ in days])[i],
                                            text=kw.get("text", [""] * len(days))[i])
            DailyCheckin.objects.filter(id=c.id).update(created_at=ago(d, 21))

    def rx(self, p, doc, code, name, dose, times, start_days, adherence=0.95, end_days=None, seed=1):
        r = Prescription.objects.create(patient=p, drug_code=code, drug_name=name, dose_mg=dose, times_per_day=times,
                                        start_date=TODAY - timedelta(days=start_days), prescribed_by=doc,
                                        status="stopped" if end_days is not None else "active",
                                        end_date=TODAY - timedelta(days=end_days) if end_days is not None else None)
        if end_days is None:
            S.ensure_schedule(r)
            rnd = random.Random(seed)
            for i in r.intakes.filter(scheduled_at__lt=timezone.now() - timedelta(hours=2)):
                i.taken = rnd.random() < adherence
                i.marked_at = i.scheduled_at + timedelta(minutes=20)
                i.save()
        return r

    def patient(self, doc, username, last, first, middle, born, sex, icd, title, **kw):
        u = self.user(username, first, last, "patient") if username else None
        p = PatientProfile.objects.create(user=u, last_name=last, first_name=first, middle_name=middle, birth_date=born, sex=sex,
                                          egfr=kw.get("egfr"), child_pugh_class=kw.get("cp", ""), is_pregnant=kw.get("pregnant", False))
        if icd:  # у пациента терапевта может не быть психиатрического диагноза (его ставит психиатр)
            Diagnosis.objects.create(patient=p, icd10=icd, title=title, established_by=kw.get("dx_by", doc))
        for c in kw.get("conds", []):
            Condition.objects.create(patient=p, code=c)
        link = CareLink.objects.create(doctor=doc, patient=p, invite_code=kw.get("code", f"D{p.id:05d}"))
        return p, link

    def msg(self, link, sender, text, days, hour=12):
        m = Message.objects.create(care_link=link, sender=sender, text=text)
        Message.objects.filter(id=m.id).update(created_at=ago(days, hour), read_at=ago(days, hour + 1))

    @transaction.atomic
    def handle(self, reset, **kw):
        if reset:
            PatientProfile.objects.all().delete()
            Alert.objects.all().delete()
            User.objects.filter(is_superuser=False).delete()
        # история заливается напрямую в базу (без сигналов), затем один раз проверяем текущее состояние каждого пациента
        self.build()
        for p in PatientProfile.objects.all():
            alerts_bridge.evaluate_current(p)
        self.stdout.write(self.style.SUCCESS(
            "Готово. Врачи: petrova (психиатр), sidorov (терапевт). Пациенты: anna (А), maria (Б), oleg (В), darya (Г), igor, elena. Пароль: " + PWD))

    def build(self):
        pu = self.user("petrova", "Елена", "Петрова", "doctor", "petrova@example.org")
        petrova, _ = DoctorProfile.objects.get_or_create(user=pu, defaults={"specialty": "psychiatrist", "organization": "ПНД № 7 (вымышл.)"})
        su = self.user("sidorov", "Андрей", "Сидоров", "doctor", "sidorov@example.org")
        sidorov, _ = DoctorProfile.objects.get_or_create(user=su, defaults={"specialty": "therapist", "organization": "ГП № 12 (вымышл.)", "backup_doctor": petrova})

        # А. ГТР, Авиандр показан: 34 года, СКФ 98, других болезней и лекарств нет. 8 недель терапии.
        #    Старт: PHQ-9 = 8, GAD-7 = 15, HAM-A = 22 → неделя 4: GAD-7 = 9 (−40%, частичный) → неделя 8: GAD-7 = 6 (−60%, ответ).
        p, l = self.patient(petrova, "anna", "Смирнова", "Анна", "Викторовна", date(1992, 3, 14), "F", "F41.1", "Генерализованное тревожное расстройство", egfr=98)
        self.scale(p, "phq9", phq(8), 57)
        self.scale(p, "auditc", [1, 0, 0], 57)
        for d, g in [(57, 15), (42, 12), (28, 9), (14, 7), (1, 6)]:
            self.scale(p, "gad7", gad(g), d)
        self.scale(p, "phq9", phq(6), 28)
        self.scale(p, "phq9", phq(4), 1)
        for d, h in [(57, 22), (28, 15), (1, 10)]:
            self.scale(p, "hama", [min(4, max(0, (h - i) // 14 + (1 if i < h % 14 else 0))) for i in range(14)], d, by="doctor", hour=11)
        self.scale(p, "cgi_s", [4], 57, by="doctor", hour=11)
        self.scale(p, "cgi_i", [2], 1, by="doctor", hour=11)
        self.rx(p, petrova, "aviandr", "Авиандр (маритупирдин)", 20, 2, 56, adherence=0.96, seed=1)
        days = list(range(55, 0, -1))
        self.checkins(p, days, mood=[4 + i * 4 // 55 for i in range(55)], anx=[8 - i * 5 // 55 for i in range(55)],
                      se=[["сонливость"] if i < 6 else [] for i in range(55)],
                      joy=[""] * 50 + ["прогулка в парке", "", "позвонить маме", "", "тёплая ванна"])
        self.msg(l, pu, "Анна, первые дни возможна лёгкая сонливость — это ожидаемо. Не садитесь за руль, пока не пройдёт, и не пейте алкоголь.", 55)
        self.msg(l, p.user, "Спасибо, сонливость уже меньше. Тревога по утрам тоже стала слабее.", 40)
        TrustedContact.objects.create(patient=p, name="Сергей", relation="муж", phone="+7 900 000-00-01", doctor_may_call=True)

        # Дополнительно. Депрессия на сертралине без ответа, терапевт рассматривает Авиандр — «вне инструкции» + СИОЗС
        p, l = self.patient(sidorov, "igor", "Волков", "Игорь", "Николаевич", date(1985, 6, 2), "M", "F32.1", "Депрессивный эпизод средней степени",
                            conds=["cardiovascular_disease"], egfr=86, dx_by=petrova)
        for d, q, g in [(43, 16, 12), (28, 15, 11), (14, 15, 11), (2, 15, 12)]:
            self.scale(p, "phq9", phq(q), d)
            self.scale(p, "gad7", gad(g), d)
        self.rx(p, sidorov, "сертралин", "сертралин", 50, 1, 42, adherence=0.9, seed=2)
        self.checkins(p, list(range(20, 0, -2)), mood=[3, 3, 4, 3, 3, 4, 3, 3, 4, 3], anx=[6, 7, 6, 6, 7, 6, 6, 7, 6, 6])
        self.msg(l, p.user, "Доктор, таблетки пью шестую неделю, а легче не стало. Тревога мешает спать. Может, добавить что-то от тревоги?", 1)
        # обратная связь направившему врачу: психиатр принял пациента и оставил рекомендации
        ref = Referral.objects.create(patient=p, urgency="desirable", status="confirmed",
                                      reasons=[{"reason": "Нет улучшения к 4-й неделе (снижение балла меньше 25%)", "source": "КР ГТР 2024 (ID 457)"}],
                                      accepted_by=petrova, accepted_at=ago(10, 15),
                                      recommendations="Продолжить сертралин 50 мг, повторная оценка через 2 недели; Авиандр при депрессивном эпизоде — вне инструкции, не рекомендую. Направлен на КПТ.")
        Referral.objects.filter(id=ref.id).update(created_at=ago(14, 12))
        CareLink.objects.create(doctor=petrova, patient=p, is_lead=False, invite_code=f"C{p.id:05d}")
        CareTeamMember.objects.create(patient=p, name="Петрова Е. В.", specialty="Психиатр", contact="кабинет 214")

        # Б. Противопоказание и взаимодействия: 68 лет, ГТР (диагноз психиатра), ХБП 3а (СКФ 52), гипертония.
        #    Сертралин 3 месяца (эффект частичный), трамадол при болях в спине, амлодипин. GAD-7 = 13, PHQ-9 = 9 — алертов нет.
        p, l = self.patient(sidorov, "maria", "Кузнецова", "Мария", "Олеговна", date(1958, 2, 11), "F", "F41.1", "Генерализованное тревожное расстройство",
                            conds=["cardiovascular_disease"], egfr=52, dx_by=petrova)
        self.scale(p, "gad7", gad(18), 92)
        self.scale(p, "phq9", phq(11), 92)
        self.scale(p, "gad7", gad(14), 30)
        self.scale(p, "gad7", gad(13), 3)
        self.scale(p, "phq9", phq(9), 3)
        self.rx(p, petrova, "сертралин", "сертралин", 50, 1, 90, adherence=0.95, seed=3)
        self.rx(p, sidorov, "трамадол", "трамадол", 50, 1, 20, adherence=0.95, seed=4)
        self.rx(p, sidorov, "амлодипин", "амлодипин", 5, 1, 400, adherence=0.97, seed=6)
        self.checkins(p, list(range(14, 0, -1)), mood=[5, 5, 4, 5, 5, 4, 5, 5, 4, 5, 5, 4, 5, 5], anx=[6, 6, 7, 6, 6, 7, 6, 6, 7, 6, 6, 6, 6, 6],
                      sleep=[5.5] * 14)
        CareTeamMember.objects.create(patient=p, name="Орлова Н. С.", specialty="Нефролог", contact="ГКБ № 52, по записи")
        self.msg(l, p.user, "Андрей Николаевич, после того как не стало мужа, плохо сплю и всё время тревожно. Сертралин немного помогает. Может, есть что-то ещё от тревоги?", 2)

        # В. Обязательное направление к психиатру: пациент терапевта, 52 года, водитель; психиатрического диагноза нет.
        #    Бессонница, сниженное настроение, тревога, потеря интереса, похудел на 4 кг, «устал от всего»; алкоголь 2–3 раза в неделю.
        #    PHQ-9 = 21 (вопрос 9 = 2), AUDIT-C = 5 → Колумбийская шкала: есть намерение, плана нет → красный сигнал, «обязательно, экстренно».
        p, l = self.patient(sidorov, "oleg", "Морозов", "Олег", "Сергеевич", date(1974, 1, 9), "M", None, "", egfr=74)
        TrustedContact.objects.create(patient=p, name="Ирина", relation="сестра", phone="+7 900 000-00-02", doctor_may_call=True)
        self.scale(p, "phq9", phq(13), 14)
        self.scale(p, "gad7", gad(11), 14)
        self.checkins(p, list(range(12, 0, -1)), mood=[4, 4, 3, 3, 3, 2, 2, 2, 2, 1, 1, 1], anx=[5, 5, 5, 6, 6, 6, 7, 7, 8, 8, 9, 9],
                      sleep=[6, 6, 5, 5, 5, 4.5, 4.5, 4, 5, 4, 4, 4], ate=[True] * 9 + [False] * 3,
                      alc=[0, 2, 0, 0, 3, 0, 2, 0, 3, 0, 2, 0], drink="Крепкий напиток (водка, коньяк, виски)",
                      text=[""] * 11 + ["Устал от всего. Иногда думаю, что не вижу смысла жить дальше."])
        self.scale(p, "gad7", gad(15), 0)
        self.scale(p, "auditc", [3, 1, 1], 0)
        self.scale(p, "phq9", phq(21, item9=2), 0)
        self.scale(p, "cssrs", [1, 1, 1, 1, 0, 0, 0], 0, hour=20)
        self.msg(l, p.user, "После увольнения совсем не сплю, ничего не радует. Можно что-нибудь от нервов?", 3)

        # Дополнительно. На Авиандре, но алкоголь и пропуски приёма
        p, l = self.patient(petrova, "elena", "Новикова", "Елена", "Андреевна", date(1981, 8, 30), "F", "F41.1", "Генерализованное тревожное расстройство", egfr=91)
        for d, g in [(21, 14), (7, 11)]:
            self.scale(p, "gad7", gad(g), d)
            self.scale(p, "phq9", phq(6), d)
        rx = self.rx(p, petrova, "aviandr", "Авиандр (маритупирдин)", 20, 2, 21, adherence=0.9, seed=5)
        for i in rx.intakes.filter(scheduled_at__lt=timezone.now()).order_by("-scheduled_at")[:3]:
            i.taken = False
            i.save()
        self.checkins(p, list(range(10, 0, -1)), mood=[5, 5, 6, 6, 5, 6, 6, 5, 5, 4], anx=[6, 6, 5, 5, 5, 4, 4, 5, 5, 6],
                      alc=[0] * 9 + [3], text=[""] * 9 + ["Были на дне рождения у подруги, выпила немного вина."])

        # Г. Паническое расстройство (диагноз психиатра), 28 лет: атаки 2 раза в неделю, избегает метро, 5 дней не выходит из дома.
        #    GAD-7 = 12 → жёлтое уведомление; Авиандр не предлагается — назначение только «вне инструкции» с обоснованием.
        p, l = self.patient(petrova, "darya", "Соколова", "Дарья", "Сергеевна", date(1998, 4, 17), "F", "F41.0", "Паническое расстройство", egfr=104)
        self.scale(p, "gad7", gad(12), 16)
        self.scale(p, "phq9", phq(7), 16)
        self.scale(p, "gad7", gad(12), 1)
        self.checkins(p, list(range(9, 0, -1)), mood=[5, 5, 4, 4, 4, 4, 3, 4, 4], anx=[6, 7, 7, 8, 8, 8, 8, 7, 8],
                      home=[True] * 4 + [False] * 5, text=[""] * 8 + ["Снова приступ в магазине у дома. Боюсь выходить, учусь удалённо."])
        self.msg(l, p.user, "Елена Викторовна, приступы примерно 2 раза в неделю, в метро не спускаюсь совсем. Слышала, что есть новое лекарство от тревоги — мне подойдёт?", 2)

        # Карточка создана, пациент ещё не подключился — показывает код приглашения
        self.patient(petrova, None, "Павлов", "Дмитрий", "Игоревич", date(1990, 5, 5), "M", "F41.0", "Паническое расстройство", code="K7Q2MX")
