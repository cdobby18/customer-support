"""Startup configuration validation.

Fails fast on misconfigured providers and refuses to boot production with
development defaults or the mock LLM provider.
"""

import os


def validate_security_configuration() -> None:
    provider = os.getenv("SUPPORT_TOOL_PROVIDER", "").strip().lower()
    if provider and provider not in {"mock", "zendesk", "hubspot"}:
        raise RuntimeError(
            f"SUPPORT_TOOL_PROVIDER={provider!r} is invalid; expected one of: mock, zendesk, hubspot"
        )
    llm_provider = os.getenv("LLM_PROVIDER", "").strip().lower()
    if llm_provider and llm_provider not in {"mock", "openai", "azure_openai"}:
        raise RuntimeError(
            f"LLM_PROVIDER={llm_provider!r} is invalid; expected one of: mock, openai, azure_openai"
        )
    embedding_provider = os.getenv("EMBEDDING_PROVIDER", "local").strip().lower()
    if embedding_provider not in {"local", "openai"}:
        raise RuntimeError(
            f"EMBEDDING_PROVIDER={embedding_provider!r} is invalid; expected one of: local, openai"
        )
    if os.getenv("APP_ENV", "development").lower() != "production":
        return
    required_secrets = {
        "JWT_SECRET": "local-development-secret-change-me",
        "CHANNEL_WEBHOOK_SECRET": "local-webhook-secret",
    }
    for variable_name, insecure_default in required_secrets.items():
        configured_value = os.getenv(variable_name, "")
        if not configured_value or configured_value == insecure_default:
            raise RuntimeError(f"{variable_name} must be configured for production")
    if llm_provider == "mock":
        raise RuntimeError(
            "LLM_PROVIDER=mock is not allowed in production; use openai or azure_openai"
        )
    if llm_provider == "openai" and not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError(
            "OPENAI_API_KEY must be configured when LLM_PROVIDER=openai in production"
        )
    if llm_provider == "azure_openai":
        missing = [
            variable
            for variable in (
                "AZURE_OPENAI_ENDPOINT",
                "AZURE_OPENAI_DEPLOYMENT",
                "AZURE_OPENAI_API_KEY",
            )
            if not os.getenv(variable)
        ]
        if missing:
            required = ", ".join(missing)
            raise RuntimeError(
                f"{required} must be configured when LLM_PROVIDER=azure_openai in production"
            )
    if embedding_provider == "openai" and not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError(
            "OPENAI_API_KEY must be configured when EMBEDDING_PROVIDER=openai in production"
        )
