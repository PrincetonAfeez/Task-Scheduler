""" Auth views for the scheduler app. """

from __future__ import annotations

from django.contrib.auth import views as auth_views
from django.http import HttpRequest, HttpResponse
from django.urls import reverse_lazy

from scheduler_app.login_throttle import clear_login_failures, is_login_blocked, record_login_failure


class ThrottledLoginView(auth_views.LoginView):
    template_name = "registration/login.html"
    redirect_authenticated_user = True

    def get_success_url(self) -> str:
        return str(self.get_redirect_url() or reverse_lazy("scheduler_app:dashboard"))

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        username = request.POST.get("username", "")
        ip = request.META.get("REMOTE_ADDR", "unknown")
        if username and is_login_blocked(username=username, ip=ip):
            form = self.get_form()
            form.add_error(None, "Too many failed sign-in attempts. Try again later.")
            return self.form_invalid(form)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        username = form.cleaned_data.get("username", "")
        ip = self.request.META.get("REMOTE_ADDR", "unknown")
        if username:
            clear_login_failures(username=username, ip=ip)
        return super().form_valid(form)

    def form_invalid(self, form):
        username = self.request.POST.get("username", "")
        ip = self.request.META.get("REMOTE_ADDR", "unknown")
        if username:
            record_login_failure(username=username, ip=ip)
        return super().form_invalid(form)
