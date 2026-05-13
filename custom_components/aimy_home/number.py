# -*- coding: utf-8 -*-

from typing import Optional

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .aiot.aiot_device import AIoTPropertyEntity, AIoTDevice
from .aiot.aiot_spec import AIoTSpecProperty
from .aiot.const import DOMAIN


async def async_setup_entry(
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        async_add_entities: AddEntitiesCallback,
) -> None:
    device_list: list[AIoTDevice] = hass.data[DOMAIN]['devices'][config_entry.entry_id]

    new_entities = []
    for aiot_device in device_list:
        for prop in aiot_device.prop_list.get('number', []):
            new_entities.append(AimyNumberEntity(aiot_device=aiot_device, spec=prop))

    if new_entities:
        async_add_entities(new_entities)


class AimyNumberEntity(AIoTPropertyEntity, NumberEntity):
    def __init__(self, aiot_device: AIoTDevice, spec: AIoTSpecProperty) -> None:
        super().__init__(aiot_device=aiot_device, spec=spec)

        # Set value range
        if self._value_range:
            self._attr_native_min_value = self._value_range.min_
            self._attr_native_max_value = self._value_range.max_
            self._attr_native_step = self._value_range.step

    @property
    def native_value(self) -> Optional[float]:
        """Return the current value of the number."""
        return self._value

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        await self.set_property_async(value=value)
