# -*- coding: utf-8 -*-
import logging
from typing import Any

from homeassistant.components.light import LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .aiot.aiot_device import AIoTDevice, AIoTPropertyEntity
from .aiot.const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        async_add_entities: AddEntitiesCallback,
) -> None:
    """设置灯光平台."""
    device_list: list[AIoTDevice] = hass.data[DOMAIN]['devices'][config_entry.entry_id]

    # 创建灯光实体
    new_entities = []
    for aiot_device in device_list:
        for data in aiot_device.entity_list.get('light', []):
            new_entities.append(AimyLightEntity(aiot_device=aiot_device))

    if new_entities:
        async_add_entities(new_entities)


class AimyLightEntity(AIoTPropertyEntity, LightEntity):
    """表示智能盒子灯光实体."""

    def __init__(self, aiot_device: AIoTDevice) -> None:
        """初始化灯光."""
        super().__init__(aiot_device=aiot_device)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """打开灯光."""
        # 构建控制命令
        json_data = {"State": 1}

        # 处理亮度
        if "brightness" in kwargs:
            brightness = kwargs["brightness"]
            json_data["Brightness"] = brightness
            # self._attr_brightness = brightness

        # 处理颜色温度
        if "color_temp" in kwargs:
            color_temp = kwargs["color_temp"]
            json_data["ColorTemp"] = color_temp

        # 处理RGB颜色
        if "rgb_color" in kwargs:
            rgb_color = kwargs["rgb_color"]
            json_data["RGB"] = rgb_color

        cmd = ''
        await self.ctrl_device_async(cmd, json_data)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """关闭灯光."""
        cmd = ''
        json_data = {"State": 0}
        await self.ctrl_device_async(cmd, json_data)
