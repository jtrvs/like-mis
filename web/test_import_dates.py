from datetime import date, timedelta
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from core.models import PatientProfile, Prescription
from . import services as S


class ImportMedicationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with open('/dev/null', 'w') as output:
            call_command('seed_demo', '--reset', stdout=output)

    def setUp(self):
        self.client.login(username='petrova', password='Demo-2026!')
        self.patient = PatientProfile.objects.get(last_name='Павлов')
        self.url = f'/doctor/patient/{self.patient.public_id}/import/'
        self.payload = {'diagnoses': ['G40'], 'medications': [
            {'inn': 'сертралин', 'dose_mg': 50, 'times_per_day': 1}],
            'stopped_medications': ['моклобемид'], 'conditions': [], 'scales': {},
            'suicidal_risk': False}

    def parse(self):
        with patch('web.views_doctor.extract_text', return_value=self.payload):
            return self.client.post(self.url, {'text': 'Историческая выписка. Сертралин с 01.01.2020.'})

    def apply(self, **changes):
        values = {'apply': '1', 'med': ['0'], 'current-0-start_date': '2020-01-01'}
        values.update(changes)
        return self.client.post(self.url, values)

    def test_dates_are_required_and_no_partial_changes_are_written(self):
        self.parse()
        response = self.apply(**{'current-0-start_date': '', 'dx': ['G40']})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Перенос не выполнен.')
        self.assertContains(response, 'Историческая выписка.')
        self.assertFalse(self.patient.prescriptions.filter(drug_name='сертралин').exists())
        self.assertFalse(self.patient.diagnoses.filter(icd10='G40').exists())

    def test_history_dates_are_saved_and_repeat_import_is_idempotent(self):
        self.parse()
        self.apply(stopped=['0'], **{'stopped-0-start_date': '2019-01-01', 'stopped-0-end_date': '2019-02-01'})
        self.parse()
        self.apply(stopped=['0'], **{'stopped-0-start_date': '2019-01-01', 'stopped-0-end_date': '2019-02-01'})
        active = self.patient.prescriptions.get(drug_name='сертралин')
        stopped = self.patient.prescriptions.get(drug_name='моклобемид')
        self.assertEqual(active.start_date, date(2020, 1, 1))
        self.assertEqual(stopped.start_date, date(2019, 1, 1))
        self.assertEqual(stopped.end_date, date(2019, 2, 1))

    def test_conflicting_regimen_is_not_overwritten(self):
        self.parse(); self.apply()
        self.payload['medications'][0]['dose_mg'] = 100
        self.parse()
        response = self.apply(dx=['G40'])
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'другой датой или схемой')
        self.assertEqual(self.patient.prescriptions.get(drug_name='сертралин').dose_mg, 50)
        self.assertFalse(self.patient.diagnoses.filter(icd10='G40').exists())

    def test_invalid_stop_order_and_future_dates_are_rejected(self):
        self.parse()
        response = self.apply(med=[], stopped=['0'], **{'stopped-0-start_date': '2020-02-01', 'stopped-0-end_date': '2020-01-01'})
        self.assertContains(response, 'раньше начала')
        response = self.apply(**{'current-0-start_date': (timezone.localdate()+timedelta(days=1)).isoformat()})
        self.assertContains(response, 'не может быть в будущем')
        self.assertFalse(self.patient.prescriptions.filter(drug_name='сертралин').exists())

    def test_unselected_medication_does_not_require_dates(self):
        self.parse()
        response = self.apply(med=[], dx=['G40'])
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.patient.diagnoses.filter(icd10='G40').exists())
        self.assertFalse(self.patient.prescriptions.filter(drug_name='сертралин').exists())

    def test_import_does_not_fabricate_unmarked_historical_intakes(self):
        self.parse(); self.apply()
        rx = self.patient.prescriptions.get(drug_name='сертралин')
        S.ensure_schedule(rx)
        self.assertFalse(rx.intakes.filter(scheduled_at__date__lt=timezone.localdate(rx.created_at)).exists())

    def test_trade_name_alias_does_not_create_second_aviandr(self):
        from web.import_review import prescription_name
        self.payload['medications'][0] = {'inn': 'маритупирдин', 'dose_mg': 20, 'times_per_day': 2}
        original = Prescription.objects.create(patient=self.patient, drug_code='aviandr', drug_name='Авиандр (маритупирдин)',
            dose_mg=20, times_per_day=2, start_date=date(2020, 1, 1))
        self.parse(); self.apply()
        matches = [rx for rx in self.patient.prescriptions.all() if prescription_name(rx) == 'маритупирдин']
        self.assertEqual([rx.id for rx in matches], [original.id])

    def test_error_keeps_unchecked_diagnosis_unchecked(self):
        self.parse()
        response = self.apply(**{'current-0-start_date': ''})
        self.assertNotContains(response, 'name="dx" value="G40" checked')

    def test_stale_or_forged_medication_selection_cannot_partially_save(self):
        self.parse()
        response = self.apply(med=['сертралин'], dx=['G40'])
        self.assertContains(response, 'Список препаратов не соответствует текущему разбору')
        self.assertFalse(self.patient.diagnoses.filter(icd10='G40').exists())
        self.assertFalse(self.patient.prescriptions.filter(drug_name='сертралин').exists())
