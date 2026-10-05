# pyright: reportAttributeAccessIssue=false

from django.test import TestCase
from django.urls import reverse

from apps.core.models import Organization, OrganizationMembership, User
from apps.personalities.models import Personality


class PersonalityViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="alice", password="pass", email="alice@example.com"
        )
        self.organization = Organization.objects.create(name="Acme", slug="acme")
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role=OrganizationMembership.Role.OWNER,
        )
        self.client.force_login(self.user)

    def _create_org_personality(self, **kwargs):
        defaults = {
            "organization": self.organization,
            "name": "Test Personality",
            "description": "A test personality",
            "prompt_text": "You are a helpful SRE.",
            "scope": Personality.Scope.ORGANIZATION,
        }
        defaults.update(kwargs)
        return Personality.objects.create(**defaults)

    def _create_system_personality(self, **kwargs):
        defaults = {
            "organization": None,
            "name": "System Personality",
            "description": "Built-in",
            "prompt_text": "You are the system.",
            "scope": Personality.Scope.SYSTEM,
        }
        defaults.update(kwargs)
        return Personality.objects.create(**defaults)

    # List View
    def test_list_view_shows_org_personality(self):
        personality = self._create_org_personality()
        response = self.client.get(reverse("personalities:personality-list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, personality.name)

    def test_list_view_shows_system_personality(self):
        personality = self._create_system_personality()
        response = self.client.get(reverse("personalities:personality-list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, personality.name)

    def test_list_view_excludes_other_org_personality(self):
        other_org = Organization.objects.create(name="Other", slug="other")
        other = User.objects.create_user(username="bob", password="pass", email="bob@example.com")
        OrganizationMembership.objects.create(
            user=other, organization=other_org, role=OrganizationMembership.Role.OWNER
        )
        personality = self._create_org_personality(name="Acme Only")
        self.client.force_login(other)
        response = self.client.get(reverse("personalities:personality-list"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, personality.name)

    # Detail View
    def test_detail_view_org_personality(self):
        personality = self._create_org_personality()
        response = self.client.get(
            reverse("personalities:personality-detail", kwargs={"personality_id": personality.id})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, personality.name)

    def test_detail_view_system_personality(self):
        personality = self._create_system_personality()
        response = self.client.get(
            reverse("personalities:personality-detail", kwargs={"personality_id": personality.id})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, personality.name)

    def test_detail_view_other_org_is_404(self):
        other_org = Organization.objects.create(name="Other", slug="other")
        personality = Personality.objects.create(
            organization=other_org,
            name="Other Org",
            prompt_text="secret",
            scope=Personality.Scope.ORGANIZATION,
        )
        response = self.client.get(
            reverse("personalities:personality-detail", kwargs={"personality_id": personality.id})
        )
        self.assertEqual(response.status_code, 404)

    # Create View
    def test_create_view_get_requires_admin(self):
        response = self.client.get(reverse("personalities:personality-create"))
        self.assertEqual(response.status_code, 200)

    def test_create_view_get_denied_for_member(self):
        member = User.objects.create_user(
            username="member", password="pass", email="member@example.com"
        )
        OrganizationMembership.objects.create(
            user=member, organization=self.organization, role=OrganizationMembership.Role.MEMBER
        )
        self.client.force_login(member)
        response = self.client.get(reverse("personalities:personality-create"))
        self.assertEqual(response.status_code, 403)

    def test_create_view_post(self):
        response = self.client.post(
            reverse("personalities:personality-create"),
            {
                "name": "New Personality",
                "description": "Desc",
                "prompt_text": "Prompt",
                "category": "SRE",
                "tags": "tag1, tag2",
            },
        )
        self.assertRedirects(
            response,
            reverse(
                "personalities:personality-detail",
                kwargs={"personality_id": Personality.objects.get(name="New Personality").id},
            ),
        )
        personality = Personality.objects.get(name="New Personality")
        self.assertEqual(personality.organization, self.organization)
        self.assertEqual(personality.scope, Personality.Scope.ORGANIZATION)
        self.assertEqual(personality.created_by, self.user)
        self.assertEqual(personality.tags, ["tag1", "tag2"])

    def test_create_view_post_missing_name(self):
        response = self.client.post(
            reverse("personalities:personality-create"),
            {"name": "", "prompt_text": "Prompt"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Name is required.")

    def test_create_view_post_missing_prompt_text(self):
        response = self.client.post(
            reverse("personalities:personality-create"),
            {"name": "New Personality", "prompt_text": ""},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Prompt text is required.")

    # Edit View
    def test_edit_view_get(self):
        personality = self._create_org_personality()
        response = self.client.get(
            reverse("personalities:personality-edit", kwargs={"personality_id": personality.id})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, personality.name)

    def test_edit_view_post(self):
        personality = self._create_org_personality()
        response = self.client.post(
            reverse("personalities:personality-edit", kwargs={"personality_id": personality.id}),
            {
                "name": "Updated Name",
                "description": "Updated desc",
                "prompt_text": "Updated prompt",
                "category": "Updated cat",
                "tags": "foo, bar",
            },
        )
        self.assertRedirects(
            response,
            reverse("personalities:personality-detail", kwargs={"personality_id": personality.id}),
        )
        personality.refresh_from_db()
        self.assertEqual(personality.name, "Updated Name")
        self.assertEqual(personality.tags, ["foo", "bar"])

    def test_edit_view_denied_for_member(self):
        personality = self._create_org_personality()
        member = User.objects.create_user(
            username="member", password="pass", email="member@example.com"
        )
        OrganizationMembership.objects.create(
            user=member, organization=self.organization, role=OrganizationMembership.Role.MEMBER
        )
        self.client.force_login(member)
        response = self.client.get(
            reverse("personalities:personality-edit", kwargs={"personality_id": personality.id})
        )
        self.assertEqual(response.status_code, 403)

    def test_edit_system_personality_denied(self):
        personality = self._create_system_personality()
        response = self.client.get(
            reverse("personalities:personality-edit", kwargs={"personality_id": personality.id})
        )
        self.assertEqual(response.status_code, 403)

    # Delete View
    def test_delete_view_get(self):
        personality = self._create_org_personality()
        response = self.client.get(
            reverse("personalities:personality-delete", kwargs={"personality_id": personality.id})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Delete Personality")

    def test_delete_view_post(self):
        personality = self._create_org_personality()
        response = self.client.post(
            reverse("personalities:personality-delete", kwargs={"personality_id": personality.id})
        )
        self.assertRedirects(response, reverse("personalities:personality-list"))
        self.assertFalse(Personality.objects.filter(id=personality.id).exists())

    def test_delete_view_denied_for_member(self):
        personality = self._create_org_personality()
        member = User.objects.create_user(
            username="member", password="pass", email="member@example.com"
        )
        OrganizationMembership.objects.create(
            user=member, organization=self.organization, role=OrganizationMembership.Role.MEMBER
        )
        self.client.force_login(member)
        response = self.client.post(
            reverse("personalities:personality-delete", kwargs={"personality_id": personality.id})
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Personality.objects.filter(id=personality.id).exists())

    def test_delete_system_personality_denied(self):
        personality = self._create_system_personality()
        response = self.client.get(
            reverse("personalities:personality-delete", kwargs={"personality_id": personality.id})
        )
        self.assertEqual(response.status_code, 403)
