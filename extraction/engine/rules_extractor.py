"""
Извлечение структурированных сведений из истории болезни произвольного шаблона.

Подход «правила + словари» (быстро, детерминированно, работает без внешних сервисов —
данные пациента не покидают сервер). LLM-адаптер (llm.py) опционально дозаполняет пропуски.

Результат — словарь той же схемы, что и эталонная разметка (см. extraction/README.md).
"""
from __future__ import annotations

import re
from datetime import date
from functools import lru_cache
from pathlib import Path

import yaml

DATA = Path(__file__).resolve().parent.parent / "data"
TODAY = date.today()  # evaluate.py фиксирует дату разметки эталона для воспроизводимости

# --- нормализация -----------------------------------------------------------------------

_LAT = str.maketrans({"А": "A", "В": "B", "С": "C", "Е": "E", "Н": "H", "К": "K", "М": "M", "О": "O", "Р": "P", "Т": "T", "Х": "X", "Ф": "F"})


def norm(text: str) -> str:
    t = text.replace("ё", "е").replace("Ё", "Е").replace(" ", " ")
    t = t.replace("½", "1/2").replace("¼", "1/4").replace("–", "-").replace("—", "-")
    return t


def sentences(text: str) -> list[str]:
    # предложение = до точки/воскл/вопр/точки с запятой или перевода строки
    parts = re.split(r"(?<=[.!?;])\s+|\n+", text)
    return [p.strip() for p in parts if p.strip()]


NEG = re.compile(
    r"отрица|\bнет\b|не\s+(выявл|высказ|обнаруж|предъяв|отмеча|было|имеет|страда)|отсутств|низк\w*\s+суицид|"
    r"суицидальн\w+\s+риск\w*\s+(низк|не\s+выяв|отсут)|без\s+суицид|исключ",
    re.IGNORECASE,
)

# --- словари ----------------------------------------------------------------------------


@lru_cache
def drug_names() -> dict[str, str]:
    d = yaml.safe_load((DATA / "drug_names.yaml").read_text(encoding="utf-8"))
    return {k.lower(): v for k, v in d.items()}


def _name_pattern(name: str) -> str:
    """Однословное имя — точное написание + падежное окончание; многословное — по основам слов
    («вальпроевую кислоту», «лития карбоната»)."""
    words = name.split()
    if len(words) == 1:
        return re.escape(name) + r"(?:а|у|ом|ем|е|ы|ой|и)?"
    stems = [re.escape(w[:-2]) + r"[а-яa-z]{0,4}" if len(w) > 5 else re.escape(w) for w in words]
    return r"\s+".join(stems)


@lru_cache
def _name_regexes():
    names = sorted(drug_names(), key=len, reverse=True)
    return [(re.compile(rf"^{_name_pattern(n)}$", re.IGNORECASE), n) for n in names]


@lru_cache
def drug_regex():
    names = sorted(drug_names(), key=len, reverse=True)
    alt = "|".join(f"(?:{_name_pattern(n)})" for n in names)
    return re.compile(rf"(?<![а-яa-z])({alt})(?![а-яa-z])", re.IGNORECASE)


def inn_for(matched: str) -> str:
    low = matched.lower()
    if low in drug_names():
        return drug_names()[low]
    for rx, n in _name_regexes():
        if rx.match(low):
            return drug_names()[n]
    for rx, n in _name_regexes():  # падежная форма однословного имени
        if low.startswith(n):
            return drug_names()[n]
    return low


TEXT_DX = [
    (r"генерализованн\w+\s+тревожн\w+\s+расстройств|\bгтр\b", "F41.1"),
    (r"паническ\w+\s+расстройств", "F41.0"),
    (r"смешанн\w+\s+тревожн\w+\s+и\s+депрессивн", "F41.2"),
    (r"тревожно-депрессивн\w+\s+расстройств", "F41.2"),
    (r"рекуррентн\w+\s+депрессивн\w+\s+расстройств\w*[^.\n]{0,60}?(средн|умерен)", "F33.1"),
    (r"рекуррентн\w+\s+депрессивн\w+\s+расстройств\w*[^.\n]{0,60}?легк", "F33.0"),
    (r"рекуррентн\w+\s+депрессивн\w+\s+расстройств\w*[^.\n]{0,60}?тяжел", "F33.2"),
    (r"(депрессивн\w+\s+эпизод\w*[^.\n]{0,25}?(средн|умерен))|((средн|умерен)\w*\s+(степени\s+)?(тяжести\s+)?депрессивн\w+\s+эпизод)", "F32.1"),
    (r"(депрессивн\w+\s+эпизод\w*[^.\n]{0,25}?легк)|(легк\w+\s+депрессивн\w+\s+эпизод)", "F32.0"),
    (r"(депрессивн\w+\s+эпизод\w*[^.\n]{0,25}?тяжел)|(тяжел\w+\s+депрессивн\w+\s+эпизод)", "F32.2"),
    (r"расстройств\w+\s+адаптаци", "F43.2"),
    (r"посттравматическ\w+\s+стрессов|\bптср\b", "F43.1"),
    (r"соматоформн", "F45"),
    (r"гипертоническ\w+\s+болезн|артериальн\w+\s+гипертензи|\bаг\b\s*(i|ii|iii|1|2|3)?", "I10"),
    (r"сахарн\w+\s+диабет\w*\s*(2|ii|второго)|\bсд\s*(2|ii)", "E11"),
    (r"сахарн\w+\s+диабет\w*\s*(1|i\b|первого)|\bсд\s*(1|i)\b", "E10"),
    (r"\bэпилепси", "G40"),
    (r"\bибс\b|ишемическ\w+\s+болезн\w+\s+сердц", "I25"),
    (r"\bхбп\b|хроническ\w+\s+болезн\w+\s+почек", "N18"),
    (r"цирроз", "K74"),
    (r"хроническ\w+\s+(вирусн\w+\s+)?гепатит\w*\s*c|\bхгс\b", "B18.2"),
    (r"гипотиреоз", "E03"),
    (r"мигрен", "G43"),
]

CONDITION_KW = [
    (r"эпилепс", "epilepsy"),
    (r"органическ\w+\s+(поражени|заболевани|расстройств)|\bf0[67]", "organic_brain_disease"),
    (r"\bибс\b|ишемическ|гипертоническ\w+\s+болезн|артериальн\w+\s+гипертенз|\bаг\b|\bхсн\b|инфаркт|аритми|фибрилляц", "cardiovascular_disease"),
    (r"диабет|\bсд\s*(1|2|i)", "diabetes"),
    (r"зависимост|алкоголизм|\bf1\d\.2", "substance_dependence"),
    (r"печеночн\w+\s+недостаточн|чайлд|цирроз", "hepatic_failure"),
    (r"почечн\w+\s+недостаточн|\bхпн\b|\bхбп\s*(3|4|5|с3|c3|с4|c4)", "renal_failure"),
]

CODE_TO_CONDITION = {
    "G40": "epilepsy", "F06": "organic_brain_disease", "F07": "organic_brain_disease",
    "I10": "cardiovascular_disease", "I11": "cardiovascular_disease", "I20": "cardiovascular_disease",
    "I25": "cardiovascular_disease", "I48": "cardiovascular_disease", "I50": "cardiovascular_disease",
    "E10": "diabetes", "E11": "diabetes", "K74": "hepatic_failure", "K72": "hepatic_failure",
}

SCALES = {
    "phq9": r"phq[\s-]?9|пхк[\s-]?9",
    "gad7": r"gad[\s-]?7|гтр[\s-]?7",
    "hads_a": r"hads[\s-]?a\b|hads[\s-]?т\b|hads[^.\n]{0,15}?тревог\w*",
    "hads_d": r"hads[\s-]?d\b|hads[\s-]?д\b|hads[^.\n]{0,40}?депресси\w*",
    "hamd": r"ham[\s-]?d(?:[\s-]?17)?|hdrs(?:[\s-]?17)?|hrsd(?:[\s-]?17)?|гамильтон\w*[^.\n]{0,30}?депресс\w*(?:[\s-]?17)?",
    "hama": r"ham[\s-]?a\b|hars|гамильтон\w*[^.\n]{0,30}?тревог\w*",
}

FREQ = [
    (r"(\d)\s*(?:р(?:аз|аза)?\.?|x|х|×)\s*(?:/|в)\s*(?:д|день|сут|сутки)\b", None),
    (r"(\d)\s*р(?:аз|аза)?\.?\s*/\s*(?:д|дн|сут)", None),
    (r"(\d)\s*раз\w*\s+в\s+(?:день|сутки)", None),
    (r"(\d)\s*р\.?\s*/?\s*(?:д|сут)\b", None),
    (r"[x×х]\s*(\d)\b", None),
    (r"трижды|3-?х?\s*кратно", 3),
    (r"дважды|2-?х?\s*кратно|утром\s+и\s+(вечером|на\s+ночь)|утро[-\s]вечер", 2),
    (r"однократно|1\s*р\.?\s*/\s*сут|\bн/н\b|утром|на\s+ночь|вечером|перед\s+сном|в\s+обед", 1),
]

STOP_CTX = re.compile(
    r"отмен|ранее\s+(принимал|получал|назнач|был)|в\s+анамнезе\s+(прием|терапи)|прекрат|"
    r"без\s+эффекта|не\s+переносил|замен\w+\s+на|принимал\w*\s+ранее|получал\w*\s+ранее|была\s+отмен|"
    r"неэффективн|самостоятельно\s+прекрат|бросил",
    re.IGNORECASE,
)
NOT_PRESCRIBED = re.compile(
    r"противопоказан|не\s+назнач|нежелательн|воздерж|рассмотр\w*\s+(вопрос|назначени|возможност)|"
    r"вопрос\w*\s+о\s+(возможности\s+)?назначени|решени\w+\s+вопроса|отказ\w*\s+от|не\s+рекоменд|"
    r"не\s+показан|отклон",
    re.IGNORECASE,
)
NO_CONTRA = re.compile(r"противопоказани\w*[^.;]{0,40}(нет|не\s+выявл|отсутств)", re.IGNORECASE)
START_CTX = re.compile(r"назнач|рекомендован|начат|продолж|принимает|прием\s|рекоменд|rp|\bсхема|лечение", re.IGNORECASE)

# --- извлечение по полям -----------------------------------------------------------------


def extract_icd(text: str) -> list[str]:
    codes = []
    for s in sentences(text):
        if re.search(r"исключ|под\s+вопрос|\?\s*$|дифференц", s, re.IGNORECASE):
            continue
        s2 = s.translate(_LAT)
        for m in re.finditer(r"(?<![A-Za-z0-9])([A-TV-Z]\d{2}(?:\.\d{1,2})?)(?![0-9])", s2):
            c = m.group(1)
            # соглашение разметки: психиатрия (F) — подрубрика, соматика — рубрика из 3 знаков
            codes.append(c if c.startswith("F") else c[:3])
    has_f = any(c.startswith("F") for c in codes)
    for s in sentences(text):
        low = s.lower()
        if NEG.search(low) or re.search(r"исключ|под\s+вопрос", low):
            continue
        for pat, code in TEXT_DX:
            if has_f and code.startswith("F"):
                continue  # психиатрический код уже указан явно — текстом не дублируем
            if re.search(pat, low) and not any(c.startswith(code) for c in codes):
                codes.append(code)
    seen, out = set(), []
    for c in codes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _dose_and_freq(seg: str):
    s = seg.lower()
    dose = None
    m = re.search(r"по\s+(\d+(?:[.,]\d+)?)\s*мг", s)
    strength = re.search(r"(\d+(?:[.,]\d+)?)\s*мг", s)
    frac = re.search(r"(?:по\s+)?(\d+/\d+|\d+[.,]\d+|\d+)\s*(?:т\b|т\.|таб|капс)", s)
    if m:
        dose = float(m.group(1).replace(",", "."))
    elif strength and frac and frac.start() > strength.start():
        f = frac.group(1)
        f = eval_frac(f)
        dose = float(strength.group(1).replace(",", ".")) * f if f else None
    elif strength:
        dose = float(strength.group(1).replace(",", "."))
    times = None
    for pat, val in FREQ:
        mm = re.search(pat, s)
        if mm:
            times = int(mm.group(1)) if val is None else val
            if times and 1 <= times <= 6:
                break
            times = None
    if dose is not None and dose == int(dose):
        dose = int(dose)
    return dose, times


def eval_frac(f: str):
    try:
        if "/" in f:
            a, b = f.split("/")
            return int(a) / int(b)
        return float(f.replace(",", "."))
    except (ValueError, ZeroDivisionError):
        return None


def extract_meds(text: str):
    current: dict[str, dict] = {}
    stopped: list[str] = []
    rx = drug_regex()
    for sent in sentences(text):
        matches = list(rx.finditer(sent))
        for i, m in enumerate(matches):
            inn = inn_for(m.group(1))
            # сегмент препарата: от названия до следующего препарата / конца предложения
            end = matches[i + 1].start() if i + 1 < len(matches) else len(sent)
            seg = sent[m.start():end]
            before = sent[max(0, m.start() - 60):m.start()]
            # «отменён» после названия в том же сегменте или «ранее принимал» перед ним
            is_stopped = bool(STOP_CTX.search(before) and not START_CTX.search(before[-25:])) or bool(
                re.search(r"отмен|прекращ|не\s+переносил|без\s+эффекта|неэффективн", seg, re.IGNORECASE)
            )
            if is_stopped:
                if inn not in stopped:
                    stopped.append(inn)
                continue
            if NOT_PRESCRIBED.search(sent) and not NO_CONTRA.search(sent) and not re.search(r"назначен[оаы]?\s*:?\s*$|\brp\b", before, re.I):
                continue  # обсуждался/противопоказан, но не назначен
            dose, times = _dose_and_freq(seg)
            prev = current.get(inn)
            if prev is None or (prev["dose_mg"] is None and dose is not None) or (prev["times_per_day"] is None and times):
                current[inn] = {
                    "inn": inn,
                    "dose_mg": dose if dose is not None else (prev or {}).get("dose_mg"),
                    "times_per_day": times or (prev or {}).get("times_per_day"),
                }
    # если препарат и отменён, и назначен (смена дозы) — остаётся в текущих
    stopped = [s for s in stopped if s not in current]
    return list(current.values()), stopped


def extract_scales(text: str) -> dict:
    out = {}
    low = norm(text).lower()
    for code, pat in SCALES.items():
        for m in re.finditer(pat, low):
            tail = re.sub(r"^\s*\([^)]{0,20}\)", "", low[m.end():m.end() + 45])[:30]
            n = re.search(r"(\d{1,2})(?!\s*(?:мг|лет|год|нед|дн|мес|%))", tail)
            if n:
                v = int(n.group(1))
                if v <= 63:
                    out[code] = v
                    break
    # «HADS: тревога 12, депрессия 9»
    m = re.search(r"hads[^\n]{0,15}?тревог\w*\D{0,5}(\d{1,2})[^\n]{0,20}?депресс\w*\D{0,5}(\d{1,2})", low)
    if m:
        out["hads_a"], out["hads_d"] = int(m.group(1)), int(m.group(2))
    return out


SUI = re.compile(
    r"суицид|самоубийств|мысл\w*\s+о\s+смерти|не\s+хоч\w+\s+жить|нежелани\w+\s+жить|самоповрежд|"
    r"парасуицид|поконч\w+\s+с\s+собой|антивитальн|жить\s+не\s+хочет|наглотал|порез\w*\s+(вен|рук|предплеч)",
    re.IGNORECASE,
)


def clauses(s: str) -> list[str]:
    return [c for c in re.split(r",\s+(?=[а-яa-z])|\s+-\s+|;|\(|\)", s) if c.strip()]


def extract_suicidal(text: str) -> bool:
    for s in sentences(text):
        for c in clauses(s):
            if SUI.search(c) and not NEG.search(c):
                return True
    return False


def extract_conditions(text: str, codes: list[str], egfr, child_pugh) -> list[str]:
    found = []
    for s in sentences(text):
        low = s.lower()
        if NEG.search(low):
            continue
        for pat, cond in CONDITION_KW:
            if re.search(pat, low) and cond not in found:
                found.append(cond)
    for c in codes:
        cond = CODE_TO_CONDITION.get(c.split(".")[0])
        if cond and cond not in found:
            found.append(cond)
    if egfr is not None and egfr < 60 and "renal_failure" not in found:
        found.append("renal_failure")
    if child_pugh and "hepatic_failure" not in found:
        found.append("hepatic_failure")
    return found


def extract_egfr(text: str):
    m = re.search(r"(?:скф|ckd-epi|рскф)[^\d\n]{0,25}(\d{1,3}(?:[.,]\d)?)", text, re.IGNORECASE)
    return float(m.group(1).replace(",", ".")) if m else None


def extract_child_pugh(text: str):
    m = re.search(r"чайлд\w*[-\s]*пь\w*[^A-CА-С\n]{0,30}([ABCАВС])\b|класс\w*\s+([ABCАВС])\s+(?:по\s+)?чайлд", text, re.IGNORECASE)
    if not m:
        return None
    return (m.group(1) or m.group(2)).upper().translate(_LAT)


def extract_age_sex(text: str):
    low = text.lower()
    age = None
    m = re.search(r"(?:д\.?\s*р\.?|дата\s+рождения|г\.?\s*р\.?)[:\s]*(\d{1,2})[./](\d{1,2})[./](\d{4})", low)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        age = TODAY.year - y - ((TODAY.month, TODAY.day) < (mo, d))
    else:
        m = None
        for c in re.finditer(r"(?:возраст[:\s]*)?\b(\d{2})\s*(?:лет|год(?:а)?\b|г\.(?!\s*р))", low):
            ctx = low[max(0, c.start() - 45):c.start()]
            if re.search(r"диабет|гипертенз|болеет|в\s+течение|стаж|назад|курит|страда|после|около|более|с\s+$|до\s+$|на\s+протяжении|\bв\s+$|возрасте", ctx):
                continue
            m = c
            break
        m = m or re.search(r"возраст[:\s]*(\d{2})", low)
        if m:
            age = int(m.group(1))
        else:
            m = re.search(r"(\d{4})\s*г\.?\s*р", low)
            if m:
                age = TODAY.year - int(m.group(1))
    sex = None
    m = re.search(r"пол[:\s]+(ж|м)", low)
    m = m or re.search(r"(?:лет|г\.|года?|\d{4})\s*,\s*(ж|м)(?:ен|уж)?\w*\.?(?![а-я])", low)
    if m:
        sex = "F" if m.group(1) == "ж" else "M"
    elif re.search(r"пациентк|больн(ая|ой\s+женщ)|\bженщина\b|жалуется\s+она|беременн|родил|обратилась|поступила", low):
        sex = "F"
    elif re.search(r"пациент\b|больной\b|\bмужчина\b|обратился|поступил\b", low):
        sex = "M"
    return age, sex


def extract(text: str) -> dict:
    t = norm(text)
    codes = extract_icd(t)
    meds, stopped = extract_meds(t)
    egfr = extract_egfr(t)
    cp = extract_child_pugh(t)
    age, sex = extract_age_sex(t)
    low = t.lower()
    pregnant = any(re.search(r"беременн|\bб\s*\d{1,2}\s*нед", s.lower()) and not NEG.search(s.lower()) for s in sentences(t))
    breastfeeding = any(
        re.search(r"лактац|грудн\w+\s+вскармл|кормит\s+грудью", s.lower()) and not re.search(r"прекращ|завершил|отрица|\bнет\b", s.lower())
        for s in sentences(t)
    )
    return {
        "sex": sex,
        "age": age,
        "diagnoses": codes,
        "medications": meds,
        "stopped_medications": stopped,
        "scales": extract_scales(t),
        "suicidal_risk": extract_suicidal(t),
        "conditions": extract_conditions(t, codes, egfr, cp),
        "pregnant": bool(pregnant) and "не беремен" not in low,
        "breastfeeding": bool(breastfeeding),
        "egfr": egfr,
        "child_pugh": cp,
    }
