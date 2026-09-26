from unittest.mock import patch

from django.test import TestCase

from apps.core.models import Organization, OrganizationMembership, User
from apps.llm.models import LLMProvider


class LLMRegistryTests(TestCase):
    @patch("services.llm.registry.OpenAICompatibleClient")
    def test_get_llm_client_explicit_provider_id(self, mock_client):
        from services.llm.registry import get_llm_client

        org = Organization.objects.create(name="Reg", slug="reg")
        provider = LLMProvider.objects.create(
            organization=org,
            name="Explicit",
            provider_type=LLMProvider.ProviderType.OPENAI_COMPAT,
            base_url="http://explicit:11434/v1",
            model="explicit-model",
        )
        LLMProvider.objects.create(
            organization=org,
            name="Default",
            provider_type=LLMProvider.ProviderType.OLLAMA,
            base_url="http://default:11434/v1",
            model="default-model",
            is_default=True,
        )

        get_llm_client(organization_id=str(org.id), provider_id=str(provider.id))

        call_kwargs = mock_client.call_args.kwargs
        self.assertEqual(call_kwargs["base_url"], "http://explicit:11434/v1")
        self.assertEqual(call_kwargs["model"], "explicit-model")

    @patch("services.llm.registry.OpenAICompatibleClient")
    def test_get_llm_client_no_provider_id_uses_org_default(self, mock_client):
        from services.llm.registry import get_llm_client

        org = Organization.objects.create(name="Reg2", slug="reg2")
        LLMProvider.objects.create(
            organization=org,
            name="Default",
            provider_type=LLMProvider.ProviderType.OLLAMA,
            base_url="http://default:11434/v1",
            model="default-model",
            is_default=True,
        )

        get_llm_client(organization_id=str(org.id))

        call_kwargs = mock_client.call_args.kwargs
        self.assertEqual(call_kwargs["base_url"], "http://default:11434/v1")
        self.assertEqual(call_kwargs["model"], "default-model")

    @patch("services.llm.registry.OpenAICompatibleClient")
    def test_get_llm_client_invalid_provider_id_falls_back_to_default(self, mock_client):
        from services.llm.registry import get_llm_client

        org = Organization.objects.create(name="Reg3", slug="reg3")
        LLMProvider.objects.create(
            organization=org,
            name="Default",
            provider_type=LLMProvider.ProviderType.OLLAMA,
            base_url="http://default:11434/v1",
            model="default-model",
            is_default=True,
        )

        get_llm_client(organization_id=str(org.id), provider_id="nonexistent-uuid")

        call_kwargs = mock_client.call_args.kwargs
        self.assertEqual(call_kwargs["base_url"], "http://default:11434/v1")
        self.assertEqual(call_kwargs["model"], "default-model")


class LLMApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="llm_api", password="pass")
        self.organization = Organization.objects.create(name="LLM Org", slug="llm-org")
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.organization,
            role=OrganizationMembership.Role.OWNER,
        )
        self.client.force_login(self.user)

    def test_llm_provider_list_api_returns_org_providers(self):
        LLMProvider.objects.create(
            organization=self.organization,
            name="Ollama Cloud",
            provider_type=LLMProvider.ProviderType.OLLAMA,
            base_url="https://ollama.com/v1",
            model="qwen3:latest",
        )
        response = self.client.get("/api/llm-providers/")
        self.assertEqual(response.status_code, 200)
        data = response.json()["results"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["name"], "Ollama Cloud")

    def test_llm_provider_create_api(self):
        response = self.client.post(
            "/api/llm-providers/",
            {
                "organization": str(self.organization.id),
                "name": "New Provider",
                "provider_type": "openai_compat",
                "base_url": "http://localhost:11434/v1",
                "model": "qwen3:latest",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["name"], "New Provider")

    def test_llm_provider_api_cross_org_isolation(self):
        other_org = Organization.objects.create(name="Other", slug="other")
        LLMProvider.objects.create(
            organization=other_org,
            name="Other Provider",
            provider_type=LLMProvider.ProviderType.OLLAMA,
            base_url="https://other.com/v1",
            model="other-model",
        )
        response = self.client.get("/api/llm-providers/")
        self.assertEqual(response.status_code, 200)
        data = response.json()["results"]
        self.assertEqual(len(data), 0)

    def test_llm_provider_api_key_is_write_only(self):
        provider = LLMProvider.objects.create(
            organization=self.organization,
            name="Secret Provider",
            provider_type=LLMProvider.ProviderType.OLLAMA,
            base_url="https://ollama.com/v1",
            api_key="super-secret-key",
            model="qwen3:latest",
        )
        response = self.client.get(f"/api/llm-providers/{provider.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("api_key", response.json())

    def test_llm_provider_default_unique_constraint(self):
        LLMProvider.objects.create(
            organization=self.organization,
            name="Default Provider",
            provider_type=LLMProvider.ProviderType.OLLAMA,
            is_default=True,
            model="qwen3:latest",
        )
        response = self.client.post(
            "/api/llm-providers/",
            {
                "organization": str(self.organization.id),
                "name": "Another Default",
                "provider_type": "openai_compat",
                "is_default": True,
                "model": "other-model",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)


class LLMProviderEncryptionTests(TestCase):
    def test_api_key_is_encrypted_at_rest(self):
        org = Organization.objects.create(name="Enc", slug="enc")
        provider = LLMProvider.objects.create(
            organization=org,
            name="Encrypted",
            provider_type=LLMProvider.ProviderType.OLLAMA,
            model="qwen3:latest",
        )
        provider.api_key = "my-secret-key"
        provider.save()

        provider.refresh_from_db()
        self.assertNotEqual(provider._api_key_encrypted, "my-secret-key")
        self.assertTrue(provider._api_key_encrypted.startswith("gAAAA"))

    def test_api_key_decrypts_on_read(self):
        org = Organization.objects.create(name="Dec", slug="dec")
        provider = LLMProvider.objects.create(
            organization=org,
            name="Decrypted",
            provider_type=LLMProvider.ProviderType.OLLAMA,
            model="qwen3:latest",
        )
        provider.api_key = "another-secret"
        provider.save()

        provider.refresh_from_db()
        self.assertEqual(provider.api_key, "another-secret")

    def test_api_key_backwards_compatible_with_plaintext(self):
        org = Organization.objects.create(name="Plain", slug="plain")
        provider = LLMProvider.objects.create(
            organization=org,
            name="Plaintext",
            provider_type=LLMProvider.ProviderType.OLLAMA,
            model="qwen3:latest",
        )
        provider._api_key_encrypted = "old-plaintext-key"
        provider.save()

        provider.refresh_from_db()
        self.assertEqual(provider.api_key, "old-plaintext-key")
