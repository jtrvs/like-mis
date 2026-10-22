"""
Демонстрация без моделей пациента: создаёт врача и резервного врача и «присылает» несколько событий.
  python manage.py alerts_demo
Потом открыть http://localhost:8000/admin/login/?next=/alerts/ и войти: doctor_demo / Demo-2026!
"""
from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand

from alerts import facts, service


class Command(BaseCommand):
    help = "Создать демо-врача и несколько сигналов"

    def user(self, name, first, last):
        u, _ = User.objects.get_or_create(username=name, defaults={"first_name": first, "last_name": last})
        u.set_password("Demo-2026!")
        u.is_staff = True  # пока у проекта нет своей страницы входа — входим через /admin/login/
        u.save()
        u.groups.add(Group.objects.get_or_create(name="doctor")[0])
        return u

    def handle(self, **kw):
        doc = self.user("doctor_demo", "Андрей", "Сидоров")
        backup = self.user("backup_demo", "Елена", "Петрова")
        common = dict(doctor=doc, backup_doctor=backup)

        r1 = service.report_event(patient_id=1001, patient_label="П-1001", trigger="scale", **common,
                                  facts=facts.from_scale("phq9", [2, 2, 2, 2, 2, 2, 2, 2, 1], previous_total=12))
        r2 = service.report_event(patient_id=1002, patient_label="П-1002", trigger="message", **common,
                                  text="Мне очень плохо, не вижу смысла жить дальше")
        checkins = [{"mood": 5, "anxiety": 5, "alcohol_units": 3, "sleep_hours": 7, "left_home": True, "ate_normally": True}]
        r3 = service.report_event(patient_id=1003, patient_label="П-1003", trigger="checkin", **common,
                                  facts=facts.from_checkins(checkins, on_aviandr=True))
        for name, r in [("PHQ-9 с п.9", r1), ("сообщение", r2), ("алкоголь на Авиандре", r3)]:
            self.stdout.write(f"{name}: сигналов {len(r['alerts'])}, экран помощи пациенту: {r['show_crisis_screen']}")
        self.stdout.write(self.style.SUCCESS("Готово. Откройте http://localhost:8000/admin/login/?next=/alerts/  логин doctor_demo, пароль Demo-2026!"))
