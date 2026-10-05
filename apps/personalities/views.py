# pyright: reportAttributeAccessIssue=false, reportCallIssue=false

import logging

from django.db.models import Q
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views import View

from apps.core.mixins import OrganizationRequiredMixin
from apps.personalities.models import Personality

logger = logging.getLogger(__name__)


class _AdminCheckMixin:
    """Verify the current user is an admin/owner of self.organization."""

    def _is_admin(self, request) -> bool:
        membership = request.user.memberships.filter(organization=self.organization).first()  # type: ignore[attr-defined]
        return bool(membership and membership.is_admin())


class PersonalityListView(OrganizationRequiredMixin, View):
    def get(self, request):
        personalities = Personality.objects.filter(
            Q(organization=self.organization)
            | Q(scope=Personality.Scope.SYSTEM, organization__isnull=True)
        ).order_by("name")
        if request.headers.get("HX-Request") or request.GET.get("format") == "picker":
            return render(
                request, "personalities/_personality_picker.html", {"personalities": personalities}
            )
        context = {
            "personalities": personalities,
            "organization": self.organization,
            "active_tab": "personalities",
        }
        return render(request, "personalities/personality_list.html", context)


class PersonalityDetailView(OrganizationRequiredMixin, View):
    def get(self, request, personality_id):
        personality = get_object_or_404(
            Personality,
            id=personality_id,
        )
        # Org-scoped personalities must belong to the user's org.
        if (
            personality.scope == Personality.Scope.ORGANIZATION
            and personality.organization_id != self.organization.id
        ):
            from django.http import Http404

            raise Http404("Personality not found.")
        context = {
            "personality": personality,
            "organization": self.organization,
            "active_tab": "personalities",
        }
        return render(request, "personalities/personality_detail.html", context)


class PersonalityCreateView(OrganizationRequiredMixin, _AdminCheckMixin, View):
    def get(self, request):
        if not self._is_admin(request):
            from django.core.exceptions import PermissionDenied

            raise PermissionDenied("Only organization admins can create personalities.")
        context = {
            "is_create": True,
            "organization": self.organization,
            "active_tab": "personalities",
        }
        return render(request, "personalities/personality_form.html", context)

    def post(self, request):
        if not self._is_admin(request):
            from django.core.exceptions import PermissionDenied

            raise PermissionDenied("Only organization admins can create personalities.")
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()
        prompt_text = request.POST.get("prompt_text", "").strip()
        category = request.POST.get("category", "").strip()
        tags_raw = request.POST.get("tags", "").strip()

        if not name:
            return render(
                request,
                "personalities/personality_form.html",
                {
                    "error": "Name is required.",
                    "is_create": True,
                    "organization": self.organization,
                    "active_tab": "personalities",
                },
            )

        if not prompt_text:
            return render(
                request,
                "personalities/personality_form.html",
                {
                    "error": "Prompt text is required.",
                    "is_create": True,
                    "organization": self.organization,
                    "active_tab": "personalities",
                },
            )

        tags = [tag.strip() for tag in tags_raw.split(",") if tag.strip()]

        personality = Personality.objects.create(
            organization=self.organization,
            name=name,
            description=description,
            prompt_text=prompt_text,
            category=category,
            tags=tags,
            scope=Personality.Scope.ORGANIZATION,
            created_by=request.user,
        )
        logger.info("Created personality %s for org %s", personality.id, self.organization.slug)
        return HttpResponseRedirect(
            reverse("personalities:personality-detail", kwargs={"personality_id": personality.id})
        )


class PersonalityEditView(OrganizationRequiredMixin, _AdminCheckMixin, View):
    def get(self, request, personality_id):
        if not self._is_admin(request):
            from django.core.exceptions import PermissionDenied

            raise PermissionDenied("Only organization admins can edit personalities.")
        personality = get_object_or_404(Personality, id=personality_id)
        if personality.scope == Personality.Scope.SYSTEM:
            from django.core.exceptions import PermissionDenied

            raise PermissionDenied("System personalities cannot be edited via the web UI.")
        if personality.organization_id != self.organization.id:
            from django.http import Http404

            raise Http404("Personality not found.")
        context = {
            "personality": personality,
            "is_create": False,
            "organization": self.organization,
            "active_tab": "personalities",
        }
        return render(request, "personalities/personality_form.html", context)

    def post(self, request, personality_id):
        if not self._is_admin(request):
            from django.core.exceptions import PermissionDenied

            raise PermissionDenied("Only organization admins can edit personalities.")
        personality = get_object_or_404(Personality, id=personality_id)
        if personality.scope == Personality.Scope.SYSTEM:
            from django.core.exceptions import PermissionDenied

            raise PermissionDenied("System personalities cannot be edited via the web UI.")
        if personality.organization_id != self.organization.id:
            from django.http import Http404

            raise Http404("Personality not found.")

        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()
        prompt_text = request.POST.get("prompt_text", "").strip()
        category = request.POST.get("category", "").strip()
        tags_raw = request.POST.get("tags", "").strip()

        if not name:
            return render(
                request,
                "personalities/personality_form.html",
                {
                    "error": "Name is required.",
                    "personality": personality,
                    "is_create": False,
                    "organization": self.organization,
                    "active_tab": "personalities",
                },
            )

        if not prompt_text:
            return render(
                request,
                "personalities/personality_form.html",
                {
                    "error": "Prompt text is required.",
                    "personality": personality,
                    "is_create": False,
                    "organization": self.organization,
                    "active_tab": "personalities",
                },
            )

        tags = [tag.strip() for tag in tags_raw.split(",") if tag.strip()]

        personality.name = name
        personality.description = description
        personality.prompt_text = prompt_text
        personality.category = category
        personality.tags = tags
        personality.save()
        logger.info("Updated personality %s for org %s", personality.id, self.organization.slug)
        return HttpResponseRedirect(
            reverse("personalities:personality-detail", kwargs={"personality_id": personality.id})
        )


class PersonalityDeleteView(OrganizationRequiredMixin, _AdminCheckMixin, View):
    def get(self, request, personality_id):
        if not self._is_admin(request):
            from django.core.exceptions import PermissionDenied

            raise PermissionDenied("Only organization admins can delete personalities.")
        personality = get_object_or_404(Personality, id=personality_id)
        if personality.scope == Personality.Scope.SYSTEM:
            from django.core.exceptions import PermissionDenied

            raise PermissionDenied("System personalities cannot be deleted via the web UI.")
        if personality.organization_id != self.organization.id:
            from django.http import Http404

            raise Http404("Personality not found.")
        context = {
            "personality": personality,
            "organization": self.organization,
            "active_tab": "personalities",
        }
        return render(request, "personalities/personality_delete.html", context)

    def post(self, request, personality_id):
        if not self._is_admin(request):
            from django.core.exceptions import PermissionDenied

            raise PermissionDenied("Only organization admins can delete personalities.")
        personality = get_object_or_404(Personality, id=personality_id)
        if personality.scope == Personality.Scope.SYSTEM:
            from django.core.exceptions import PermissionDenied

            raise PermissionDenied("System personalities cannot be deleted via the web UI.")
        if personality.organization_id != self.organization.id:
            from django.http import Http404

            raise Http404("Personality not found.")
        personality.delete()
        logger.info("Deleted personality %s for org %s", personality_id, self.organization.slug)
        return HttpResponseRedirect(reverse("personalities:personality-list"))
