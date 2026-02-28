"""Switch platform for SonicBit Media Sync."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SonicBitCoordinator

SWITCH_DESCRIPTION = SwitchEntityDescription(
    key="auto_delete",
    name="SonicBit Auto Delete",
    icon="mdi:delete-sweep",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SonicBit switch from a config entry."""
    coordinator: SonicBitCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SonicBitAutoDeleteSwitch(coordinator, entry)])


class SonicBitAutoDeleteSwitch(
    CoordinatorEntity[SonicBitCoordinator], SwitchEntity, RestoreEntity
):
    """Switch that enables or disables automatic seedbox cleanup after download."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SonicBitCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = SWITCH_DESCRIPTION
        self._attr_unique_id = f"{entry.entry_id}_auto_delete"

    @property
    def is_on(self) -> bool:
        """Return True if auto-delete is enabled."""
        return self.coordinator.auto_delete

    async def async_added_to_hass(self) -> None:
        """Restore last known state on startup."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self.coordinator.auto_delete = last_state.state != "off"

    async def async_turn_on(self, **kwargs) -> None:
        """Enable automatic deletion of the seedbox copy after download."""
        self.coordinator.auto_delete = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        """Disable automatic deletion of the seedbox copy after download."""
        self.coordinator.auto_delete = False
        self.async_write_ha_state()
