"""
Django settings for vogon project.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-change-me-in-production",
)

DEBUG = os.environ.get("DJANGO_DEBUG", "True").lower() in ("true", "1", "yes")

ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,web,grpc,mcp").split(
    ","
)

CSRF_TRUSTED_ORIGINS = (
    os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS")
    else []
)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

INSTALLED_APPS = [
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "whitenoise.runserver_nostatic",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "rest_framework",
    "rest_framework.authtoken",
    "apps.core",
    "apps.llm",
    "apps.marvins",
    "apps.sessions",
    "apps.infradesigns",
    "apps.ws",
    "channels_postgres",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "apps.core.middleware.OrganizationSetupMiddleware",
]

ROOT_URLCONF = "vogon.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "vogon.wsgi.application"
ASGI_APPLICATION = "vogon.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": os.environ.get("DJANGO_DB_ENGINE", "django.db.backends.postgresql"),
        "NAME": os.environ.get("DJANGO_DB_NAME", "vogon"),
        "USER": os.environ.get("DJANGO_DB_USER", "postgres"),
        "PASSWORD": os.environ.get("DJANGO_DB_PASSWORD", "postgres"),
        "HOST": os.environ.get("DJANGO_DB_HOST", "localhost"),
        "PORT": os.environ.get("DJANGO_DB_PORT", "5432"),
    },
    "channels_postgres": {
        "ENGINE": os.environ.get("DJANGO_DB_ENGINE", "django.db.backends.postgresql"),
        "NAME": os.environ.get("DJANGO_DB_NAME", "vogon"),
        "USER": os.environ.get("DJANGO_DB_USER", "postgres"),
        "PASSWORD": os.environ.get("DJANGO_DB_PASSWORD", "postgres"),
        "HOST": os.environ.get("DJANGO_DB_HOST", "localhost"),
        "PORT": os.environ.get("DJANGO_DB_PORT", "5432"),
    },
}

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = Path("/tmp/vogon-staticfiles")
STATICFILES_DIRS = [BASE_DIR / "static"]

STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "core.User"

# DRF
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.TokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
}

# django-allauth
SITE_ID = 1
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]
ACCOUNT_SIGNUP_FIELDS = ["email*", "username*", "password1*", "password2*"]
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/"

# Temporal
TEMPORAL_HOST = os.environ.get("TEMPORAL_HOST", "localhost:7233")
TEMPORAL_NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "default")
TEMPORAL_TASK_QUEUE = os.environ.get("TEMPORAL_TASK_QUEUE", "vogon")

# gRPC
GRPC_PORT = int(os.environ.get("GRPC_PORT", "50051"))

# MCP
MCP_PORT = int(os.environ.get("MCP_PORT", "8080"))

# LLM
LLM_PROVIDER_TYPE = os.environ.get("LLM_PROVIDER_TYPE", "ollama")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://ollama.com/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-oss:20b")
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.2"))
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "4096"))
LLM_MAX_AGENT_ITERATIONS = int(os.environ.get("LLM_MAX_AGENT_ITERATIONS", "12"))
LLM_TOOL_TIMEOUT_SECONDS = int(os.environ.get("LLM_TOOL_TIMEOUT_SECONDS", "60"))
MCP_TOOL_TIMEOUT_SECONDS = int(os.environ.get("MCP_TOOL_TIMEOUT_SECONDS", "60"))
LLM_MAX_PARALLEL_TOOL_CALLS = int(os.environ.get("LLM_MAX_PARALLEL_TOOL_CALLS", "6"))
LLM_HTTP_TIMEOUT_SECONDS = int(os.environ.get("LLM_HTTP_TIMEOUT_SECONDS", "590"))
# Feature gate: expose Prometheus tools directly to the LLM (via standard tools).
# When False, Prometheus queries must go through Marvin capabilities.
LLM_PROMETHEUS_TOOLS_ENABLED = os.environ.get("LLM_PROMETHEUS_TOOLS_ENABLED", "False").lower() in (
    "true",
    "1",
    "yes",
)
PROMETHEUS_BASE_URL = os.environ.get("PROMETHEUS_BASE_URL", "")
PROMETHEUS_AUTH_TOKEN = os.environ.get("PROMETHEUS_AUTH_TOKEN", "")

# Embeddings
EMBEDDING_PROVIDER_TYPE = os.environ.get("EMBEDDING_PROVIDER_TYPE", "openai")
EMBEDDING_BASE_URL = os.environ.get("EMBEDDING_BASE_URL", "https://api.openai.com/v1")
EMBEDDING_API_KEY = os.environ.get("EMBEDDING_API_KEY", "")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIMENSIONS = int(os.environ.get("EMBEDDING_DIMENSIONS", "1536"))
EMBEDDING_SEARCH_ENABLED = os.environ.get("EMBEDDING_SEARCH_ENABLED", "False").lower() in (
    "true",
    "1",
    "yes",
)

EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = os.environ.get("EMAIL_HOST", "localhost")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "25"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "False").lower() in (
    "true",
    "1",
    "yes",
)
EMAIL_USE_SSL = os.environ.get("EMAIL_USE_SSL", "False").lower() in (
    "true",
    "1",
    "yes",
)
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "")

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_postgres.core.PostgresChannelLayer",
        "CONFIG": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("DJANGO_DB_NAME", "vogon"),
            "USER": os.environ.get("DJANGO_DB_USER", "postgres"),
            "PASSWORD": os.environ.get("DJANGO_DB_PASSWORD", "postgres"),
            "HOST": os.environ.get("DJANGO_DB_HOST", "localhost"),
            "PORT": os.environ.get("DJANGO_DB_PORT", "5432"),
            "TIME_ZONE": TIME_ZONE,
            "USE_TZ": USE_TZ,
        },
    }
}
