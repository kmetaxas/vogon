# pyright: reportAttributeAccessIssue=false, reportCallIssue=false, reportArgumentType=false

from django.test import TestCase
from django.urls import reverse

from apps.core.models import Organization, OrganizationMembership, User
from apps.infradesigns.models import InfrastructureDesign


class InfrastructureDesignModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="pass")
        self.organization = Organization.objects.create(name="Acme", slug="acme")
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role=OrganizationMembership.Role.MEMBER,
        )

    def test_create_design(self):
        d = InfrastructureDesign.objects.create(
            organization=self.organization,
            name="Test Design",
            description="A test design",
            environment=InfrastructureDesign.Environment.STAGING,
            mermaid_topology="graph TD\n    A --> B",
            marvin_selector={"labels": {"team": "platform"}},
            created_by=self.user,
        )
        self.assertEqual(d.name, "Test Design")
        self.assertEqual(d.environment, "staging")
        self.assertNotEqual(d.search_document, "")
        self.assertIn("Design: Test Design", d.search_document)
        self.assertEqual(d.search_document_version, 1)

    def test_search_document_updated_on_save(self):
        d = InfrastructureDesign.objects.create(
            organization=self.organization,
            name="Test Design",
            description="A test design",
            created_by=self.user,
        )
        old_version = d.search_document_version
        d.description = "Updated description"
        d.save()
        self.assertEqual(d.search_document_version, old_version + 1)

    def test_unique_together(self):
        InfrastructureDesign.objects.create(
            organization=self.organization,
            name="Unique Name",
            created_by=self.user,
        )
        with self.assertRaises(Exception):
            InfrastructureDesign.objects.create(
                organization=self.organization,
                name="Unique Name",
                created_by=self.user,
            )

    def test_str(self):
        d = InfrastructureDesign.objects.create(
            organization=self.organization,
            name="Prod Kafka",
            environment=InfrastructureDesign.Environment.PRODUCTION,
            created_by=self.user,
        )
        self.assertEqual(str(d), "Prod Kafka (production)")


class InfraDesignViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="pass")
        self.organization = Organization.objects.create(name="Acme", slug="acme")
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role=OrganizationMembership.Role.MEMBER,
        )
        self.client.force_login(self.user)
        self.design = InfrastructureDesign.objects.create(
            organization=self.organization,
            name="Prod Kafka",
            description="Kafka cluster topology",
            environment=InfrastructureDesign.Environment.PRODUCTION,
            mermaid_topology="graph TD\n    A --> B",
            marvin_selector={"labels": {"env": "production"}},
            created_by=self.user,
        )

    def test_list_requires_login(self):
        from django.test import Client

        c = Client()
        response = c.get("/designs/")
        self.assertEqual(response.status_code, 302)

    def test_list_shows_org_designs(self):
        response = self.client.get("/designs/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Prod Kafka")

    def test_create_design(self):
        response = self.client.post(
            "/designs/create/",
            {
                "name": "New Design",
                "description": "Desc",
                "environment": "development",
                "mermaid_topology": "graph TD\n    A --> B",
                "marvin_selector": '{"labels": {"env": "dev"}}',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(InfrastructureDesign.objects.filter(name="New Design").exists())

    def test_edit_design(self):
        response = self.client.post(
            reverse("infradesigns:design-edit", kwargs={"design_id": self.design.id}),
            {
                "name": "Updated Kafka",
                "description": self.design.description,
                "environment": self.design.environment,
                "mermaid_topology": self.design.mermaid_topology,
                "marvin_selector": '{"labels": {"env": "production"}}',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.design.refresh_from_db()
        self.assertEqual(self.design.name, "Updated Kafka")

    def test_detail_requires_org_member(self):
        other_org = Organization.objects.create(name="Other", slug="other")
        other_user = User.objects.create_user(username="bob", password="pass")
        OrganizationMembership.objects.create(
            user=other_user,
            organization=other_org,
            role=OrganizationMembership.Role.MEMBER,
        )
        self.client.force_login(other_user)
        response = self.client.get(
            reverse("infradesigns:design-detail", kwargs={"design_id": self.design.id})
        )
        self.assertEqual(response.status_code, 404)
