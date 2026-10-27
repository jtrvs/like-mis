from datetime import timedelta

from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings
from django.utils import timezone
from django.core.cache import cache

from alerts import facts, service
from alerts.keywords import scan
from alerts.models import Alert


@override_settings(ALERTS={"ACK_TIMEOUT_MIN": 15, "MAX_RETRIES": 2, "CHANNELS": ["inapp", "log"]})
class AlertFlow(TestCase):
    def setUp(self):
        g = Group.objects.create(name="doctor")
        self.doc = User.objects.create_user("doc", password="x")
        self.backup = User.objects.create_user("backup", password="x")
        self.other = User.objects.create_user("other", password="x")
        for u in (self.doc, self.backup, self.other):
            u.groups.add(g)
        cache.delete('alert_rules_cache')

    def ev(self, trigger, f=None, text="", pid=1):
        return service.report_event(patient_id=pid, patient_label=f"П-{pid}", doctor=self.doc, backup_doctor=self.backup,
                                    trigger=trigger, facts=f, text=text, card_url=f"/p/{pid}/")

    def phq(self, answers, prev=None):
        return self.ev("scale", facts.from_scale("phq9", answers, prev))

    def test_suicide_item_critical_and_crisis_screen(self):
        r = self.phq([1, 1, 1, 1, 0, 1, 1, 0, 1])
        a = Alert.objects.get(rule_id="suicide_risk_scale")
        self.assertEqual((a.severity, a.status), ("critical", "delivered"))
        self.assertLess(a.delivery_latency_ms, 1000)
        self.assertTrue(r["show_crisis_screen"])
        self.assertIn("propose_mandatory_referral", r["doctor_actions"])

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

    def test_alcohol_on_aviandr(self):
        f = facts.from_checkins([{"mood": 5, "anxiety": 5, "alcohol_units": 2}], on_aviandr=True)
        self.ev("checkin", f)
        self.assertTrue(Alert.objects.filter(rule_id="alcohol_on_aviandr", severity="high").exists())

    def test_self_neglect_streak(self):
        day = {"mood": 2, "anxiety": 8, "left_home": False, "ate_normally": False}
        self.ev("checkin", facts.from_checkins([day, day, day]))
        self.assertTrue(Alert.objects.filter(rule_id="self_neglect").exists())

    def test_unmarked_doses(self):
        now = timezone.now()
        intakes = [{"scheduled_at": now - timedelta(hours=13 + 12 * i), "taken": None} for i in range(6)]
        self.ev("schedule", facts.from_intakes(intakes, now))
        self.assertTrue(Alert.objects.filter(rule_id="diary_abandoned").exists())

    def tick(self, a, n=1):
        for _ in range(n):
            a.deliveries.update(sent_at=timezone.now() - timedelta(minutes=30))
            service.process_pending()
        a.refresh_from_db()

    def test_retry_then_escalate_then_backup_sees_it(self):
        self.phq([0, 0, 0, 0, 0, 0, 0, 0, 2])
        a = Alert.objects.get()
        self.tick(a, 3)
        self.assertEqual((a.status, a.retries, a.escalated_to), ("escalated", 0, self.backup))
        self.assertIn(a, service.visible_to(self.backup))
        self.assertNotIn(a, service.visible_to(self.other))

    def test_backup_gets_retries_then_unanswered_logged(self):
        self.phq([0, 0, 0, 0, 0, 0, 0, 0, 2])
        a = Alert.objects.get()
        self.tick(a, 5)  # 2 повтора ведущему, эскалация, 2 повтора резервному
        self.assertEqual((a.escalated_to, a.retries), (self.backup, 2))
        self.assertEqual(a.deliveries.filter(channel="inapp").count(), 6)
        with self.assertLogs("alerts", level="CRITICAL"):
            self.tick(a)
        self.assertIsNotNone(a.exhausted_at)

    def test_no_backup_unanswered_logged(self):
        service.report_event(patient_id=7, patient_label="П-7", doctor=self.doc, trigger="scale",
                             facts=facts.from_scale("phq9", [0] * 8 + [1]))
        a = Alert.objects.get()
        self.tick(a, 2)
        with self.assertLogs("alerts", level="CRITICAL"):
            self.tick(a)

    def test_worker_does_not_overwrite_ack(self):
        self.phq([0, 0, 0, 0, 0, 0, 0, 0, 2])
        a = Alert.objects.get()
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
        with mock.patch("alerts.service.process_pending", side_effect=RuntimeError("db down")):
            call_command("process_alerts", stdout=open("/dev/null", "w"))  # не падает

    def test_bad_patient_id_param(self):
        self.client.login(username="doc", password="x")
        self.assertEqual(self.client.get("/api/alerts/?patient_id=abc").status_code, 400)

    def test_ack_stops_retries(self):
        self.phq([0, 0, 0, 0, 0, 0, 0, 0, 2])
        a = Alert.objects.get()
        service.acknowledge(a, self.doc, "позвонил")
        a.deliveries.update(sent_at=timezone.now() - timedelta(minutes=30))
        service.process_pending()
        a.refresh_from_db()
        self.assertEqual((a.status, a.retries), ("acknowledged", 0))

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
