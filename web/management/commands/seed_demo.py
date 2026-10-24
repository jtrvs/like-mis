"""
Демо-данные для показа жюри: 5 клинических сценариев с Авиандром, 2 врача, история за 5 недель.
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
from core.models import (CareLink, Condition, DailyCheckin, Diagnosis, DoctorProfile, Message, PatientProfile,
                         Prescription, Profile, ScaleResponse, TrustedContact)
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

    def scale(self, p, code, answers, days):
        r = scales.score(code, answers)
        s = ScaleResponse.objects.create(patient=p, scale_code=code, answers=answers, total=r.total, severity=r.severity, filled_by="patient")
        ScaleResponse.objects.filter(id=s.id).update(created_at=ago(days, 19))

    def checkins(self, p, days, mood, anx, **kw):
        for i, d in enumerate(days):
            c = DailyCheckin.objects.create(patient=p, mood=mood[i], anxiety=anx[i], sleep_hours=kw.get("sleep", [7] * len(days))[i],
                                            left_home=kw.get("home", [True] * len(days))[i], ate_normally=kw.get("ate", [True] * len(days))[i],
                                            alcohol_units=kw.get("alc", [0] * len(days))[i], side_effects=kw.get("se", [[]] * len(days))[i],
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
        Diagnosis.objects.create(patient=p, icd10=icd, title=title, established_by=doc)
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
            "Готово. Врачи: petrova (психиатр), sidorov (терапевт). Пациенты: anna, igor, maria, oleg, elena. Пароль: " + PWD))

    def build(self):
        pu = self.user("petrova", "Елена", "Петрова", "doctor", "petrova@example.org")
        petrova, _ = DoctorProfile.objects.get_or_create(user=pu, defaults={"specialty": "psychiatrist", "organization": "ПНД № 7 (вымышл.)"})
        su = self.user("sidorov", "Андрей", "Сидоров", "doctor", "sidorov@example.org")
        sidorov, _ = DoctorProfile.objects.get_or_create(user=su, defaults={"specialty": "therapist", "organization": "ГП № 12 (вымышл.)", "backup_doctor": petrova})

        # 1. ГТР, Авиандр показан и работает — «зелёный» сценарий
        p, l = self.patient(petrova, "anna", "Смирнова", "Анна", "Викторовна", date(1992, 3, 14), "F", "F41.1", "Генерализованное тревожное расстройство", egfr=98)
        for d, g, q in [(35, 16, 9), (21, 12, 8), (7, 8, 6), (1, 7, 5)]:
            self.scale(p, "gad7", gad(g), d)
            self.scale(p, "phq9", phq(q), d)
        self.rx(p, petrova, "aviandr", "Авиандр (маритупирдин)", 20, 2, 28, adherence=0.93, seed=1)
        days = list(range(27, 0, -1))
        self.checkins(p, days, mood=[4 + i * 4 // 27 for i in range(27)], anx=[8 - i * 5 // 27 for i in range(27)],
                      se=[["сонливость"] if i < 4 else [] for i in range(27)])
        self.msg(l, pu, "Анна, первые дни возможна сонливость — это ожидаемо. Не садитесь за руль, пока не пройдёт.", 27)
        self.msg(l, p.user, "Спасибо, сонливость уже меньше. Тревога по утрам тоже стала слабее.", 20)
        TrustedContact.objects.create(patient=p, name="Сергей", relation="муж", phone="+7 900 000-00-01", doctor_may_call=True)

        # 2. Депрессия на сертралине без ответа, терапевт рассматривает Авиандр — «жёлтый»: вне инструкции + СИОЗС
        p, l = self.patient(sidorov, "igor", "Волков", "Игорь", "Николаевич", date(1985, 6, 2), "M", "F32.1", "Депрессивный эпизод средней степени",
                            conds=["cardiovascular_disease"], egfr=86)
        for d, q, g in [(42, 16, 12), (28, 15, 11), (14, 15, 11), (2, 15, 12)]:
            self.scale(p, "phq9", phq(q), d)
            self.scale(p, "gad7", gad(g), d)
        self.rx(p, sidorov, "сертралин", "сертралин", 50, 1, 42, adherence=0.85, seed=2)
        self.checkins(p, list(range(20, 0, -2)), mood=[3, 3, 4, 3, 3, 4, 3, 3, 4, 3], anx=[6, 7, 6, 6, 7, 6, 6, 7, 6, 6])
        self.msg(l, p.user, "Доктор, таблетки пью шестую неделю, а легче не стало. Тревога мешает спать. Может, добавить что-то от тревоги?", 1)

        # 3. ГТР, но печёночная недостаточность и недавно отменённый ИМАО — «красный»
        p, l = self.patient(petrova, "maria", "Кузнецова", "Мария", "Олеговна", date(1997, 11, 20), "F", "F41.1", "Генерализованное тревожное расстройство",
                            cp="A", egfr=88)
        Prescription.objects.create(patient=p, drug_code="моклобемид", drug_name="моклобемид", dose_mg=150, times_per_day=2,
                                    start_date=TODAY - timedelta(days=60), end_date=TODAY - timedelta(days=5), status="stopped", prescribed_by=petrova)
        for d, g, q in [(30, 15, 7), (5, 17, 8)]:
            self.scale(p, "gad7", gad(g), d)
            self.scale(p, "phq9", phq(q), d)
        self.checkins(p, [6, 4, 2, 1], mood=[5, 4, 4, 4], anx=[7, 8, 8, 8])

        # 4. Нарастание депрессии у пациента терапевта, суицидальные мысли — критический сигнал и обязательное направление
        p, l = self.patient(sidorov, "oleg", "Морозов", "Олег", "Сергеевич", date(1974, 1, 9), "M", "F41.2", "Смешанное тревожное и депрессивное расстройство",
                            conds=["diabetes"], egfr=74)
        TrustedContact.objects.create(patient=p, name="Ирина", relation="сестра", phone="+7 900 000-00-02", doctor_may_call=True)
        for d, q, g in [(28, 8, 9), (14, 13, 11)]:
            self.scale(p, "phq9", phq(q), d)
            self.scale(p, "gad7", gad(g), d)
        self.checkins(p, list(range(12, 0, -1)), mood=[4, 4, 3, 3, 3, 2, 2, 2, 2, 1, 1, 1], anx=[6, 6, 7, 7, 7, 8, 8, 8, 8, 9, 9, 9],
                      sleep=[6, 6, 5, 5, 5, 4.5, 4.5, 4, 5, 4, 4, 4], home=[True] * 8 + [False] * 4, ate=[True] * 9 + [False] * 3,
                      text=[""] * 11 + ["Всё тяжелее. Иногда думаю, что не вижу смысла жить дальше."])
        self.scale(p, "phq9", phq(19, item9=1), 0)

        # 5. На Авиандре, но алкоголь и пропуски приёма — важный сигнал
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

        # 6. Карточка создана, пациент ещё не подключился — показывает код приглашения
        self.patient(petrova, None, "Павлов", "Дмитрий", "Игоревич", date(1990, 5, 5), "M", "F41.0", "Паническое расстройство", code="K7Q2MX")
