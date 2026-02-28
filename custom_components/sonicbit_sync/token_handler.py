"""Home Assistant-aware token handler for the SonicBit SDK."""

from __future__ import annotations

import json
import logging
import os

from sonicbit.handlers.token_handler import TokenHandler
from sonicbit.models import AuthResponse

_LOGGER = logging.getLogger(__name__)


class HATokenHandler(TokenHandler):
    """Stores SonicBit auth tokens in the HA config directory.

    This replaces the default TokenFileHandler (which writes to the
    current working directory) with one that targets a deterministic,
    per-config-entry path inside the HA config folder.
    """

    def __init__(self, config_dir: str, entry_id: str) -> None:
        super().__init__()
        self._path = os.path.join(config_dir, f".sonicbit_token_{entry_id}.json")

    def read(self, email: str) -> str | None:
        """Return the cached token for *email*, or None if not found."""
        try:
            with open(self._path) as fh:
                data = json.load(fh)
            return data.get(email)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def write(self, email: str, auth: AuthResponse) -> None:
        """Persist the token from *auth* for *email* to disk."""
        try:
            with open(self._path, "w") as fh:
                json.dump({email: auth.token}, fh)
        except OSError as err:
            _LOGGER.warning("Could not write SonicBit token cache: %s", err)
