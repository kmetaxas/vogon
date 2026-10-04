# pyright: reportAttributeAccessIssue=false

import json

from django.core.exceptions import ValidationError
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views import View

from apps.checks.models import (
    Check,
    CheckExecution,
    CheckVersion,
    validate_capability_selectors,
)
from apps.core.mixins import OrganizationRequiredMixin


class CheckListView(OrganizationRequiredMixin, View):
    def get(self, request):
        checks = Check.objects.filter(organization=self.organization).order_by("-created_at")
        for check in checks:
            check.last_execution = CheckExecution.last_for_check(str(check.id))
        context = {"checks": checks, "organization": self.organization}
        return render(request, "checks/check_list.html", context)


class CheckDetailView(OrganizationRequiredMixin, View):
    def get(self, request, check_id):
        check = get_object_or_404(Check, id=check_id, organization=self.organization)
        executions = CheckExecution.objects.filter(check=check).order_by("-triggered_at")[:20]
        health_state = getattr(check, "health_state", None)
        context = {
            "check": check,
            "executions": executions,
            "health_state": health_state,
        }
        return render(request, "checks/check_detail.html", context)


class CheckCreateView(OrganizationRequiredMixin, View):
    def get(self, request):
        context = {
            "organization": self.organization,
            "schedule_types": Check.ScheduleType.choices,
            "execution_modes": Check.ExecutionMode.choices,
        }
        return render(request, "checks/check_form.html", context)

    def post(self, request):
        evaluation_config_raw = request.POST.get("evaluation_config", "{}").strip()
        notification_config_raw = request.POST.get("notification_config", "{}").strip()

        try:
            evaluation_config = json.loads(evaluation_config_raw) if evaluation_config_raw else {}
            notification_config = (
                json.loads(notification_config_raw) if notification_config_raw else {}
            )
        except json.JSONDecodeError:
            check = {
                "name": request.POST.get("name", ""),
                "description": request.POST.get("description", ""),
                "instructions": request.POST.get("instructions", ""),
                "schedule_type": request.POST.get("schedule_type", Check.ScheduleType.INTERVAL),
                "schedule_expression": request.POST.get("schedule_expression", "60"),
                "timezone": request.POST.get("timezone", "UTC"),
                "execution_mode": request.POST.get(
                    "execution_mode", Check.ExecutionMode.DETERMINISTIC
                ),
            }
            return render(
                request,
                "checks/check_form.html",
                {
                    "organization": self.organization,
                    "schedule_types": Check.ScheduleType.choices,
                    "execution_modes": Check.ExecutionMode.choices,
                    "check": check,
                    "error": "Invalid JSON in config fields.",
                    "evaluation_config_raw": evaluation_config_raw,
                    "notification_config_raw": notification_config_raw,
                },
            )

        try:
            validate_capability_selectors(evaluation_config)
        except ValidationError as exc:
            check = {
                "name": request.POST.get("name", ""),
                "description": request.POST.get("description", ""),
                "instructions": request.POST.get("instructions", ""),
                "schedule_type": request.POST.get("schedule_type", Check.ScheduleType.INTERVAL),
                "schedule_expression": request.POST.get("schedule_expression", "60"),
                "timezone": request.POST.get("timezone", "UTC"),
                "execution_mode": request.POST.get(
                    "execution_mode", Check.ExecutionMode.DETERMINISTIC
                ),
            }
            return render(
                request,
                "checks/check_form.html",
                {
                    "organization": self.organization,
                    "schedule_types": Check.ScheduleType.choices,
                    "execution_modes": Check.ExecutionMode.choices,
                    "check": check,
                    "error": " ".join(exc.messages),
                    "evaluation_config_raw": evaluation_config_raw,
                    "notification_config_raw": notification_config_raw,
                },
            )

        check = Check.objects.create(
            organization=self.organization,
            name=request.POST.get("name", "Unnamed Check"),
            description=request.POST.get("description", ""),
            instructions=request.POST.get("instructions", ""),
            schedule_type=request.POST.get("schedule_type", Check.ScheduleType.INTERVAL),
            schedule_expression=request.POST.get("schedule_expression", "60"),
            timezone=request.POST.get("timezone", "UTC"),
            execution_mode=request.POST.get("execution_mode", Check.ExecutionMode.DETERMINISTIC),
            evaluation_config=evaluation_config,
            notification_config=notification_config,
            created_by=request.user,
        )
        try:
            import asyncio

            from services.checks.scheduler import CheckScheduler

            asyncio.run(CheckScheduler.create_schedule(check))
        except Exception:
            pass  # Temporal may be offline; never fail the request
        CheckVersion.objects.create(check=check, version_number=1, definition_snapshot={})
        return HttpResponseRedirect(reverse("checks:check-detail", kwargs={"check_id": check.id}))


class CheckEditView(OrganizationRequiredMixin, View):
    def get(self, request, check_id):
        check = get_object_or_404(Check, id=check_id, organization=self.organization)
        context = {
            "check": check,
            "schedule_types": Check.ScheduleType.choices,
            "execution_modes": Check.ExecutionMode.choices,
        }
        return render(request, "checks/check_form.html", context)

    def post(self, request, check_id):
        check = get_object_or_404(Check, id=check_id, organization=self.organization)
        check.name = request.POST.get("name", check.name)
        check.description = request.POST.get("description", check.description)
        check.instructions = request.POST.get("instructions", check.instructions)
        check.schedule_type = request.POST.get("schedule_type", check.schedule_type)
        check.schedule_expression = request.POST.get(
            "schedule_expression", check.schedule_expression
        )
        check.timezone = request.POST.get("timezone", check.timezone)
        check.execution_mode = request.POST.get("execution_mode", check.execution_mode)

        evaluation_config_raw = request.POST.get("evaluation_config", "{}").strip()
        notification_config_raw = request.POST.get("notification_config", "{}").strip()

        try:
            evaluation_config = json.loads(evaluation_config_raw) if evaluation_config_raw else {}
            notification_config = (
                json.loads(notification_config_raw) if notification_config_raw else {}
            )
        except json.JSONDecodeError:
            return render(
                request,
                "checks/check_form.html",
                {
                    "check": check,
                    "schedule_types": Check.ScheduleType.choices,
                    "execution_modes": Check.ExecutionMode.choices,
                    "error": "Invalid JSON in config fields.",
                    "evaluation_config_raw": evaluation_config_raw,
                    "notification_config_raw": notification_config_raw,
                },
            )

        try:
            validate_capability_selectors(evaluation_config)
        except ValidationError as exc:
            return render(
                request,
                "checks/check_form.html",
                {
                    "check": check,
                    "schedule_types": Check.ScheduleType.choices,
                    "execution_modes": Check.ExecutionMode.choices,
                    "error": " ".join(exc.messages),
                    "evaluation_config_raw": evaluation_config_raw,
                    "notification_config_raw": notification_config_raw,
                },
            )

        check.evaluation_config = evaluation_config
        check.notification_config = notification_config
        check.save()
        try:
            import asyncio

            from services.checks.scheduler import CheckScheduler

            asyncio.run(CheckScheduler.update_schedule(check))
        except Exception:
            pass  # Temporal may be offline; never fail the request
        CheckVersion.objects.create(
            check=check,
            version_number=(CheckVersion.objects.filter(check=check).count() + 1),
            definition_snapshot={
                "name": check.name,
                "description": check.description,
                "schedule_type": check.schedule_type,
                "schedule_expression": check.schedule_expression,
                "execution_mode": check.execution_mode,
            },
        )
        return HttpResponseRedirect(reverse("checks:check-detail", kwargs={"check_id": check.id}))


class CheckToggleView(OrganizationRequiredMixin, View):
    def post(self, request, check_id):
        check = get_object_or_404(Check, id=check_id, organization=self.organization)
        check.enabled = not check.enabled
        check.save()
        try:
            import asyncio

            from services.checks.scheduler import CheckScheduler

            if check.enabled:
                asyncio.run(CheckScheduler.resume_schedule(str(check.id)))
            else:
                asyncio.run(CheckScheduler.pause_schedule(str(check.id)))
        except Exception:
            pass  # Temporal may be offline; never fail the request
        hx_target = request.META.get("HTTP_HX_TARGET", "")
        if hx_target == "detail-actions" or hx_target.startswith("#detail-actions"):
            return render(request, "checks/_check_detail_actions.html", {"check": check})
        return render(request, "checks/_check_row.html", {"check": check})


class CheckTriggerView(OrganizationRequiredMixin, View):
    def post(self, request, check_id):
        check = get_object_or_404(Check, id=check_id, organization=self.organization)
        try:
            import asyncio

            from services.checks.scheduler import CheckScheduler

            result = asyncio.run(CheckScheduler.trigger_now(check))
            return render(
                request,
                "checks/_action_result.html",
                {"check": check, "message": f"Triggered: {result}"},
            )
        except Exception as exc:
            return render(
                request,
                "checks/_action_result.html",
                {"check": check, "message": f"Error: {exc}", "error": True},
            )


class CheckDryRunView(OrganizationRequiredMixin, View):
    def post(self, request, check_id):
        check = get_object_or_404(Check, id=check_id, organization=self.organization)
        try:
            import asyncio

            from services.checks.scheduler import CheckScheduler

            result = asyncio.run(CheckScheduler.trigger_now(check, dry_run=True))
            return render(
                request,
                "checks/_action_result.html",
                {"check": check, "message": f"Dry run: {result}"},
            )
        except Exception as exc:
            return render(
                request,
                "checks/_action_result.html",
                {"check": check, "message": f"Error: {exc}", "error": True},
            )


class CheckExecutionDetailView(OrganizationRequiredMixin, View):
    def get(self, request, check_id, execution_id):
        check = get_object_or_404(Check, id=check_id, organization=self.organization)
        execution = get_object_or_404(CheckExecution, id=execution_id, check=check)
        context = {
            "check": check,
            "execution": execution,
        }
        return render(request, "checks/check_execution_detail.html", context)
