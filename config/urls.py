from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from cameras import auth_views, views as camera_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/login/", auth_views.login_view, name="login"),
    path("accounts/ims/start/", auth_views.ims_login_start, name="ims_login_start"),
    path("accounts/ims/callback/", auth_views.ims_callback, name="ims_callback"),
    path("accounts/logout/", auth_views.logout_view, name="logout"),
    path("", camera_views.dashboard, name="dashboard"),
    path("cameras/", include("cameras.urls")),
    path("audits/", include("audits.urls")),
    path("map/", camera_views.camera_map, name="map"),
    path("sync/", camera_views.sync_installations_view, name="sync_installations"),
    path(
        "dragoneye/teamviewer-ids/",
        camera_views.upload_dragoneye_tv_csv,
        name="upload_dragoneye_tv_csv",
    ),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
