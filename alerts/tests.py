from datetime import timedelta

from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings
from django.utils import timezone

from alerts import facts, service
from alerts.keywords import scan
from alerts.models import Alert


@override_settings(ALERTS={"ACK_TIMEOUT_MIN": 15, "REPEAT_EVERY_MIN": 5, "ESCALATE_AFTER_MIN": 30, "CHANNELS": ["inapp", "log"]})
class AlertFlow(TestCase):
    def setUp(self):
        g = Group.objects.create(name="doctor")
        self.doc = User.objects.create_user("doc", password="x")
        self.backup = User.objects.create_user("backup", password="x")
        self.other = User.objects.create_user("other", password="x")
        for u in (self.doc, self.backup, self.other):
            u.groups.add(g)
        service.load_rules.cache_clear()

    def ev(self, trigger, f=None, text="", pid=1):
        return service.report_event(patient_id=pid, patient_label=f"П-{pid}", doctor=self.doc, backup_doctor=self.backup,
                                    trigger=trigger, facts=f, text=text, card_url=f"/p/{pid}/")

    def phq(self, answers, prev=None):
        return self.ev("scale", facts.from_scale("phq9", answers, prev))

    def cssrs(self, answers, pid=1):
        from clinical.engine import scales
        res = scales.score("cssrs", answers)
        return self.ev("cssrs", {"cssrs_risk": res.severity, "cssrs_label": res.severity_label}, pid=pid)

    def test_suicide_item_orange_opens_cssrs(self):
        # таблица правил 1.2: PHQ-9 вопрос 9 ≥ 1 — оранжевый, пациенту открыть C-SSRS и показать контакты поддержки
        r = self.phq([1, 1, 1, 1, 0, 1, 1, 0, 1])
        a = Alert.objects.get(rule_id="suicide_risk_scale")
        self.assertEqual((a.severity, a.status), ("high", "delivered"))
        self.assertEqual(a.title, "PHQ-9, вопрос 9 = 1. Пациенту открыта Колумбийская шкала")
        self.assertLess(a.delivery_latency_ms, 1000)
        self.assertTrue(r["show_crisis_screen"])
        self.assertTrue(r["open_cssrs"])
        self.assertIn("propose_mandatory_referral", r["doctor_actions"])

    def test_cssrs_levels(self):
        # высокий (намерение) — красный; умеренный (способ) — оранжевый; низкий (желание умереть) — жёлтый
        self.cssrs([1, 1, 1, 1, 0, 0, 0], pid=1)
        self.cssrs([1, 1, 1, 0, 0, 0, 0], pid=2)
        self.cssrs([1, 0, 0, 0, 0, 0, 0], pid=3)
        self.cssrs([0, 0, 0, 0, 0, 0, 0], pid=4)
        got = {a.patient_id: (a.rule_id, a.severity) for a in Alert.objects.all()}
        self.assertEqual(got, {1: ("cssrs_high", "critical"), 2: ("cssrs_moderate", "high"), 3: ("cssrs_low", "medium")})
        self.assertIn("propose_emergency_referral", Alert.objects.get(patient_id=1).doctor_actions)

    def test_cssrs_recent_attempt_is_high_old_is_moderate(self):
        from clinical.engine import scales
        self.assertEqual(scales.score("cssrs", [0, 0, 0, 0, 0, 1, 1]).severity, "high")
        self.assertEqual(scales.score("cssrs", [0, 0, 0, 0, 0, 1, 0]).severity, "moderate")
        with self.assertRaises(ValueError):  # «нет» на вопрос 2 — на 3–5 ответов быть не должно
            scales.score("cssrs", [1, 0, 1, 0, 0, 0, 0])

    def test_normal_no_alert(self):
        r = self.phq([0] * 9)
        self.assertEqual(r["alerts"], [])
        self.assertFalse(r["show_crisis_screen"])

    def test_dedup_until_acknowledged_then_new_event_raises_again(self):
        self.ev("message", text="хочу умереть")
        self.ev("message", text="всё равно хочу умереть")
        self.assertEqual(Alert.objects.filter(rule_id="suicide_risk_text").count(), 1)
        service.acknowledge(Alert.objects.get(), self.doc, "позвонил")
        self.ev("message", text="наложу на себя руки")
        self.assertEqual(Alert.objects.filter(rule_id="suicide_risk_text").count(), 2)

    def test_rule_fires_only_on_its_trigger(self):
        # «старый» п.9 не поднимает сигнал, если пришла обычная отметка дневника
        r = self.ev("checkin", {"phq9_item9": 2, "mood_last": 5})
        self.assertEqual(r["alerts"], [])

    def test_alcohol_on_aviandr_is_safety(self):
        f = facts.from_checkins([{"mood": 5, "anxiety": 5, "alcohol_units": 2}], on_aviandr=True)
        r = self.ev("checkin", f)
        self.assertTrue(Alert.objects.filter(rule_id="alcohol_on_aviandr", severity="safety").exists())
        self.assertIn("remind_no_alcohol", r["patient_actions"])

    def days(self, n, **day):
        now = timezone.now()
        return [{"mood": 4, "anxiety": 6, **day, "created_at": now - timedelta(days=i)} for i in range(n)]

    def test_not_eating_two_days_by_calendar(self):
        self.ev("checkin", facts.from_checkins(self.days(1, ate_normally=False)))
        self.assertFalse(Alert.objects.exists())
        two_same_day = [{"mood": 4, "anxiety": 6, "ate_normally": False, "created_at": timezone.now() - timedelta(hours=h)} for h in (0, 1)]
        self.ev("checkin", facts.from_checkins(two_same_day))
        self.assertFalse(Alert.objects.exists())  # две записи за один день — это один день
        self.ev("checkin", facts.from_checkins(self.days(2, ate_normally=False)))
        self.assertEqual(Alert.objects.get().rule_id, "not_eating")

    def test_home_bound_yellow_or_orange(self):
        self.ev("checkin", {**facts.from_checkins(self.days(5, left_home=False)), "scales_worse": False}, pid=1)
        self.ev("checkin", {**facts.from_checkins(self.days(5, left_home=False)), "scales_worse": True}, pid=2)
        self.ev("checkin", {**facts.from_checkins(self.days(4, left_home=False)), "scales_worse": False}, pid=3)
        got = {a.patient_id: (a.rule_id, a.severity) for a in Alert.objects.all()}
        self.assertEqual(got, {1: ("home_bound", "medium"), 2: ("home_bound_worse", "high")})

    def test_serotonin_syndrome_red(self):
        f = facts.from_checkins([{"mood": 5, "anxiety": 7, "serotonin_signs": ["жар", "дрожь"]}], on_aviandr=True, on_serotonergic=True)
        self.ev("checkin", f)
        self.assertEqual(Alert.objects.get().severity, "critical")
        f = facts.from_checkins([{"mood": 5, "anxiety": 7, "serotonin_signs": ["жар", "дрожь"]}], on_aviandr=True, on_serotonergic=False)
        self.ev("checkin", f, pid=2)
        self.assertFalse(Alert.objects.filter(patient_id=2).exists())

    def test_reports_overdose_pregnancy_stop(self):
        base = {"overdose_now": False, "reported_pregnancy": False}
        self.ev("report", {**base, "reported_kind": "extra_dose", "extra_tablets": 3, "overdose_now": True}, pid=1)
        self.ev("report", {**base, "reported_kind": "extra_dose", "extra_tablets": 1}, pid=2)
        self.ev("report", {**base, "reported_kind": "pregnancy", "reported_pregnancy": True, "on_aviandr": True}, pid=3)
        self.ev("report", {**base, "reported_kind": "stopped", "stop_reason": "Дорого"}, pid=4)
        got = {a.patient_id: (a.rule_id, a.severity) for a in Alert.objects.all()}
        self.assertEqual(got, {1: ("overdose", "critical"), 2: ("took_more", "high"),
                               3: ("pregnancy_on_aviandr", "critical"), 4: ("stopped_taking", "medium")})
        self.assertEqual(Alert.objects.get(patient_id=4).title, "Пациент прекратил приём. Причина: Дорого")

    def test_unmarked_doses_five_days(self):
        now = timezone.now()
        intakes = [{"scheduled_at": now - timedelta(hours=13 + 12 * i), "taken": None} for i in range(12)]
        f = facts.from_intakes(intakes, now)
        self.assertGreaterEqual(f["unmarked_days"], 5)
        self.ev("schedule", f)
        self.assertTrue(Alert.objects.filter(rule_id="diary_abandoned").exists())

    def test_three_missed_doses_in_week(self):
        now = timezone.now()
        intakes = [{"scheduled_at": now - timedelta(hours=2 + 24 * i), "taken": i not in (1, 3, 5)} for i in range(7)]
        self.ev("intake", facts.from_intakes(intakes, now))
        self.assertTrue(Alert.objects.filter(rule_id="nonadherence").exists())

    def test_yellow_suppressed_72h_unless_worse_red_never(self):
        # антиспам: то же правило по тому же пациенту не повторяется 72 ч, если не стало хуже
        self.ev("scale", {"gad7_total": 16, "phq9_total": 8})
        a = Alert.objects.get(rule_id="severe_worsening")
        service.acknowledge(a, self.doc, "позвонил")
        self.ev("scale", {"gad7_total": 16, "phq9_total": 8})
        self.assertEqual(Alert.objects.filter(rule_id="severe_worsening").count(), 1)
        self.ev("scale", {"gad7_total": 18, "phq9_total": 8})  # стало хуже — сигнал снова
        self.assertEqual(Alert.objects.filter(rule_id="severe_worsening").count(), 2)
        Alert.objects.filter(rule_id="severe_worsening").update(acknowledged_at=timezone.now() - timedelta(hours=73), status="acknowledged")
        self.ev("scale", {"gad7_total": 16, "phq9_total": 8})  # прошло 72 ч
        self.assertEqual(Alert.objects.filter(rule_id="severe_worsening").count(), 3)
        # красный не подавляется никогда
        self.cssrs([1, 1, 1, 1, 0, 0, 0], pid=9)
        service.acknowledge(Alert.objects.get(patient_id=9), self.doc, "звонок")
        self.cssrs([1, 1, 1, 1, 0, 0, 0], pid=9)
        self.assertEqual(Alert.objects.filter(patient_id=9, rule_id="cssrs_high").count(), 2)

    def test_false_positive_mark(self):
        self.ev("message", text="хочу умереть")
        a = Alert.objects.get()
        service.acknowledge(a, self.doc, "цитата из фильма", false_positive=True)
        a.refresh_from_db()
        self.assertTrue(a.false_positive)

    def red(self, pid=1, **kw):
        self.cssrs([1, 1, 1, 1, 0, 0, 0], pid=pid) if not kw else service.report_event(
            patient_id=pid, patient_label=f"П-{pid}", trigger="cssrs", facts={"cssrs_risk": "high"}, **kw)
        return Alert.objects.get(patient_id=pid)

    def at(self, a, minutes):
        """Запуск фонового процесса через `minutes` минут после доставки сигнала."""
        service.process_pending(now=a.delivered_at + timedelta(minutes=minutes))
        a.refresh_from_db()

    def test_red_repeat_15_then_every_5_then_backup_at_30(self):
        a = self.red()
        self.at(a, 10)
        self.assertEqual(a.retries, 0)  # 15 минут ещё не прошло
        self.at(a, 15)
        self.assertEqual(a.retries, 1)
        self.at(a, 18)
        self.assertEqual(a.retries, 1)  # следующий повтор — через 5 минут
        self.at(a, 21)
        self.at(a, 26)
        self.assertEqual((a.retries, a.status), (3, "delivered"))
        self.at(a, 30)
        self.assertEqual((a.status, a.retries, a.escalated_to), ("escalated", 0, self.backup))
        self.assertIn(a, service.visible_to(self.backup))
        self.assertNotIn(a, service.visible_to(self.other))

    def test_backup_gets_same_cycle_then_unanswered_logged(self):
        a = self.red()
        self.at(a, 30)
        self.assertEqual(a.escalated_to, self.backup)
        t0 = (a.escalated_at - a.delivered_at).total_seconds() / 60
        self.at(a, t0 + 15)
        self.assertEqual(a.retries, 1)
        with self.assertLogs("alerts", level="CRITICAL"):
            self.at(a, t0 + 30)
        self.assertIsNotNone(a.exhausted_at)

    def test_no_backup_unanswered_logged(self):
        a = self.red(pid=7, doctor=self.doc)
        with self.assertLogs("alerts", level="CRITICAL"):
            self.at(a, 30)

    def test_orange_not_repeated(self):
        self.phq([0, 0, 0, 0, 0, 0, 0, 0, 2])
        a = Alert.objects.get()
        self.at(a, 60)
        self.assertEqual((a.retries, a.status, a.escalated_to), (0, "delivered", None))

    def test_channels_by_category(self):
        from alerts.channels import BY_SEVERITY
        self.assertIsNone(BY_SEVERITY["critical"])  # красный — во все каналы (второй канал: бот, почта)
        self.assertNotIn("telegram", BY_SEVERITY["high"])
        self.assertEqual(BY_SEVERITY["safety"], {"inapp"})

    def test_worker_does_not_overwrite_ack(self):
        a = self.red()
        stale = Alert.objects.get(id=a.id)  # «фоновый процесс» прочитал строку
        service.acknowledge(a, self.doc, "позвонил")
        stale.retries = 99
        # условное обновление: закрытый сигнал не трогается
        self.assertEqual(Alert.objects.filter(id=a.id, status__in=["delivered", "escalated"]).update(status="escalated"), 0)
        a.refresh_from_db()
        self.assertEqual(a.status, "acknowledged")

    def test_second_ack_keeps_first(self):
        self.phq([0, 0, 0, 0, 0, 0, 0, 0, 2])
        a = Alert.objects.get()
        self.assertTrue(service.acknowledge(a, self.doc, "первый"))
        self.assertFalse(service.acknowledge(a, self.backup, "второй"))
        a.refresh_from_db()
        self.assertEqual((a.resolution, a.acknowledged_by), ("первый", self.doc))

    def test_db_blocks_duplicate_open_alert(self):
        from django.db import IntegrityError, transaction
        self.phq([0, 0, 0, 0, 0, 0, 0, 0, 2])
        a = Alert.objects.get()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Alert.objects.create(patient_id=a.patient_id, patient_label="x", doctor=self.doc, rule_id=a.rule_id,
                                 severity="critical", title="t", source_received_at=timezone.now())

    def test_bad_trigger_and_fio_label_rejected(self):
        with self.assertRaises(ValueError):
            self.ev("messages", text="хочу умереть")
        with self.assertRaises(ValueError):
            service.report_event(patient_id=1, patient_label="Иванова Анна", doctor=self.doc, trigger="message")

    def test_messenger_text_has_no_health_data(self):
        self.ev("message", text="хочу умереть")
        from alerts.channels import Channel
        t = Channel.text(Alert.objects.get())
        self.assertNotIn("суицид", t.lower())
        self.assertIn("П-1", t)

    def test_naive_datetime_in_facts(self):
        from datetime import datetime
        f = facts.from_intakes([{"scheduled_at": datetime(2026, 10, 1, 9), "taken": None}] * 6)
        self.ev("schedule", {**f, "when": datetime(2026, 10, 1)})  # datetime в фактах не ломает сохранение
        self.assertTrue(Alert.objects.filter(rule_id="diary_abandoned").exists())

    def test_worker_survives_missing_tables(self):
        from unittest import mock
        from django.core.management import call_command
        # connection.close() внутри команды закрыл бы соединение тестовой транзакции — подменяем
        with mock.patch("alerts.service.process_pending", side_effect=RuntimeError("db down")), \
                mock.patch("alerts.management.commands.process_alerts.connection.close") as close:
            call_command("process_alerts", stdout=open("/dev/null", "w"))  # не падает
        close.assert_called_once()

    def test_bad_patient_id_param(self):
        self.client.login(username="doc", password="x")
        self.assertEqual(self.client.get("/api/alerts/?patient_id=abc").status_code, 400)

    def test_ack_stops_retries(self):
        a = self.red()
        service.acknowledge(a, self.doc, "позвонил")
        self.at(a, 40)
        self.assertEqual((a.status, a.retries, a.escalated_to), ("acknowledged", 0, None))

    def test_api_permissions(self):
        self.phq([0, 0, 0, 0, 0, 0, 0, 0, 2])
        a = Alert.objects.get()
        self.assertEqual(self.client.get("/api/alerts/").status_code, 302)  # не вошёл
        self.client.login(username="other", password="x")
        self.assertEqual(self.client.get("/api/alerts/").json()["alerts"], [])
        self.assertEqual(self.client.post(f"/api/alerts/{a.id}/ack/").status_code, 404)  # чужой сигнал
        self.client.login(username="doc", password="x")
        self.assertEqual(len(self.client.get("/api/alerts/?open=1").json()["alerts"]), 1)
        self.assertEqual(self.client.get("/alerts/").status_code, 200)
        User.objects.create_user("admin", password="x", is_staff=True)  # админ без роли врача — не врач
        self.client.login(username="admin", password="x")
        self.assertEqual(self.client.get("/api/alerts/").status_code, 403)
        self.client.login(username="doc", password="x")
        r = self.client.post(f"/api/alerts/{a.id}/ack/", '{"resolution": "звонок"}', content_type="application/json")
        self.assertEqual(r.json()["alert"]["status"], "acknowledged")


class Keywords(TestCase):
    def test_phrases(self):
        for t in ["хочу умереть", "не хочу больше жить", "свести счёты с жизнью", "наложу на себя руки", "выпить все таблетки"]:
            self.assertTrue(scan(t)["hit"], t)
        for t in ["устал на работе", "хочу жить нормально", "боюсь умереть от сердца"]:
            self.assertFalse(scan(t)["hit"], t)

    def test_crisis_screen_helper(self):
        self.assertTrue(facts.needs_crisis_screen("не хочу жить"))
        self.assertTrue(facts.needs_crisis_screen(phq9_answers=[0] * 8 + [1]))
        self.assertFalse(facts.needs_crisis_screen("всё нормально", [0] * 9))
