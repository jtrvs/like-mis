"""
Проверка правил перед активацией в админке («Версии правил»).
Опечатка в имени факта или лишний отступ могут молча отключить сигнал о суицидальном риске,
поэтому версия с ошибками не сохраняется, а врачу-администратору показывается понятный список ошибок.
"""
import yaml

from . import rules as R

# Факты, которые вычисляет система (alerts/facts.py, web/alerts_bridge.py, web/services.py)
KNOWN_FACTS = {
    "phq9_total", "phq9_item9", "phq9_delta", "gad7_total", "gad7_delta", "scales_worse", "auditc_total", "auditc_risk",
    "mood_last", "calm_last", "anxiety_last", "alcohol_units_last", "alcohol_days_7", "short_sleep_nights",
    "days_not_left_home", "days_not_eating", "monitored_side_effects", "side_effects_last", "side_effect_strong_streak",
    "side_effect_disrupts", "serotonin_signs", "on_aviandr", "on_serotonergic", "serotonergic_drugs",
    "missed_doses_streak", "unmarked_doses_streak", "missed_doses_7d", "unmarked_days", "adherence_30d_pct",
    "therapy_days", "cssrs_risk", "cssrs_label", "suicide_flag_unassessed", "suicide_keywords",
    "overdose_now", "overdose_recent", "reported_pregnancy", "reported_kind", "extra_tablets", "stop_reason",
    "pregnant", "sos", "last_kind", "worse_marks_3d", "other_substances_last", "other_substances_days_7", "psychotic_symptoms", "manic_symptoms", "substance_dependence", "weeks_on_therapy",
    "score_improvement_pct", "doctor_is_psychiatrist", "has_f_diagnosis", "has_concomitant_problems",
}
SEVERITIES = {"critical", "high", "medium", "safety"}
URGENCIES = {"emergency", "mandatory", "desirable"}
TRIGGERS = {"scale", "cssrs", "checkin", "message", "intake", "schedule", "report", "sos"}
# Без этих правил система теряет обязательный механизм алертинга по суицидальному риску
REQUIRED_ALERTS = {"cssrs_high", "suicide_risk_scale", "suicide_risk_text"}


def validate_text(file_type: str, text: str) -> list[str]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return [f"Ошибка разметки YAML: {e}"]
    if not isinstance(data, dict):
        return ["Ожидался словарь верхнего уровня (например, «rules: [...]»)."]
    if file_type == "aviandr":
        return _validate_drug(data)
    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        return ["Нет списка правил «rules»."]
    errs, ids = [], set()
    for i, r in enumerate(rules, 1):
        rid = r.get("id") if isinstance(r, dict) else None
        where = f"правило {rid or '№' + str(i)}"
        if not isinstance(r, dict) or not rid:
            errs.append(f"{where}: нет поля id")
            continue
        if rid in ids:
            errs.append(f"{where}: id повторяется")
        ids.add(rid)
        if "when" not in r:
            errs.append(f"{where}: нет условия when")
        else:
            errs += [f"{where}: {e}" for e in R.validate(r["when"], KNOWN_FACTS)]
        if file_type == "alerts":
            if r.get("severity") not in SEVERITIES:
                errs.append(f"{where}: категория {r.get('severity')!r} — допустимо {sorted(SEVERITIES)}")
            bad = set(r.get("events", [])) - TRIGGERS
            if bad:
                errs.append(f"{where}: неизвестные события {sorted(bad)}")
            if not r.get("title"):
                errs.append(f"{where}: нет текста для врача (title)")
        else:
            if r.get("urgency") not in URGENCIES:
                errs.append(f"{where}: срочность {r.get('urgency')!r} — допустимо {sorted(URGENCIES)}")
            if not r.get("source"):
                errs.append(f"{where}: не указан источник (source) — без него рекомендация не обоснована")
    if file_type == "alerts":
        missing = REQUIRED_ALERTS - ids
        if missing:
            errs.append(f"Нельзя удалить обязательные правила суицидального риска: {', '.join(sorted(missing))}")
    return errs


def _validate_drug(d: dict) -> list[str]:
    errs = []
    for key in ("code", "trade_name", "source", "indications", "dosing", "contraindications"):
        if key not in d:
            errs.append(f"нет раздела {key}")
    if d.get("code") not in (None, "aviandr"):
        errs.append("code должен быть aviandr")
    for c in d.get("contraindications", []):
        if not c.get("id") or not c.get("text"):
            errs.append(f"противопоказание без id или текста: {c}")
    return errs
