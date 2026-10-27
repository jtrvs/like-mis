from django.test import SimpleTestCase

from clinical.engine import routing, safety, scales


class ScalesTest(SimpleTestCase):
    def test_phq9_bands_and_suicide_flag(self):
        r = scales.score("phq9", [2, 2, 2, 2, 1, 2, 2, 1, 1])
        self.assertEqual((r.total, r.severity), (15, "moderately_severe"))
        self.assertIn("suicidal_ideation", r.flags)

    def test_gad7(self):
        self.assertEqual(scales.score("gad7", [3] * 7).severity, "severe")

    def test_bad_input(self):
        with self.assertRaises(ValueError):
            scales.score("gad7", [1] * 6)


class AviandrScenarios(SimpleTestCase):
    """Три демонстрационных сценария кейса."""

    base = dict(age=34, pregnant=False, breastfeeding=False, egfr=95, child_pugh="", conditions=[], current_drugs=[])

    def test_s1_gad_green(self):
        f = safety.check("aviandr", {**self.base, "icd10": ["F41.1"]})
        self.assertEqual(safety.verdict(f), "green")

    def test_s2_ssri_caution_and_offlabel_for_depression(self):
        f = safety.check("aviandr", {**self.base, "icd10": ["F32.1"], "current_drugs": ["Сертралин"]})
        codes = {x.code for x in f}
        self.assertEqual(safety.verdict(f), "yellow")
        self.assertTrue(any(c.startswith("interaction_ssri") for c in codes))
        self.assertEqual(sum(1 for x in f if "ертралин" in x.text), 1)  # одна строка на препарат
        self.assertIn("no_indication", codes)

    def test_s3_contraindications(self):
        ctx = {**self.base, "icd10": ["F41.1"], "child_pugh": "A", "egfr": 45,
               "stopped_drugs": [{"name": "моклобемид", "days_ago": 5}]}
        f = safety.check("aviandr", ctx)
        codes = {x.code for x in f if x.level == "contraindication"}
        self.assertEqual(codes, {"hepatic", "renal", "interaction_maoi"})
        self.assertEqual(safety.verdict(f), "red")

    def test_maoi_after_washout_ok(self):
        f = safety.check("aviandr", {**self.base, "icd10": ["F41.1"], "stopped_drugs": [{"name": "моклобемид", "days_ago": 20}]})
        self.assertEqual(safety.verdict(f), "green")


class RoutingTest(SimpleTestCase):
    def test_suicide_mandatory_until_cssrs(self):
        # мысли о смерти без оценки по Колумбийской шкале — «обязательно»; дальше решает C-SSRS
        self.assertEqual(routing.summary({"suicide_flag_unassessed": True, "phq9_total": 8})["urgency"], "mandatory")
        self.assertEqual(routing.summary({"cssrs_risk": "high"})["urgency"], "emergency")
        self.assertEqual(routing.summary({"cssrs_risk": "moderate"})["urgency"], "mandatory")
        self.assertIsNone(routing.summary({"cssrs_risk": "low", "phq9_total": 8})["urgency"])

    def test_alina_routing_table(self):
        f = lambda **kw: [r["id"] for r in routing.route(kw)]  # noqa: E731
        self.assertIn("severe_depression", f(phq9_total=20))
        self.assertIn("substance_use", f(alcohol_days_7=3))
        self.assertIn("no_response_8w", f(weeks_on_therapy=8, score_improvement_pct=10))
        self.assertIn("partial_response_8w", f(weeks_on_therapy=9, score_improvement_pct=30))
        self.assertIn("no_improvement_4w", f(weeks_on_therapy=4, score_improvement_pct=20))
        self.assertEqual(f(weeks_on_therapy=8, score_improvement_pct=55), [])
        # терапевту нужен диагноз психиатра; психиатру — нет
        self.assertIn("diagnosis_needed", f(doctor_is_psychiatrist=False, has_f_diagnosis=False, gad7_total=12))
        self.assertNotIn("diagnosis_needed", f(doctor_is_psychiatrist=True, has_f_diagnosis=False, gad7_total=12))
        # умеренные симптомы — «желательно» только при сопутствующих проблемах
        self.assertIn("moderate_with_problems", f(gad7_total=12, has_concomitant_problems=True))
        self.assertEqual(f(gad7_total=12, has_concomitant_problems=False), [])

    def test_desirable(self):
        s = routing.summary({"phq9_total": 9, "gad7_total": 16})
        self.assertEqual(s["urgency"], "desirable")

    def test_none(self):
        self.assertIsNone(routing.summary({"phq9_total": 4, "gad7_total": 3})["urgency"])


class SafetyIncompleteData(SimpleTestCase):
    base = dict(age=34, pregnant=False, breastfeeding=False, egfr=95, child_pugh="", conditions=[], current_drugs=[], icd10=["F41.1"])

    def codes(self, **kw):
        return {f.code for f in safety.check("aviandr", {**self.base, **kw})}

    def test_unknown_egfr_not_green(self):
        self.assertIn("egfr_unknown", self.codes(egfr=None))

    def test_no_diagnosis_not_green(self):
        self.assertIn("no_diagnosis", self.codes(icd10=[]))

    def test_trade_name_and_maob(self):
        self.assertTrue(any(c.startswith("interaction_ssri") for c in self.codes(current_drugs=["Золофт"])))
        self.assertIn("interaction_maoi", self.codes(current_drugs=["селегилин"]))
        self.assertIn("unknown_drug", self.codes(current_drugs=["неведомин"]))

    def test_post_covid_indication(self):
        self.assertIn("no_indication", self.codes(icd10=["F43.9"]))
        self.assertNotIn("no_indication", self.codes(icd10=["F43.9"], post_covid=True))
        self.assertIn("course_exceeded", self.codes(icd10=["F43.9"], post_covid=True, weeks_on_drug=7))


class ReviewRegressions(SimpleTestCase):
    full = dict(age=34, pregnant=False, breastfeeding=False, egfr=95, child_pugh="", conditions=[], current_drugs=[], icd10=["F41.1"])

    def v(self, **kw):
        return safety.verdict(safety.check("aviandr", {**self.full, **kw}))

    def test_liver_and_kidney_failure_without_class_are_red(self):
        self.assertEqual(self.v(conditions=["hepatic_failure"]), "red")
        self.assertEqual(self.v(conditions=["renal_failure"], egfr=None), "red")

    def test_lowercase_child_pugh(self):
        self.assertEqual(self.v(child_pugh="b"), "red")

    def test_drug_names_with_dose(self):
        self.assertEqual(self.v(stopped_drugs=[{"name": "Аурорикс 300 мг", "days_ago": 3}]), "red")
        self.assertEqual(self.v(current_drugs=["Аурорикс 300 мг"]), "red")
        self.assertEqual(self.v(current_drugs=["Золофт (сертралин) 50мг"]), "yellow")

    def test_unknown_pregnancy_not_green(self):
        self.assertEqual(self.v(pregnant=None), "yellow")
        self.assertEqual(self.v(), "green")

    def test_unknown_stopped_drug_flagged(self):
        codes = {f.code for f in safety.check("aviandr", {**self.full, "stopped_drugs": [{"name": "неведомин", "days_ago": 1}]})}
        self.assertIn("unknown_drug", codes)

    def test_post_covid_course(self):
        base = dict(icd10=["F43.9"], post_covid=True)
        self.assertEqual(self.v(**base, weeks_on_drug=3), "green")
        self.assertEqual(self.v(**base, weeks_on_drug=5), "yellow")
        codes = {f.code for f in safety.check("aviandr", {**self.full, **base, "weeks_on_drug": 7})}
        self.assertIn("course_exceeded", codes)


class AlinaScalesTest(SimpleTestCase):
    def test_auditc_threshold_by_sex(self):
        from clinical.engine import scales
        self.assertTrue(scales.auditc_risk(4, "M"))
        self.assertFalse(scales.auditc_risk(3, "M"))
        self.assertTrue(scales.auditc_risk(3, "F"))

    def test_response_bands(self):
        from clinical.engine import scales
        self.assertEqual(scales.response(15, 6)["code"], "response")   # −60%
        self.assertEqual(scales.response(15, 9)["code"], "partial")    # −40%
        self.assertEqual(scales.response(15, 13)["code"], "none")      # −13%

    def test_scenario_b_four_warnings(self):
        # сценарий Б: СКФ 52, сертралин, трамадол, 68 лет — Авиандр противопоказан, 4 предупреждения
        f = safety.check("aviandr", {"age": 68, "pregnant": False, "breastfeeding": False, "egfr": 52,
                                     "icd10": ["F41.1"], "current_drugs": ["сертралин", "трамадол", "амлодипин"]})
        self.assertEqual(safety.verdict(f), "red")
        text = " ".join(x.text for x in f)
        for part in ("СКФ < 60", "сертралин", "трамадол", "повышает концентрацию", "Пожилой возраст"):
            self.assertIn(part, text)

    def test_green_no_interaction_note(self):
        f = safety.check("aviandr", {"age": 40, "pregnant": False, "breastfeeding": False, "egfr": 90,
                                     "icd10": ["F41.1"], "current_drugs": ["метопролол"]})
        self.assertEqual(safety.verdict(f), "green")
        self.assertTrue(any(x.level == "ok" and "метопролол" in x.text for x in f))

    def test_panic_offlabel_points_to_guideline(self):
        f = safety.check("aviandr", {"age": 28, "pregnant": False, "breastfeeding": False, "egfr": 100, "icd10": ["F41.0"]})
        self.assertTrue(any(x.level == "off_label" and "ID 456" in x.text for x in f))
