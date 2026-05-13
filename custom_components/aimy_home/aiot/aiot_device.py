# -*- coding: utf-8 -*-

import asyncio
import logging
from abc import abstractmethod
from typing import Any, Optional, Callable

from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity import Entity

from .aiot_client import AIoTClient
from .aiot_error import AIoTDeviceError, AIoTClientError
from .aiot_mips import AIoTDeviceState
from .aiot_spec import (
    AIoTSpecProperty,
    AIoTSpecAction,
    AIoTSpecInstance,
    AIoTSpecEvent,
    AIoTSpecValueRange,
    AIoTSpecValueList,
    AIoTSpecService
)
from .common import slugify_did, slugify_name, get_service_name, get_prop_name
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class AIoTEntityData:
    """AIoT 实体数据."""
    platform: str
    device_class: Any
    spec: AIoTSpecInstance | AIoTSpecService

    props: set[AIoTSpecProperty]
    events: set[AIoTSpecEvent]
    actions: set[AIoTSpecAction]

    def __init__(
            self, platform: str, spec: AIoTSpecInstance | AIoTSpecService
    ) -> None:
        self.platform = platform
        self.spec = spec
        self.device_class = None
        self.props = set()
        self.events = set()
        self.actions = set()


class AIoTDevice:
    """智能设备."""
    # pylint: disable=unused-argument
    aiot_client: AIoTClient
    spec_instance: AIoTSpecInstance

    _online: bool

    _did: str
    _name: str
    _model: str
    _manufacturer: str
    _fw_version: str

    _sub_id: int
    _device_state_sub_list: dict[str, dict[str, Callable[[str, AIoTDeviceState], None]]]
    _value_sub_list: dict[str, dict[str, Callable[[dict, Any], None]]]

    _entity_list: dict[str, list[AIoTEntityData]]
    _prop_list: dict[str, list[AIoTSpecProperty]]
    _event_list: dict[str, list[AIoTSpecEvent]]
    _action_list: dict[str, list[AIoTSpecAction]]

    # 实体值映射表
    _entity_map: dict[str, Any]
    _mid_bind_id: str
    _product_key: str
    _endpoint: str
    _group_id: str
    _endpoint_name: str

    def __init__(self, aiot_client: AIoTClient, device_info: dict[str, Any], spec_instance: AIoTSpecInstance) -> None:
        self.aiot_client = aiot_client
        self.spec_instance = spec_instance
        self._entity_map = {}

        # 当设备不在线时会显示不可用
        self._online = device_info.get('online', 0) == 1
        self._did = device_info['did']
        self._name = device_info.get('name', '')

        self._model = device_info.get('skuId', '')
        self._manufacturer = device_info.get('manufacturer', '艾美科技')
        self._fw_version = device_info.get('version', '')

        self._sub_id = 0
        self._device_state_sub_list = {}
        self._value_sub_list = {}
        self._entity_list = {}
        self._prop_list = {}
        self._event_list = {}
        self._action_list = {}

        # 新增自定义
        self._mid_bind_id = device_info.get('midBindId', '')
        self._product_key = device_info.get('productKey', '')
        self._group_id = device_info.get('groupId', '')
        self._endpoint = device_info.get('endpoint', '')
        self._endpoint_name = device_info.get('endpointName', '')

        # Sub device state
        self.aiot_client.sub_device_state(self._did, self.__on_device_state_changed)

        _LOGGER.debug('aiot device init %s', device_info)

    @property
    def online(self) -> bool:
        return self._online

    @property
    def entity_list(self) -> dict[str, list[AIoTEntityData]]:
        return self._entity_list

    @property
    def prop_list(self) -> dict[str, list[AIoTSpecProperty]]:
        return self._prop_list

    @property
    def event_list(self) -> dict[str, list[AIoTSpecEvent]]:
        return self._event_list

    @property
    def action_list(self) -> dict[str, list[AIoTSpecAction]]:
        return self._action_list

    async def action_async(self, aam_cmd: str, aam_prop_name: str, in_list: list) -> list:
        return await self.aiot_client.action_async(did=self._did, siid=siid, aiid=aiid, in_list=in_list)

    def sub_device_state(self, key: str, handler: Callable[[str, AIoTDeviceState], None]) -> int:
        """设备状态订阅"""
        sub_id = self.__gen_sub_id()
        if key in self._device_state_sub_list:
            self._device_state_sub_list[key][str(sub_id)] = handler
        else:
            self._device_state_sub_list[key] = {str(sub_id): handler}
        return sub_id

    def unsub_device_state(self, key: str, sub_id: int) -> None:
        """设备状态取消订阅"""
        sub_list = self._device_state_sub_list.get(key, None)
        if sub_list:
            sub_list.pop(str(sub_id), None)
        if not sub_list:
            self._device_state_sub_list.pop(key, None)

    def sub_property(self, handler: Callable[[dict, Any], None], aam_cmd: str, aam_prop_name: str) -> int:
        """属性订阅"""
        key: str = f'p.{aam_cmd}.{aam_prop_name}'

        def _on_prop_changed(params: dict, ctx: Any) -> None:
            for handler in self._value_sub_list[key].values():
                handler(params, ctx)

        sub_id = self.__gen_sub_id()
        if key in self._value_sub_list:
            self._value_sub_list[key][str(sub_id)] = handler
        else:
            self._value_sub_list[key] = {str(sub_id): handler}
            self.aiot_client.sub_prop(did=self._did, handler=_on_prop_changed, aam_cmd=aam_cmd,
                                      aam_prop_name=aam_prop_name)
        return sub_id

    def unsub_property(self, aam_cmd: str, aam_prop_name: str, sub_id: int) -> None:
        """属性取消订阅"""
        key: str = f'p.{aam_cmd}.{aam_prop_name}'

        sub_list = self._value_sub_list.get(key, None)
        if sub_list:
            sub_list.pop(str(sub_id), None)
        if not sub_list:
            self.aiot_client.unsub_prop(did=self._did, aam_cmd=aam_cmd, aam_prop_name=aam_prop_name)
            self._value_sub_list.pop(key, None)

    def sub_event(self, handler: Callable[[dict, Any], None], aam_cmd: str, aam_prop_name: str) -> int:
        """事件订阅"""
        key: str = f'e.{aam_cmd}.{aam_prop_name}'

        def _on_event_occurred(params: dict, ctx: Any) -> None:
            for handler in self._value_sub_list[key].values():
                handler(params, ctx)

        sub_id = self.__gen_sub_id()
        if key in self._value_sub_list:
            self._value_sub_list[key][str(sub_id)] = handler
        else:
            self._value_sub_list[key] = {str(sub_id): handler}
            self.aiot_client.sub_event(
                did=self._did,
                handler=_on_event_occurred,
                aam_cmd=aam_cmd,
                aam_prop_name=aam_prop_name
            )
        return sub_id

    def unsub_event(self, aam_cmd: str, aam_prop_name: str, sub_id: int) -> None:
        """事件取消订阅"""
        key: str = f'e.{aam_cmd}.{aam_prop_name}'

        sub_list = self._value_sub_list.get(key, None)
        if sub_list:
            sub_list.pop(str(sub_id), None)
        if not sub_list:
            self.aiot_client.unsub_event(did=self._did, aam_cmd=aam_cmd, aam_prop_name=aam_prop_name)
            self._value_sub_list.pop(key, None)

    @property
    def device_info(self) -> DeviceInfo:
        """设备信息."""
        return DeviceInfo(
            # 设备唯一标识
            identifiers={(DOMAIN, self.did_tag)},
            name=self._name,
            sw_version=self._fw_version,
            model=self._model,
            manufacturer=self._manufacturer,
            # suggested_area=self._suggested_area,
            # configuration_url=('')
        )

    @property
    def mid_bind_id(self) -> str:
        return self._mid_bind_id

    @property
    def product_key(self) -> str:
        return self._product_key

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def group_id(self) -> str:
        return self._group_id

    @property
    def endpoint_name(self) -> str:
        return self._endpoint_name

    @property
    def did(self) -> str:
        """Device Id."""
        return self._did

    @property
    def did_tag(self) -> str:
        return slugify_did(lan_server=self.aiot_client.lan_server, did=self._did)

    def gen_device_entity_id(self, ha_domain: str) -> str:
        return (
            f'{ha_domain}.{self._model_strs[0][:9]}_{self.did_tag}_'
            f'{self._model_strs[-1][:20]}'
        )

    def gen_service_entity_id(self, ha_domain: str, siid: int,
                              description: str) -> str:
        return (
            f'{ha_domain}.{self._model_strs[0][:9]}_{self.did_tag}_'
            f'{self._model_strs[-1][:20]}_s_{siid}_{description}')

    def gen_prop_entity_id(self, ha_domain: str, spec_name: str, mid_bind_id: str, endpoint: str) -> str:
        return f'{ha_domain}.{slugify_name(spec_name)}_{mid_bind_id}_{endpoint}'

    def gen_event_entity_id(
            self, ha_domain: str, spec_name: str, siid: int, eiid: int
    ) -> str:
        return (
            f'{ha_domain}.{self._model_strs[0][:9]}_{self.did_tag}_'
            f'{self._model_strs[-1][:20]}_{slugify_name(spec_name)}'
            f'_e_{siid}_{eiid}')

    def gen_action_entity_id(self, ha_domain: str, spec_name: str, mid_bind_id: str, endpoint: str) -> str:
        return f'{ha_domain}.{slugify_name(spec_name)}_{mid_bind_id}_{endpoint}'

    @property
    def name(self) -> str:
        return self._name

    @property
    def model(self) -> str:
        return self._model

    def append_entity(self, entity_data: AIoTEntityData) -> None:
        self._entity_list.setdefault(entity_data.platform, [])
        self._entity_list[entity_data.platform].append(entity_data)

    def append_prop(self, prop: AIoTSpecProperty) -> None:
        if not prop.platform:
            return
        self._prop_list.setdefault(prop.platform, [])
        self._prop_list[prop.platform].append(prop)

    def append_event(self, event: AIoTSpecEvent) -> None:
        if not event.platform:
            return
        self._event_list.setdefault(event.platform, [])
        self._event_list[event.platform].append(event)

    def append_action(self, action: AIoTSpecAction) -> None:
        if not action.platform:
            return
        self._action_list.setdefault(action.platform, [])
        self._action_list[action.platform].append(action)

    def add_entity_map(self, cmd: str, prop_name: str, value: Any):
        """添加实体映射."""
        key = f'{cmd}_{prop_name}_{self.endpoint}'
        self._entity_map.setdefault(key, value)

    def get_entity_map_value(self, cmd: str, prop_name: str) -> Optional[Any]:
        """获取实体映射值."""
        key = f'{cmd}_{prop_name}_{self.endpoint}'
        return self._entity_map.get(key)

    def __gen_sub_id(self) -> int:
        self._sub_id += 1
        return self._sub_id

    def __on_device_state_changed(
            self, did: str, state: AIoTDeviceState, ctx: Any
    ) -> None:
        self._online = state == AIoTDeviceState.ONLINE
        for key, sub_list in self._device_state_sub_list.items():
            for handler in sub_list.values():
                self.aiot_client.main_loop.call_soon_threadsafe(
                    handler, key, state)


class AIoTServiceEntity(Entity):
    """AIoT Service Entity."""
    # pylint: disable=unused-argument
    # pylint: disable=inconsistent-quotes
    aiot_device: AIoTDevice
    entity_data: AIoTEntityData

    _main_loop: asyncio.AbstractEventLoop
    _prop_value_map: dict[AIoTSpecProperty, Any]
    _state_sub_id: int
    _value_sub_ids: dict[str, int]

    _event_occurred_handler: Optional[
        Callable[[AIoTSpecEvent, dict], None]]
    _prop_changed_subs: dict[
        AIoTSpecProperty, Callable[[AIoTSpecProperty, Any], None]]

    _pending_write_ha_state_timer: Optional[asyncio.TimerHandle]

    def __init__(
            self, aiot_device: AIoTDevice, entity_data: AIoTEntityData
    ) -> None:
        if (
                aiot_device is None
                or entity_data is None
                or entity_data.spec is None
        ):
            raise AIoTDeviceError('init error, invalid params')
        self.aiot_device = aiot_device
        self.entity_data = entity_data
        self._main_loop = aiot_device.aiot_client.main_loop
        self._prop_value_map = {}
        self._state_sub_id = 0
        self._value_sub_ids = {}
        # Gen entity id
        if isinstance(self.entity_data.spec, AIoTSpecInstance):
            self.entity_id = aiot_device.gen_device_entity_id(DOMAIN)
            self._attr_name = f' {self.entity_data.spec.description_trans}'
        elif isinstance(self.entity_data.spec, AIoTSpecService):
            self.entity_id = aiot_device.gen_service_entity_id(
                DOMAIN,
                siid=self.entity_data.spec.iid,
                description=self.entity_data.spec.description
            )
            self._attr_name = (
                f'{"* " if self.entity_data.spec.proprietary else " "}'
                f'{self.entity_data.spec.description_trans}'
            )
            self._attr_entity_category = entity_data.spec.entity_category
        # Set entity attr
        self._attr_unique_id = self.entity_id
        self._attr_should_poll = False
        self._attr_has_entity_name = True
        self._attr_available = aiot_device.online

        self._event_occurred_handler = None
        self._prop_changed_subs = {}
        self._pending_write_ha_state_timer = None
        _LOGGER.info(
            'new aiot service entity, %s, %s, %s, %s',
            self.aiot_device.name, self._attr_name, self.entity_data.spec.name, self.entity_id
        )

    @property
    def event_occurred_handler(self) -> Optional[Callable[[AIoTSpecEvent, dict], None]]:
        return self._event_occurred_handler

    @event_occurred_handler.setter
    def event_occurred_handler(self, func) -> None:
        self._event_occurred_handler = func

    def sub_prop_changed(self, prop: AIoTSpecProperty, handler: Callable[[AIoTSpecProperty, Any], None]) -> None:
        if not prop or not handler:
            _LOGGER.error('sub_prop_changed error, invalid prop/handler')
            return
        self._prop_changed_subs[prop] = handler

    def unsub_prop_changed(self, prop: AIoTSpecProperty) -> None:
        self._prop_changed_subs.pop(prop, None)

    @property
    def device_info(self) -> Optional[DeviceInfo]:
        return self.aiot_device.device_info

    async def async_added_to_hass(self) -> None:
        state_id = 's.0'
        if isinstance(self.entity_data.spec, AIoTSpecService):
            state_id = f's.{self.entity_data.spec.iid}'
        self._state_sub_id = self.aiot_device.sub_device_state(
            key=state_id,
            handler=self.__on_device_state_changed
        )
        # Sub prop
        for prop in self.entity_data.props:
            key = f'p.{prop.service.iid}.{prop.iid}'
            self._value_sub_ids[key] = self.aiot_device.sub_property(
                handler=self.__on_properties_changed,
                siid=prop.service.iid,
                piid=prop.iid
            )
        # Sub event
        for event in self.entity_data.events:
            key = f'e.{event.service.iid}.{event.iid}'
            self._value_sub_ids[key] = self.aiot_device.sub_event(
                handler=self.__on_event_occurred,
                siid=event.service.iid,
                eiid=event.iid
            )

        # Refresh value
        if self._attr_available:
            self.__refresh_props_value()

    async def async_will_remove_from_hass(self) -> None:
        if self._pending_write_ha_state_timer:
            self._pending_write_ha_state_timer.cancel()
            self._pending_write_ha_state_timer = None
        state_id = 's.0'
        if isinstance(self.entity_data.spec, AIoTSpecService):
            state_id = f's.{self.entity_data.spec.iid}'
        self.aiot_device.unsub_device_state(key=state_id, sub_id=self._state_sub_id)
        # Unsub prop
        for prop in self.entity_data.props:
            if not prop.notifiable and not prop.readable:
                continue
            sub_id = self._value_sub_ids.pop(f'p.{prop.service.iid}.{prop.iid}', None)
            if sub_id:
                self.aiot_device.unsub_property(siid=prop.service.iid, piid=prop.iid, sub_id=sub_id)
        # Unsub event
        for event in self.entity_data.events:
            sub_id = self._value_sub_ids.pop(
                f'e.{event.service.iid}.{event.iid}', None)
            if sub_id:
                self.aiot_device.unsub_event(
                    siid=event.service.iid, eiid=event.iid, sub_id=sub_id)

    def get_map_value(
            self, map_: Optional[dict[int, Any]], key: int
    ) -> Any:
        if map_ is None:
            return None
        return map_.get(key, None)

    def get_map_key(
            self, map_: Optional[dict[int, Any]], value: Any
    ) -> Optional[int]:
        if map_ is None:
            return None
        for key, value_ in map_.items():
            if value_ == value:
                return key
        return None

    def get_prop_value(self, prop: Optional[AIoTSpecProperty]) -> Any:
        if not prop:
            _LOGGER.error(
                'get_prop_value error, property is None, %s, %s',
                self._attr_name, self.entity_id)
            return None
        return self._prop_value_map.get(prop, None)

    def set_prop_value(
            self, prop: Optional[AIoTSpecProperty], value: Any
    ) -> None:
        if not prop:
            _LOGGER.error(
                'set_prop_value error, property is None, %s, %s',
                self._attr_name, self.entity_id)
            return
        self._prop_value_map[prop] = value

    async def set_property_async(
            self, prop: Optional[AIoTSpecProperty], value: Any,
            update_value: bool = True, write_ha_state: bool = True
    ) -> bool:
        if not prop:
            raise RuntimeError(
                f'set property failed, property is None, '
                f'{self.entity_id}, {self.name}')
        value = prop.value_format(value)
        value = prop.value_precision(value)
        if prop not in self.entity_data.props:
            raise RuntimeError(
                f'set property failed, unknown property, '
                f'{self.entity_id}, {self.name}, {prop.name}')
        try:
            await self.aiot_device.aiot_client.set_prop_async(
                did=self.aiot_device.did, siid=prop.service.iid,
                piid=prop.iid, value=value)
        except AIoTClientError as e:
            raise RuntimeError(
                f'{e}, {self.entity_id}, {self.name}, {prop.name}') from e
        if update_value:
            self._prop_value_map[prop] = value
        if write_ha_state:
            self.async_write_ha_state()
        return True

    async def get_property_async(self, prop: AIoTSpecProperty) -> Any:
        if not prop:
            _LOGGER.error(
                'get property failed, property is None, %s, %s',
                self.entity_id, self.name)
            return None
        if prop not in self.entity_data.props:
            _LOGGER.error(
                'get property failed, unknown property, %s, %s, %s',
                self.entity_id, self.name, prop.name)
            return None
        value: Any = prop.value_format(
            await self.aiot_device.aiot_client.get_prop_async(
                did=self.aiot_device.did, siid=prop.service.iid, piid=prop.iid))
        value = prop.eval_expr(value)
        result = prop.value_precision(value)
        if result != self._prop_value_map[prop]:
            self._prop_value_map[prop] = result
            self.async_write_ha_state()
        return result

    async def action_async(
            self, action: AIoTSpecAction, in_list: Optional[list] = None
    ) -> bool:
        if not action:
            raise RuntimeError(
                f'action failed, action is None, {self.entity_id}, {self.name}')
        try:
            await self.aiot_device.aiot_client.action_async(
                did=self.aiot_device.did, siid=action.service.iid,
                aiid=action.iid, in_list=in_list or [])
        except AIoTClientError as e:
            raise RuntimeError(
                f'{e}, {self.entity_id}, {self.name}, {action.name}') from e
        return True

    def __on_properties_changed(self, params: dict, ctx: Any) -> None:
        _LOGGER.debug('properties changed, %s', params)
        for prop in self.entity_data.props:
            if (
                    prop.iid != params['piid']
                    or prop.service.iid != params['siid']
            ):
                continue
            value: Any = prop.value_format(params['value'])
            value = prop.eval_expr(value)
            value = prop.value_precision(value)
            self._prop_value_map[prop] = value
            if prop in self._prop_changed_subs:
                self._prop_changed_subs[prop](prop, value)
            break
        if not self._pending_write_ha_state_timer:
            self.async_write_ha_state()

    def __on_event_occurred(self, params: dict, ctx: Any) -> None:
        _LOGGER.debug('event occurred, %s', params)
        if self._event_occurred_handler is None:
            return
        for event in self.entity_data.events:
            if (
                    event.iid != params['eiid']
                    or event.service.iid != params['siid']
            ):
                continue
            trans_arg = {}
            for item in params['arguments']:
                for prop in event.argument:
                    if prop.iid == item['piid']:
                        trans_arg[prop.description_trans] = item['value']
                        break
            self._event_occurred_handler(event, trans_arg)
            break

    def __on_device_state_changed(
            self, key: str, state: AIoTDeviceState
    ) -> None:
        state_new = state == AIoTDeviceState.ONLINE
        if state_new == self._attr_available:
            return
        self._attr_available = state_new
        if not self._attr_available:
            self.async_write_ha_state()
            return
        self.__refresh_props_value()

    def __refresh_props_value(self) -> None:
        for prop in self.entity_data.props:
            self.aiot_device.aiot_client.request_refresh_prop(
                did=self.aiot_device.did, siid=prop.service.iid, piid=prop.iid)
        if self._pending_write_ha_state_timer:
            self._pending_write_ha_state_timer.cancel()
        self._pending_write_ha_state_timer = self._main_loop.call_later(
            1, self.__write_ha_state_handler)

    def __write_ha_state_handler(self) -> None:
        self._pending_write_ha_state_timer = None
        self.async_write_ha_state()


class AIoTPropertyEntity(Entity):
    """智能设备属性."""
    aiot_device: AIoTDevice
    spec: AIoTSpecProperty
    service: AIoTSpecService

    _main_loop: asyncio.AbstractEventLoop
    _value_range: Optional[AIoTSpecValueRange]
    _value_list: Optional[AIoTSpecValueList]
    _value: Any
    _state_sub_id: int
    _value_sub_id: int

    _pending_write_ha_state_timer: Optional[asyncio.TimerHandle]

    # 新增自定义
    _aam_cmd: str  # 修改属性的命令
    _param_key: str  # 修改属性的参数key

    def __init__(self, aiot_device: AIoTDevice, spec: AIoTSpecProperty) -> None:
        if aiot_device is None or spec is None or spec.service is None:
            raise AIoTDeviceError('init error, invalid params')
        self.aiot_device = aiot_device
        self.spec = spec
        self.service = spec.service
        self._main_loop = aiot_device.aiot_client.main_loop
        self._value_range = spec.value_range
        self._value_list = spec.value_list
        self._value = None
        self._state_sub_id = 0
        self._value_sub_id = 0
        self._pending_write_ha_state_timer = None
        # Gen entity_id
        self.entity_id = self.iot_device.gen_prop_entity_id(
            ha_domain=DOMAIN,
            spec_name=spec.name,
            mid_bind_id=aiot_device.mid_bind_id,
            endpoint=aiot_device.endpoint
        )
        # Set entity attr
        self._attr_unique_id = self.entity_id  # 实体唯一标识
        self._attr_should_poll = False
        self._attr_has_entity_name = True  # 是否有实体名称
        self._attr_name = f'{aiot_device.endpoint_name}  {spec.description}'  # 实体名
        self._attr_available = aiot_device.online  # 实体当前是否可用

        _LOGGER.info(
            'new aiot property entity, %s, %s, %s, %s, %s',
            self.aiot_device.name, self._attr_name, spec.platform,
            spec.device_class, self.entity_id)

        # 解析属性设置命令和参数
        self._aam_cmd = get_service_name(spec.service.type_)
        self._param_key = get_prop_name(spec.type_)

    @property
    def device_info(self) -> Optional[DeviceInfo]:
        return self.aiot_device.device_info

    async def async_added_to_hass(self) -> None:
        # Sub device state changed
        self._state_sub_id = self.aiot_device.sub_device_state(
            key=f'{self.service.iid}.{self.spec.iid}',
            handler=self.__on_device_state_changed)
        # Sub value changed
        self._value_sub_id = self.aiot_device.sub_property(
            handler=self.__on_value_changed,
            siid=self.service.iid, piid=self.spec.iid)
        # Refresh value
        if self._attr_available:
            self.__request_refresh_prop()

    async def async_will_remove_from_hass(self) -> None:
        if self._pending_write_ha_state_timer:
            self._pending_write_ha_state_timer.cancel()
            self._pending_write_ha_state_timer = None
        self.aiot_device.unsub_device_state(
            key=f'{self.service.iid}.{self.spec.iid}',
            sub_id=self._state_sub_id)
        self.aiot_device.unsub_property(
            siid=self.service.iid, piid=self.spec.iid,
            sub_id=self._value_sub_id)

    def get_vlist_description(self, value: Any) -> Optional[str]:
        # 根据值获取描述
        if not self._value_list:
            return None
        return self._value_list.get_description_by_value(value)

    def get_vlist_value(self, description: str) -> Any:
        # 根据描述获取值
        if not self._value_list:
            return None
        return self._value_list.get_value_by_description(description)

    async def set_property_async(self, value: any) -> bool:
        try:
            json_data = {self._param_key: value}

            # 如果属性有group_key，需要收集同一组的其他属性一起发送
            if self.spec.group_key:
                for prop in self.iot_device.prop_list.get('number', []):
                    if prop.group_key == self.spec.group_key and prop.name != self.spec.name:
                        # 获取同一组其他属性的当前值
                        prop_value = self.iot_device.get_entity_map_value(self._cmd, prop.name)
                        if prop_value is not None:
                            json_data[prop.name] = prop_value
                        else:
                            # 如果其他属性没有当前值，使用默认值
                            json_data[prop.name] = self.spec.get_default_value()

            await self.aiot_device.aiot_client.set_prop_async(
                did=self.aiot_device.did,
                aam_cmd=self._aam_cmd,
                group_id=self.iot_device.group_id,
                json_data=json_data,
            )
        except AIoTClientError as e:
            raise RuntimeError(f'{e}, {self.iot_device.mid_bind_id}, {self.iot_device.name}') from e
        self._value = value
        self.iot_device.add_entity_map(self._cmd, self.spec.name, value)
        # 立即更新UI
        self.async_write_ha_state()
        return True

    async def get_property_async(self) -> Any:
        value: Any = self.spec.value_format(
            await self.aiot_device.aiot_client.get_prop_async(
                did=self.aiot_device.did,
                siid=self.spec.service.iid,
                piid=self.spec.iid))
        value = self.spec.eval_expr(value)
        result = self.spec.value_precision(value)
        return result

    def __on_value_changed(self, params: dict, ctx: Any) -> None:
        _LOGGER.debug('property changed, %s', params)
        value: Any = self.spec.value_format(params['value'])
        value = self.spec.eval_expr(value)
        self._value = self.spec.value_precision(value)
        if not self._pending_write_ha_state_timer:
            self.async_write_ha_state()

    def __on_device_state_changed(self, key: str, state: AIoTDeviceState) -> None:
        self._attr_available = state == AIoTDeviceState.ONLINE
        if not self._attr_available:
            self.async_write_ha_state()
            return
        # Refresh value
        self.__request_refresh_prop()

    def __request_refresh_prop(self) -> None:
        self.aiot_device.aiot_client.request_refresh_prop(
            did=self.aiot_device.did,
            siid=self.service.iid,
            piid=self.spec.iid
        )
        if self._pending_write_ha_state_timer:
            self._pending_write_ha_state_timer.cancel()
        self._pending_write_ha_state_timer = self._main_loop.call_later(1, self.__write_ha_state_handler)

    def __write_ha_state_handler(self) -> None:
        self._pending_write_ha_state_timer = None
        self.async_write_ha_state()


class AIoTEventEntity(Entity):
    """AIoT Event Entity."""
    # pylint: disable=unused-argument
    # pylint: disable=inconsistent-quotes
    aiot_device: AIoTDevice
    spec: AIoTSpecEvent
    service: AIoTSpecService

    _main_loop: asyncio.AbstractEventLoop
    _attr_event_types: list[str]
    _arguments_map: dict[int, str]
    _state_sub_id: int
    _value_sub_id: int

    def __init__(self, aiot_device: AIoTDevice, spec: AIoTSpecEvent) -> None:
        if aiot_device is None or spec is None or spec.service is None:
            raise AIoTDeviceError('init error, invalid params')
        self.aiot_device = aiot_device
        self.spec = spec
        self.service = spec.service
        self._main_loop = aiot_device.aiot_client.main_loop
        # Gen entity_id
        self.entity_id = self.aiot_device.gen_event_entity_id(
            ha_domain=DOMAIN, spec_name=spec.name,
            siid=spec.service.iid, eiid=spec.iid)
        # Set entity attr
        self._attr_unique_id = self.entity_id
        self._attr_should_poll = False
        self._attr_has_entity_name = True
        self._attr_name = (
            f'{"* " if self.spec.proprietary else " "}'
            f'{self.service.description_trans} {spec.description_trans}')
        self._attr_available = aiot_device.online
        self._attr_event_types = [spec.description_trans]

        self._arguments_map = {}
        for prop in spec.argument:
            self._arguments_map[prop.iid] = prop.description_trans
        self._state_sub_id = 0
        self._value_sub_id = 0

        _LOGGER.info(
            'new aiot event entity, %s, %s, %s, %s, %s',
            self.aiot_device.name, self._attr_name, spec.platform,
            spec.device_class, self.entity_id)

    @property
    def device_info(self) -> Optional[DeviceInfo]:
        return self.aiot_device.device_info

    async def async_added_to_hass(self) -> None:
        self._state_sub_id = self.aiot_device.sub_device_state(
            key=f'event.{self.service.iid}.{self.spec.iid}',
            handler=self.__on_device_state_changed)
        self._value_sub_id = self.aiot_device.sub_event(
            handler=self.__on_event_occurred,
            siid=self.service.iid, eiid=self.spec.iid)

    async def async_will_remove_from_hass(self) -> None:
        self.aiot_device.unsub_device_state(
            key=f'event.{self.service.iid}.{self.spec.iid}',
            sub_id=self._state_sub_id)
        self.aiot_device.unsub_event(
            siid=self.service.iid, eiid=self.spec.iid,
            sub_id=self._value_sub_id)

    @abstractmethod
    def on_event_occurred(
            self, name: str, arguments: dict[str, Any] | None = None
    ) -> None:
        ...

    def __on_event_occurred(self, params: dict, ctx: Any) -> None:
        _LOGGER.debug('event occurred, %s', params)
        trans_arg = {}
        for item in params['arguments']:
            try:
                if 'value' not in item:
                    continue
                if 'piid' in item:
                    trans_arg[self._arguments_map[item['piid']]] = item[
                        'value']
                elif (
                        isinstance(item['value'], list)
                        and len(item['value']) == len(self.spec.argument)
                ):
                    # Dirty fix for cloud multi-arguments
                    trans_arg = {
                        prop.description_trans: item['value'][index]
                        for index, prop in enumerate(self.spec.argument)
                    }
                    break
            except KeyError as error:
                _LOGGER.debug(
                    'on event msg, invalid args, %s, %s, %s',
                    self.entity_id, params, error)
        self.on_event_occurred(
            name=self.spec.description_trans, arguments=trans_arg)
        self.async_write_ha_state()

    def __on_device_state_changed(
            self, key: str, state: AIoTDeviceState
    ) -> None:
        state_new = state == AIoTDeviceState.ONLINE
        if state_new == self._attr_available:
            return
        self._attr_available = state_new
        self.async_write_ha_state()


class AIoTActionEntity(Entity):
    """智能设备操作."""
    aiot_device: AIoTDevice
    spec: AIoTSpecAction
    service: AIoTSpecService

    _main_loop: asyncio.AbstractEventLoop
    _in_map: dict[int, AIoTSpecProperty]
    _out_map: dict[int, AIoTSpecProperty]
    _state_sub_id: int

    def __init__(self, aiot_device: AIoTDevice, spec: AIoTSpecAction) -> None:
        if aiot_device is None or spec is None:
            raise AIoTDeviceError('init error, invalid params')
        self.aiot_device = aiot_device
        self.spec = spec
        self._main_loop = aiot_device.aiot_client.main_loop
        self._state_sub_id = 0
        # Gen entity_id
        self.entity_id = self.iot_device.gen_action_entity_id(
            ha_domain=DOMAIN,
            spec_name=spec.name,
            mid_bind_id=aiot_device.mid_bind_id,
            endpoint=aiot_device.endpoint
        )
        # Set entity attr
        self._attr_unique_id = self.entity_id
        self._attr_should_poll = False
        self._attr_has_entity_name = True
        self._attr_name = f'{aiot_device.endpoint_name}  {spec.description}'  # 实体名
        self._attr_available = aiot_device.online
        _LOGGER.debug(
            'new aiot action entity, %s, %s, %s, %s, %s',
            self.aiot_device.name, self._attr_name, spec.platform, spec.device_class, self.entity_id
        )

    @property
    def device_info(self) -> Optional[DeviceInfo]:
        return self.aiot_device.device_info

    async def async_added_to_hass(self) -> None:
        self._state_sub_id = self.aiot_device.sub_device_state(
            key=f'a.{self.service.iid}.{self.spec.iid}',
            handler=self.__on_device_state_changed)

    async def async_will_remove_from_hass(self) -> None:
        self.aiot_device.unsub_device_state(
            key=f'a.{self.service.iid}.{self.spec.iid}',
            sub_id=self._state_sub_id)

    async def action_async(
            self, in_list: Optional[list] = None
    ) -> Optional[list]:
        try:
            return await self.aiot_device.aiot_client.action_async(
                did=self.aiot_device.did,
                siid=self.service.iid,
                aiid=self.spec.iid,
                in_list=in_list or [])
        except AIoTClientError as e:
            raise RuntimeError(f'{e}, {self.entity_id}, {self.name}') from e

    def __on_device_state_changed(
            self, key: str, state: AIoTDeviceState
    ) -> None:
        state_new = state == AIoTDeviceState.ONLINE
        if state_new == self._attr_available:
            return
        self._attr_available = state_new
        self.async_write_ha_state()
