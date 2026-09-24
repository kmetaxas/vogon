# pyright: reportAttributeAccessIssue=false
"""Authentication support for Marvin agents using registration keys."""

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import BaseBackend

from apps.marvins.models import MarvinRegistrationKey

User = get_user_model()


def validate_marvin_registration_key(key: str):
    """Validate a Marvin registration key and return its organization.

    Args:
        key: The registration key string sent by the Marvin agent.

    Returns:
        The Organization associated with the key, or None if the key
        is invalid or inactive.
    """
    try:
        reg_key = MarvinRegistrationKey.objects.select_related("organization").get(
            key=key, active=True
        )
        return reg_key.organization
    except MarvinRegistrationKey.DoesNotExist:
        return None


class MarvinKeyBackend(BaseBackend):
    """Django authentication backend that validates Marvin registration keys.

    This backend allows Marvin agents to authenticate using their
    organization-scoped registration key.  It is intended for use by
    gRPC/DRF layers that need to identify the agent's organization.
    """

    def authenticate(self, request, marvin_key=None, **kwargs):
        if not marvin_key:
            return None

        organization = validate_marvin_registration_key(marvin_key)
        if organization is None:
            return None

        # Return the first user in the organization so that Django's
        # auth machinery has a valid User object.  In practice this
        # backend is used for organization validation, not interactive
        # login.
        return organization.users.first()

    def get_user(self, user_id):
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None
