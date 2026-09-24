from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    User,
)


class IndexViewTests(TestCase):
    def test_anonymous_user_sees_landing_page(self):
        response = self.client.get(reverse("core:index"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "core/landing.html")

    def test_authenticated_user_with_org_sees_dashboard(self):
        user = User.objects.create_user(
            username="alice", password="pass", email="alice@example.com"
        )
        org = Organization.objects.create(name="Acme", slug="acme")
        OrganizationMembership.objects.create(user=user, organization=org)
        self.client.force_login(user)
        response = self.client.get(reverse("core:index"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "core/dashboard.html")

    def test_authenticated_user_without_org_redirected_to_setup(self):
        user = User.objects.create_user(
            username="alice", password="pass", email="alice@example.com"
        )
        self.client.force_login(user)
        response = self.client.get(reverse("core:index"))
        self.assertRedirects(response, reverse("core:org-setup"), fetch_redirect_response=False)


class OrganizationRequiredMixinTests(TestCase):
    def test_anonymous_user_redirected_to_login(self):
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_authenticated_user_without_organization_redirected_to_setup(self):
        user = User.objects.create_user(username="bob", password="pass", email="bob@example.com")
        self.client.force_login(user)
        response = self.client.get(reverse("core:dashboard"))
        self.assertRedirects(response, reverse("core:org-setup"), fetch_redirect_response=False)

    def test_authenticated_user_with_organization_sees_dashboard(self):
        user = User.objects.create_user(
            username="carol", password="pass", email="carol@example.com"
        )
        org = Organization.objects.create(name="Acme", slug="acme")
        OrganizationMembership.objects.create(
            user=user, organization=org, role=OrganizationMembership.Role.OWNER
        )
        self.client.force_login(user)
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "core/dashboard.html")

    def test_superuser_without_organization_gets_default_org(self):
        superuser = User.objects.create_superuser(
            username="admin", password="pass", email="admin@example.com"
        )
        self.client.force_login(superuser)
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "core/dashboard.html")
        self.assertTrue(superuser.organizations.exists())


class OrganizationAPITests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="api_org", password="pass")
        self.organization = Organization.objects.create(name="API Org", slug="api-org")
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role=OrganizationMembership.Role.OWNER,
        )
        self.client.force_login(self.user)

    def test_organization_list_api_returns_user_orgs(self):
        response = self.client.get("/api/organizations/")
        self.assertEqual(response.status_code, 200)
        data = response.json()["results"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["name"], "API Org")

    def test_organization_api_cross_org_isolation(self):
        Organization.objects.create(name="Other", slug="other")
        response = self.client.get("/api/organizations/")
        self.assertEqual(response.status_code, 200)
        data = response.json()["results"]
        self.assertEqual(len(data), 1)

    def test_anonymous_user_cannot_access_organization_api(self):
        self.client.logout()
        response = self.client.get("/api/organizations/")
        self.assertEqual(response.status_code, 403)


class PasswordResetTests(TestCase):
    def test_login_page_has_forgot_password_link(self):
        response = self.client.get(reverse("account_login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("account_reset_password"))

    def test_password_reset_get_renders_template(self):
        response = self.client.get(reverse("account_reset_password"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "account/password_reset.html")

    def test_password_reset_post_redirects_to_done(self):
        User.objects.create_user(
            username="testuser", email="test@example.com", password="oldpassword"
        )
        response = self.client.post(
            reverse("account_reset_password"),
            {"email": "test@example.com"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse("account_reset_password_done"))

    def test_password_reset_done_page_renders(self):
        response = self.client.get(reverse("account_reset_password_done"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "account/password_reset_done.html")


class OrganizationSetupViewTests(TestCase):
    def test_user_with_org_redirected_to_dashboard(self):
        user = User.objects.create_user(
            username="alice", password="pass", email="alice@example.com"
        )
        org = Organization.objects.create(name="Acme", slug="acme")
        OrganizationMembership.objects.create(user=user, organization=org)
        self.client.force_login(user)
        response = self.client.get(reverse("core:org-setup"))
        self.assertRedirects(response, reverse("core:dashboard"))

    def test_user_can_create_org(self):
        user = User.objects.create_user(username="bob", password="pass", email="bob@example.com")
        self.client.force_login(user)
        response = self.client.post(
            reverse("core:org-setup"),
            {"action": "create", "name": "BobCorp", "email_domain": "bobcorp.com"},
        )
        self.assertRedirects(response, reverse("core:dashboard"))
        self.assertTrue(user.organizations.filter(name="BobCorp").exists())
        membership = OrganizationMembership.objects.get(user=user, organization__name="BobCorp")
        self.assertEqual(membership.role, OrganizationMembership.Role.OWNER)

    def test_org_creation_requires_name(self):
        user = User.objects.create_user(username="bob", password="pass", email="bob@example.com")
        self.client.force_login(user)
        response = self.client.post(
            reverse("core:org-setup"),
            {"action": "create", "name": ""},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(user.organizations.exists())

    def test_user_can_join_via_invitation(self):
        owner = User.objects.create_user(
            username="owner", password="pass", email="owner@example.com"
        )
        org = Organization.objects.create(name="Acme", slug="acme")
        OrganizationMembership.objects.create(
            user=owner, organization=org, role=OrganizationMembership.Role.OWNER
        )
        invitation = OrganizationInvitation.objects.create(
            organization=org, email="alice@example.com", code="abc123"
        )
        user = User.objects.create_user(
            username="alice", password="pass", email="alice@example.com"
        )
        self.client.force_login(user)
        response = self.client.post(
            reverse("core:org-setup"),
            {"action": "join", "code": "abc123"},
        )
        self.assertRedirects(response, reverse("core:dashboard"))
        self.assertTrue(user.organizations.filter(id=org.id).exists())
        invitation.refresh_from_db()
        self.assertIsNotNone(invitation.accepted_at)

    def test_join_invalid_code(self):
        user = User.objects.create_user(
            username="alice", password="pass", email="alice@example.com"
        )
        self.client.force_login(user)
        response = self.client.post(
            reverse("core:org-setup"),
            {"action": "join", "code": "badcode"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(user.organizations.exists())

    def test_join_wrong_email(self):
        owner = User.objects.create_user(
            username="owner", password="pass", email="owner@example.com"
        )
        org = Organization.objects.create(name="Acme", slug="acme")
        OrganizationMembership.objects.create(
            user=owner, organization=org, role=OrganizationMembership.Role.OWNER
        )
        OrganizationInvitation.objects.create(
            organization=org, email="alice@example.com", code="abc123"
        )
        user = User.objects.create_user(username="bob", password="pass", email="bob@example.com")
        self.client.force_login(user)
        response = self.client.post(
            reverse("core:org-setup"),
            {"action": "join", "code": "abc123"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(user.organizations.exists())

    def test_join_email_domain_restriction(self):
        owner = User.objects.create_user(username="owner", password="pass", email="owner@acme.com")
        org = Organization.objects.create(name="Acme", slug="acme", email_domain="acme.com")
        OrganizationMembership.objects.create(
            user=owner, organization=org, role=OrganizationMembership.Role.OWNER
        )
        OrganizationInvitation.objects.create(
            organization=org, email="alice@acme.com", code="abc123"
        )
        user = User.objects.create_user(username="alice", password="pass", email="alice@other.com")
        self.client.force_login(user)
        response = self.client.post(
            reverse("core:org-setup"),
            {"action": "join", "code": "abc123"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(user.organizations.exists())

    def test_join_valid_email_domain(self):
        owner = User.objects.create_user(username="owner", password="pass", email="owner@acme.com")
        org = Organization.objects.create(name="Acme", slug="acme", email_domain="acme.com")
        OrganizationMembership.objects.create(
            user=owner, organization=org, role=OrganizationMembership.Role.OWNER
        )
        OrganizationInvitation.objects.create(
            organization=org, email="alice@acme.com", code="abc123"
        )
        user = User.objects.create_user(username="alice", password="pass", email="alice@acme.com")
        self.client.force_login(user)
        response = self.client.post(
            reverse("core:org-setup"),
            {"action": "join", "code": "abc123"},
        )
        self.assertRedirects(response, reverse("core:dashboard"))
        self.assertTrue(user.organizations.filter(id=org.id).exists())

    def test_invitation_param_prefills_code(self):
        user = User.objects.create_user(
            username="alice", password="pass", email="alice@example.com"
        )
        self.client.force_login(user)
        response = self.client.get(reverse("core:org-setup") + "?invitation=MYCODE")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "MYCODE")


class OrganizationDetailViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="alice", password="pass", email="alice@example.com"
        )
        self.org = Organization.objects.create(name="Acme", slug="acme")
        OrganizationMembership.objects.create(
            user=self.user, organization=self.org, role=OrganizationMembership.Role.OWNER
        )
        self.client.force_login(self.user)

    def test_member_can_view_org_details(self):
        response = self.client.get(reverse("core:org-detail"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Acme")

    def test_admin_can_edit_org(self):
        response = self.client.post(
            reverse("core:org-detail"),
            {"name": "Acme Inc", "email_domain": "acme.com"},
        )
        self.assertRedirects(response, reverse("core:org-detail"))
        self.org.refresh_from_db()
        self.assertEqual(self.org.name, "Acme Inc")
        self.assertEqual(self.org.email_domain, "acme.com")

    def test_non_admin_cannot_edit_org(self):
        member = User.objects.create_user(username="bob", password="pass", email="bob@example.com")
        OrganizationMembership.objects.create(
            user=member, organization=self.org, role=OrganizationMembership.Role.MEMBER
        )
        self.client.force_login(member)
        response = self.client.post(
            reverse("core:org-detail"),
            {"name": "Hacked", "email_domain": ""},
        )
        self.assertEqual(response.status_code, 403)
        self.org.refresh_from_db()
        self.assertEqual(self.org.name, "Acme")


class OrganizationInviteViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="alice", password="pass", email="alice@example.com"
        )
        self.org = Organization.objects.create(name="Acme", slug="acme")
        OrganizationMembership.objects.create(
            user=self.user, organization=self.org, role=OrganizationMembership.Role.OWNER
        )
        self.client.force_login(self.user)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_admin_can_send_invitation(self):
        response = self.client.post(
            reverse("core:org-invite"),
            {"email": "new@example.com"},
        )
        self.assertRedirects(response, reverse("core:org-detail"))
        self.assertTrue(
            OrganizationInvitation.objects.filter(
                organization=self.org, email="new@example.com"
            ).exists()
        )

    def test_member_cannot_send_invitation(self):
        member = User.objects.create_user(username="bob", password="pass", email="bob@example.com")
        OrganizationMembership.objects.create(
            user=member, organization=self.org, role=OrganizationMembership.Role.MEMBER
        )
        self.client.force_login(member)
        response = self.client.post(
            reverse("core:org-invite"),
            {"email": "new@example.com"},
        )
        self.assertEqual(response.status_code, 403)

    def test_invitation_respects_email_domain(self):
        self.org.email_domain = "acme.com"
        self.org.save()
        response = self.client.post(
            reverse("core:org-invite"),
            {"email": "new@other.com"},
        )
        self.assertRedirects(response, reverse("core:org-detail"))
        self.assertFalse(
            OrganizationInvitation.objects.filter(
                organization=self.org, email="new@other.com"
            ).exists()
        )


class OrganizationMemberManagementTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="owner", password="pass", email="owner@example.com"
        )
        self.org = Organization.objects.create(name="Acme", slug="acme")
        OrganizationMembership.objects.create(
            user=self.owner, organization=self.org, role=OrganizationMembership.Role.OWNER
        )
        self.client.force_login(self.owner)

    def test_admin_can_remove_member(self):
        member = User.objects.create_user(username="bob", password="pass", email="bob@example.com")
        OrganizationMembership.objects.create(
            user=member, organization=self.org, role=OrganizationMembership.Role.MEMBER
        )
        response = self.client.post(
            reverse("core:org-member-remove", kwargs={"user_id": member.id})
        )
        self.assertRedirects(response, reverse("core:org-detail"))
        self.assertFalse(
            OrganizationMembership.objects.filter(user=member, organization=self.org).exists()
        )

    def test_admin_cannot_remove_owner(self):
        response = self.client.post(
            reverse("core:org-member-remove", kwargs={"user_id": self.owner.id})
        )
        self.assertRedirects(response, reverse("core:org-detail"))
        self.assertTrue(
            OrganizationMembership.objects.filter(user=self.owner, organization=self.org).exists()
        )

    def test_admin_cannot_remove_themselves(self):
        admin = User.objects.create_user(
            username="admin", password="pass", email="admin@example.com"
        )
        OrganizationMembership.objects.create(
            user=admin, organization=self.org, role=OrganizationMembership.Role.ADMIN
        )
        self.client.force_login(admin)
        response = self.client.post(reverse("core:org-member-remove", kwargs={"user_id": admin.id}))
        self.assertRedirects(response, reverse("core:org-detail"))
        self.assertTrue(
            OrganizationMembership.objects.filter(user=admin, organization=self.org).exists()
        )

    def test_member_cannot_remove_others(self):
        member = User.objects.create_user(username="bob", password="pass", email="bob@example.com")
        OrganizationMembership.objects.create(
            user=member, organization=self.org, role=OrganizationMembership.Role.MEMBER
        )
        other = User.objects.create_user(
            username="charlie", password="pass", email="charlie@example.com"
        )
        OrganizationMembership.objects.create(
            user=other, organization=self.org, role=OrganizationMembership.Role.MEMBER
        )
        self.client.force_login(member)
        response = self.client.post(reverse("core:org-member-remove", kwargs={"user_id": other.id}))
        self.assertEqual(response.status_code, 403)

    def test_admin_can_change_member_role(self):
        member = User.objects.create_user(username="bob", password="pass", email="bob@example.com")
        OrganizationMembership.objects.create(
            user=member, organization=self.org, role=OrganizationMembership.Role.MEMBER
        )
        response = self.client.post(
            reverse("core:org-member-role-update", kwargs={"user_id": member.id}),
            {"role": "admin"},
        )
        self.assertRedirects(response, reverse("core:org-detail"))
        membership = OrganizationMembership.objects.get(user=member, organization=self.org)
        self.assertEqual(membership.role, OrganizationMembership.Role.ADMIN)

    def test_admin_cannot_assign_owner_role(self):
        admin = User.objects.create_user(
            username="admin", password="pass", email="admin@example.com"
        )
        OrganizationMembership.objects.create(
            user=admin, organization=self.org, role=OrganizationMembership.Role.ADMIN
        )
        member = User.objects.create_user(username="bob", password="pass", email="bob@example.com")
        OrganizationMembership.objects.create(
            user=member, organization=self.org, role=OrganizationMembership.Role.MEMBER
        )
        self.client.force_login(admin)
        response = self.client.post(
            reverse("core:org-member-role-update", kwargs={"user_id": member.id}),
            {"role": "owner"},
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_cannot_change_owner_role(self):
        admin = User.objects.create_user(
            username="admin", password="pass", email="admin@example.com"
        )
        OrganizationMembership.objects.create(
            user=admin, organization=self.org, role=OrganizationMembership.Role.ADMIN
        )
        self.client.force_login(admin)
        response = self.client.post(
            reverse("core:org-member-role-update", kwargs={"user_id": self.owner.id}),
            {"role": "admin"},
        )
        self.assertEqual(response.status_code, 403)


class OrganizationSetupMiddlewareTests(TestCase):
    def test_redirects_authenticated_user_without_org(self):
        user = User.objects.create_user(
            username="alice", password="pass", email="alice@example.com"
        )
        self.client.force_login(user)
        response = self.client.get(reverse("core:dashboard"))
        self.assertRedirects(response, reverse("core:org-setup"), fetch_redirect_response=False)

    def test_no_redirect_for_anonymous(self):
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_no_redirect_for_user_with_org(self):
        user = User.objects.create_user(
            username="alice", password="pass", email="alice@example.com"
        )
        org = Organization.objects.create(name="Acme", slug="acme")
        OrganizationMembership.objects.create(user=user, organization=org)
        self.client.force_login(user)
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 200)

    def test_no_redirect_for_superuser(self):
        superuser = User.objects.create_superuser(
            username="admin", password="pass", email="admin@example.com"
        )
        self.client.force_login(superuser)
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 200)
