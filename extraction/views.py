"""
POST /api/extract   тело: {"text": "текст выписки"}
Ответ: извлечённые сведения + проверка Авиандра по инструкции + маршрутизация к психиатру.
Доступ — только врачу (см. alerts/permissions.py).
"""
import json
import logging
import time

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from alerts.permissions import is_doctor
from clinical.engine import routing, safety

from .engine.llm import extract

log = logging.getLogger("extraction")


@csrf_exempt  # запрос ничего не меняет в базе; доступ — только врачу с активной сессией
@require_POST
def extract_view(request):
    if not is_doctor(request.user):
        return JsonResponse({"error": "нужен вход врача"}, status=401)
    t0 = time.perf_counter()
    try:
        body = json.loads(request.body or b"{}")
        text = body.get("text", "") if isinstance(body, dict) else None
    except (ValueError, UnicodeDecodeError):
        text = None
    if not isinstance(text, str) or not text.strip():
        return JsonResponse({"error": 'ожидается JSON вида {"text": "..."}'}, status=400)
    if len(text) > 100_000:
        return JsonResponse({"error": "текст длиннее 100 000 символов"}, status=413)
    log.info("extract by user %s, %s chars", request.user.id, len(text))
    data = extract(text)
    # Если в выписке беременность/ГВ не упомянуты, это «неизвестно», а не «нет» (кроме мужчин) — не даём ложный «зелёный».
    is_male = data["sex"] == "M"
    ctx = {
        "age": data["age"],
        "pregnant": True if data["pregnant"] else (False if is_male else None),
        "breastfeeding": True if data["breastfeeding"] else (False if is_male else None),
        "egfr": data["egfr"], "child_pugh": data["child_pugh"] or "", "conditions": data["conditions"],
        "icd10": data["diagnoses"], "current_drugs": [m["inn"] for m in data["medications"]],
        "stopped_drugs": [{"name": n, "days_ago": 0} for n in data["stopped_medications"]],  # дата отмены неизвестна — считаем недавней
    }
    findings = safety.check("aviandr", ctx)
    route_facts = {"suicide_keywords": data["suicidal_risk"], **{f"{k}_total": v for k, v in data["scales"].items()}}
    return JsonResponse({
        "extracted": data,
        "aviandr": {"verdict": safety.verdict(findings), "findings": [f.as_dict() for f in findings], "dosing": safety.dosing_hint("aviandr")},
        "routing": routing.summary(route_facts),
        "ms": round((time.perf_counter() - t0) * 1000, 1),
    }, json_dumps_params={"ensure_ascii": False})
