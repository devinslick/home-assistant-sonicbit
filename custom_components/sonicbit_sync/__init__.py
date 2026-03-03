"""SonicBit Media Sync – Home Assistant integration."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall

from .const import DOMAIN
from .coordinator import SonicBitCoordinator

PLATFORMS: list[str] = ["sensor", "button", "switch"]

SERVICE_ADD_TORRENT = "add_torrent"
SERVICE_SCHEMA_ADD_TORRENT = vol.Schema({vol.Required("uri"): str})


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SonicBit Media Sync from a config entry."""
    coordinator = SonicBitCoordinator(hass, entry)

    # Perform the first refresh so entities have data before they load.
    # UpdateFailed raised here will surface as a config entry error in the UI.
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register the add_torrent service once; the handler fans out to all
    # active coordinators so it works regardless of how many entries exist.
    if not hass.services.has_service(DOMAIN, SERVICE_ADD_TORRENT):

        async def _handle_add_torrent(call: ServiceCall) -> None:
            uri: str = call.data["uri"]
            for coord in hass.data.get(DOMAIN, {}).values():
                if isinstance(coord, SonicBitCoordinator):
                    await coord.async_add_torrent(uri)

        hass.services.async_register(
            DOMAIN,
            SERVICE_ADD_TORRENT,
            _handle_add_torrent,
            schema=SERVICE_SCHEMA_ADD_TORRENT,
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a SonicBit config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        # Remove the service when the last entry is unloaded.
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_ADD_TORRENT)
    return unload_ok
