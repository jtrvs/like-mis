from django.contrib.auth import views as auth_views
from django.urls import path
from django.views.generic import TemplateView

from . import views_common as c
from . import views_doctor as d
from . import views_patient as p

urlpatterns = [
    path("", c.home, name="home"),
    path("login/", auth_views.LoginView.as_view(template_name="web/login.html", redirect_authenticated_user=True), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("join/", c.join, name="join"),
    path("manifest.webmanifest", TemplateView.as_view(template_name="web/manifest.webmanifest", content_type="application/manifest+json")),
    path("sw.js", TemplateView.as_view(template_name="web/sw.js", content_type="application/javascript")),

    path("doctor/", d.doctor_home, name="doctor_home"),
    path("doctor/alerts/", d.doctor_alerts, name="doctor_alerts"),
    path("doctor/alerts/<int:alert_id>/ack/", d.alert_ack, name="alert_ack"),
    path("doctor/patient/new/", d.patient_new, name="patient_new"),
    path("doctor/patient/<uuid:public_id>/", d.patient_card, name="doctor_patient"),
    path("doctor/patient/<uuid:public_id>/aviandr/", d.prescribe_aviandr, name="prescribe_aviandr"),
    path("doctor/patient/<uuid:public_id>/rx/<int:rx_id>/stop/", d.stop_prescription, name="stop_rx"),
    path("doctor/patient/<uuid:public_id>/referral/<int:ref_id>/", d.referral_decide, name="referral_decide"),
    path("doctor/patient/<uuid:public_id>/route/", d.routing_confirm, name="routing_confirm"),
    path("doctor/patient/<uuid:public_id>/message/", d.send_message, name="doctor_message"),
    path("doctor/patient/<uuid:public_id>/import/", d.import_history, name="import_history"),

    path("me/", p.patient_home, name="patient_home"),
    path("me/checkin/", p.checkin, name="patient_checkin"),
    path("me/scale/<str:code>/", p.scale_fill, name="patient_scale"),
    path("me/meds/", p.meds, name="patient_meds"),
    path("me/meds/<int:intake_id>/", p.dose_mark, name="dose_mark"),
    path("me/progress/", p.progress, name="patient_progress"),
    path("me/chat/", p.chat, name="patient_chat"),
    path("me/help/", p.help_page, name="patient_help"),
    path("me/help/contact/<int:cid>/delete/", p.contact_delete, name="contact_delete"),
]
