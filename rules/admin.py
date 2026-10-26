from django.contrib import admin
from .models import RuleVersion

@admin.register(RuleVersion)
class RuleVersionAdmin(admin.ModelAdmin):
    list_display = ('file_type', 'version', 'is_active', 'created_at', 'uploaded_by')
    list_filter = ('file_type', 'is_active')
    readonly_fields = ('created_at', 'uploaded_by', 'version')
