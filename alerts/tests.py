from datetime import timedelta

from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings
from django.utils import timezone

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
        service.load_rules.cache_clear()

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

    def test_retry_then_escalate_then_backup_sees_it(self):
        self.phq([0, 0, 0, 0, 0, 0, 0, 0, 2])
        a = Alert.objects.get()
        for _ in range(3):
            a.deliveries.update(sent_at=timezone.now() - timedelta(minutes=30))
            service.process_pending()
        a.refresh_from_db()
        self.assertEqual((a.status, a.retries, a.escalated_to), ("escalated", 2, self.backup))
        self.assertIn(a, service.visible_to(self.backup))
        self.assertNotIn(a, service.visible_to(self.other))

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
