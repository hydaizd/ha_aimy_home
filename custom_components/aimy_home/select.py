# -*- coding: utf-8 -*-
import logging
from typing import Optional

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .aiot.aiot_device import AIoTPropertyEntity, AIoTDevice
from .aiot.aiot_spec import AIoTSpecProperty
from .aiot.const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up a config entry."""
    device_list: list[AIoTDevice] = hass.data[DOMAIN]['devices'][config_entry.entry_id]

    new_entities = []
    for aiot_device in device_list:
        for prop in aiot_device.prop_list.get('select', []):
            new_entities.append(AimySelectEntity(aiot_device=aiot_device, spec=prop))

    if new_entities:
        async_add_entities(new_entities)


class AimySelectEntity(AIoTPropertyEntity, SelectEntity):
    def __init__(
            self,
            aiot_device: AIoTDevice,
            spec: AIoTSpecProperty
    ) -> None:
        """Initialize the Select."""
        super().__init__(aiot_device=aiot_device, spec=spec)
        if self._value_list:
            # 下拉框所有选项
            self._attr_options = self._value_list.descriptions

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        value = self.get_vlist_value(description=option)
        await self.set_property_async(value)

    @property
    def current_option(self) -> Optional[str]:
        """Return the current selected option."""
        return self.get_vlist_description(value=self._value)
