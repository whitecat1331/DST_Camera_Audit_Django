from django.urls import path

from audits import review_views, views

urlpatterns = [
    path("", views.audit_list, name="audit_list"),
    path("dst/", views.dst_audit_page, name="dst_audit"),
    path("dst/start/", views.start_dst_audit, name="dst_audit_start"),
    path("dst/recent/", views.dst_recent_audits, name="dst_recent_audits"),
    path("dst/publish/", views.publish_dst, name="dst_publish"),
    path("dst/schedule/", views.schedule_dst_audit, name="dst_schedule"),
    path("dst/schedule/status/", views.schedule_status, name="dst_schedule_status"),
    path(
        "dst/schedule/<int:pk>/<str:action>/",
        views.schedule_action,
        name="dst_schedule_action",
    ),
    path("review/", review_views.review_round, name="review_round"),
    path("review/<int:pk>/", review_views.review_round, name="review_round_pk"),
    path("review/<int:pk>/item/", review_views.review_update, name="review_update"),
    path("review/<int:pk>/events/", review_views.review_events, name="review_events"),
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
    path(
        "rejection-report/",
        views.rejection_report_page,
        name="rejection_report",
    ),
    path(
        "rejection-report/preview/",
        views.preview_rejection_report,
        name="rejection_report_preview",
    ),
    path(
        "rejection-report/start/",
        views.start_rejection_report,
        name="rejection_report_start",
    ),
    path(
        "rejection-report/<int:pk>/download/",
        views.download_rejection_report,
        name="rejection_report_download",
    ),
    path("<int:pk>/", views.audit_detail, name="audit_detail"),
    path("<int:pk>/status/", views.audit_job_status, name="audit_job_status"),
    path("<int:pk>/cancel/", views.cancel_audit_job, name="audit_cancel"),
]
