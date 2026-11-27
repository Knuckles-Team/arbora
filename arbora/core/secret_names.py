"""Which configuration names hold a credential value."""

from __future__ import annotations

# Suffixes that mark a key as a credential VALUE holder: blanked in generated
# templates so a committed or shared config.json never carries a secret.
# Suffix-precise so config keys like SECRETS_BACKEND and SECRETS_VAULT_URL (not
# credentials) are NOT blanked, while raw credential-value fields stay excluded
# from current config.
SECRET_SUFFIXES = (
    "API_KEY",
    "PASSWORD",
    "PRIVATE_KEY",
    "SECRET_KEY",
    "ACCESS_KEY",
    "_SECRET",
    "_TOKEN",
    "TOKEN",
)


def is_secret_name(env_key: str) -> bool:
    """Whether ``env_key`` names a credential value."""
    up = env_key.upper()
    return any(up.endswith(suffix) for suffix in SECRET_SUFFIXES)
