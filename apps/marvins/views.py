import json

from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from apps.core.mixins import OrganizationRequiredMixin
from apps.marvins.models import Marvin, MarvinConfig, Resource, ResourceType


def _resource_type_schemas() -> dict[str, dict]:
    """Return a mapping of resource type id -> config_json_schema."""
    return {str(rt.id): rt.config_json_schema for rt in ResourceType.objects.all()}


class MarvinListView(OrganizationRequiredMixin, View):
    def get(self, request):
        marvins = self.organization.marvins.all()
        context = {
            "marvins": marvins,
        }
        return render(request, "marvins/marvin_list.html", context)


class MarvinDetailView(OrganizationRequiredMixin, View):
    def get(self, request, marvin_id):
        marvin = get_object_or_404(
            Marvin,
            id=marvin_id,
            organization=self.organization,
        )
        capabilities = marvin.capabilities.all()
        recent_tool_calls = marvin.tool_calls.select_related("capability").order_by("-created_at")[
            :20
        ]
        pending_tool_calls = (
            marvin.tool_calls.select_related("capability")
            .filter(status__in=["pending", "in_progress"])
            .order_by("-created_at")
        )
        attached_resources = marvin.attached_resources.select_related("resource_type").all()
        available_resources = Resource.objects.filter(
            organization=self.organization,
            enabled=True,
        ).exclude(id__in=attached_resources.values_list("id", flat=True))
        overridden_caps = set()
        if hasattr(marvin, "online_config") and marvin.online_config.capability_overrides:
            overridden_caps = {
                name for name, cfg in marvin.online_config.capability_overrides.items() if cfg
            }

        context = {
            "marvin": marvin,
            "capabilities": capabilities,
            "recent_tool_calls": recent_tool_calls,
            "pending_tool_calls": pending_tool_calls,
            "attached_resources": attached_resources,
            "available_resources": available_resources,
            "overridden_capabilities": overridden_caps,
        }
        return render(request, "marvins/marvin_detail.html", context)


class ResourceListView(OrganizationRequiredMixin, View):
    def get(self, request):
        resources = Resource.objects.filter(organization=self.organization).select_related(
            "resource_type"
        )
        context = {
            "resources": resources,
        }
        return render(request, "marvins/resource_list.html", context)


class ResourceCreateView(OrganizationRequiredMixin, View):
    def get(self, request):
        resource_types = ResourceType.objects.all()
        context = {
            "resource_types": resource_types,
            "is_create": True,
            "resource_type_schemas": _resource_type_schemas(),
        }
        return render(request, "marvins/resource_form.html", context)

    def post(self, request):
        name = request.POST.get("name", "").strip()
        resource_type_id = request.POST.get("resource_type", "").strip()
        description = request.POST.get("description", "").strip()
        config_raw = request.POST.get("config", "{}").strip()
        enabled = request.POST.get("enabled") == "on"

        if not name or not resource_type_id:
            return render(
                request,
                "marvins/resource_form.html",
                {
                    "error": "Name and resource type are required.",
                    "resource_types": ResourceType.objects.all(),
                    "is_create": True,
                    "resource_type_schemas": _resource_type_schemas(),
                },
            )

        resource_type = get_object_or_404(ResourceType, id=resource_type_id)
        try:
            config = json.loads(config_raw) if config_raw else {}
        except json.JSONDecodeError:
            return render(
                request,
                "marvins/resource_form.html",
                {
                    "error": "Config must be valid JSON.",
                    "resource_types": ResourceType.objects.all(),
                    "is_create": True,
                    "resource_type_schemas": _resource_type_schemas(),
                },
            )

        Resource.objects.create(
            organization=self.organization,
            name=name,
            resource_type=resource_type,
            description=description,
            config=config,
            enabled=enabled,
        )
        return redirect("marvins:resource-list")


class ResourceEditView(OrganizationRequiredMixin, View):
    def get(self, request, resource_id):
        resource = get_object_or_404(
            Resource,
            id=resource_id,
            organization=self.organization,
        )
        resource_types = ResourceType.objects.all()
        context = {
            "resource": resource,
            "resource_types": resource_types,
            "is_create": False,
            "resource_type_schemas": _resource_type_schemas(),
        }
        return render(request, "marvins/resource_form.html", context)

    def post(self, request, resource_id):
        resource = get_object_or_404(
            Resource,
            id=resource_id,
            organization=self.organization,
        )
        name = request.POST.get("name", "").strip()
        resource_type_id = request.POST.get("resource_type", "").strip()
        description = request.POST.get("description", "").strip()
        config_raw = request.POST.get("config", "{}").strip()
        enabled = request.POST.get("enabled") == "on"

        if not name or not resource_type_id:
            return render(
                request,
                "marvins/resource_form.html",
                {
                    "error": "Name and resource type are required.",
                    "resource": resource,
                    "resource_types": ResourceType.objects.all(),
                    "is_create": False,
                    "resource_type_schemas": _resource_type_schemas(),
                },
            )

        resource_type = get_object_or_404(ResourceType, id=resource_type_id)
        try:
            config = json.loads(config_raw) if config_raw else {}
        except json.JSONDecodeError:
            return render(
                request,
                "marvins/resource_form.html",
                {
                    "error": "Config must be valid JSON.",
                    "resource": resource,
                    "resource_types": ResourceType.objects.all(),
                    "is_create": False,
                    "resource_type_schemas": _resource_type_schemas(),
                },
            )

        resource.name = name
        resource.resource_type = resource_type
        resource.description = description
        resource.config = config
        resource.enabled = enabled
        resource.save()
        return redirect("marvins:resource-list")


class MarvinConfigUpdateView(OrganizationRequiredMixin, View):
    def post(self, request, marvin_id):
        marvin = get_object_or_404(
            Marvin,
            id=marvin_id,
            organization=self.organization,
        )

        online_config, _ = MarvinConfig.objects.get_or_create(marvin=marvin)

        cap_name = request.POST.get("_capability_name", "").strip()
        clear_override = request.POST.get("_clear_override") == "1"

        if cap_name:
            overrides = dict(online_config.capability_overrides or {})
            if clear_override:
                overrides.pop(cap_name, None)
            else:
                cap_override: dict[str, object] = {}
                for key in request.POST:
                    if key.startswith("cap_field_") and not key.startswith("cap_field_type_"):
                        field_name = key[len("cap_field_") :]
                        raw_value = request.POST.get(key, "")
                        type_hint = request.POST.get(f"cap_field_type_{field_name}", "string")

                        if type_hint == "boolean":
                            cap_override[field_name] = raw_value == "true"
                        elif type_hint == "integer":
                            try:
                                cap_override[field_name] = int(raw_value)
                            except ValueError:
                                cap_override[field_name] = 0
                        elif type_hint == "number":
                            try:
                                cap_override[field_name] = float(raw_value)
                            except ValueError:
                                cap_override[field_name] = 0.0
                        else:
                            cap_override[field_name] = raw_value

                overrides[cap_name] = cap_override

            online_config.capability_overrides = overrides
            online_config.save()

            if request.headers.get("HX-Request"):
                return render(
                    request,
                    "marvins/_marvin_config.html",
                    {"marvin": marvin, "capabilities": marvin.capabilities.all()},
                )
            return redirect("marvins:marvin-detail", marvin_id=marvin_id)

        global_config_raw = request.POST.get("global_config", "{}").strip()
        capability_overrides_raw = request.POST.get("capability_overrides", "{}").strip()

        try:
            global_config = json.loads(global_config_raw) if global_config_raw else {}
            capability_overrides = (
                json.loads(capability_overrides_raw) if capability_overrides_raw else {}
            )
        except json.JSONDecodeError:
            return render(
                request,
                "marvins/_marvin_config.html",
                {
                    "marvin": marvin,
                    "capabilities": marvin.capabilities.all(),
                    "error": "Invalid JSON in config fields.",
                },
                status=400,
            )

        online_config.global_config = global_config
        online_config.capability_overrides = capability_overrides
        online_config.save()

        if request.headers.get("HX-Request"):
            return render(
                request,
                "marvins/_marvin_config.html",
                {"marvin": marvin, "capabilities": marvin.capabilities.all()},
            )
        return redirect("marvins:marvin-detail", marvin_id=marvin_id)


class MarvinAttachResourceView(OrganizationRequiredMixin, View):
    def post(self, request, marvin_id):
        marvin = get_object_or_404(
            Marvin,
            id=marvin_id,
            organization=self.organization,
        )
        resource_id = request.POST.get("resource_id", "").strip()
        if resource_id:
            resource = get_object_or_404(
                Resource,
                id=resource_id,
                organization=self.organization,
            )
            marvin.attached_resources.add(resource)

        if request.headers.get("HX-Request"):
            attached_resources = marvin.attached_resources.select_related("resource_type").all()
            available_resources = Resource.objects.filter(
                organization=self.organization,
                enabled=True,
            ).exclude(id__in=attached_resources.values_list("id", flat=True))
            return render(
                request,
                "marvins/_marvin_resources.html",
                {
                    "marvin": marvin,
                    "attached_resources": attached_resources,
                    "available_resources": available_resources,
                },
            )
        return redirect("marvins:marvin-detail", marvin_id=marvin_id)


class MarvinDetachResourceView(OrganizationRequiredMixin, View):
    def post(self, request, marvin_id):
        marvin = get_object_or_404(
            Marvin,
            id=marvin_id,
            organization=self.organization,
        )
        resource_id = request.POST.get("resource_id", "").strip()
        if resource_id:
            resource = get_object_or_404(
                Resource,
                id=resource_id,
                organization=self.organization,
            )
            marvin.attached_resources.remove(resource)

        if request.headers.get("HX-Request"):
            attached_resources = marvin.attached_resources.select_related("resource_type").all()
            available_resources = Resource.objects.filter(
                organization=self.organization,
                enabled=True,
            ).exclude(id__in=attached_resources.values_list("id", flat=True))
            return render(
                request,
                "marvins/_marvin_resources.html",
                {
                    "marvin": marvin,
                    "attached_resources": attached_resources,
                    "available_resources": available_resources,
                },
            )
        return redirect("marvins:marvin-detail", marvin_id=marvin_id)
