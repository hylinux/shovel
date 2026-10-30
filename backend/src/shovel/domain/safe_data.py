from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, SecretBytes, SecretStr

_CREDENTIAL_FIELDS = frozenset(
    {
        "password",
        "passwd",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "client_secret",
        "private_key",
        "authorization",
        "credentials",
        "secret",
    }
)


def ensure_no_credentials(value: object) -> None:
    if isinstance(value, SecretStr | SecretBytes):
        raise ValueError("Credentials must use connection references, not persisted values.")
    if isinstance(value, BaseModel):
        for field in type(value).model_fields:
            ensure_no_credentials(getattr(value, field))
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and key.casefold().replace("-", "_") in _CREDENTIAL_FIELDS:
                raise ValueError("Credentials must use connection references, not persisted fields.")
            ensure_no_credentials(item)
    elif isinstance(value, tuple | list | set | frozenset):
        for item in value:
            ensure_no_credentials(item)
