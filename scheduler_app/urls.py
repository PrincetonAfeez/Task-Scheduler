""" URLs for the scheduler app. """

from django.urls import path

from . import views

app_name = "scheduler_app"

urlpatterns = [
    path("", views.home, name="home"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("healthz", views.healthz, name="healthz"),
    path("readyz", views.readyz, name="readyz"),
    path("fragments/summary", views.summary_fragment, name="summary_fragment"),
    path("fragments/queue", views.queue_fragment, name="queue_fragment"),
    path("fragments/health", views.health_fragment, name="health_fragment"),
    path("fragments/activity", views.activity_fragment, name="activity_fragment"),
    path("fragments/upcoming", views.upcoming_fragment, name="upcoming_fragment"),
    path("fragments/jobs", views.jobs_fragment, name="jobs_fragment"),
    path("jobs/", views.job_list, name="job_list"),
    path("jobs/new/", views.job_create, name="job_create"),
    path("jobs/<int:job_id>/", views.job_detail, name="job_detail"),
    path("jobs/<int:job_id>/edit/", views.job_edit, name="job_edit"),
    path("jobs/<int:job_id>/delete/", views.job_delete, name="job_delete"),
    path("jobs/<int:job_id>/<str:action>/", views.job_action, name="job_action"),
    path("executions/", views.execution_list, name="execution_list"),
    path("executions/<int:execution_id>/", views.execution_detail, name="execution_detail"),
    path("executions/<int:execution_id>/<str:action>/", views.execution_action, name="execution_action"),
    path("alerts/", views.alert_list, name="alert_list"),
    path("alerts/resolve/", views.alert_resolve_bulk, name="alert_resolve_bulk"),
    path("alerts/<int:alert_id>/resolve/", views.alert_resolve, name="alert_resolve"),
    path("health/", views.health_view, name="health"),
]
