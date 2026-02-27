"""Button platform for SonicBit Media Sync."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SonicBitCoordinator

BUTTON_DESCRIPTION = ButtonEntityDescription(
    key="force_sync",
    name="SonicBit Force Sync",
    icon="mdi:cloud-download",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SonicBit button from a config entry."""
    coordinator: SonicBitCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SonicBitForceSyncButton(coordinator, entry)])


class SonicBitForceSyncButton(CoordinatorEntity[SonicBitCoordinator], ButtonEntity):
    """Button that manually triggers a SonicBit sync cycle."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SonicBitCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = BUTTON_DESCRIPTION
        self._attr_unique_id = f"{entry.entry_id}_force_sync"

    async def async_press(self) -> None:
        """Trigger an immediate sync when the button is pressed."""
        await self.coordinator.async_force_sync()
