import logging
import secrets

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.mail import send_mail
from django.db import transaction
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views import View

from apps.core.mixins import OrganizationRequiredMixin
from apps.core.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
)
from apps.sessions.models import TSession

logger = logging.getLogger(__name__)


class IndexView(View):
    def get(self, request):
        if request.user.is_authenticated:
            # Users without an org must set one up before using the site
            if not request.user.is_superuser and not request.user.organizations.exists():
                return redirect("core:org-setup")
            active_sessions = TSession.objects.filter(
                status__in=[TSession.Status.ACTIVE, TSession.Status.PENDING]
            )
            previous_sessions = TSession.objects.filter(
                status__in=[
                    TSession.Status.COMPLETED,
                    TSession.Status.FAILED,
                    TSession.Status.PAUSED,
                ]
            )
            context = {
                "active_sessions": active_sessions,
                "previous_sessions": previous_sessions,
            }
            return render(request, "core/dashboard.html", context)
        return render(request, "core/landing.html")


class DashboardView(OrganizationRequiredMixin, View):
    def get(self, request):
        active_sessions = TSession.objects.filter(
            organization=self.organization,
            status__in=[TSession.Status.ACTIVE, TSession.Status.PENDING],
        )
        previous_sessions = TSession.objects.filter(
            organization=self.organization,
            status__in=[
                TSession.Status.COMPLETED,
                TSession.Status.FAILED,
                TSession.Status.PAUSED,
            ],
        )
        context = {
            "active_sessions": active_sessions,
            "previous_sessions": previous_sessions,
        }
        return render(request, "core/dashboard.html", context)


class ProfileView(OrganizationRequiredMixin, View):
    def get(self, request):
        return render(request, "core/profile.html")


class SettingsView(OrganizationRequiredMixin, View):
    def get(self, request):
        return render(request, "core/settings.html")


class OrganizationSetupView(LoginRequiredMixin, View):
    """User must create or join an org before using the site."""

    def get(self, request):
        # If user already has an org, redirect to dashboard
        if request.user.organizations.exists():
            return redirect("core:dashboard")

        invitation_code = request.GET.get("invitation", "")
        return render(
            request,
            "core/org_setup.html",
            {"invitation_code": invitation_code},
        )

    def post(self, request):
        action = request.POST.get("action")

        if action == "create":
            return self._handle_create(request)
        elif action == "join":
            return self._handle_join(request)

        messages.error(request, "Invalid action.")
        return redirect("core:org-setup")

    def _handle_create(self, request):
        name = request.POST.get("name", "").strip()
        email_domain = request.POST.get("email_domain", "").strip()

        if not name:
            messages.error(request, "Organization name is required.")
            return render(request, "core/org_setup.html", {"tab": "create"})

        slug_base = slugify(name)
        slug = slug_base
        counter = 1
        while Organization.objects.filter(slug=slug).exists():
            slug = f"{slug_base}-{counter}"
            counter += 1

        with transaction.atomic():
            org = Organization.objects.create(
                name=name,
                slug=slug,
                email_domain=email_domain,
            )
            OrganizationMembership.objects.create(
                user=request.user,
                organization=org,
                role=OrganizationMembership.Role.OWNER,
            )

        messages.success(request, f"Organization '{name}' created.")
        return redirect("core:dashboard")

    def _handle_join(self, request):
        code = request.POST.get("code", "").strip()

        if not code:
            messages.error(request, "Invitation code is required.")
            return render(request, "core/org_setup.html", {"tab": "join"})

        try:
            invitation = OrganizationInvitation.objects.select_related("organization").get(
                code=code, accepted_at__isnull=True
            )
        except OrganizationInvitation.DoesNotExist:
            messages.error(request, "Invalid or expired invitation code.")
            return render(request, "core/org_setup.html", {"tab": "join", "code": code})

        # Validate email match
        if invitation.email.lower() != request.user.email.lower():
            messages.error(
                request,
                "This invitation was sent to a different email address.",
            )
            return render(request, "core/org_setup.html", {"tab": "join", "code": code})

        # Validate email domain restriction
        org = invitation.organization
        if org.email_domain:
            user_domain = request.user.email.split("@")[1].lower()
            if user_domain != org.email_domain.lower():
                messages.error(
                    request,
                    f"Only users with an @{org.email_domain} email address "
                    "can join this organization.",
                )
                return render(request, "core/org_setup.html", {"tab": "join", "code": code})

        with transaction.atomic():
            OrganizationMembership.objects.create(
                user=request.user,
                organization=org,
                role=OrganizationMembership.Role.MEMBER,
            )
            invitation.accepted_at = timezone.now()
            invitation.accepted_by = request.user
            invitation.save(update_fields=["accepted_at", "accepted_by"])

        messages.success(request, f"You joined '{org.name}'.")
        return redirect("core:dashboard")


class OrganizationDetailView(OrganizationRequiredMixin, View):
    """Organization settings page. Role-aware display."""

    def get(self, request):
        membership = get_object_or_404(
            OrganizationMembership,
            user=request.user,
            organization=self.organization,
        )
        members = (
            OrganizationMembership.objects.filter(organization=self.organization)
            .select_related("user")
            .order_by("-role", "user__username")
        )
        invitations = (
            OrganizationInvitation.objects.filter(
                organization=self.organization, accepted_at__isnull=True
            )
            .select_related("created_by")
            .order_by("-created_at")
        )

        context = {
            "organization": self.organization,
            "membership": membership,
            "members": members,
            "invitations": invitations,
            "is_admin": membership.is_admin(),
            "is_owner": membership.role == OrganizationMembership.Role.OWNER,
        }
        return render(request, "core/org_detail.html", context)

    def post(self, request):
        """Admin-only: update org name and email_domain."""
        membership = get_object_or_404(
            OrganizationMembership,
            user=request.user,
            organization=self.organization,
        )
        if not membership.is_admin():
            return HttpResponseForbidden("Only admins can edit organization settings.")

        name = request.POST.get("name", "").strip()
        email_domain = request.POST.get("email_domain", "").strip()

        if name:
            self.organization.name = name
        self.organization.email_domain = email_domain
        self.organization.save(update_fields=["name", "email_domain"])

        messages.success(request, "Organization settings updated.")
        return redirect("core:org-detail")


class OrganizationInviteView(OrganizationRequiredMixin, View):
    """POST only. Admin sends invite email."""

    def post(self, request):
        membership = get_object_or_404(
            OrganizationMembership,
            user=request.user,
            organization=self.organization,
        )
        if not membership.is_admin():
            return HttpResponseForbidden("Only admins can send invitations.")

        email = request.POST.get("email", "").strip()
        if not email:
            messages.error(request, "Email address is required.")
            return redirect("core:org-detail")

        # Validate email domain restriction
        if self.organization.email_domain:
            invited_domain = email.split("@")[1].lower()
            if invited_domain != self.organization.email_domain.lower():
                messages.error(
                    request,
                    f"Only users with an @{self.organization.email_domain} "
                    "email address can be invited.",
                )
                return redirect("core:org-detail")

        code = secrets.token_urlsafe(32)
        OrganizationInvitation.objects.create(
            organization=self.organization,
            email=email,
            code=code,
            created_by=request.user,
        )

        invite_url = request.build_absolute_uri(reverse("core:org-setup") + f"?invitation={code}")
        subject = f"You've been invited to join {self.organization.name} on Vogon"
        body = (
            f"Hi,\n\n"
            f"{request.user.username} has invited you to join "
            f"{self.organization.name} on Vogon.\n\n"
            f"Click here to accept: {invite_url}\n\n"
            f"Your invitation code: {code}\n\n"
            f"If you don't have an account yet, sign up first and then use the link above.\n"
        )

        try:
            send_mail(
                subject=subject,
                message=body,
                from_email=None,  # uses DEFAULT_FROM_EMAIL
                recipient_list=[email],
                fail_silently=False,
            )
            messages.success(request, f"Invitation sent to {email}.")
        except Exception:
            logger.exception("Failed to send invitation email to %s", email)
            messages.warning(
                request,
                f"Invitation created for {email}, but the email could not be sent. "
                f"Please share this link manually: {invite_url}",
            )

        return redirect("core:org-detail")


class OrganizationMemberRemoveView(OrganizationRequiredMixin, View):
    """POST only. Admin removes a member (cannot remove self or owner)."""

    def post(self, request, user_id):
        admin_membership = get_object_or_404(
            OrganizationMembership,
            user=request.user,
            organization=self.organization,
        )
        if not admin_membership.is_admin():
            return HttpResponseForbidden("Only admins can remove members.")

        target_membership = get_object_or_404(
            OrganizationMembership,
            user__id=user_id,
            organization=self.organization,
        )

        if target_membership.user_id == request.user.id:
            messages.error(request, "You cannot remove yourself.")
            return redirect("core:org-detail")

        if target_membership.role == OrganizationMembership.Role.OWNER:
            messages.error(request, "You cannot remove the organization owner.")
            return redirect("core:org-detail")

        target_membership.delete()
        messages.success(request, "Member removed.")
        return redirect("core:org-detail")


class OrganizationMemberRoleUpdateView(OrganizationRequiredMixin, View):
    """POST only. Admin changes a member's role."""

    def post(self, request, user_id):
        admin_membership = get_object_or_404(
            OrganizationMembership,
            user=request.user,
            organization=self.organization,
        )
        if not admin_membership.is_admin():
            return HttpResponseForbidden("Only admins can change member roles.")

        target_membership = get_object_or_404(
            OrganizationMembership,
            user__id=user_id,
            organization=self.organization,
        )

        if target_membership.user_id == request.user.id:
            messages.error(request, "You cannot change your own role.")
            return redirect("core:org-detail")

        new_role = request.POST.get("role", "")
        valid_roles = {r for r, _ in OrganizationMembership.Role.choices}
        if new_role not in valid_roles:
            messages.error(request, "Invalid role.")
            return redirect("core:org-detail")

        # Only owner can assign owner role; admins cannot promote to owner
        if (
            new_role == OrganizationMembership.Role.OWNER
            and admin_membership.role != OrganizationMembership.Role.OWNER
        ):
            return HttpResponseForbidden("Only the owner can assign the owner role.")

        # Only owner can demote another owner
        if (
            target_membership.role == OrganizationMembership.Role.OWNER
            and admin_membership.role != OrganizationMembership.Role.OWNER
        ):
            return HttpResponseForbidden("Only the owner can change the owner's role.")

        target_membership.role = new_role
        target_membership.save(update_fields=["role"])
        messages.success(request, "Member role updated.")
        return redirect("core:org-detail")
