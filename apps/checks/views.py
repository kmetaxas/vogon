# pyright: reportAttributeAccessIssue=false, reportCallIssue=false

import json

from django.db.models import Q
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views import View

from apps.checks.models import (
    Check,
    CheckExecution,
    CheckReceiver,
    CheckVersion,
    ReceiverEvent,
)
from apps.core.mixins import OrganizationRequiredMixin
from apps.llm.models import LLMProvider
from apps.notifications.models import NotificationPolicy
from apps.personalities.models import Personality


def _personality_queryset(organization):
    return Personality.objects.filter(
        Q(organization=organization) | Q(scope=Personality.Scope.SYSTEM, organization__isnull=True)
    ).order_by("name")


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
        receivers = CheckReceiver.objects.filter(check=check)
        context = {
            "check": check,
            "executions": executions,
            "receivers": receivers,
        }
        return render(request, "checks/check_detail.html", context)


class CheckCreateView(OrganizationRequiredMixin, View):
    def get(self, request):
        context = {
            "organization": self.organization,
            "schedule_types": Check.ScheduleType.choices,
            "personalities": _personality_queryset(self.organization),
            "notification_policies": NotificationPolicy.objects.filter(
                organization=self.organization
            ).order_by("name"),
        }
        return render(request, "checks/check_form.html", context)

    def post(self, request):
        notification_config_raw = request.POST.get("notification_config", "{}").strip()

        try:
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
                "notification_policy_name": request.POST.get("notification_policy_name", ""),
            }
            return render(
                request,
                "checks/check_form.html",
                {
                    "organization": self.organization,
                    "schedule_types": Check.ScheduleType.choices,
                    "personalities": _personality_queryset(self.organization),
                    "notification_policies": NotificationPolicy.objects.filter(
                        organization=self.organization
                    ).order_by("name"),
                    "check": check,
                    "error": "Invalid JSON in config fields.",
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
            notification_config=notification_config,
            notification_policy_name=request.POST.get("notification_policy_name", "").strip(),
            personality_id=request.POST.get("personality") or None,
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
            "personalities": _personality_queryset(self.organization),
            "notification_policies": NotificationPolicy.objects.filter(
                organization=self.organization
            ).order_by("name"),
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
        check.notification_policy_name = request.POST.get(
            "notification_policy_name", check.notification_policy_name
        ).strip()
        check.personality_id = request.POST.get("personality") or None

        notification_config_raw = request.POST.get("notification_config", "{}").strip()

        try:
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
                    "personalities": _personality_queryset(self.organization),
                    "notification_policies": NotificationPolicy.objects.filter(
                        organization=self.organization
                    ).order_by("name"),
                    "error": "Invalid JSON in config fields.",
                    "notification_config_raw": notification_config_raw,
                },
            )

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


class CheckDeleteView(OrganizationRequiredMixin, View):
    def post(self, request, check_id):
        check = get_object_or_404(Check, id=check_id, organization=self.organization)
        try:
            import asyncio

            from services.checks.scheduler import CheckScheduler

            asyncio.run(CheckScheduler.delete_schedule(str(check.id)))
        except Exception:
            pass
        check.delete()
        return HttpResponseRedirect(reverse("checks:check-list"))


class CheckExecutionDetailView(OrganizationRequiredMixin, View):
    def get(self, request, check_id, execution_id):
        check = get_object_or_404(Check, id=check_id, organization=self.organization)
        execution = get_object_or_404(CheckExecution, id=execution_id, check=check)
        context = {
            "check": check,
            "execution": execution,
        }
        return render(request, "checks/check_execution_detail.html", context)


class ReceiverListView(OrganizationRequiredMixin, View):
    def get(self, request):
        receivers = (
            CheckReceiver.objects.filter(organization=self.organization)
            .select_related("check")
            .order_by("-created_at")
        )
        context = {"receivers": receivers, "organization": self.organization}
        return render(request, "checks/receiver_list.html", context)


class ReceiverCreateView(OrganizationRequiredMixin, View):
    def get(self, request):
        context = {
            "organization": self.organization,
            "checks": Check.objects.filter(organization=self.organization),
            "llm_providers": LLMProvider.objects.filter(
                organization=self.organization, enabled=True, is_jev=True
            ).order_by("name"),
        }
        return render(request, "checks/receiver_form.html", context)

    def post(self, request):
        check = get_object_or_404(
            Check, id=request.POST.get("check_id"), organization=self.organization
        )
        receiver = CheckReceiver.objects.create(
            organization=self.organization,
            check=check,
            name=request.POST.get("name", "Unnamed Receiver"),
            source_type=request.POST.get("source_type", "alertmanager"),
            enabled=request.POST.get("enabled") == "on",
            admission_mode=request.POST.get("admission_mode", "always"),
            admission_llm_provider_id=request.POST.get("admission_llm_provider") or None,
            gating_prompt=request.POST.get("gating_prompt", ""),
            max_active_executions=int(request.POST.get("max_active_executions", "1")),
            dedup_window_seconds=int(request.POST.get("dedup_window_seconds", "300")),
            fail_open_on_timeout=request.POST.get("fail_open_on_timeout") == "on",
        )
        secret = receiver.generate_secret()
        # Store in session so it can be displayed once on the detail page
        request.session[f"receiver_secret_{receiver.id}"] = secret
        return HttpResponseRedirect(
            reverse("checks:receiver-detail", kwargs={"receiver_id": receiver.id})
            + "?created=1&show_secret=1"
        )


class ReceiverDetailView(OrganizationRequiredMixin, View):
    def get(self, request, receiver_id):
        receiver = get_object_or_404(CheckReceiver, id=receiver_id, organization=self.organization)
        events = ReceiverEvent.objects.filter(receiver=receiver).order_by("-created_at")[:20]
        show_secret = request.GET.get("show_secret")
        secret = None
        if show_secret:
            secret_key = f"receiver_secret_{receiver.id}"
            secret = request.session.pop(secret_key, None)  # Remove after reading (show once only)
        context = {
            "receiver": receiver,
            "events": events,
            "created": request.GET.get("created"),
            "secret": secret,
        }
        return render(request, "checks/receiver_detail.html", context)


class ReceiverEditView(OrganizationRequiredMixin, View):
    def get(self, request, receiver_id):
        receiver = get_object_or_404(CheckReceiver, id=receiver_id, organization=self.organization)
        context = {
            "receiver": receiver,
            "organization": self.organization,
            "checks": Check.objects.filter(organization=self.organization),
            "llm_providers": LLMProvider.objects.filter(
                organization=self.organization, enabled=True, is_jev=True
            ).order_by("name"),
        }
        return render(request, "checks/receiver_form.html", context)

    def post(self, request, receiver_id):
        receiver = get_object_or_404(CheckReceiver, id=receiver_id, organization=self.organization)
        receiver.name = request.POST.get("name", receiver.name)
        receiver.source_type = request.POST.get("source_type", receiver.source_type)
        receiver.enabled = request.POST.get("enabled") == "on"
        receiver.admission_mode = request.POST.get("admission_mode", receiver.admission_mode)
        receiver.admission_llm_provider_id = request.POST.get("admission_llm_provider") or None
        receiver.gating_prompt = request.POST.get("gating_prompt", receiver.gating_prompt)
        receiver.max_active_executions = int(
            request.POST.get("max_active_executions", str(receiver.max_active_executions))
        )
        receiver.dedup_window_seconds = int(
            request.POST.get("dedup_window_seconds", str(receiver.dedup_window_seconds))
        )
        receiver.fail_open_on_timeout = request.POST.get("fail_open_on_timeout") == "on"
        receiver.save()
        return HttpResponseRedirect(
            reverse("checks:receiver-detail", kwargs={"receiver_id": receiver.id})
        )


class ReceiverDeleteView(OrganizationRequiredMixin, View):
    def post(self, request, receiver_id):
        receiver = get_object_or_404(CheckReceiver, id=receiver_id, organization=self.organization)
        receiver.delete()
        return HttpResponseRedirect(reverse("checks:receiver-list"))


class ReceiverRegenerateSecretView(OrganizationRequiredMixin, View):
    def post(self, request, receiver_id):
        receiver = get_object_or_404(CheckReceiver, id=receiver_id, organization=self.organization)
        secret = receiver.generate_secret()
        request.session[f"receiver_secret_{receiver.id}"] = secret
        return HttpResponseRedirect(
            reverse("checks:receiver-detail", kwargs={"receiver_id": receiver.id})
            + "?regenerated=1&show_secret=1"
        )


class ReceiverToggleEnabledView(OrganizationRequiredMixin, View):
    def post(self, request, receiver_id):
        receiver = get_object_or_404(CheckReceiver, id=receiver_id, organization=self.organization)
        receiver.enabled = not receiver.enabled
        receiver.save()
        return HttpResponseRedirect(reverse("checks:receiver-list"))
