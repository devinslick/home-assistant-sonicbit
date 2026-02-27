"""Sensor platform for SonicBit Media Sync."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, STATUS_IDLE
from .coordinator import SonicBitCoordinator

SENSOR_DESCRIPTIONS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="storage",
        name="SonicBit Storage",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:cloud-percent",
    ),
    SensorEntityDescription(
        key="status",
        name="SonicBit Status",
        icon="mdi:cloud-sync",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SonicBit sensors from a config entry."""
    coordinator: SonicBitCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        SonicBitSensor(coordinator, entry, desc)
        for desc in SENSOR_DESCRIPTIONS
    )


class SonicBitSensor(CoordinatorEntity[SonicBitCoordinator], SensorEntity):
    """A sensor that reads live data from the SonicBit coordinator."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SonicBitCoordinator,
        entry: ConfigEntry,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"

    @property
    def native_value(self) -> float | str | None:
        """Return the current sensor value."""
        if self.entity_description.key == "storage":
            return round(self.coordinator.storage_percent, 1)
        if self.entity_description.key == "status":
            return self.coordinator.status
        return None

    @property
    def extra_state_attributes(self) -> dict | None:
        """Expose extra details for the storage sensor."""
        if self.entity_description.key == "storage" and self.coordinator.data:
            return {
                "currently_downloading": list(self.coordinator._downloading),
            }
        return None
