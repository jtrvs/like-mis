"""Плановая проверка: пациент перестал отмечать таблетки → сигнал врачу (правило diary_abandoned)."""
from datetime import date, timedelta

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from alerts.management.commands.process_alerts import run_schedule_hook
from alerts.models import Alert
from core.models import MedicationIntake, PatientProfile

from .alerts_bridge import silent_patients


class SilentPatientsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with open('/dev/null', 'w') as output:
            call_command('seed_demo', '--reset', stdout=output)

    def make_silent(self, last_name, days=4):
        """Пациент на терапии последние `days` дней и ни разу не открывал приложение (приёмов в базе нет)."""
        p = PatientProfile.objects.get(last_name=last_name)
        MedicationIntake.objects.filter(prescription__patient=p).delete()
        Alert.objects.filter(patient_id=p.id).delete()
        p.prescriptions.filter(status='active').update(
            start_date=date.today() - timedelta(days=days), comment='',
            created_at=timezone.now() - timedelta(days=days + 1))
        return p

    def alerts(self, p):
        return Alert.objects.filter(patient_id=p.id, rule_id='diary_abandoned')

    def test_silent_patient_raises_alert(self):
        p = self.make_silent('Смирнова')
        run_schedule_hook()
        a = self.alerts(p).get()
        self.assertEqual(a.patient_label, f'П-{p.id:04d}')
        self.assertGreaterEqual(a.facts['unmarked_doses_streak'], 6)
        # расписание достроено, хотя никто не открывал экран
        self.assertTrue(MedicationIntake.objects.filter(prescription__patient=p).exists())

    def test_patient_who_marks_doses_is_quiet(self):
        p = self.make_silent('Смирнова')
        for rx in p.prescriptions.filter(status='active'):
            from . import services as S
            S.ensure_schedule(rx)
        MedicationIntake.objects.filter(prescription__patient=p).update(taken=True)
        run_schedule_hook()
        self.assertFalse(self.alerts(p).exists())

    def test_no_duplicate_and_no_hourly_nagging_after_ack(self):
        p = self.make_silent('Смирнова')
        run_schedule_hook()
        run_schedule_hook()  # через час — тот же открытый сигнал, второго нет
        self.assertEqual(self.alerts(p).count(), 1)
        a = self.alerts(p).get()
        Alert.objects.filter(pk=a.pk).update(status='acknowledged', acknowledged_at=timezone.now())
        run_schedule_hook()  # врач отметил — сутки не повторяем
        self.assertEqual(self.alerts(p).count(), 1)
        Alert.objects.filter(pk=a.pk).update(acknowledged_at=timezone.now() - timedelta(hours=25))
        run_schedule_hook()  # прошли сутки, пациент так и молчит — снова сигнал
        self.assertEqual(self.alerts(p).count(), 2)

    def test_patient_label_has_no_name(self):
        for kwargs in silent_patients():
            self.assertRegex(kwargs['patient_label'], r'^П-\d{4}$')
            self.assertNotIn('trigger', kwargs)
