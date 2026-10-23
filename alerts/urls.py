from django.urls import path

from . import views

urlpatterns = [
    path("alerts/", views.alerts_page, name="alerts_page"),
    path("api/alerts/", views.alerts_json, name="alerts_json"),
    path("api/alerts/<int:alert_id>/ack/", views.alert_ack, name="alert_ack"),
]
