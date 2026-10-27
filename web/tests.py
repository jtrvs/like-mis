from django.core.management import call_command
from django.test import TestCase

from alerts.models import Alert


def pid_of(last):
    return PatientProfile.objects.get(last_name=last).id
from core.models import AuditLog, DoctorDecision, Message, PatientProfile, Prescription, Referral, ScaleResponse


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
        for url in ["/me/", "/me/checkin/", "/me/scale/phq9/", "/me/scale/gad7/", "/me/scale/auditc/", "/me/short/", "/me/cssrs/",
                    "/me/meds/", "/me/meds/report/", "/me/progress/", "/me/chat/", "/me/help/"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertEqual(self.client.get("/me/scale/hama/").status_code, 302)  # шкалы врача пациенту не открываются

    # --- клинические действия только через врача ---
    def test_red_requires_override_and_rationale(self):
        self.login("sidorov")  # сценарий Б: пациентка терапевта
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
        self.assertEqual(ref.urgency, "emergency")  # сценарий В: C-SSRS — намерение без плана
        self.client.post(f"/doctor/patient/{p.public_id}/referral/{ref.id}/", {"decision": "confirm"})
        ref.refresh_from_db()
        self.assertEqual(ref.status, "confirmed")

    # --- пациент: данные сразу у врача, кризисный экран ---
    def test_checkin_reaches_doctor_and_crisis_screen(self):
        self.login("anna")
        r = self.client.post("/me/checkin/", {"mood": 1, "calm": 9, "text": "хочу умереть"})
        # таблица правил 1.2: сразу Колумбийская шкала с контактами поддержки, врачу — оранжевый сигнал
        self.assertRedirects(r, "/me/cssrs/", fetch_redirect_response=False)
        a = Alert.objects.get(patient_id=pid_of("Смирнова"), rule_id="suicide_risk_text")
        self.assertEqual((a.doctor.username, a.severity), ("petrova", "high"))
        self.assertIsNotNone(a.delivered_at)
        page = self.client.get("/me/cssrs/")
        self.assertContains(page, "8 800 100-49-94")
        # намерение без плана → красный сигнал и экран помощи
        r = self.client.post("/me/cssrs/", {"q0": "1", "q1": "1", "q2": "1", "q3": "1", "q4": "0", "q5": "0"})
        self.assertRedirects(r, "/me/help/?crisis=1", fetch_redirect_response=False)
        self.assertTrue(Alert.objects.filter(patient_id=pid_of("Смирнова"), rule_id="cssrs_high", severity="critical").exists())
        self.assertContains(self.client.get("/me/help/?crisis=1"), "Ваш врач получил уведомление")

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
        self.assertRedirects(r, "/me/cssrs/", fetch_redirect_response=False)
        self.assertEqual(Alert.objects.filter(patient_id=pid_of("Смирнова"), rule_id="suicide_risk_text").count(), 2)

    def test_old_message_does_not_retrigger(self):
        self.login("anna")
        self.client.post("/me/chat/", {"text": "хочу умереть"})
        Alert.objects.filter(patient_id=pid_of("Смирнова")).update(status="acknowledged")
        r = self.client.post("/me/checkin/", {"mood": 5, "calm": 4})
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


class AlinaScenarios(TestCase):
    """Клинические сценарии А–Г и правила клинического психолога команды."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", "--reset", stdout=open("/dev/null", "w"))

    def login(self, u):
        self.client.logout()
        self.assertTrue(self.client.login(username=u, password="Demo-2026!"))

    def p(self, last):
        return PatientProfile.objects.get(last_name=last)

    def test_a_no_extra_alerts_and_response(self):
        from web import services as S
        p = self.p("Смирнова")
        self.assertFalse(Alert.objects.filter(patient_id=p.id).exists())
        self.assertEqual(S.aviandr_panel(p)["verdict"], "green")
        self.assertEqual(S.treatment_response(p)["code"], "response")
        self.login("petrova")
        r = self.client.get(f"/doctor/patient/{p.public_id}/")
        self.assertContains(r, "Ответ на терапию")
        self.assertContains(r, "HAM-A")

    def test_b_contraindication_and_desirable(self):
        from web import services as S
        p = self.p("Кузнецова")
        self.assertEqual(S.aviandr_panel(p)["verdict"], "red")
        self.assertFalse(Alert.objects.filter(patient_id=p.id).exists())
        self.assertEqual(S.routing_panel(p)["urgency"], "desirable")
        self.login("sidorov")
        r = self.client.get(f"/doctor/patient/{p.public_id}/")
        self.assertContains(r, "ЖЕЛАТЕЛЬНО")

    def test_c_therapist_patient_red_and_emergency(self):
        p = self.p("Морозов")
        self.assertFalse(p.diagnoses.exists())
        self.assertTrue(Alert.objects.filter(patient_id=p.id, rule_id="cssrs_high", severity="critical").exists())
        self.login("sidorov")
        r = self.client.get(f"/doctor/patient/{p.public_id}/")
        self.assertContains(r, "ОБЯЗАТЕЛЬНО · ЭКСТРЕННО")
        self.assertContains(r, "Лечение — после консультации психиатра")
        self.assertContains(r, "Сработало правил")  # несколько правил — одна карточка с перечнем
        # одна отметка на карточку, с «ложным срабатыванием»
        self.client.post(f"/doctor/patient/{p.public_id}/alerts/ack/", {"resolution": "позвонил", "false_positive": ""})
        self.assertFalse(Alert.objects.filter(patient_id=p.id, status__in=Alert.OPEN).exists())

    def test_d_panic_home_bound_yellow_offlabel(self):
        from web import services as S
        p = self.p("Соколова")
        self.assertEqual(Alert.objects.get(patient_id=p.id).rule_id, "home_bound")
        self.assertTrue(any(f.level == "off_label" and "ID 456" in f.text for f in S.aviandr_panel(p)["findings"]))

    def test_cssrs_skip_logic(self):
        self.login("anna")
        # «нет» на вопрос 2 — вопросы 3–5 не обязательны; только желание умереть → низкий риск
        r = self.client.post("/me/cssrs/", {"q0": "1", "q1": "0", "q5": "0"})
        self.assertRedirects(r, "/me/help/?support=1", fetch_redirect_response=False)
        last = ScaleResponse.objects.filter(patient__last_name="Смирнова", scale_code="cssrs").latest("created_at")
        self.assertEqual(last.severity, "low")
        r = self.client.post("/me/cssrs/", {"q0": "1"})
        self.assertContains(r, "Ответьте, пожалуйста")

    def test_phq9_item9_goes_to_cssrs(self):
        self.login("anna")
        data = {f"q{i}": 0 for i in range(9)}
        data["q8"] = 1
        r = self.client.post("/me/scale/phq9/", data)
        self.assertRedirects(r, "/me/cssrs/", fetch_redirect_response=False)

    def test_report_overdose_and_pregnancy(self):
        self.login("anna")
        r = self.client.post("/me/meds/report/", {"kind": "extra_dose", "extra_tablets": 4, "feels_bad": "0"})
        self.assertRedirects(r, "/me/help/?reason=overdose", fetch_redirect_response=False)
        self.assertContains(self.client.get("/me/help/?reason=overdose"), "Возьмите с собой упаковку")
        self.client.post("/me/meds/report/", {"kind": "pregnancy"})
        rules = set(Alert.objects.filter(patient_id=self.p("Смирнова").id).values_list("rule_id", flat=True))
        self.assertTrue({"overdose", "pregnancy_on_aviandr"} <= rules)
        r = self.client.post("/me/meds/report/", {"kind": "extra_dose"})
        self.assertContains(r, "Укажите, сколько таблеток")

    def test_sos_button_alerts_doctor_once(self):
        self.login("anna")
        self.client.get("/me/help/?sos=1")
        self.client.get("/me/help/?sos=1")
        self.assertEqual(Alert.objects.filter(patient_id=self.p("Смирнова").id, rule_id="sos_pressed").count(), 1)
        self.client.get("/me/help/")
        self.assertEqual(Alert.objects.filter(rule_id="sos_pressed").count(), 1)

    def test_serotonin_question_only_with_combination(self):
        self.login("anna")
        self.assertNotContains(self.client.get("/me/checkin/"), "сильный пот")
        p = self.p("Смирнова")
        Prescription.objects.create(patient=p, drug_code="сертралин", drug_name="сертралин", dose_mg=50, times_per_day=1,
                                    start_date=p.prescriptions.first().start_date)
        self.assertContains(self.client.get("/me/checkin/"), "сильный пот")
        r = self.client.post("/me/checkin/", {"mood": 4, "calm": 7, "serotonin_signs": ["жар", "дрожь"]})
        self.assertRedirects(r, "/me/help/?reason=serotonin", fetch_redirect_response=False)
        self.assertTrue(Alert.objects.filter(patient_id=p.id, rule_id="serotonin_syndrome", severity="critical").exists())

    def test_doctor_assessments(self):
        p = self.p("Соколова")
        self.login("petrova")
        url = f"/doctor/patient/{p.public_id}/assess/"
        self.client.post(url + "hama/", {f"q{i}": 2 for i in range(14)})
        self.client.post(url + "cgi_s/", {"q0": 4})
        self.assertEqual(ScaleResponse.objects.get(patient=p, scale_code="hama").total, 28)
        self.assertEqual(ScaleResponse.objects.get(patient=p, scale_code="cgi_s").filled_by, "doctor")
        self.client.post(url + "hama/", {"q0": 1})  # не все пункты — не сохраняется
        self.assertEqual(ScaleResponse.objects.filter(patient=p, scale_code="hama").count(), 1)
        self.login("sidorov")
        self.assertEqual(self.client.post(url + "hama/", {f"q{i}": 2 for i in range(14)}).status_code, 404)  # чужой пациент

    def test_patient_pages_have_no_labels(self):
        """Тексты для интерфейса: в кабинете пациента нет слов-ярлыков."""
        import re
        self.login("oleg")
        for url in ["/me/", "/me/checkin/", "/me/scale/phq9/", "/me/scale/gad7/", "/me/short/", "/me/cssrs/", "/me/meds/",
                    "/me/meds/report/", "/me/progress/", "/me/chat/", "/me/help/", "/me/help/?crisis=1", "/me/help/?support=1",
                    "/me/learn/", "/me/learn/aviandr/", "/me/learn/specialist/", "/me/learn/between/", "/me/?one=1"]:
            html = self.client.get(url).content.decode()
            text = re.sub(r"<[^>]+>", " ", re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S)).lower()
            for word in ("диагноз", "расстройств", "суицид", "тяжёлая депрессия", "критическое состояние", "патолог", "отклонени"):
                self.assertNotIn(word, text, f"{url}: «{word}»")


class NightFeatures(TestCase):
    """Доработки по отзывам пациентов и врачей, опросам и кастдеву."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", "--reset", stdout=open("/dev/null", "w"))

    def login(self, u):
        self.client.logout()
        self.assertTrue(self.client.login(username=u, password="Demo-2026!"))

    def test_calm_scale_same_direction(self):
        from core.models import DailyCheckin
        self.login("anna")
        self.client.post("/me/checkin/", {"mood": 8, "calm": 9})
        c = DailyCheckin.objects.filter(patient__last_name="Смирнова").latest("created_at")
        self.assertEqual((c.mood, c.anxiety, c.calm), (8, 1, 9))
        self.assertContains(self.client.get("/me/checkin/"), "10 — спокойно")

    def test_alcohol_in_ml_counts_standard_units_and_warns(self):
        from core.models import DailyCheckin
        self.login("anna")
        r = self.client.post("/me/checkin/", {"mood": 6, "calm": 6, "alcohol_drink": "wine", "alcohol_ml": 150}, follow=True)
        c = DailyCheckin.objects.filter(patient__last_name="Смирнова").latest("created_at")
        self.assertEqual((c.alcohol_abv, c.alcohol_units), (12, 1.4))  # 150 мл × 12% × 0,789 / 10 г
        self.assertContains(r, "позвоните врачу")
        self.assertTrue(Alert.objects.filter(patient_id=c.patient_id, rule_id="alcohol_on_aviandr", severity="safety").exists())
        r = self.client.post("/me/checkin/", {"mood": 6, "calm": 6, "alcohol_drink": "beer"})
        self.assertContains(r, "объём")

    def test_private_note_is_not_shown_to_doctor(self):
        self.login("anna")
        r = self.client.post("/me/checkin/", {"mood": 5, "calm": 5, "private_text": "секрет про работу"})
        self.assertRedirects(r, "/me/", fetch_redirect_response=False)
        self.login("petrova")
        p = PatientProfile.objects.get(last_name="Смирнова")
        self.assertNotContains(self.client.get(f"/doctor/patient/{p.public_id}/"), "секрет про работу")
        # тревожные слова в личной заметке: пациенту — поддержка, врачу — ничего
        self.login("anna")
        before = Alert.objects.count()
        r = self.client.post("/me/checkin/", {"mood": 2, "calm": 2, "private_text": "хочу умереть"})
        self.assertRedirects(r, "/me/help/?support=1&private=1", fetch_redirect_response=False)
        self.assertEqual(Alert.objects.count(), before)

    def test_worse_button_and_one_question(self):
        self.login("anna")
        r = self.client.post("/me/worse/")
        self.assertRedirects(r, "/me/?one=1", fetch_redirect_response=False)
        pid = pid_of("Смирнова")
        self.assertTrue(Alert.objects.filter(patient_id=pid, rule_id="worse_pressed", severity="medium").exists())
        self.client.post("/me/worse/")
        self.assertTrue(Alert.objects.filter(patient_id=pid, rule_id="worse_repeated", severity="high").exists())
        self.assertContains(self.client.get("/me/?one=1"), "Один вопрос")
        self.client.post("/me/one/", {"mood": 3})
        from core.models import DailyCheckin
        self.assertEqual(DailyCheckin.objects.filter(patient_id=pid).latest("created_at").kind, "one")

    def test_clinic_service_only_with_reason_and_can_hide(self):
        from core.models import ServiceRequest
        self.login("anna")  # ответ на терапию — повода нет
        self.assertNotContains(self.client.get("/me/"), "Оставить заявку")
        self.login("maria")  # частичный ответ, GAD-7 13 — уместна КПТ/психолог
        page = self.client.get("/me/")
        self.assertContains(page, "Оставить заявку")
        code = page.context["offer"]["code"]
        self.client.post(f"/me/service/{code}/")
        self.assertTrue(ServiceRequest.objects.filter(patient__last_name="Кузнецова", service=code).exists())
        self.login("sidorov")
        p = PatientProfile.objects.get(last_name="Кузнецова")
        self.assertContains(self.client.get(f"/doctor/patient/{p.public_id}/"), "Заявки пациента на услуги клиники")

    def test_referral_text_material_and_feedback_loop(self):
        self.login("sidorov")
        p = PatientProfile.objects.get(last_name="Морозов")
        r = self.client.get(f"/doctor/patient/{p.public_id}/")
        self.assertContains(r, "Готовый текст направления")
        self.assertIn("C-SSRS", r.context["referral_text"])
        self.client.post(f"/doctor/patient/{p.public_id}/material/")
        self.assertTrue(Message.objects.filter(care_link__patient=p, text__contains="/me/learn/specialist/").exists())
        ref = Referral.objects.get(patient=p, status="proposed")
        self.client.post(f"/doctor/patient/{p.public_id}/referral/{ref.id}/", {"decision": "confirm"})
        self.login("petrova")
        home = self.client.get("/doctor/")
        self.assertContains(home, "Входящие направления")
        self.client.post(f"/doctor/referral/{ref.id}/accept/", {"recommendations": "Госпитализация не нужна, начат сертралин"})
        self.login("sidorov")
        self.assertContains(self.client.get(f"/doctor/patient/{p.public_id}/"), "Госпитализация не нужна")

    def test_igor_feedback_and_improving_badge(self):
        self.login("sidorov")
        p = PatientProfile.objects.get(last_name="Волков")
        self.assertContains(self.client.get(f"/doctor/patient/{p.public_id}/"), "Пациент дошёл")
        self.login("petrova")
        rows = {r["p"].last_name: r for r in self.client.get("/doctor/").context["rows"]}
        self.assertTrue(rows["Смирнова"]["improving"])
        self.assertFalse(rows["Новикова"]["improving"])

    def test_summary_legal_team(self):
        self.login("petrova")
        p = PatientProfile.objects.get(last_name="Смирнова")
        r = self.client.get(f"/doctor/patient/{p.public_id}/")
        for t in ("Сводка перед приёмом", "Юридические подсказки", "323-ФЗ, ст. 36.2", "Другие врачи пациента"):
            self.assertContains(r, t)
        self.client.post(f"/doctor/patient/{p.public_id}/team/", {"name": "Иванова А. А.", "specialty": "Психолог"})
        self.assertContains(self.client.get(f"/doctor/patient/{p.public_id}/"), "Иванова А. А.")

    def test_show_password_and_new_logo(self):
        r = self.client.get("/login/")
        self.assertContains(r, "pw-toggle")
        self.assertContains(r, "Ассистент врача")
        self.assertNotContains(r, "С возвращением")

    def test_aviandr_has_no_dependence_warning(self):
        from web import services as S
        p = PatientProfile.objects.get(last_name="Смирнова")
        text = " ".join(f.text for f in S.aviandr_panel(p)["findings"] + S.aviandr_panel(p)["warnings"])
        self.assertNotIn("зависимость", text.lower().replace("алкогольная или наркотическая зависимость", ""))


class RuleAdmin(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        self.admin = User.objects.create_superuser("dev", password="x")
        self.client.login(username="dev", password="x")

    def test_bad_rules_rejected_good_activated_reset(self):
        from rules.models import RuleVersion
        from alerts import service
        good = open("alerts/data/alert_rules.yaml", encoding="utf-8").read()
        bad = good.replace("fact: phq9_item9, op: gte", "fact: phq9_itm9, op: gte")
        r = self.client.post("/admin/upload-rules/", {"file_type": "alerts", "content": bad})
        self.assertContains(r, "неизвестный факт")
        self.assertFalse(RuleVersion.objects.exists())
        no_suicide = "rules:\n  - {id: x, events: [scale], severity: medium, title: t, when: {fact: phq9_total, op: gte, value: 5}}\n"
        r = self.client.post("/admin/upload-rules/", {"file_type": "alerts", "content": no_suicide})
        self.assertContains(r, "обязательные правила")
        self.client.post("/admin/upload-rules/", {"file_type": "alerts", "content": good.replace("Пациенту открыта Колумбийская шкала\"", "Открыта C-SSRS\"", 1)})
        self.assertTrue(RuleVersion.objects.filter(is_active=True).exists())
        self.assertTrue(any("Открыта C-SSRS" in r["title"] for r in service.load_rules()))
        self.client.post("/admin/upload-rules/", {"file_type": "alerts", "reset": "1", "content": ""})
        self.assertFalse(RuleVersion.objects.filter(is_active=True).exists())
        self.assertFalse(any("Открыта C-SSRS" in r["title"] for r in service.load_rules()))
