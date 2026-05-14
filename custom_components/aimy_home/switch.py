# -*- coding: utf-8 -*-
import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .aiot.aiot_device import AIoTDevice, AIoTPropertyEntity
from .aiot.aiot_spec import AIoTSpecProperty
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
        for prop in aiot_device.prop_list.get('switch', []):
            new_entities.append(AimySwitchEntity(aiot_device=aiot_device, spec=prop))

    if new_entities:
        async_add_entities(new_entities)


class AimySwitchEntity(AIoTPropertyEntity, SwitchEntity):
    """表示智空间盒子开关实体."""
    def __init__(
            self,
            aiot_device: AIoTDevice,
            spec: AIoTSpecProperty
    ) -> None:
        """初始化开关."""
        super().__init__(aiot_device=aiot_device, spec=spec)

    @property
    def is_on(self) -> bool:
        """开/关 状态."""
        return self._value == 1

    async def async_turn_on(self, **kwargs: Any) -> None:
        """打开开关."""
        value = 1
        await self.set_property_async(value)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """关闭开关."""
        value = 0
        await self.set_property_async(value)

    async def async_toggle(self, **kwargs: Any) -> None:
        """切换开关."""
        value = 0 if self.is_on else 1
        await self.set_property_async(value)
