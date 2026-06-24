""" Test auth for the scheduler app. """

from __future__ import annotations

import pytest
from django.test import override_settings
from django.urls import reverse


@pytest.mark.django_db
@override_settings(WEBUI_AUTH_ENABLED=True)
def test_mutating_job_create_requires_login(client):
    response = client.get(reverse("scheduler_app:job_create"))
    assert response.status_code == 302
    assert "/accounts/login/" in response.url


@pytest.mark.django_db
@override_settings(WEBUI_AUTH_ENABLED=True)
def test_authenticated_user_can_open_job_create(auth_client):
    assert auth_client.get(reverse("scheduler_app:job_create")).status_code == 200


@pytest.mark.django_db
@override_settings(WEBUI_AUTH_ENABLED=True)
def test_alert_resolve_requires_login(client, db):
    from scheduler_app.models import Alert

    alert = Alert.objects.create(message="needs resolve")
    response = client.post(reverse("scheduler_app:alert_resolve", args=[alert.id]))
    assert response.status_code == 302
    assert "/accounts/login/" in response.url


@pytest.mark.django_db
@override_settings(WEBUI_AUTH_ENABLED=True)
def test_authenticated_user_can_resolve_alert(auth_client):
    from scheduler_app.models import Alert

    alert = Alert.objects.create(message="needs resolve")
    response = auth_client.post(reverse("scheduler_app:alert_resolve", args=[alert.id]))
    assert response.status_code == 302
    alert.refresh_from_db()
    assert alert.resolved is True


@pytest.mark.django_db
def test_logout_post_clears_session(auth_client):
    assert auth_client.session.get("_auth_user_id")
    response = auth_client.post(reverse("logout"))
    assert response.status_code == 302
    assert "_auth_user_id" not in auth_client.session


@pytest.mark.django_db
def test_logout_get_is_not_allowed(client, django_user_model):
    django_user_model.objects.create_user(username="operator", password="secret")
    client.login(username="operator", password="secret")
    response = client.get(reverse("logout"))
    assert response.status_code == 405
