"""Where credentials come from at run time.

Artifacts only ever NAME secrets ({{secrets.operator_password}}). Values are read from
environment variables called CUA_SECRET_<NAME>, which locally come from .env. In
production, this class is the seam where a real vault (AWS Secrets Manager,
HashiCorp Vault, ...) would plug in, without changing anything that calls it.
"""

import os
from collections.abc import Iterable, Mapping


class MissingSecret(LookupError):
    """A secret the app profile needs has not been configured."""


class SecretStore:
    PREFIX = "CUA_SECRET_"

    def __init__(self, environ: Mapping[str, str] | None = None):
        self._environ = os.environ if environ is None else environ

    def get(self, name: str) -> str:
        key = self.PREFIX + name.upper()
        value = self._environ.get(key)
        if not value:
            # The message names the variable to set, never any value.
            raise MissingSecret(f"secret '{name}' is not configured (set {key})")
        return value

    def resolve(self, names: Iterable[str]) -> dict[str, str]:
        return {name: self.get(name) for name in names}