from django.urls import path

from audits import views

urlpatterns = [
    path("", views.audit_list, name="audit_list"),
    path("dst/", views.dst_audit_page, name="dst_audit"),
    path("dst/start/", views.start_dst_audit, name="dst_audit_start"),
    path("dst/recent/", views.dst_recent_audits, name="dst_recent_audits"),
    path("confirm-captures/", views.confirm_captures_page, name="confirm_captures"),
    path(
        "confirm-captures/preview/",
        views.preview_confirm_ims_list,
        name="confirm_captures_preview",
    ),
    path(
        "confirm-captures/start/",
        views.start_confirm_captures,
        name="confirm_captures_start",
    ),
    path("start/", views.audit_start, name="audit_start"),
    path("capture-pole/", views.capture_pole, name="capture_pole"),
    path("vbe-daily-check/", views.start_vbe_daily_check, name="vbe_daily_check"),
    path("vbe-daily-checks/", views.start_vbe_daily_checks, name="vbe_daily_checks"),
    path("turn-relays/", views.turn_relays, name="turn_relays"),
    path("turn-ovrc-dcam/", views.turn_ovrc_dcam, name="turn_ovrc_dcam"),
    path("<int:pk>/", views.audit_detail, name="audit_detail"),
    path("<int:pk>/status/", views.audit_job_status, name="audit_job_status"),
    path("<int:pk>/cancel/", views.cancel_audit_job, name="audit_cancel"),
]
