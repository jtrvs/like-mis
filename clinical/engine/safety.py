"""
Проверка безопасности назначения по инструкции препарата.
Вход — простой словарь «контекст пациента», чтобы движок не зависел от ORM и легко тестировался.

ctx = {
  "age": 34, "pregnant": False, "breastfeeding": False,   # None = неизвестно → «проверьте», а не «зелёный»
  "egfr": 90, "child_pugh": "",              # "" | "A" | "B" | "C" (регистр не важен)
  "conditions": ["epilepsy", ...],           # коды из словаря состояний
  "icd10": ["F41.1"],                         # диагнозы
  "current_drugs": ["сертралин"],            # МНН/торговые названия
  "stopped_drugs": [{"name": "моклобемид", "days_ago": 5}],
}
"""
from dataclasses import dataclass, asdict

from . import loader

LEVEL_ORDER = {"contraindication": 0, "caution": 1, "off_label": 1, "info": 2}


@dataclass
class Finding:
    level: str  # contraindication | caution | off_label | info
    code: str
    text: str
    source: str

    def as_dict(self):
        return asdict(self)


def normalize(drug_name: str) -> str:
    """Торговое название → МНН (общий словарь с модулем разбора выписок).
    «Аурорикс 300 мг», «Золофт (сертралин) 50мг» → «моклобемид», «сертралин»."""
    import re
    n = drug_name.strip().lower().replace("ё", "е")
    n = n.split("(")[0]
    n = re.split(r"\s*\d", n)[0].strip(" ,.;")  # отрезаем дозу и всё после неё
    return loader.trade_names().get(n, n)


def classes_of(drug_name: str) -> list[str]:
    return loader.drug_classes().get(normalize(drug_name), [])


def check(drug_code: str, ctx: dict) -> list[Finding]:
    d = loader.drugs()[drug_code]
    src = f"{d['source']['title']}, РУ {d['source']['reg_number']}"
    out: list[Finding] = []
    conditions = set(ctx.get("conditions") or [])
    age = ctx.get("age")
    child_pugh = str(ctx.get("child_pugh") or "").strip().upper()

    # противопоказания
    for c in d["contraindications"]:
        hit = (
            ("condition" in c and c["condition"] in conditions)
            or ("pregnant" in c and ctx.get("pregnant"))
            or ("breastfeeding" in c and ctx.get("breastfeeding"))
            or ("egfr_below" in c and ctx.get("egfr") is not None and ctx["egfr"] < c["egfr_below"])
            or ("child_pugh" in c and child_pugh in c["child_pugh"])
            or ("age_below" in c and age is not None and age < c["age_below"])
        )
        if hit:
            out.append(Finding("contraindication", c["id"], c["text"], src))

    # осторожность
    for c in d.get("cautions", []):
        hit = ("condition" in c and c["condition"] in conditions) or (
            "age_from" in c and age is not None and age >= c["age_from"]
        )
        if hit:
            out.append(Finding("caution", c["id"], c["text"], src))

    # взаимодействия: текущие препараты + недавно отменённые (для отмывочного периода).
    # Все механизмы для одного препарата сводятся в одну находку с наивысшим уровнем.
    current = [(n, None) for n in ctx.get("current_drugs") or []]
    recent = [(s["name"], s["days_ago"]) for s in ctx.get("stopped_drugs") or []]
    per_drug: dict[str, list] = {}
    for rule in d.get("interactions", []):
        washout = next(
            (c.get("washout_days") for c in d["contraindications"] if c.get("drug_class") == rule["drug_class"]), None
        )
        for name, days_ago in current + recent:
            if rule["drug_class"] not in classes_of(name):
                continue
            if days_ago is not None and (washout is None or days_ago >= washout):
                continue
            per_drug.setdefault((name, days_ago), []).append(rule)
    for (name, days_ago), rules_hit in per_drug.items():
        level = min((r["level"] for r in rules_hit), key=lambda lv: LEVEL_ORDER[lv])
        suffix = f" (отменён {days_ago} дн. назад)" if days_ago is not None else ""
        code = "interaction_" + "_".join(r["drug_class"] for r in rules_hit)
        out.append(Finding(level, code, f"{name}{suffix}: " + "; ".join(r["text"] for r in rules_hit), src))

    # показания: вне инструкции — только решение врача
    allowed, all_codes = set(), set()
    for ind in d["indications"]:
        all_codes |= set(ind["icd10"])
        if not ind.get("requires") or ctx.get(ind["requires"]):
            allowed |= set(ind["icd10"])
    dx = {str(c).strip().upper() for c in ctx.get("icd10") or []}
    if not dx:
        out.append(Finding("off_label", "no_diagnosis", "Диагноз не указан — соответствие показаниям не проверено.", src))
    elif not dx & allowed:
        covid_note = ""
        if dx & (all_codes - allowed):
            covid_note = " F41.9/F43.9 — показание только для тревоги после перенесённого COVID-19; отметьте это в карточке, если так."
        out.append(
            Finding(
                "off_label",
                "no_indication",
                f"Диагнозы {', '.join(sorted(dx))} не входят в показания инструкции "
                f"({', '.join(sorted(allowed or all_codes))}).{covid_note} Применение вне инструкции — только решение врача.",
                src,
            )
        )

    # курс при тревоге после COVID-19 — стандартно 4 недели, по решению врача до 6
    max_weeks = max((ind.get("max_weeks", 0) for ind in d["indications"] if set(ind["icd10"]) & dx and ind.get("requires")), default=0)
    if max_weeks and not dx & {c for ind in d["indications"] if not ind.get("requires") for c in ind["icd10"]}:
        weeks = ctx.get("weeks_on_drug")
        if weeks is not None and weeks > max_weeks:
            out.append(Finding("off_label", "course_exceeded", f"Курс при тревоге после COVID-19 — не более {max_weeks} недель; идёт {weeks}-я неделя. Продление — вне инструкции.", src))
        elif weeks is not None and weeks > 4:
            out.append(Finding("caution", "course_limit", f"Стандартный курс при тревоге после COVID-19 — 4 недели (продление до {max_weeks} — по решению врача); идёт {weeks}-я неделя.", src))

    # неполные данные: не выдаём «зелёный» по умолчанию.
    # Чтобы получить «зелёный», вызывающий код должен явно передать pregnant/breastfeeding=False и СКФ.
    if ctx.get("egfr") is None and "renal_failure" not in conditions:
        out.append(Finding("caution", "egfr_unknown", "СКФ не указана — противопоказание при почечной недостаточности не проверено.", src))
    if age is None:
        out.append(Finding("caution", "age_unknown", "Возраст не указан — ограничение «до 18 лет» не проверено.", src))
    for key, what in (("pregnant", "беременность"), ("breastfeeding", "грудное вскармливание")):
        if ctx.get(key) is None:
            out.append(Finding("caution", f"{key}_unknown", f"Не указано, есть ли {what}, — противопоказание не проверено.", src))
    for name in list(ctx.get("current_drugs") or []) + [s["name"] for s in ctx.get("stopped_drugs") or []]:
        if normalize(name) not in loader.drug_classes():
            out.append(Finding("caution", "unknown_drug", f"{name}: препарат не найден в справочнике — проверьте взаимодействия вручную.", src))

    for w in d.get("warnings", []):
        out.append(Finding("info", "warning", w, src))

    # одна находка на код (например, два СИОЗС не дублируют строку)
    seen, uniq = set(), []
    for f in sorted(out, key=lambda f: LEVEL_ORDER[f.level]):
        key = (f.code, f.text)
        if key not in seen:
            seen.add(key)
            uniq.append(f)
    return uniq


def verdict(findings: list[Finding]) -> str:
    """Итог для светофора в интерфейсе."""
    levels = {f.level for f in findings}
    if "contraindication" in levels:
        return "red"
    if levels & {"caution", "off_label"}:
        return "yellow"
    return "green"


def dosing_hint(drug_code: str) -> str:
    d = loader.drugs()[drug_code]["dosing"]
    return f"{d['dose_mg']} мг × {d['times_per_day']} р/сут ({d['timing']}), максимум {d['daily_max_mg']} мг/сут"
