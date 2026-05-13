# -*- coding: utf-8 -*-
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .aiot.aiot_device import AIoTDevice, AIoTActionEntity
from .aiot.aiot_spec import AIoTSpecAction
from .aiot.const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        async_add_entities: AddEntitiesCallback,
) -> None:
    """设置开关平台."""
    device_list: list[AIoTDevice] = hass.data[DOMAIN]['devices'][config_entry.entry_id]

    # 创建开关实体
    new_entities = []
    for aiot_device in device_list:
        for action in aiot_device.action_list.get('button', []):
            new_entities.append(AimyButtonEntity(aiot_device=aiot_device, spec=action))

    if new_entities:
        async_add_entities(new_entities)


class AimyButtonEntity(AIoTActionEntity, ButtonEntity):
    """Button entities for Aimy Home."""

    def __init__(self, aiot_device: AIoTDevice, spec: AIoTSpecAction) -> None:
        """Initialize the Button."""
        super().__init__(aiot_device=aiot_device, spec=spec)
        # Use default device class

    async def async_press(self) -> None:
        """Press the button."""
        return await self.action_async()
