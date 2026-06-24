""" URLs for the scheduler app. """

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("scheduler_app.accounts_urls")),
    path("", include("scheduler_app.urls")),
]

