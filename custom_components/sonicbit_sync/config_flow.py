"""Config flow for SonicBit Media Sync."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.exceptions import HomeAssistantError

from .const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REMOTE_FOLDER,
    CONF_STORAGE_PATH,
    DEFAULT_STORAGE_PATH,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_STORAGE_PATH, default=DEFAULT_STORAGE_PATH): str,
        vol.Optional(CONF_REMOTE_FOLDER, default=""): str,
    }
)


def _test_credentials(email: str, password: str, config_dir: str) -> None:
    """Attempt to authenticate with SonicBit (blocking).

    Raises an exception if authentication fails so the config flow can
    surface an appropriate error to the user.
    """
    from sonicbit import SonicBit  # noqa: PLC0415

    from .compat import apply_sonicbit_patches  # noqa: PLC0415
    from .token_handler import HATokenHandler

    apply_sonicbit_patches()
    handler = HATokenHandler(config_dir, "validation")
    client = SonicBit(email=email, password=password, token_handler=handler)
    # A lightweight call to confirm credentials are accepted
    client.get_user_details()


class SonicBitConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial UI setup for SonicBit Media Sync."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the setup form and validate credentials on submit."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Prevent the same account being configured twice
            await self.async_set_unique_id(user_input[CONF_EMAIL].lower())
            self._abort_if_unique_id_configured()

            try:
                await self.hass.async_add_executor_job(
                    _test_credentials,
                    user_input[CONF_EMAIL],
                    user_input[CONF_PASSWORD],
                    self.hass.config.config_dir,
                )
            except HomeAssistantError:
                raise
            except Exception:  # noqa: BLE001
                _LOGGER.exception("SonicBit authentication failed during setup")
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=user_input[CONF_EMAIL],
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )
