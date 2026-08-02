from django.urls import path

from cameras import views

urlpatterns = [
    path("", views.camera_list, name="camera_list"),
    path("teamviewer-id/", views.update_teamviewer_id, name="update_teamviewer_id"),
    path("<int:pk>/", views.camera_detail, name="camera_detail"),
]
