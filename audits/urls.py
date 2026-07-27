from django.urls import path

from audits import views

urlpatterns = [
    path("", views.audit_list, name="audit_list"),
    path("start/", views.audit_start, name="audit_start"),
    path("capture-pole/", views.capture_pole, name="capture_pole"),
    path("turn-relays/", views.turn_relays, name="turn_relays"),
    path("<int:pk>/", views.audit_detail, name="audit_detail"),
    path("<int:pk>/status/", views.audit_job_status, name="audit_job_status"),
]
