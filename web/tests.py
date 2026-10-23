from django.core.management import call_command
from django.test import TestCase

from alerts.models import Alert


def pid_of(last):
    return PatientProfile.objects.get(last_name=last).id
from core.models import AuditLog, DoctorDecision, PatientProfile, Prescription, Referral, ScaleResponse


class WebFlows(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", "--reset", stdout=open("/dev/null", "w"))

    def login(self, u):
        self.client.logout()
        self.assertTrue(self.client.login(username=u, password="Demo-2026!"))

    def pid(self, last):
        return PatientProfile.objects.get(last_name=last).public_id

    # --- права доступа ---
    def test_doctor_sees_only_own_patients(self):
        self.login("sidorov")
        self.assertEqual(self.client.get(f"/doctor/patient/{self.pid('Морозов')}/").status_code, 200)
        self.assertEqual(self.client.get(f"/doctor/patient/{self.pid('Смирнова')}/").status_code, 404)

    def test_patient_cannot_open_doctor_pages(self):
        self.login("anna")
        self.assertEqual(self.client.get("/doctor/").status_code, 403)
        self.assertEqual(self.client.get(f"/doctor/patient/{self.pid('Смирнова')}/").status_code, 403)

    def test_anonymous_redirected(self):
        self.assertEqual(self.client.get("/doctor/").status_code, 302)
        self.assertEqual(self.client.get("/me/").status_code, 302)

    def test_card_view_is_audited(self):
        self.login("petrova")
        self.client.get(f"/doctor/patient/{self.pid('Смирнова')}/")
        self.assertTrue(AuditLog.objects.filter(action="view_card", patient_id=pid_of("Смирнова")).exists())

    def test_patient_cannot_mark_foreign_dose(self):
        other = Prescription.objects.filter(patient_id=pid_of("Смирнова")).first().intakes.first()
        self.login("elena")
        self.assertEqual(self.client.post(f"/me/meds/{other.id}/", {"taken": "1"}).status_code, 404)

    # --- все страницы открываются ---
    def test_pages_render(self):
        self.login("petrova")
        for url in ["/doctor/", "/doctor/alerts/", "/doctor/patient/new/"] + [f"/doctor/patient/{p.public_id}/" for p in PatientProfile.objects.filter(care_links__doctor__user__username="petrova")]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.login("anna")
        for url in ["/me/", "/me/checkin/", "/me/scale/phq9/", "/me/scale/gad7/", "/me/meds/", "/me/progress/", "/me/chat/", "/me/help/"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    # --- клинические действия только через врача ---
    def test_red_requires_override_and_rationale(self):
        self.login("petrova")
        url = f"/doctor/patient/{self.pid('Кузнецова')}/aviandr/"
        self.client.post(url, {"start_date": "2026-10-21"})
        self.assertFalse(Prescription.objects.filter(patient_id=pid_of("Кузнецова"), drug_code="aviandr").exists())
        self.client.post(url, {"start_date": "2026-10-21", "override": "on", "rationale": "консилиум"})
        rx = Prescription.objects.get(patient_id=pid_of("Кузнецова"), drug_code="aviandr")
        self.assertTrue(rx.off_label)
        self.assertTrue(DoctorDecision.objects.filter(kind="prescribe", patient=rx.patient).exists())

    def test_green_prescription_creates_schedule(self):
        self.login("petrova")
        p = PatientProfile.objects.get(last_name="Павлов")
        Prescription.objects.filter(patient=p).delete()
        p.diagnoses.update(icd10="F41.1")
        # СКФ неизвестна → «жёлтый», без комментария врача не назначается
        self.client.post(f"/doctor/patient/{p.public_id}/aviandr/", {"start_date": "2026-10-21"})
        self.assertFalse(Prescription.objects.filter(patient=p, drug_code="aviandr").exists())
        p.egfr = 95
        p.save()
        self.client.post(f"/doctor/patient/{p.public_id}/aviandr/", {"start_date": "2026-10-21"})
        self.client.post(f"/doctor/patient/{p.public_id}/aviandr/", {"start_date": "2026-10-21"})  # повторный клик
        self.assertEqual(Prescription.objects.filter(patient=p, drug_code="aviandr", status="active").count(), 1)
        rx = Prescription.objects.get(patient=p, drug_code="aviandr")
        self.assertFalse(rx.off_label)
        self.assertTrue(rx.intakes.exists())

    def test_referral_confirm(self):
        self.login("sidorov")
        p = PatientProfile.objects.get(last_name="Морозов")
        ref = Referral.objects.get(patient=p, status="proposed")
        self.assertEqual(ref.urgency, "mandatory")
        self.client.post(f"/doctor/patient/{p.public_id}/referral/{ref.id}/", {"decision": "confirm"})
        ref.refresh_from_db()
        self.assertEqual(ref.status, "confirmed")

    # --- пациент: данные сразу у врача, кризисный экран ---
    def test_checkin_reaches_doctor_and_crisis_screen(self):
        self.login("anna")
        r = self.client.post("/me/checkin/", {"mood": 1, "anxiety": 9, "text": "хочу умереть"})
        self.assertRedirects(r, "/me/help/?crisis=1", fetch_redirect_response=False)
        a = Alert.objects.get(patient_id=pid_of("Смирнова"), rule_id="suicide_risk_text")
        self.assertEqual(a.doctor.username, "petrova")
        self.assertIsNotNone(a.delivered_at)

    def test_scale_submission(self):
        self.login("anna")
        data = {f"q{i}": 1 for i in range(7)}
        self.client.post("/me/scale/gad7/", data)
        self.assertEqual(ScaleResponse.objects.filter(patient_id=pid_of("Смирнова"), scale_code="gad7").order_by("-created_at").first().total, 7)

    def test_join_by_code(self):
        r = self.client.post("/join/", {"code": "K7Q2MX", "username": "dmitry", "password": "Pavlov-2026-x!", "consent_pd": "on", "consent_remote": "on"})
        self.assertRedirects(r, "/me/", fetch_redirect_response=False)
        p = PatientProfile.objects.get(last_name="Павлов")
        self.assertEqual(p.user.username, "dmitry")
        self.assertEqual(p.consents.count(), 2)

    def test_import_history(self):
        self.login("petrova")
        p = self.pid("Павлов")
        text = "Ds: F41.1 ГТР. Сопутствующий: эпилепсия (G40). Принимает вальпроевую кислоту 500 мг 2 р/д. GAD-7 16."
        r = self.client.post(f"/doctor/patient/{p}/import/", {"text": text})
        self.assertContains(r, "G40")
        self.assertContains(r, "вальпроевая кислота")


class ReviewRegressions(TestCase):
    """Регрессии по итогам независимого ревью."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", "--reset", stdout=open("/dev/null", "w"))

    def login(self, u):
        self.client.logout()
        self.client.login(username=u, password="Demo-2026!")

    def test_new_crisis_after_ack_raises_again(self):
        self.login("anna")
        self.client.post("/me/chat/", {"text": "хочу умереть"})
        a = Alert.objects.get(patient_id=pid_of("Смирнова"), rule_id="suicide_risk_text")
        self.login("petrova")
        self.client.post(f"/doctor/alerts/{a.id}/ack/", {"resolution": "звонок"})
        self.login("anna")
        r = self.client.post("/me/chat/", {"text": "всё равно наложу на себя руки"})
        self.assertRedirects(r, "/me/help/?crisis=1", fetch_redirect_response=False)
        self.assertEqual(Alert.objects.filter(patient_id=pid_of("Смирнова"), rule_id="suicide_risk_text").count(), 2)

    def test_old_message_does_not_retrigger(self):
        self.login("anna")
        self.client.post("/me/chat/", {"text": "хочу умереть"})
        Alert.objects.filter(patient_id=pid_of("Смирнова")).update(status="acknowledged")
        r = self.client.post("/me/checkin/", {"mood": 5, "anxiety": 4})
        self.assertRedirects(r, "/me/", fetch_redirect_response=False)
        self.assertEqual(Alert.objects.filter(patient_id=pid_of("Смирнова"), rule_id="suicide_risk_text").count(), 1)

    def test_escalated_backup_doctor_can_act(self):
        a = Alert.objects.filter(patient_id=pid_of("Морозов"), severity="critical").first()
        a.escalated_to = a.doctor.doctor.backup_doctor.user
        a.status = "escalated"
        a.save()
        self.login("petrova")
        self.assertContains(self.client.get("/doctor/alerts/"), "Морозов")
        self.assertEqual(self.client.get(a.card_url).status_code, 200)
        self.client.post(f"/doctor/alerts/{a.id}/ack/", {"resolution": "связалась"})
        a.refresh_from_db()
        self.assertEqual(a.status, "acknowledged")

    def test_api_requires_doctor_and_rejects_bad_json(self):
        self.assertEqual(self.client.post("/api/extract", "{}", content_type="application/json").status_code, 401)
        self.login("petrova")
        for body in ["not json", "[1]", '{"text": 5}']:
            self.assertEqual(self.client.post("/api/extract", body, content_type="application/json").status_code, 400, body)

    def test_import_payload_not_trusted_from_client(self):
        self.login("petrova")
        p = PatientProfile.objects.get(last_name="Павлов")
        r = self.client.post(f"/doctor/patient/{p.public_id}/import/", {"apply": "1", "dx": "Z99", "payload": "{broken"})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(p.diagnoses.filter(icd10="Z99").exists())

    def test_open_redirect_blocked(self):
        a = Alert.objects.filter(patient_id=pid_of("Новикова")).first()
        self.login("petrova")
        r = self.client.post(f"/doctor/alerts/{a.id}/ack/", {"next": "https://evil.example/"})
        self.assertNotIn("evil", r["Location"])

    def test_join_rate_limited(self):
        for _ in range(10):
            self.client.post("/join/", {"code": "WRONGCODE1", "username": "x", "password": "y"})
        self.assertEqual(self.client.post("/join/", {"code": "WRONGCODE1"}).status_code, 429)

    def test_invalid_patient_form_values(self):
        self.login("petrova")
        r = self.client.post("/doctor/patient/new/", {"last_name": "А", "first_name": "Б", "birth_date": "1990-01-01", "sex": "F",
                                                     "child_pugh_class": "x", "egfr": "-5", "icd10": "абв"})
        self.assertEqual(r.status_code, 200)  # форма с ошибками, карточка не создана
        self.assertFalse(PatientProfile.objects.filter(last_name="А").exists())
