# -*- coding: utf-8 -*-
import logging
from typing import Optional

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry, entity_registry

from aiot.const import (
    DOMAIN,
    SUPPORTED_PLATFORMS,
    DEFAULT_INTEGRATION_LANGUAGE
)
from .aiot.aiot_client import AIoTClient, get_aiot_instance_async
from .aiot.aiot_device import AIoTDevice
from .aiot.aiot_error import AIoTAuthError, AIoTClientError
from .aiot.aiot_spec import AIoTSpecParser, AIoTSpecInstance
from .aiot.aiot_storage import AIoTStorage
from .aiot.common import slugify_did

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, hass_config: dict) -> bool:
    # pylint: disable=unused-argument
    hass.data.setdefault(DOMAIN, {})
    # {[entry_id:str]: IoTClient}, iot client instance
    hass.data[DOMAIN].setdefault('aiot_clients', {})
    # {[entry_id:str]: list[IoTDevice]}
    hass.data[DOMAIN].setdefault('devices', {})
    # {[entry_id:str]: entities}
    hass.data[DOMAIN].setdefault('entities', {})
    for platform in SUPPORTED_PLATFORMS:
        hass.data[DOMAIN]['entities'][platform] = []
    return True


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """设置配置条目.配置条目创建后集成的入口点"""

    def ha_persistent_notify(
            notify_id: str,
            title: Optional[str] = None,
            message: Optional[str] = None
    ) -> None:
        """Send messages in Notifications dialog box."""
        if title:
            persistent_notification.async_create(
                hass=hass,
                message=message or '',
                title=title,
                notification_id=notify_id
            )
        else:
            persistent_notification.async_dismiss(hass=hass, notification_id=notify_id)

    entry_id = config_entry.entry_id
    entry_data = dict(config_entry.data)

    ha_persistent_notify(notify_id=f'{entry_id}.auth_error', title=None, message=None)

    try:
        aiot_client: AIoTClient = await get_aiot_instance_async(
            hass=hass,
            entry_id=entry_id,
            entry_data=entry_data,
            persistent_notify=ha_persistent_notify
        )
        # Spec parser
        spec_parser = AIoTSpecParser(
            lang=entry_data.get('integration_language', DEFAULT_INTEGRATION_LANGUAGE),
            storage=aiot_client.aiot_storage,
            entry_data=entry_data,
            loop=aiot_client.main_loop
        )
        await spec_parser.init_async()

        aiot_devices: list[AIoTDevice] = []
        for did, info in aiot_client.device_list.items():
            spec_instance = await spec_parser.parse(urn=info['urn'])
            if not isinstance(spec_instance, AIoTSpecInstance):
                _LOGGER.error('spec content is None, %s, %s', did, info)
                continue
            device: AIoTDevice = AIoTDevice(
                aiot_client=aiot_client,
                device_info={
                    **info,
                    'manufacturer': "艾美科技"
                },
                spec_instance=spec_instance
            )
            aiot_devices.append(device)

        hass.data[DOMAIN]['devices'][config_entry.entry_id] = aiot_devices
        # 设置平台
        await hass.config_entries.async_forward_entry_setups(config_entry, SUPPORTED_PLATFORMS)

        # 移除删除的设备
        devices_remove = (await aiot_client.aiot_storage.load_user_config_async(
            uname=config_entry.data['uname'],
            lan_server=config_entry.data['lan_server'],
            keys=['devices_remove']
        )).get('devices_remove', [])
        if isinstance(devices_remove, list) and devices_remove:
            dr = device_registry.async_get(hass)
            for did in devices_remove:
                device_entry = dr.async_get_device(
                    identifiers={(
                        DOMAIN,
                        slugify_did(lan_server=config_entry.data['lan_server'], did=did))
                    },
                    connections=None
                )
                if not device_entry:
                    _LOGGER.error('remove device not found, %s', did)
                    continue
                dr.async_remove_device(device_id=device_entry.id)
                _LOGGER.info('delete device entry, %s, %s', did, device_entry.id)
            await aiot_client.aiot_storage.update_user_config_async(
                uname=config_entry.data['uname'],
                lan_server=config_entry.data['lan_server'],
                config={'devices_remove': []}
            )
    except AIoTAuthError as auth_error:
        ha_persistent_notify(
            notify_id=f'{entry_id}.auth_error',
            title='Aimy Auth Error',
            message=f'Please re-add.\r\nerror: {auth_error}'
        )
    except Exception as err:
        raise err

    return True


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """卸载配置条目."""
    entry_id = config_entry.entry_id
    unload_ok = await hass.config_entries.async_unload_platforms(config_entry, SUPPORTED_PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN]['entities'].pop(entry_id, None)
        hass.data[DOMAIN]['devices'].pop(entry_id, None)

    # Remove integration data
    aiot_client: AIoTClient = hass.data[DOMAIN]['aiot_clients'].pop(entry_id, None)
    if aiot_client:
        await aiot_client.deinit_async()
    del aiot_client

    return True


async def async_remove_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Remove the entry."""
    entry_data = dict(config_entry.data)
    uname: str = entry_data['uname']
    lan_server: str = entry_data['lan_server']
    aiot_storage: AIoTStorage = hass.data[DOMAIN]['aiot_storage']

    # Clean device list
    await aiot_storage.remove_async(domain='aiot_devices', name=f'{uname}_{lan_server}', type_=dict)
    # Clean user configuration
    await aiot_storage.update_user_config_async(uname=uname, lan_server=lan_server, config=None)
    return True


async def async_remove_config_entry_device(
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        device_entry: device_registry.DeviceEntry
) -> bool:
    """Remove the device."""
    aiot_client: AIoTClient = await get_aiot_instance_async(hass=hass, entry_id=config_entry.entry_id)

    if len(device_entry.identifiers) != 1:
        _LOGGER.error('remove device failed, invalid identifiers, %s, %s', device_entry.id, device_entry.identifiers)
        return False
    identifiers = list(device_entry.identifiers)[0]
    if identifiers[0] != DOMAIN:
        _LOGGER.error('remove device failed, invalid domain, %s, %s', device_entry.id, device_entry.identifiers)
        return False

    # Remove device
    await aiot_client.remove_device2_async(did_tag=identifiers[1])
    device_registry.async_get(hass).async_remove_device(device_entry.id)
    _LOGGER.info('remove device, %s, %s', identifiers[1], device_entry.id)
    return True
