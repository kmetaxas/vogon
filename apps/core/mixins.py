from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.shortcuts import redirect


class OrganizationRequiredMixin(LoginRequiredMixin):
    """Require an authenticated user that belongs to at least one organization.

    Sets ``self.organization`` to the user's first organization. Anonymous
    users are redirected to the login page. Superusers bypass the org
    check and use the org from the URL query param (if provided) or the
    first org in the database.

    Authenticated users without an organization are redirected to the
    organization setup page so they can create or join one.
    """

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()

        # Superusers bypass org checks
        if request.user.is_superuser:
            from apps.core.models import Organization

            org_id = request.GET.get("org")
            if org_id:
                self.organization = Organization.objects.filter(id=org_id).first()
            else:
                self.organization = (
                    request.user.get_current_organization() or Organization.objects.first()
                )
            if self.organization is None:
                # If there are no orgs at all, create a default one for the superuser
                self.organization = Organization.objects.create(name="Default", slug="default")
                from apps.core.models import OrganizationMembership

                OrganizationMembership.objects.create(
                    user=request.user,
                    organization=self.organization,
                    role=OrganizationMembership.Role.OWNER,
                )
            return super().dispatch(request, *args, **kwargs)

        self.organization = request.user.get_current_organization()
        if self.organization is None:
            return redirect("core:org-setup")
        return super().dispatch(request, *args, **kwargs)


class OrganizationQuerySetMixin:
    """Filter a view's queryset to the user's organizations.

    Requires ``self.organization`` to be set (e.g. by
    :class:`OrganizationRequiredMixin`). The model must expose an
    ``organization`` foreign key.
    """

    def get_queryset(self):
        queryset = super().get_queryset()
        organization = getattr(self, "organization", None)
        if organization is not None:
            return queryset.filter(organization=organization)
        return queryset.filter(Q(organization__in=self.request.user.organizations.all()))
