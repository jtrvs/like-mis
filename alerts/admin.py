from django.contrib import admin

from .models import Alert, AlertDelivery, AlertRecipient


class DeliveryInline(admin.TabularInline):
    model = AlertDelivery
    extra = 0
    readonly_fields = ("channel", "attempt", "recipient", "sent_at", "ok", "error")


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ("id", "severity", "title", "patient_label", "doctor", "status", "created_at", "latency")
    list_filter = ("severity", "status", "rule_id")
    search_fields = ("patient_label", "title")
    inlines = [DeliveryInline]

    @admin.display(description="Доставка, мс")
    def latency(self, obj):
        return obj.delivery_latency_ms if obj.delivery_latency_ms is not None else "—"


admin.site.register(AlertRecipient)
