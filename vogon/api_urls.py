from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.core.api_views import OrganizationViewSet
from apps.infradesigns.api_views import InfrastructureDesignViewSet
from apps.llm.api_views import LLMProviderViewSet
from apps.marvins.api_views import (
    CapabilityViewSet,
    MarvinConfigViewSet,
    MarvinViewSet,
    ResourceTypeViewSet,
    ResourceViewSet,
)
from apps.sessions.api_views import (
    AgentEventViewSet,
    ArchitectureRequestViewSet,
    MessageViewSet,
    ThreadMembershipViewSet,
    ThreadViewSet,
    ToolCallViewSet,
    TSessionViewSet,
)

router = DefaultRouter()
router.register(r"organizations", OrganizationViewSet, basename="organization")
router.register(r"sessions", TSessionViewSet, basename="session")
router.register(r"threads", ThreadViewSet, basename="thread")
router.register(r"thread-memberships", ThreadMembershipViewSet, basename="threadmembership")
router.register(r"messages", MessageViewSet, basename="message")
router.register(r"tool-calls", ToolCallViewSet, basename="toolcall")
router.register(r"agent-events", AgentEventViewSet, basename="agentevent")
router.register(
    r"architecture-requests", ArchitectureRequestViewSet, basename="architecturerequest"
)
router.register(r"infrastructure-designs", InfrastructureDesignViewSet, basename="infradesign")
router.register(r"marvins", MarvinViewSet, basename="marvin")
router.register(r"capabilities", CapabilityViewSet, basename="capability")
router.register(r"resource-types", ResourceTypeViewSet, basename="resourcetype")
router.register(r"resources", ResourceViewSet, basename="resource")
router.register(r"marvin-configs", MarvinConfigViewSet, basename="marvinconfig")
router.register(r"llm-providers", LLMProviderViewSet, basename="llmprovider")

urlpatterns = [
    path("", include(router.urls)),
]
