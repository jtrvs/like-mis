"""
Для кабинета врача:
  GET  /alerts/                    — простая страница со списком сигналов (пока нет интерфейса Стаса)
  GET  /api/alerts/?open=1         — сигналы текущего врача в JSON (для интерфейса)
  POST /api/alerts/<id>/ack/       — отметить «просмотрено», поле resolution — что сделано
"""
import json

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import service
from .permissions import is_doctor


@login_required
def alerts_page(request):
    if not is_doctor(request.user):
        return HttpResponseForbidden("Только для врачей")
    qs = service.visible_to(request.user).order_by("status", "-created_at")[:100]
    items = sorted(qs, key=lambda a: (a.status not in a.OPEN, a.SEVERITY_ORDER.get(a.severity, 9), -a.created_at.timestamp()))
    return render(request, "alerts/list.html", {"alerts": items})


@login_required
def alerts_json(request):
    if not is_doctor(request.user):
        return JsonResponse({"error": "только для врачей"}, status=403)
    qs = service.visible_to(request.user)
    if request.GET.get("open"):
        qs = qs.filter(status__in=qs.model.OPEN)
    if request.GET.get("patient_id"):
        if not request.GET["patient_id"].isdigit():
            return JsonResponse({"error": "patient_id должен быть числом"}, status=400)
        qs = qs.filter(patient_id=int(request.GET["patient_id"]))
    return JsonResponse({"alerts": [a.as_dict() for a in qs[:200]]}, json_dumps_params={"ensure_ascii": False})


@login_required
@require_POST
def alert_ack(request, alert_id):
    if not is_doctor(request.user):
        return JsonResponse({"error": "только для врачей"}, status=403)
    a = get_object_or_404(service.visible_to(request.user), id=alert_id)
    if request.content_type == "application/json":
        try:
            resolution = (json.loads(request.body or b"{}") or {}).get("resolution", "")
        except ValueError:
            return JsonResponse({"error": "некорректный JSON"}, status=400)
    else:
        resolution = request.POST.get("resolution", "")
    service.acknowledge(a, request.user, str(resolution)[:2000], false_positive=bool(request.POST.get("false_positive")))
    if request.content_type == "application/json":
        return JsonResponse({"ok": True, "alert": a.as_dict()}, json_dumps_params={"ensure_ascii": False})
    return redirect("alerts_page")
