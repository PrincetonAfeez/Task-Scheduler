""" Forms for the scheduler app. """

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django import forms
from django.core.exceptions import ValidationError

from scheduler_app.models import Job, ScheduleType
from scheduler_app.services.clock import SystemClock
from scheduler_app.services.claiming import cancel_queued_executions_for_job
from scheduler_app.services.job_schedule import apply_next_run_after_edit, schedule_fields_changed
from scheduler_app.services.schedules import initial_next_run
from scheduler_app.services.task_config import validate_task_config
from scheduler_app.tasks.registry import registered_tasks


class JobForm(forms.ModelForm):
    registered_task_name = forms.ChoiceField(choices=[])

    class Meta:
        model = Job
        fields = [
            "name",
            "description",
            "registered_task_name",
            "task_config",
            "schedule_type",
            "schedule_value",
            "timezone",
            "enabled",
            "overlap_policy",
            "misfire_policy",
            "misfire_grace_seconds",
            "max_attempts",
            "retry_backoff_seconds",
            "timeout_seconds",
            "retention_count",
            "retention_days",
            "alert_mode",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "task_config": forms.Textarea(attrs={"rows": 5}),
            "schedule_value": forms.Textarea(attrs={"rows": 5}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        tasks = registered_tasks()
        self.fields["registered_task_name"].choices = [
            (name, f"{name} - {spec.description}") for name, spec in sorted(tasks.items())
        ]

    def clean_name(self) -> str:
        value = self.cleaned_data["name"]
        queryset = Job.objects.filter(name=value)
        if self.instance.pk:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise ValidationError("A job with this name already exists.")
        return value

    def clean_registered_task_name(self) -> str:
        value = self.cleaned_data["registered_task_name"]
        if value not in registered_tasks():
            raise ValidationError("Choose a registered task from the catalog.")
        return value

    def clean_task_config(self) -> dict:
        task_name = self.cleaned_data.get("registered_task_name") or (
            self.instance.registered_task_name if self.instance.pk else ""
        )
        if not task_name:
            return self.cleaned_data.get("task_config") or {}
        try:
            return validate_task_config(task_name, self.cleaned_data.get("task_config") or {})
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

    def clean_timezone(self) -> str:
        value = (self.cleaned_data.get("timezone") or "UTC").strip()
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise forms.ValidationError(
                "Enter a valid IANA timezone name (for example UTC or America/New_York)."
            ) from exc
        return value

    def clean(self):
        cleaned = super().clean()
        schedule_type = cleaned.get("schedule_type")
        schedule_value = cleaned.get("schedule_value")
        timezone_name = cleaned.get("timezone") or "UTC"
        if schedule_type and schedule_value:
            try:
                initial_next_run(
                    schedule_type,
                    schedule_value,
                    now=SystemClock().now(),
                    timezone_name=timezone_name,
                )
            except Exception as exc:  # noqa: BLE001 - validation should surface parser errors
                raise ValidationError({"schedule_value": str(exc)}) from exc
        return cleaned

    def save(self, commit: bool = True) -> Job:
        was_enabled = self.instance.enabled if self.instance.pk else False
        previous = Job.objects.filter(pk=self.instance.pk).first() if self.instance.pk else None
        job = super().save(commit=False)
        re_enabled = bool(self.instance.pk and "enabled" in self.changed_data and job.enabled and not was_enabled)
        schedule_changed = schedule_fields_changed(self.changed_data) or not self.instance.pk
        now = SystemClock().now()
        try:
            apply_next_run_after_edit(
                job,
                now=now,
                schedule_changed=schedule_changed,
                re_enabled=re_enabled,
                previous=previous,
            )
        except ValueError as exc:
            if job.schedule_type == ScheduleType.ONE_TIME and str(exc).startswith("This one-time job"):
                raise ValidationError({"enabled": str(exc)}) from exc
            raise ValidationError(str(exc)) from exc
        if commit:
            job.save()
            self.save_m2m()
            if not job.enabled:
                cancel_queued_executions_for_job(job, reason="job disabled")
        return job
