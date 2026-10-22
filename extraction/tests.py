from django.contrib.auth.models import Group, User
from django.test import TestCase

from extraction.engine.rules_extractor import extract


class ExtractionApi(TestCase):
    def test_requires_doctor_and_validates(self):
        self.assertEqual(self.client.post("/api/extract", "{}", content_type="application/json").status_code, 401)
        u = User.objects.create_user("doc", password="x")
        u.groups.add(Group.objects.create(name="doctor"))
        self.client.login(username="doc", password="x")
        for body in ["not json", "[1]", '{"text": 5}']:
            self.assertEqual(self.client.post("/api/extract", body, content_type="application/json").status_code, 400, body)
        text = "Ds: F41.1 ГТР. Сопутствующий: эпилепсия (G40). Принимает вальпроевую кислоту 500 мг 2 р/д. GAD-7 16."
        r = self.client.post("/api/extract", {"text": text}, content_type="application/json").json()
        self.assertIn("G40", r["extracted"]["diagnoses"])
        self.assertEqual(r["aviandr"]["verdict"], "red")  # эпилепсия — противопоказание

    def test_cirrhosis_without_class_is_red(self):
        u = User.objects.create_user("doc2", password="x")
        u.groups.add(Group.objects.get_or_create(name="doctor")[0])
        self.client.login(username="doc2", password="x")
        r = self.client.post("/api/extract", {"text": "Пациент 40 лет. Ds F41.1. Цирроз печени, печеночная недостаточность. СКФ 92"},
                             content_type="application/json").json()
        self.assertEqual(r["aviandr"]["verdict"], "red")

    def test_extractor_basics(self):
        d = extract("Пациентка 34 лет. Ds: F41.1. Принимает Золофт 50 мг 1 р/д. Ранее принимала пароксетин, отменён. PHQ-9 — 14 б.")
        self.assertEqual(d["medications"][0]["inn"], "сертралин")
        self.assertEqual(d["stopped_medications"], ["пароксетин"])
        self.assertEqual(d["scales"]["phq9"], 14)
