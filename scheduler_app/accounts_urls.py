""" Accounts URLs for the scheduler app. """

from __future__ import annotations

from django.contrib.auth import views as auth_views
from django.urls import path

from scheduler_app.views_auth import ThrottledLoginView

urlpatterns = [
    path("login/", ThrottledLoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
]
