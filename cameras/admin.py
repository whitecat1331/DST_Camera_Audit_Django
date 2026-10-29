from django.contrib import admin

from cameras.models import (
    DragonEyeTeamViewerId,
    Installation,
    SyncState,
    UserProfile,
)


@admin.register(Installation)
class InstallationAdmin(admin.ModelAdmin):
    list_display = (
        "ims_id",
        "identifier",
        "pole_number",
        "primary_platform",
        "state",
        "agency",
        "status",
        "is_active",
    )
    list_filter = ("primary_platform", "state", "agency", "status", "is_active")
    search_fields = ("identifier", "pole_number", "serial_number", "fl_number", "location")


@admin.register(DragonEyeTeamViewerId)
class DragonEyeTeamViewerIdAdmin(admin.ModelAdmin):
    list_display = (
        "fx_number",
        "lane",
        "teamviewer_id",
        "label",
        "source_filename",
        "updated_at",
    )
    list_filter = ("lane",)
    search_fields = ("fx_number", "teamviewer_id", "label")


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "ims_role", "updated_at")
    search_fields = ("user__username", "ims_role")


@admin.register(SyncState)
class SyncStateAdmin(admin.ModelAdmin):
    list_display = ("key", "value", "updated_at")
