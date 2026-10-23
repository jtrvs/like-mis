from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import transaction
from django.shortcuts import redirect, render

from django.core.cache import cache

from core.models import CareLink, Consent, PatientProfile, Profile

from .access import audit, role_of
from .forms import JoinForm


@login_required
def home(request):
    r = role_of(request.user)
    if r == "doctor":
        return redirect("doctor_home")
    if r == "patient":
        return redirect("patient_home")
    return redirect("/admin/")


JOIN_MAX_FAILS, JOIN_WINDOW = 10, 15 * 60


def join(request):
    """Пациент регистрируется по коду, который выдал врач — так данные сразу привязаны к лечащему врачу."""
    ip = request.META.get("REMOTE_ADDR", "?")
    fails_key = f"join-fails:{ip}"
    if request.method == "POST" and cache.get(fails_key, 0) >= JOIN_MAX_FAILS:
        return render(request, "web/join.html", {"form": JoinForm(), "locked": True}, status=429)
    form = JoinForm(request.POST or None, initial={"code": request.GET.get("code", "")})
    if request.method == "POST" and not form.is_valid() and "code" in form.errors:
        cache.set(fails_key, cache.get(fails_key, 0) + 1, JOIN_WINDOW)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            link = CareLink.objects.select_for_update().select_related("patient").get(pk=form.link.pk)
            p = PatientProfile.objects.select_for_update().get(pk=link.patient_id)
            if p.user_id:  # два одновременных входа по одному коду
                messages.error(request, "Этот код уже использован.")
                return redirect("join")
            u = User.objects.create_user(form.cleaned_data["username"], password=form.cleaned_data["password"],
                                         first_name=p.first_name, last_name=p.last_name)
            Profile.objects.create(user=u, role="patient")
            p.user = u
            p.save(update_fields=["user"])
            Consent.objects.create(patient=p, kind="pd_processing", granted=True)
            Consent.objects.create(patient=p, kind="remote_monitoring", granted=True)
        login(request, u)
        audit(request, "patient_joined", p)
        messages.success(request, "Готово. Ваш врач видит ваши записи.")
        return redirect("patient_home")
    return render(request, "web/join.html", {"form": form})
