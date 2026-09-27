# pyright: reportAttributeAccessIssue=false

from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views import View

from apps.checks.models import Check, CheckExecution, CheckVersion
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
        check = Check.objects.create(
            organization=self.organization,
            name=request.POST.get("name", "Unnamed Check"),
            description=request.POST.get("description", ""),
            schedule_type=request.POST.get("schedule_type", Check.ScheduleType.INTERVAL),
            schedule_expression=request.POST.get("schedule_expression", "60"),
            timezone=request.POST.get("timezone", "UTC"),
            execution_mode=request.POST.get("execution_mode", Check.ExecutionMode.DETERMINISTIC),
            evaluation_config={},
            notification_config={},
            created_by=request.user,
        )
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
        check.schedule_type = request.POST.get("schedule_type", check.schedule_type)
        check.schedule_expression = request.POST.get(
            "schedule_expression", check.schedule_expression
        )
        check.timezone = request.POST.get("timezone", check.timezone)
        check.execution_mode = request.POST.get("execution_mode", check.execution_mode)
        check.save()
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
