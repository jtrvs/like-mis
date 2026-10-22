from django.urls import path

from .views import extract_view

urlpatterns = [path("api/extract", extract_view, name="extract")]
