import json
import logging

from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views import View

from apps.core.mixins import OrganizationRequiredMixin
from apps.infradesigns.models import InfrastructureDesign

logger = logging.getLogger(__name__)


class InfraDesignListView(OrganizationRequiredMixin, View):
    def get(self, request):
        designs = InfrastructureDesign.objects.filter(organization=self.organization).order_by(
            "-created_at"
        )
        if request.headers.get("HX-Request") or request.GET.get("format") == "picker":
            return render(request, "infradesigns/_design_picker.html", {"designs": designs})
        return render(request, "infradesigns/design_list.html", {"designs": designs})


class InfraDesignDetailView(OrganizationRequiredMixin, View):
    def get(self, request, design_id):
        design = get_object_or_404(
            InfrastructureDesign,
            id=design_id,
            organization=self.organization,
        )
        return render(request, "infradesigns/design_detail.html", {"design": design})


class InfraDesignCreateView(OrganizationRequiredMixin, View):
    def get(self, request):
        context = {
            "is_create": True,
            "environment_choices": InfrastructureDesign.Environment.choices,
        }
        return render(request, "infradesigns/design_form.html", context)

    def post(self, request):
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()
        environment = request.POST.get("environment", InfrastructureDesign.Environment.PRODUCTION)
        mermaid_topology = request.POST.get("mermaid_topology", "").strip()
        marvin_selector_raw = request.POST.get("marvin_selector", "{}").strip()

        if not name:
            return render(
                request,
                "infradesigns/design_form.html",
                {
                    "error": "Name is required.",
                    "is_create": True,
                    "environment_choices": InfrastructureDesign.Environment.choices,
                },
            )

        try:
            marvin_selector = json.loads(marvin_selector_raw) if marvin_selector_raw else {}
        except json.JSONDecodeError:
            return render(
                request,
                "infradesigns/design_form.html",
                {
                    "error": "Marvin selector must be valid JSON.",
                    "is_create": True,
                    "environment_choices": InfrastructureDesign.Environment.choices,
                },
            )

        design = InfrastructureDesign.objects.create(
            organization=self.organization,
            name=name,
            description=description,
            environment=environment,
            mermaid_topology=mermaid_topology,
            marvin_selector=marvin_selector,
            created_by=request.user,
        )
        logger.info(
            "Created infrastructure design %s for org %s", design.id, self.organization.slug
        )
        return HttpResponseRedirect(
            reverse("infradesigns:design-detail", kwargs={"design_id": design.id})
        )


class InfraDesignEditView(OrganizationRequiredMixin, View):
    def get(self, request, design_id):
        design = get_object_or_404(
            InfrastructureDesign,
            id=design_id,
            organization=self.organization,
        )
        context = {
            "design": design,
            "is_create": False,
            "environment_choices": InfrastructureDesign.Environment.choices,
        }
        return render(request, "infradesigns/design_form.html", context)

    def post(self, request, design_id):
        design = get_object_or_404(
            InfrastructureDesign,
            id=design_id,
            organization=self.organization,
        )
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()
        environment = request.POST.get("environment", InfrastructureDesign.Environment.PRODUCTION)
        mermaid_topology = request.POST.get("mermaid_topology", "").strip()
        marvin_selector_raw = request.POST.get("marvin_selector", "{}").strip()

        if not name:
            return render(
                request,
                "infradesigns/design_form.html",
                {
                    "error": "Name is required.",
                    "design": design,
                    "is_create": False,
                    "environment_choices": InfrastructureDesign.Environment.choices,
                },
            )

        try:
            marvin_selector = json.loads(marvin_selector_raw) if marvin_selector_raw else {}
        except json.JSONDecodeError:
            return render(
                request,
                "infradesigns/design_form.html",
                {
                    "error": "Marvin selector must be valid JSON.",
                    "design": design,
                    "is_create": False,
                    "environment_choices": InfrastructureDesign.Environment.choices,
                },
            )

        design.name = name
        design.description = description
        design.environment = environment
        design.mermaid_topology = mermaid_topology
        design.marvin_selector = marvin_selector
        design.save()
        return HttpResponseRedirect(
            reverse("infradesigns:design-detail", kwargs={"design_id": design.id})
        )
