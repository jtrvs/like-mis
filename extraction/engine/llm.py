"""
Опциональное дозаполнение пропусков через LLM. ПО УМОЛЧАНИЮ ВЫКЛЮЧЕНО.

152-ФЗ: история болезни — данные о здоровье. Отправлять текст можно только в модель,
развёрнутую на своём сервере (Ollama) или у российского провайдера по договору (YandexGPT/GigaChat),
и только после удаления ФИО (deidentify). Зарубежные API не используются.

Включение: EXTRACTION_LLM=ollama (+ OLLAMA_URL, OLLAMA_MODEL) или EXTRACTION_LLM=yandex (+ YANDEX_API_KEY, YANDEX_FOLDER_ID).
Не проверено в сети на момент написания — проверить на сервере (см. extraction/README.md).
"""
import json
import os
import re
import urllib.request

from .rules_extractor import extract as rules_extract

SCHEMA_HINT = (
    "Верни ТОЛЬКО JSON со схемой: {sex: 'M'|'F'|null, age: int|null, diagnoses: [коды МКБ-10], "
    "medications: [{inn, dose_mg, times_per_day}], stopped_medications: [МНН], scales: {phq9,gad7,hads_a,hads_d,hamd,hama}, "
    "suicidal_risk: bool, pregnant: bool, breastfeeding: bool, egfr: number|null, child_pugh: 'A'|'B'|'C'|null}. "
    "МНН в нижнем регистре. Только текущие назначения в medications."
)


def deidentify(text: str) -> str:
    """Грубая псевдонимизация перед отправкой наружу: ФИО, даты рождения, телефоны, полисы."""
    t = re.sub(r"\b[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.", "[ФИО]", text)
    t = re.sub(r"\b[А-ЯЁ][а-яё]+(ов|ев|ин|ын|ский|цкий|ова|ева|ина|ына|ская|цкая)\s+[А-ЯЁ][а-яё]+(\s+[А-ЯЁ][а-яё]+(вич|вна|ична))?", "[ФИО]", t)
    t = re.sub(r"\+?\d[\d\s()-]{9,}\d", "[ТЕЛ]", t)
    t = re.sub(r"\b\d{16}\b|\b\d{3}-\d{3}-\d{3}\s?\d{2}\b", "[НОМЕР]", t)
    return t


def _post(url, payload, headers):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def _ollama(prompt):
    url = os.environ.get("OLLAMA_URL", "http://ollama:11434") + "/api/generate"
    model = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b-instruct")
    return _post(url, {"model": model, "prompt": prompt, "stream": False, "format": "json"}, {})["response"]


def _yandex(prompt):
    folder = os.environ["YANDEX_FOLDER_ID"]
    payload = {
        "modelUri": f"gpt://{folder}/yandexgpt/latest",
        "completionOptions": {"temperature": 0, "maxTokens": 2000},
        "messages": [{"role": "user", "text": prompt}],
    }
    r = _post("https://llm.api.cloud.yandex.net/foundationModels/v1/completion", payload, {"Authorization": f"Api-Key {os.environ['YANDEX_API_KEY']}"})
    return r["result"]["alternatives"][0]["message"]["text"]


PROVIDERS = {"ollama": _ollama, "yandex": _yandex}


def llm_extract(text: str) -> dict | None:
    name = os.environ.get("EXTRACTION_LLM")
    if not name:
        return None
    raw = PROVIDERS[name](f"{SCHEMA_HINT}\n\nТекст:\n{deidentify(text)}")
    m = re.search(r"\{.*\}", raw, re.S)
    return json.loads(m.group(0)) if m else None


def extract(text: str) -> dict:
    """Гибрид: правила — основа; LLM только заполняет пустые поля (правила не перезаписываются)."""
    base = rules_extract(text)
    try:
        extra = llm_extract(text)
    except Exception as e:  # LLM недоступна — система продолжает работать на правилах
        base["_llm_error"] = str(e)[:200]
        return base
    if not extra:
        return base
    for k, v in extra.items():
        if k in base and base[k] in (None, [], {}, False) and v not in (None, [], {}):
            base[k] = v
    base["_llm_used"] = True
    return base
