from django.shortcuts import redirect

EXEMPT_PATHS = {
    "/accounts/",
    "/admin/",
    "/org/setup/",
    "/static/",
    "/media/",
}


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
