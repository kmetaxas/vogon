import time

from django.http import HttpResponse
from django.shortcuts import redirect

EXEMPT_PATHS = {
    "/accounts/",
    "/admin/",
    "/org/setup/",
    "/static/",
    "/media/",
}

BRAKE_EXEMPT_PATHS = {
    "/accounts/",
    "/admin/",
    "/static/",
    "/media/",
    "/org/emergency-brake/",
}

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class OrganizationSetupMiddleware:
    """Redirect authenticated users without an organization to the setup page.

    This catches any views that do not use OrganizationRequiredMixin."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            request.user.is_authenticated
            and not request.user.organizations.exists()
            and not request.user.is_superuser
            and not any(request.path.startswith(p) for p in EXEMPT_PATHS)
        ):
            return redirect("core:org-setup")
        return self.get_response(request)


class EmergencyBrakeMiddleware:
    """Block execution paths when an organization's emergency brake is enabled."""

    _brake_cache = {}
    _cache_ttl = 5

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self._should_check(request):
            org = self._get_organization(request)
            if org and self._is_brake_active(org.id):
                return HttpResponse("Emergency brake is active", status=503)
        return self.get_response(request)

    def _should_check(self, request):
        if request.method in SAFE_METHODS:
            return False
        if any(request.path.startswith(p) for p in BRAKE_EXEMPT_PATHS):
            return False
        if not request.user.is_authenticated:
            return False
        if request.user.is_superuser:
            return False
        return True

    def _get_organization(self, request):
        return request.user.get_current_organization()

    @classmethod
    def _is_brake_active(cls, org_id):
        now = time.time()
        cached = cls._brake_cache.get(org_id)
        if cached is not None:
            active, timestamp = cached
            if now - timestamp < cls._cache_ttl:
                return active
        try:
            from apps.core.models import OrganizationEmergencyBrake

            active = OrganizationEmergencyBrake.objects.filter(
                organization_id=org_id, is_active=True
            ).exists()
        except Exception:
            active = False
        cls._brake_cache[org_id] = (active, now)
        return active

    @classmethod
    def clear_cache(cls, org_id=None):
        if org_id is None:
            cls._brake_cache.clear()
        else:
            cls._brake_cache.pop(org_id, None)
