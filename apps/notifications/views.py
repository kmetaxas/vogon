# pyright: reportAttributeAccessIssue=false, reportCallIssue=false

from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views import View

from apps.core.mixins import OrganizationRequiredMixin
from apps.notifications.models import (
    NotificationChannel,
    NotificationPolicy,
    NotificationRoute,
)
from apps.notifications.templatetags.notification_config_tags import get_channel_form
from services.notifications import registry


def _is_htmx(request):
    return bool(request.headers.get("HX-Request"))


def _render_partial_or_full(request, partial_template, full_template, context):
    if _is_htmx(request):
        return render(request, partial_template, context)
    return render(request, full_template, context)


def _parse_schema_fields(request, schema, existing_credentials=None):
    """Build config/credentials dicts from POST data using a provider schema."""
    config = {}
    credentials = {}
    errors = []
    existing_credentials = existing_credentials or {}

    for field_name, prop in schema.get("properties", {}).items():
        is_secret = bool(prop.get("writeOnly"))
        field_type = prop.get("type", "string")
        is_required = field_name in schema.get("required", [])

        if field_type == "array":
            values = request.POST.getlist(f"field_{field_name}_item[]")
            values = [v.strip() for v in values if v.strip()]
            has_existing = is_secret and existing_credentials.get(field_name) is not None
            if is_required and not values and not has_existing:
                errors.append(f"{prop.get('title', field_name)} is required.")
            if values:
                target = credentials if is_secret else config
                target[field_name] = values
            elif has_existing:
                credentials[field_name] = existing_credentials[field_name]
        elif field_type == "boolean":
            value = request.POST.get(f"field_{field_name}") == "1"
            target = credentials if is_secret else config
            target[field_name] = value
        elif field_type in ("integer", "number"):
            raw = request.POST.get(f"field_{field_name}", "").strip()
            has_existing = is_secret and existing_credentials.get(field_name) is not None
            if is_required and not raw and not has_existing:
                errors.append(f"{prop.get('title', field_name)} is required.")
            if raw:
                try:
                    value = int(raw) if field_type == "integer" else float(raw)
                    target = credentials if is_secret else config
                    target[field_name] = value
                except ValueError:
                    errors.append(f"{prop.get('title', field_name)} must be a number.")
            elif has_existing:
                credentials[field_name] = existing_credentials[field_name]
        else:
            raw = request.POST.get(f"field_{field_name}", "").strip()
            has_existing = is_secret and existing_credentials.get(field_name) is not None
            if is_required and not raw and not has_existing:
                errors.append(f"{prop.get('title', field_name)} is required.")
            if raw:
                target = credentials if is_secret else config
                target[field_name] = raw
            elif has_existing:
                credentials[field_name] = existing_credentials[field_name]

    return config, credentials, errors


class NotificationsSettingsView(OrganizationRequiredMixin, View):
    def get(self, request):
        channels = self.organization.notification_channels.all()
        policies = self.organization.notification_policies.prefetch_related(
            "routes", "routes__channel"
        ).all()
        context = {
            "channels": channels,
            "policies": policies,
            "active_tab": "notifications",
        }
        if _is_htmx(request):
            return render(request, "notifications/settings/settings_base.html", context)
        return render(request, "notifications/settings/settings_base.html", context)


class ChannelListView(OrganizationRequiredMixin, View):
    def get(self, request):
        channels = self.organization.notification_channels.all()
        context = {
            "channels": channels,
            "active_tab": "notifications",
        }
        return _render_partial_or_full(
            request,
            "notifications/settings/_channel_list.html",
            "notifications/settings/settings_base.html",
            context,
        )


class ChannelCreateView(OrganizationRequiredMixin, View):
    def get(self, request):
        provider_type = request.GET.get("provider_type", "email")
        form_fields = get_channel_form(provider_type, {}, {})
        context = {
            "provider_type": provider_type,
            "provider_types": NotificationChannel.ProviderType.choices,
            "form_fields": form_fields,
            "active_tab": "notifications",
        }
        return _render_partial_or_full(
            request,
            "notifications/settings/_channel_form.html",
            "notifications/settings/settings_base.html",
            context,
        )

    def post(self, request):
        name = request.POST.get("name", "").strip()
        provider_type = request.POST.get("provider_type", "").strip()
        enabled = request.POST.get("enabled") == "1"

        if not name:
            pt = provider_type or "email"
            return _render_partial_or_full(
                request,
                "notifications/settings/_channel_form.html",
                "notifications/settings/settings_base.html",
                {
                    "error": "Name is required.",
                    "provider_type": pt,
                    "provider_types": NotificationChannel.ProviderType.choices,
                    "form_fields": get_channel_form(pt, {}, {}),
                    "active_tab": "notifications",
                },
            )

        if not provider_type:
            pt = "email"
            return _render_partial_or_full(
                request,
                "notifications/settings/_channel_form.html",
                "notifications/settings/settings_base.html",
                {
                    "error": "Provider type is required.",
                    "provider_type": pt,
                    "provider_types": NotificationChannel.ProviderType.choices,
                    "form_fields": get_channel_form(pt, {}, {}),
                    "active_tab": "notifications",
                },
            )

        try:
            provider_cls = registry.get(provider_type)
        except KeyError:
            return _render_partial_or_full(
                request,
                "notifications/settings/_channel_form.html",
                "notifications/settings/settings_base.html",
                {
                    "error": f"Invalid provider type: {provider_type}",
                    "provider_type": provider_type,
                    "provider_types": NotificationChannel.ProviderType.choices,
                    "form_fields": get_channel_form(provider_type, {}, {}),
                    "active_tab": "notifications",
                },
            )

        schema = provider_cls.config_schema
        config, credentials, errors = _parse_schema_fields(request, schema)

        if errors:
            return _render_partial_or_full(
                request,
                "notifications/settings/_channel_form.html",
                "notifications/settings/settings_base.html",
                {
                    "error": " ".join(errors),
                    "provider_type": provider_type,
                    "provider_types": NotificationChannel.ProviderType.choices,
                    "form_fields": get_channel_form(provider_type, config, credentials),
                    "active_tab": "notifications",
                },
            )

        merged = {**config, **credentials}
        provider = provider_cls()
        validation_errors = provider.validate_config(merged)
        if validation_errors:
            return _render_partial_or_full(
                request,
                "notifications/settings/_channel_form.html",
                "notifications/settings/settings_base.html",
                {
                    "error": "; ".join(validation_errors),
                    "provider_type": provider_type,
                    "provider_types": NotificationChannel.ProviderType.choices,
                    "form_fields": get_channel_form(provider_type, config, credentials),
                    "active_tab": "notifications",
                },
            )

        channel = NotificationChannel.objects.create(
            organization=self.organization,
            name=name,
            provider_type=provider_type,
            config=config,
            enabled=enabled,
        )
        if credentials:
            channel.credentials = credentials
            channel.save(update_fields=["_credentials_encrypted"])

        if _is_htmx(request):
            channels = self.organization.notification_channels.all()
            return render(
                request,
                "notifications/settings/_channel_list.html",
                {"channels": channels},
            )
        return HttpResponseRedirect(reverse("notifications:channel-list"))


class ChannelUpdateView(OrganizationRequiredMixin, View):
    def get(self, request, pk):
        channel = get_object_or_404(NotificationChannel, pk=pk, organization=self.organization)
        provider_type = request.GET.get("provider_type", channel.provider_type)
        form_fields = get_channel_form(provider_type, channel.config, channel.credentials)
        context = {
            "channel": channel,
            "provider_type": provider_type,
            "provider_types": NotificationChannel.ProviderType.choices,
            "form_fields": form_fields,
            "active_tab": "notifications",
        }
        return _render_partial_or_full(
            request,
            "notifications/settings/_channel_form.html",
            "notifications/settings/settings_base.html",
            context,
        )

    def post(self, request, pk):
        channel = get_object_or_404(NotificationChannel, pk=pk, organization=self.organization)
        name = request.POST.get("name", "").strip()
        provider_type = request.POST.get("provider_type", "").strip()
        enabled = request.POST.get("enabled") == "1"

        if not name:
            pt = provider_type or channel.provider_type
            return _render_partial_or_full(
                request,
                "notifications/settings/_channel_form.html",
                "notifications/settings/settings_base.html",
                {
                    "channel": channel,
                    "error": "Name is required.",
                    "provider_type": pt,
                    "provider_types": NotificationChannel.ProviderType.choices,
                    "form_fields": get_channel_form(pt, channel.config, channel.credentials),
                    "active_tab": "notifications",
                },
            )

        if not provider_type:
            return _render_partial_or_full(
                request,
                "notifications/settings/_channel_form.html",
                "notifications/settings/settings_base.html",
                {
                    "channel": channel,
                    "error": "Provider type is required.",
                    "provider_type": channel.provider_type,
                    "provider_types": NotificationChannel.ProviderType.choices,
                    "form_fields": get_channel_form(
                        channel.provider_type, channel.config, channel.credentials
                    ),
                    "active_tab": "notifications",
                },
            )

        try:
            provider_cls = registry.get(provider_type)
        except KeyError:
            return _render_partial_or_full(
                request,
                "notifications/settings/_channel_form.html",
                "notifications/settings/settings_base.html",
                {
                    "channel": channel,
                    "error": f"Invalid provider type: {provider_type}",
                    "provider_type": provider_type,
                    "provider_types": NotificationChannel.ProviderType.choices,
                    "form_fields": get_channel_form(
                        provider_type, channel.config, channel.credentials
                    ),
                    "active_tab": "notifications",
                },
            )

        schema = provider_cls.config_schema
        config, credentials, errors = _parse_schema_fields(
            request, schema, existing_credentials=channel.credentials
        )

        if errors:
            return _render_partial_or_full(
                request,
                "notifications/settings/_channel_form.html",
                "notifications/settings/settings_base.html",
                {
                    "channel": channel,
                    "error": " ".join(errors),
                    "provider_type": provider_type,
                    "provider_types": NotificationChannel.ProviderType.choices,
                    "form_fields": get_channel_form(provider_type, config, credentials),
                    "active_tab": "notifications",
                },
            )

        merged = {**config, **credentials}
        provider = provider_cls()
        validation_errors = provider.validate_config(merged)
        if validation_errors:
            return _render_partial_or_full(
                request,
                "notifications/settings/_channel_form.html",
                "notifications/settings/settings_base.html",
                {
                    "channel": channel,
                    "error": "; ".join(validation_errors),
                    "provider_type": provider_type,
                    "provider_types": NotificationChannel.ProviderType.choices,
                    "form_fields": get_channel_form(provider_type, config, credentials),
                    "active_tab": "notifications",
                },
            )

        channel.name = name
        channel.provider_type = provider_type
        channel.config = config
        channel.enabled = enabled
        channel.save()

        if credentials:
            channel.credentials = credentials
            channel.save(update_fields=["_credentials_encrypted"])

        if _is_htmx(request):
            channels = self.organization.notification_channels.all()
            return render(
                request,
                "notifications/settings/_channel_list.html",
                {"channels": channels},
            )
        return HttpResponseRedirect(reverse("notifications:channel-list"))


class ChannelDeleteView(OrganizationRequiredMixin, View):
    def post(self, request, pk):
        channel = get_object_or_404(NotificationChannel, pk=pk, organization=self.organization)
        channel.delete()
        if _is_htmx(request):
            channels = self.organization.notification_channels.all()
            return render(
                request,
                "notifications/settings/_channel_list.html",
                {"channels": channels},
            )
        return HttpResponseRedirect(reverse("notifications:channel-list"))


class PolicyListView(OrganizationRequiredMixin, View):
    def get(self, request):
        policies = self.organization.notification_policies.all()
        context = {
            "policies": policies,
            "active_tab": "notifications",
        }
        return _render_partial_or_full(
            request,
            "notifications/settings/_policy_list.html",
            "notifications/settings/settings_base.html",
            context,
        )


class PolicyCreateView(OrganizationRequiredMixin, View):
    def get(self, request):
        context = {"active_tab": "notifications"}
        return _render_partial_or_full(
            request,
            "notifications/settings/_policy_form.html",
            "notifications/settings/settings_base.html",
            context,
        )

    def post(self, request):
        name = request.POST.get("name", "").strip()
        is_default = request.POST.get("is_default") == "1"
        dedup_window_seconds = int(request.POST.get("dedup_window_seconds", "300") or "300")

        if not name:
            return _render_partial_or_full(
                request,
                "notifications/settings/_policy_form.html",
                "notifications/settings/settings_base.html",
                {
                    "error": "Name is required.",
                    "active_tab": "notifications",
                },
            )

        NotificationPolicy.objects.create(
            organization=self.organization,
            name=name,
            is_default=is_default,
            dedup_window_seconds=dedup_window_seconds,
        )

        if _is_htmx(request):
            policies = self.organization.notification_policies.all()
            return render(
                request,
                "notifications/settings/_policy_list.html",
                {"policies": policies},
            )
        return HttpResponseRedirect(reverse("notifications:policy-list"))


class PolicyUpdateView(OrganizationRequiredMixin, View):
    def get(self, request, pk):
        policy = get_object_or_404(NotificationPolicy, pk=pk, organization=self.organization)
        context = {
            "policy": policy,
            "active_tab": "notifications",
        }
        return _render_partial_or_full(
            request,
            "notifications/settings/_policy_form.html",
            "notifications/settings/settings_base.html",
            context,
        )

    def post(self, request, pk):
        policy = get_object_or_404(NotificationPolicy, pk=pk, organization=self.organization)
        name = request.POST.get("name", "").strip()
        is_default = request.POST.get("is_default") == "1"
        dedup_window_seconds = int(request.POST.get("dedup_window_seconds", "300") or "300")

        if not name:
            return _render_partial_or_full(
                request,
                "notifications/settings/_policy_form.html",
                "notifications/settings/settings_base.html",
                {
                    "policy": policy,
                    "error": "Name is required.",
                    "active_tab": "notifications",
                },
            )

        policy.name = name
        policy.is_default = is_default
        policy.dedup_window_seconds = dedup_window_seconds
        policy.save()

        if _is_htmx(request):
            policies = self.organization.notification_policies.all()
            return render(
                request,
                "notifications/settings/_policy_list.html",
                {"policies": policies},
            )
        return HttpResponseRedirect(reverse("notifications:policy-list"))


class PolicyDeleteView(OrganizationRequiredMixin, View):
    def post(self, request, pk):
        policy = get_object_or_404(NotificationPolicy, pk=pk, organization=self.organization)
        policy.delete()
        if _is_htmx(request):
            policies = self.organization.notification_policies.all()
            return render(
                request,
                "notifications/settings/_policy_list.html",
                {"policies": policies},
            )
        return HttpResponseRedirect(reverse("notifications:policy-list"))


class RouteConfigView(OrganizationRequiredMixin, View):
    def get(self, request, pk):
        policy = get_object_or_404(NotificationPolicy, pk=pk, organization=self.organization)
        channels = self.organization.notification_channels.all()
        routes = policy.routes.select_related("channel").all()
        policies = self.organization.notification_policies.all()
        context = {
            "policy": policy,
            "policies": policies,
            "channels": channels,
            "routes": routes,
            "severity_choices": NotificationRoute.Severity.choices,
            "attention_choices": NotificationRoute.Attention.choices,
            "active_tab": "notifications",
        }
        return _render_partial_or_full(
            request,
            "notifications/settings/_route_config.html",
            "notifications/settings/settings_base.html",
            context,
        )

    def post(self, request, pk):
        policy = get_object_or_404(NotificationPolicy, pk=pk, organization=self.organization)
        channels = self.organization.notification_channels.all()

        # Delete existing routes and recreate
        policy.routes.all().delete()

        # Process existing route updates (not used since we delete all, but keep for compatibility)
        for key in request.POST:
            if key.startswith("severity_") and not key.startswith("severity_new"):
                route_id = key.split("_", 1)[1]
                severity = request.POST.get(f"severity_{route_id}", "").strip() or None
                attention = request.POST.get(f"attention_{route_id}", "").strip() or None
                channel_id = request.POST.get(f"channel_{route_id}", "").strip()
                enabled = request.POST.get(f"enabled_{route_id}") == "1"
                if channel_id:
                    NotificationRoute.objects.create(
                        policy=policy,
                        severity=severity,
                        attention=attention,
                        channel_id=channel_id,
                        enabled=enabled,
                    )

        # Process new routes
        severity_news = request.POST.getlist("severity_new[]")
        attention_news = request.POST.getlist("attention_new[]")
        channel_news = request.POST.getlist("channel_new[]")

        for i, channel_id in enumerate(channel_news):
            if not channel_id:
                continue
            severity = severity_news[i].strip() if i < len(severity_news) else None
            attention = attention_news[i].strip() if i < len(attention_news) else None
            enabled = True
            NotificationRoute.objects.create(
                policy=policy,
                severity=severity or None,
                attention=attention or None,
                channel_id=channel_id,
                enabled=enabled,
            )

        if _is_htmx(request):
            routes = policy.routes.select_related("channel").all()
            return render(
                request,
                "notifications/settings/_route_config.html",
                {
                    "policy": policy,
                    "channels": channels,
                    "routes": routes,
                    "severity_choices": NotificationRoute.Severity.choices,
                    "attention_choices": NotificationRoute.Attention.choices,
                },
            )
        return HttpResponseRedirect(reverse("notifications:settings"))
