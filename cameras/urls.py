from django.urls import path

from cameras import views

urlpatterns = [
    path("", views.camera_list, name="camera_list"),
    path("<int:pk>/", views.camera_detail, name="camera_detail"),
]
