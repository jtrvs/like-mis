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


    def test_patient_search_is_limited_to_assigned_patients(self):
        self.login("sidorov")
        response = self.client.get("/doctor/", {"q": "мОрОз"})
        self.assertEqual([r["p"].last_name for r in response.context["rows"]], ["Морозов"])
        response = self.client.get("/doctor/", {"q": "Смирнова"})
        self.assertEqual(response.context["rows"], [])

    def test_filter_for_patients_without_accounts(self):
        self.login("petrova")
        response = self.client.get("/doctor/", {"status": "unjoined"})
        self.assertEqual([r["p"].last_name for r in response.context["rows"]], ["Павлов"])
        self.assertContains(response, "Нет записей")

    def test_zero_score_is_displayed_as_zero(self):
        patient = PatientProfile.objects.get(last_name="Смирнова")
        ScaleResponse.objects.filter(patient=patient, scale_code="phq9").update(total=0)
        self.login("petrova")
        response = self.client.get("/doctor/", {"q": "Смирнова"})
        self.assertContains(response, 'data-score="phq9">0</b>')


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

    def test_card_preserves_zero_sleep_and_scale_scores(self):
        from core.models import DailyCheckin
        p = PatientProfile.objects.get(last_name="Смирнова")
        DailyCheckin.objects.create(patient=p, mood=0, anxiety=0, sleep_hours=0)
        ScaleResponse.objects.create(patient=p, scale_code="phq9", total=0, answers=[0] * 9)
        self.login("petrova")
        response = self.client.get(f"/doctor/patient/{p.public_id}/")
        self.assertContains(response, '<dd>0 ч</dd>', html=True)
        self.assertContains(response, '0<small> / 27</small>', html=True)
        self.assertEqual(response.context["chart"]["phq9"][-1]["y"], 0)

    def test_card_without_history_has_no_empty_chart(self):
        from core.models import DailyCheckin
        p = PatientProfile.objects.get(last_name="Павлов")
        DailyCheckin.objects.filter(patient=p).delete()
        ScaleResponse.objects.filter(patient=p).delete()
        self.login("petrova")
        response = self.client.get(f"/doctor/patient/{p.public_id}/")
        self.assertContains(response, "Пациент ещё не заполнял дневник.")
        self.assertContains(response, "График появится после заполнения опросника.")
        self.assertNotContains(response, '<canvas')

    def test_invalid_diary_keeps_answers_and_explains_error(self):
        self.login("anna")
        response = self.client.post('/me/checkin/', {
            'mood': '0', 'anxiety': '4', 'sleep_hours': '25',
            'left_home': '0', 'ate_normally': '1', 'alcohol_units': '0',
            'side_effects': ['тошнота'], 'text': 'Запись для врача',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Запись пока не сохранена.')
        self.assertContains(response, 'href="#sleep_hours-question"')
        self.assertContains(response, 'value="0" checked required')
        self.assertContains(response, 'value="тошнота" checked')
        self.assertContains(response, 'Запись для врача')

    def test_incomplete_questionnaire_preserves_selected_answers(self):
        self.login("anna")
        response = self.client.post('/me/scale/gad7/', {'q0': '0', 'q1': '2'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Ответьте, пожалуйста, на все вопросы.')
        self.assertContains(response, 'name="q0" value="0" checked required')
        self.assertContains(response, 'name="q1" value="2" checked required')

    def test_chat_without_lead_does_not_pretend_to_send(self):
        from core.models import CareLink, Message
        p = PatientProfile.objects.get(user__username="anna")
        CareLink.objects.filter(patient=p).update(active=False)
        self.login("anna")
        response = self.client.get('/me/chat/')
        self.assertContains(response, 'Ведущий врач пока не подключён')
        self.assertNotContains(response, '<textarea')
        before = Message.objects.count()
        response = self.client.post('/me/chat/', {'text': 'Вопрос врачу'}, follow=True)
        self.assertContains(response, 'Сообщение не отправлено')
        self.assertEqual(Message.objects.count(), before)

    def test_invalid_contact_shows_error_and_preserves_name(self):
        self.login("anna")
        response = self.client.post('/me/help/', {'name': 'Иван', 'phone': '', 'patient_may_call': 'on'})
        self.assertContains(response, 'Контакт не сохранён.')
        self.assertContains(response, 'value="Иван"')
        self.assertContains(response, 'class="more panel contact-settings" open')

    def test_crisis_screen_does_not_claim_doctor_read_message(self):
        self.login("anna")
        response = self.client.get('/me/help/?crisis=1')
        self.assertContains(response, 'Этот экран не подтверждает')
        self.assertNotContains(response, 'Ваш врач уже получил сообщение')
        self.assertNotContains(response, 'ответят прямо сейчас')

    def test_medication_sections_separate_today_from_history(self):
        from django.utils import timezone
        self.login("anna")
        response = self.client.get('/me/meds/')
        self.assertContains(response, 'Сегодня')
        self.assertTrue(all(timezone.localdate(d.scheduled_at) == timezone.localdate() for d in response.context['today_doses']))
        self.assertTrue(all(timezone.localdate(d.scheduled_at) < timezone.localdate() for d in response.context['past_doses']))

    def test_patient_progress_shows_zero_scores_and_empty_history(self):
        from core.models import DailyCheckin
        p = PatientProfile.objects.get(user__username='anna')
        ScaleResponse.objects.filter(patient=p).delete()
        DailyCheckin.objects.filter(patient=p).delete()
        ScaleResponse.objects.create(patient=p, scale_code='phq9', total=0, answers=[0] * 9)
        self.login('anna')
        response = self.client.get('/me/progress/')
        self.assertContains(response, '0<small> / 27</small>', html=True)
        self.assertContains(response, 'Ещё не заполнен')
        self.assertContains(response, 'Записей пока нет.')
        self.assertNotContains(response, 'id="ch-diary"')
        self.assertEqual(response.context['chart']['phq9'][-1]['y'], 0)

    def test_import_review_shows_source_without_applying_it(self):
        from unittest.mock import patch
        p = PatientProfile.objects.get(last_name='Павлов')
        self.login('petrova')
        text = 'Тестовая выписка <script>alert(1)</script>'
        payload = {'diagnoses': ['G40'], 'medications': [{'inn': 'вальпроевая кислота', 'dose_mg': 500, 'times_per_day': 2}],
                   'stopped_medications': [], 'conditions': [], 'scales': {'phq9': 12}, 'suicidal_risk': False}
        before = (p.diagnoses.count(), p.prescriptions.count())
        with patch('web.views_doctor.extract_text', return_value=payload):
            response = self.client.post(f'/doctor/patient/{p.public_id}/import/', {'text': text})
        self.assertContains(response, 'Ещё не сохранено')
        self.assertContains(response, 'автоматически не переносятся')
        self.assertContains(response, '&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertNotContains(response, '<script>alert(1)</script>')
        self.assertEqual((p.diagnoses.count(), p.prescriptions.count()), before)
        self.client.post(f'/doctor/patient/{p.public_id}/import/', {'apply': '1', 'dx': ['G40']})
        self.assertTrue(p.diagnoses.filter(icd10='G40').exists())
        self.assertEqual(p.prescriptions.count(), before[1])

    def test_import_empty_text_displays_error(self):
        self.login('petrova')
        p = PatientProfile.objects.get(last_name='Павлов')
        response = self.client.post(f'/doctor/patient/{p.public_id}/import/', {'text': '   '})
        self.assertContains(response, 'Вставьте текст для разбора.')
