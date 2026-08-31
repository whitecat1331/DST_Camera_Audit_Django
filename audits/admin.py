from django.contrib import admin

from audits.models import AuditJob, AuditScreenshot


class AuditScreenshotInline(admin.TabularInline):
    model = AuditScreenshot
    extra = 0
    readonly_fields = ("label", "image", "created_at")


@admin.register(AuditJob)
class AuditJobAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "pole_number",
        "device_type",
        "status",
        "target_host",
        "parent_job",
        "created_by",
        "created_at",
    )
    list_filter = ("device_type", "status")
    search_fields = ("pole_number", "target_host")
    raw_id_fields = ("parent_job",)
    inlines = [AuditScreenshotInline]


@admin.register(AuditScreenshot)
class AuditScreenshotAdmin(admin.ModelAdmin):
    list_display = ("id", "job", "label", "created_at")
    search_fields = ("label", "job__pole_number")
