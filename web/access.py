"""Права доступа: врач видит только пациентов со своей активной связью, пациент — только себя."""
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404

from core.models import AuditLog, PatientProfile


def role_of(user):
    if not user.is_authenticated:
        return None
    if hasattr(user, "doctor"):
        return "doctor"
    if hasattr(user, "patient"):
        return "patient"
    return "admin" if user.is_staff else None


def doctor_required(view):
    @login_required
    @wraps(view)
    def inner(request, *a, **kw):
        if role_of(request.user) != "doctor":
            raise PermissionDenied
        request.doctor = request.user.doctor
        return view(request, *a, **kw)

    return inner


def patient_required(view):
    @login_required
    @wraps(view)
    def inner(request, *a, **kw):
        if role_of(request.user) != "patient":
            raise PermissionDenied
        request.patient = request.user.patient
        return view(request, *a, **kw)

    return inner


def patient_for_doctor(request, public_id, action="view_card"):
    """
    Доступ к карточке: ведущий/лечащий врач с активной связью ИЛИ резервный врач,
    которому эскалирован ещё не закрытый сигнал по этому пациенту (временный доступ).
    """
    p = PatientProfile.objects.filter(public_id=public_id, care_links__doctor=request.doctor, care_links__active=True).first()
    if p is None:
        from alerts.models import Alert
        p = PatientProfile.objects.filter(public_id=public_id).first()
        if p is None or not Alert.objects.filter(patient_id=p.id, escalated_to=request.user, status="escalated").exists():
            raise Http404  # не раскрываем, существует ли пациент
        action = action + "_escalated"
    audit(request, action, p)
    return p


def safe_next(request, default):
    """Защита от открытого редиректа через параметр next."""
    from django.utils.http import url_has_allowed_host_and_scheme
    nxt = request.POST.get("next") or request.GET.get("next")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return nxt
    return default


def audit(request, action, patient=None, **details):
    AuditLog.objects.create(
        actor=request.user if request.user.is_authenticated else None,
        action=action,
        patient=patient,
        details=details,
        ip=request.META.get("REMOTE_ADDR"),
    )
