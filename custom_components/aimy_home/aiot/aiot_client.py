# -*- coding: utf-8 -*-

import asyncio
import json
import logging
import time
import traceback
from copy import deepcopy
from dataclasses import dataclass
from typing import Optional, Callable, final, Any

from homeassistant.components import zeroconf
from homeassistant.core import HomeAssistant

from .aiot_error import AIoTClientError
from .aiot_i18n import AIoTI18n
from .aiot_lan import AIoTAuthClient, AIoTHttpClient
from .aiot_mdns import MipsService
from .aiot_mips import MipsLanClient, MipsDeviceState, AIoTDeviceState
from .aiot_network import AIoTNetwork
from .aiot_storage import AIoTStorage
from .common import (
    AIoTMatcher,
    slugify_did
)
from .const import (
    DOMAIN,
    NETWORK_REFRESH_INTERVAL,
    DEFAULT_NICK_NAME,
    DEFAULT_INTEGRATION_LANGUAGE,
    AUTH_CLIENT_ID,
    REFRESH_LAN_DEVICES_DELAY,
    REFRESH_PROPS_DELAY,
    REFRESH_LAN_DEVICES_RETRY_DELAY,
    REFRESH_PROPS_RETRY_DELAY,
    DEFAULT_COVER_DEAD_ZONE_WIDTH
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class AIoTClientSub:
    """AIoT client subscription."""
    topic: Optional[str]
    handler: Callable[[dict, Any], None]
    handler_ctx: Any = None

    def __str__(self) -> str:
        return f'{self.topic}, {id(self.handler)}, {id(self.handler_ctx)}'


class AIoTClient:
    """AIoT client 实例."""
    # pylint: disable=unused-argument
    # pylint: disable=broad-exception-caught
    # pylint: disable=inconsistent-quotes
    _main_loop: asyncio.AbstractEventLoop

    _uname: str
    _entry_id: str
    _entry_data: dict
    _lan_server: str
    # AIoT 网络监控
    _network: AIoTNetwork
    # AIoT 存储客户端
    _storage: AIoTStorage
    # AIoT mips 服务
    _mips_service: MipsService
    # AIoT 认证客户端
    _auth: AIoTAuthClient
    # AIoT http 客户端
    _http: AIoTHttpClient
    # AIoT i18n 客户端
    _i18n: AIoTI18n
    # 用户配置, 存储存放路径 .storage/aimy_home
    _user_config: dict

    # Lan mips 客户端
    _mips_lan: MipsLanClient

    # 从本地缓存加载的设备列表, {did: <info>}
    _device_list_cache: dict[str, dict]
    # 从局域网获取的设备列表, {did: <info>}
    _device_list_lan: dict[str, dict]
    # 设备列表更新时间戳
    _device_list_update_ts: int

    _sub_tree: AIoTMatcher
    _sub_device_state: dict[str, MipsDeviceState]

    _mips_local_state_changed_timers: dict[str, asyncio.TimerHandle]
    _refresh_token_timer: Optional[asyncio.TimerHandle]
    _refresh_lan_devices_timer: Optional[asyncio.TimerHandle]
    # 刷新属性
    _refresh_props_list: dict[str, dict]
    _refresh_props_timer: Optional[asyncio.TimerHandle]
    _refresh_props_retry_count: int

    # 持久性通知处理程序，参数：notify_id，title，message
    _persistence_notify: Callable[[str, Optional[str], Optional[str]], None]
    # 设备列表更改通知
    _show_devices_changed_notify_timer: Optional[asyncio.TimerHandle]
    # 显示设备更改通知
    _display_devs_notify: list[str]
    _display_notify_content_hash: Optional[int]
    # 显示二进制模式
    _display_binary_text: bool
    _display_binary_bool: bool

    def __init__(
            self,
            entry_id: str,
            entry_data: dict,
            network: AIoTNetwork,
            storage: AIoTStorage,
            mips_service: MipsService,
            loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        # 必须在运行事件循环中运行
        self._main_loop = loop or asyncio.get_running_loop()
        # 检查参数
        if not isinstance(entry_data, dict):
            raise AIoTClientError('invalid entry data')
        if 'uname' not in entry_data or 'lan_server' not in entry_data:
            raise AIoTClientError('invalid entry data content')
        if not isinstance(network, AIoTNetwork):
            raise AIoTClientError('invalid aiot network')
        if not isinstance(storage, AIoTStorage):
            raise AIoTClientError('invalid aiot storage')
        if not isinstance(mips_service, MipsService):
            raise AIoTClientError('invalid mips service')
        self._entry_id = entry_id
        self._entry_data = entry_data
        self._uname = entry_data['uname']
        self._lan_server = entry_data['lan_server']
        self._network = network
        self._storage = storage
        self._mips_service = mips_service
        self._auth = None
        self._http = None
        self._i18n = None
        self._user_config = None

        self._mips_lan = None

        self._device_list_cache = {}
        self._device_list_lan = {}
        self._device_list_gateway = {}
        self._device_list_update_ts = 0
        self._sub_tree = AIoTMatcher()
        self._sub_device_state = {}

        self._mips_local_state_changed_timers = {}
        self._refresh_token_timer = None
        self._refresh_lan_devices_timer = None

        # 刷新属性
        self._refresh_props_list = {}
        self._refresh_props_timer = None
        self._refresh_props_retry_count = 0

        self._persistence_notify = None
        self._show_devices_changed_notify_timer = None

        self._display_devs_notify = entry_data.get('display_devices_changed_notify', ['add', 'del', 'offline'])
        self._display_notify_content_hash = None
        self._display_binary_text = 'text' in entry_data.get('display_binary_mode', ['text'])
        self._display_binary_bool = 'bool' in entry_data.get('display_binary_mode', ['text'])

    async def init_async(self) -> None:
        # 加载用户配置并检查
        self._user_config = await self._storage.load_user_config_async(uname=self._uname, lan_server=self._lan_server)
        if not self._user_config:
            # 需要再次添加集成
            raise AIoTClientError('load_user_config_async error')
        # 隐藏打印中的敏感信息
        p_user_config: dict = deepcopy(self._user_config)
        p_access_token: str = p_user_config['auth_info']['access_token']
        p_refresh_token: str = p_user_config['auth_info']['refresh_token']
        p_mac_key: str = p_user_config['auth_info']['mac_key']
        p_user_config['auth_info']['access_token'] = f"{p_access_token[:5]}***{p_access_token[-5:]}"
        p_user_config['auth_info']['refresh_token'] = f"{p_refresh_token[:5]}***{p_refresh_token[-5:]}"
        p_user_config['auth_info']['mac_key'] = f"{p_mac_key[:5]}***{p_mac_key[-5:]}"
        _LOGGER.debug('user config, %s', json.dumps(p_user_config))
        # AIoT i18n 客户端
        self._i18n = AIoTI18n(
            lang=self._entry_data.get('integration_language', DEFAULT_INTEGRATION_LANGUAGE),
            loop=self._main_loop
        )
        await self._i18n.init_async()
        # 加载缓存设备列表
        await self.__load_cache_device_async()
        # AIoT 认证客户端实例
        self._auth = AIoTAuthClient(
            client_id=AUTH_CLIENT_ID,
            lan_server=self._lan_server,
            uuid=self._entry_data["uuid"],
            loop=self._main_loop
        )
        # AIoT http 客户端实例
        self._http = AIoTHttpClient(
            lan_server=self._lan_server,
            client_id=AUTH_CLIENT_ID,
            access_token=self._user_config['auth_info']['access_token'],
            loop=self._main_loop
        )
        # AIoT lan mips 客户端
        self._mips_lan = MipsLanClient(
            uuid=self._entry_data['uuid'],
            lan_server=self._lan_server,
            app_id=AUTH_CLIENT_ID,
            token=self._user_config['auth_info']['access_token'],
            loop=self._main_loop
        )
        self._mips_lan.enable_logger(logger=_LOGGER)
        self._mips_lan.sub_mips_state(
            key=f'{self._uname}-{self._lan_server}',
            handler=self.__on_mips_lan_state_changed
        )
        # 订阅网络状态
        self._network.sub_network_status(
            key=f'{self._uname}-{self._lan_server}',
            handler=self.__on_network_status_changed
        )
        await self.__on_network_status_changed(status=self._network.network_status)

        _LOGGER.info('init_async, %s, %s', self._uname, self._lan_server)

    async def deinit_async(self) -> None:
        self._network.unsub_network_status(key=f'{self._uname}-{self._lan_server}')
        # 取消刷新 props
        if self._refresh_props_timer:
            self._refresh_props_timer.cancel()
            self._refresh_props_timer = None
        self._refresh_props_list.clear()
        self._refresh_props_retry_count = 0
        # Lan mips
        self._mips_lan.unsub_mips_state(key=f'{self._uname}-{self._lan_server}')
        self._mips_lan.deinit()
        # 取消刷新局域网设备
        if self._refresh_lan_devices_timer:
            self._refresh_lan_devices_timer.cancel()
            self._refresh_lan_devices_timer = None
        # 取消刷新认证信息
        if self._refresh_token_timer:
            self._refresh_token_timer.cancel()
            self._refresh_token_timer = None
        # 取消设备更改通知定时器
        if self._show_devices_changed_notify_timer:
            self._show_devices_changed_notify_timer.cancel()
            self._show_devices_changed_notify_timer = None
        await self._auth.deinit_async()
        await self._http.deinit_async()
        # 移除通知
        self._persistence_notify(self.__gen_notify_key('dev_list_changed'), None, None)
        self.__show_client_error_notify(message=None, notify_key='auth_info')
        self.__show_client_error_notify(message=None, notify_key='device_cache')
        self.__show_client_error_notify(message=None, notify_key='device_lan')

        _LOGGER.info('deinit_async, %s', self._uname)

    @property
    def main_loop(self) -> asyncio.AbstractEventLoop:
        return self._main_loop

    @property
    def aiot_network(self) -> AIoTNetwork:
        return self._network

    @property
    def aiot_storage(self) -> AIoTStorage:
        return self._storage

    @property
    def mips_service(self) -> MipsService:
        return self._mips_service

    @property
    def aiot_auth(self) -> AIoTAuthClient:
        return self._auth

    @property
    def aiot_http(self) -> AIoTHttpClient:
        return self._http

    @property
    def aiot_i18n(self) -> AIoTI18n:
        return self._i18n

    @property
    def user_config(self) -> dict:
        return self._user_config

    @property
    def lan_server(self) -> str:
        return self._lan_server

    @property
    def action_debug(self) -> bool:
        return self._entry_data.get('action_debug', False)

    @property
    def display_devices_changed_notify(self) -> list[str]:
        return self._display_devs_notify

    @property
    def display_binary_text(self) -> bool:
        return self._display_binary_text

    @property
    def display_binary_bool(self) -> bool:
        return self._display_binary_bool

    @property
    def cover_dead_zone_width(self) -> int:
        return self._entry_data.get('cover_dead_zone_width', DEFAULT_COVER_DEAD_ZONE_WIDTH)

    @display_devices_changed_notify.setter
    def display_devices_changed_notify(self, value: list[str]) -> None:
        if set(value) == set(self._display_devs_notify):
            return
        self._display_devs_notify = value
        if value:
            self.__request_show_devices_changed_notify()
        else:
            self._persistence_notify(self.__gen_notify_key('dev_list_changed'), None, None)

    @property
    def device_list(self) -> dict:
        return self._device_list_cache

    @property
    def persistent_notify(self) -> Callable:
        return self._persistence_notify

    @persistent_notify.setter
    def persistent_notify(self, func) -> None:
        self._persistence_notify = func

    @final
    async def refresh_auth_info_async(self) -> bool:
        """刷新认证信息"""
        try:
            # 加载认证信息
            auth_info: Optional[dict] = None
            user_config: dict = await self._storage.load_user_config_async(
                uname=self._uname,
                lan_server=self._lan_server,
                keys=['auth_info']
            )
            if (
                    not user_config
                    or (auth_info := user_config.get('auth_info', None)) is None
            ):
                raise AIoTClientError('load_user_config_async error')
            if (
                    'expires_ts' not in auth_info
                    or 'access_token' not in auth_info
                    or 'refresh_token' not in auth_info
            ):
                raise AIoTClientError('invalid auth info')
            # 确定是否更新令牌
            refresh_time = int(auth_info['expires_ts'] - time.time())
            if refresh_time <= 60:
                valid_auth_info = await self._auth.refresh_access_token_async(refresh_token=auth_info['refresh_token'])
                auth_info = valid_auth_info
                # 更新http请求token
                self._http.update_http_header(access_token=valid_auth_info['access_token'])
                # 更新 mips lan token
                self._mips_lan.update_access_token(access_token=valid_auth_info['access_token'])
                # 更新存储
                if not await self._storage.update_user_config_async(
                        uname=self._uname,
                        lan_server=self._lan_server,
                        config={'auth_info': auth_info}
                ):
                    raise AIoTClientError('update_user_config_async error')
                _LOGGER.info('refresh auth info, get new access_token, %s', auth_info)
                refresh_time = int(auth_info['expires_ts'] - time.time())
                if refresh_time <= 0:
                    raise AIoTClientError('invalid expires time')
            self.__show_client_error_notify(None, 'auth_info')
            self.__request_refresh_auth_info(refresh_time)

            _LOGGER.debug('refresh auth info (%s, %s) after %ds', self._uname, self._lan_server, refresh_time)
            return True
        except Exception as err:
            self.__show_client_error_notify(
                message=self._i18n.translate('aiot.client.invalid_auth_info'),  # type: ignore
                notify_key='auth_info'
            )
            _LOGGER.error(
                'refresh auth info error (%s, %s), %s, %s',
                self._uname, self._lan_server, err, traceback.format_exc()
            )
        return False

    async def set_prop_async(
            self,
            did: str,
            snnd: str,
            group_id: str,
            json_data: dict
    ) -> bool:
        if did not in self._device_list_cache:
            raise AIoTClientError(f'did not exist, {did}')

        # Lan control
        device_lan = self._device_list_lan.get(did, None)
        if device_lan and device_lan.get('online', False):
            did_strs: list[str] = did.split('.')
            result = await self._http.set_prop_async(
                params={
                    "cmd": snnd,
                    "midBindId": did_strs[0],
                    "endpointId": did_strs[1],
                    "groupId": group_id,
                    "jsonData": json_data,

                }
            )
            _LOGGER.debug('ctrl: %s, %s, %s -> %s', did, snnd, json_data, result)
            rs = result.get('success', False)
            if rs:
                return True
            if rs in ['false']:
                # 设备移除或离线
                _LOGGER.error('device may be removed or offline, %s', did)
                self._main_loop.create_task(
                    await self.__refresh_lan_device_with_dids_async(dids=[did])
                )
            raise AIoTClientError(
                self.__get_exec_error_with_rc(rc=rs))

        # 显示错误信息
        raise AIoTClientError(
            f'{self._i18n.translate("aiot.client.device_exec_error")}, '
            f'{self._i18n.translate("error.common.-10007")}'
        )

    def request_refresh_prop(
            self,
            did: str,
            snnd: str,
            pnnd: str
    ) -> None:
        if did not in self._device_list_cache:
            raise AIoTClientError(f'did not exist, {did}')
        key: str = f'{did}|{snnd}|{pnnd}'
        if key in self._refresh_props_list:
            return
        self._refresh_props_list[key] = {
            'did': did,
            'snnd': snnd,
            'pnnd': pnnd
        }
        if self._refresh_props_timer:
            return
        self._refresh_props_timer = self._main_loop.call_later(
            REFRESH_PROPS_DELAY,
            lambda: self._main_loop.create_task(self.__refresh_props_handler())
        )

    async def get_prop_async(
            self,
            did: str,
            snnd: str,
            pnnd: str
    ) -> Any:
        if did not in self._device_list_cache:
            raise AIoTClientError(f'did not exist, {did}')

        try:
            if self._network.network_status:
                result = await self._http.get_prop_async(
                    did=did,
                    snnd=snnd,
                    pnnd=pnnd
                )
                if result:
                    return result
        except Exception as err:  # pylint: disable=broad-exception-caught
            # Catch all exceptions
            _LOGGER.error('client get prop from cloud error, %s, %s', err, traceback.format_exc())
        return None

    async def action_async(
            self,
            did: str,
            snnd: str,
            annd: str,
            in_list: list
    ) -> list:
        if did not in self._device_list_cache:
            raise AIoTClientError(f'did not exist, {did}')

        # Lan control
        device_lan = self._device_list_lan.get(did, None)
        if device_lan and device_lan.get('online', False):
            result: dict = await self._http.action_async(
                did=did,
                snnd=snnd,
                annd=annd,
                in_list=in_list
            )
            if result:
                rc = result.get('success')
                if rc in ['true']:
                    return result.get('data', [])
                raise AIoTClientError(self.__get_exec_error_with_rc(rc=rc))
        # TODO: 显示错误信息
        _LOGGER.error('client action failed, %s, %s', did, snnd)
        return []

    def sub_prop(
            self,
            did: str,
            handler: Callable[[dict, Any], None],
            snnd: Optional[str] = None,
            pnnd: Optional[str] = None,
            handler_ctx: Any = None
    ) -> bool:
        if did not in self._device_list_cache:
            raise AIoTClientError(f'did not exist, {did}')

        topic = (
            f'{did}/p/'
            f'{"#" if snnd is None or pnnd is None else f"{snnd}/{pnnd}"}'
        )
        self._sub_tree[topic] = AIoTClientSub(topic=topic, handler=handler, handler_ctx=handler_ctx)
        _LOGGER.debug('client sub prop, %s', topic)
        return True

    def unsub_prop(
            self,
            did: str,
            snnd: Optional[str] = None,
            pnnd: Optional[str] = None
    ) -> bool:
        topic = (
            f'{did}/p/'
            f'{"#" if snnd is None or pnnd is None else f"{snnd}/{pnnd}"}'
        )
        if self._sub_tree.get(topic=topic):
            del self._sub_tree[topic]
        _LOGGER.debug('client unsub prop, %s', topic)
        return True

    def sub_event(
            self,
            did: str,
            handler: Callable[[dict, Any], None],
            snnd: Optional[str] = None,
            ennd: Optional[str] = None,
            handler_ctx: Any = None
    ) -> bool:
        if did not in self._device_list_cache:
            raise AIoTClientError(f'did not exist, {did}')
        topic = (
            f'{did}/e/'
            f'{"#" if snnd is None or ennd is None else f"{snnd}/{ennd}"}'
        )
        self._sub_tree[topic] = AIoTClientSub(topic=topic, handler=handler, handler_ctx=handler_ctx)
        _LOGGER.debug('client sub event, %s', topic)
        return True

    def unsub_event(
            self,
            did: str,
            snnd: Optional[str] = None,
            ennd: Optional[str] = None
    ) -> bool:
        topic = (
            f'{did}/e/'
            f'{"#" if snnd is None or ennd is None else f"{snnd}/{ennd}"}'
        )
        if self._sub_tree.get(topic=topic):
            del self._sub_tree[topic]
        _LOGGER.debug('client unsub event, %s', topic)
        return True

    def sub_device_state(
            self,
            did: str,
            handler: Callable[[str, AIoTDeviceState, Any], None],
            handler_ctx: Any = None
    ) -> bool:
        """在主循环中回调处理"""
        if did not in self._device_list_cache:
            raise AIoTClientError(f'did not exist, {did}')
        self._sub_device_state[did] = MipsDeviceState(did=did, handler=handler, handler_ctx=handler_ctx)
        _LOGGER.debug('client sub device state, %s', did)
        return True

    def unsub_device_state(self, did: str) -> bool:
        self._sub_device_state.pop(did, None)
        _LOGGER.debug('client unsub device state, %s', did)
        return True

    async def remove_device_async(self, did: str) -> None:
        if did not in self._device_list_cache:
            return
        # 取消订阅
        self.__unsub_from(did)

        # 存储
        await self._storage.save_async(
            domain='aiot_devices',
            name=f'{self._uname}_{self._lan_server}',
            data=self._device_list_cache
        )
        # 更改通知
        self.__request_show_devices_changed_notify()

    async def remove_device2_async(self, did_tag: str) -> None:
        for did in self._device_list_cache:
            d_tag = slugify_did(lan_server=self._lan_server, did=did)
            if did_tag == d_tag:
                await self.remove_device_async(did)
                break

    def __get_exec_error_with_rc(self, rc: int) -> str:
        err_msg: str = self._i18n.translate(key=f'error.common.{rc}')  # type: ignore
        if not err_msg:
            err_msg = f'{self._i18n.translate(key="error.common.-10000")}, '
            err_msg += f'code={rc}'
        return f'{self._i18n.translate(key="aiot.client.device_exec_error")}, ' + err_msg

    @final
    def __gen_notify_key(self, name: str) -> str:
        return f'{DOMAIN}-{self._uname}-{self._lan_server}-{name}'

    @final
    def __request_refresh_auth_info(self, delay_sec: int) -> None:
        if self._refresh_token_timer:
            self._refresh_token_timer.cancel()
            self._refresh_token_timer = None
        self._refresh_token_timer = self._main_loop.call_later(
            delay_sec,
            lambda: self._main_loop.create_task(self.refresh_auth_info_async())
        )

    @final
    def __unsub_from(self, did: str) -> None:
        mips = self._mips_lan
        if mips is not None:
            try:
                mips.unsub_prop(did=did)
                mips.unsub_event(did=did)
            except RuntimeError as e:
                if 'Event loop is closed' in str(e):
                    # Ignore unsub exception when loop is closed
                    pass
                else:
                    raise

    @final
    def __sub_from(self, did: str) -> None:
        mips = self._mips_lan
        if mips is not None:
            mips.sub_prop(did=did, handler=self.__on_prop_msg)
            mips.sub_event(did=did, handler=self.__on_event_msg)

    @final
    def __update_device_msg_sub(self, did: str) -> None:
        if did not in self._device_list_cache:
            return
        # Sub new
        self.__sub_from(did)

    @final
    async def __on_network_status_changed(self, status: bool) -> None:
        _LOGGER.info('network status changed, %s', status)
        if status:
            # 检查认证信息
            if await self.refresh_auth_info_async():
                # 连接到mips局域网
                self._mips_lan.connect()
                # 更新设备列表
                self.__request_refresh_lan_devices()
        else:
            self.__request_show_devices_changed_notify(delay_sec=30)
            # 取消刷新局域网设备
            if self._refresh_lan_devices_timer:
                self._refresh_lan_devices_timer.cancel()
                self._refresh_lan_devices_timer = None
            # 断开局域网mips连接
            self._mips_lan.disconnect()

    @final
    async def __on_mips_lan_state_changed(self, key: str, state: bool) -> None:
        _LOGGER.info('lan mips state changed, %s, %s', key, state)
        if state:
            # 连接
            self.__request_refresh_lan_devices(immediately=True)
            # 订阅lan设备状态
            for did in list(self._device_list_cache.keys()):
                self._mips_lan.sub_device_state(did=did, handler=self.__on_lan_device_state_changed)
        else:
            # 断开连接
            for did, info in self._device_list_lan.items():
                lan_state_old: Optional[bool] = info.get('online', None)
                if not lan_state_old:
                    # Lan 为None或False，无需更新
                    continue
                info['online'] = False
                if did not in self._device_list_cache:
                    continue
                self.__update_device_msg_sub(did=did)
                state_old: Optional[bool] = self._device_list_cache[did].get('online', None)
                state_new: Optional[bool] = self.__check_device_state(False)
                if state_old == state_new:
                    continue
                self._device_list_cache[did]['online'] = state_new
                sub = self._sub_device_state.get(did, None)
                if sub and sub.handler:
                    sub.handler(did, AIoTDeviceState.OFFLINE, sub.handler_ctx)
            self.__request_show_devices_changed_notify()

    @final
    def __on_lan_device_state_changed(self, did: str, state: AIoTDeviceState, ctx: Any) -> None:
        _LOGGER.info('lan device state changed, %s, %s', did, state)
        lan_device = self._device_list_lan.get(did, None)
        if not lan_device:
            return
        lan_state_new: bool = state == AIoTDeviceState.ONLINE
        if lan_device.get('online', False) == lan_state_new:
            return
        lan_device['online'] = lan_state_new
        if did not in self._device_list_cache:
            return
        self.__update_device_msg_sub(did=did)
        state_old: Optional[bool] = self._device_list_cache[did].get('online', None)
        state_new: Optional[bool] = self.__check_device_state(lan_state_new)
        if state_old == state_new:
            return
        self._device_list_cache[did]['online'] = state_new
        sub = self._sub_device_state.get(did, None)
        if sub and sub.handler:
            sub.handler(
                did,
                AIoTDeviceState.ONLINE if state_new else AIoTDeviceState.OFFLINE,
                sub.handler_ctx
            )
        self.__request_show_devices_changed_notify()

    @final
    def __on_prop_msg(self, params: dict, ctx: Any) -> None:
        """参数必须包含 did, snnd, pnnd, value"""
        # BLE设备没有在线/离线消息
        try:
            subs: list[AIoTClientSub] = list(
                self._sub_tree.iter_match(f'{params["did"]}/p/{params["snnd"]}/{params["pnnd"]}')
            )
            for sub in subs:
                sub.handler(params, sub.handler_ctx)
        except Exception as err:  # pylint: disable=broad-exception-caught
            _LOGGER.error('on prop msg error, %s, %s', params, err)

    @final
    def __on_event_msg(self, params: dict, ctx: Any) -> None:
        try:
            subs: list[AIoTClientSub] = list(
                self._sub_tree.iter_match(f'{params["did"]}/e/{params["snnd"]}/{params["ennd"]}')
            )
            for sub in subs:
                sub.handler(params, sub.handler_ctx)
        except Exception as err:  # pylint: disable=broad-exception-caught
            _LOGGER.error('on event msg error, %s, %s', params, err)

    @final
    def __check_device_state(self, lan_state: Optional[bool]) -> Optional[bool]:
        if lan_state is None:
            # 设备移除
            return None
        if lan_state:
            return True
        return False

    @final
    async def __load_cache_device_async(self) -> None:
        """从缓存加载设备列表"""
        cache_list: Optional[dict[str, dict]] = await self._storage.load_async(
            domain='aiot_devices',
            name=f'{self._uname}_{self._lan_server}',
            type_=dict
        )  # type: ignore
        if not cache_list:
            self.__show_client_error_notify(
                message=self._i18n.translate('aiot.client.invalid_device_cache'),  # type: ignore
                notify_key='device_cache'
            )
            raise AIoTClientError('load device list from cache error')
        else:
            self.__show_client_error_notify(message=None, notify_key='device_cache')
        # 设置默认在线状态为 False
        self._device_list_cache = {}
        for did, info in cache_list.items():
            if info.get('online', None):
                self._device_list_cache[did] = {**info, 'online': False}
            else:
                self._device_list_cache[did] = info
        self._device_list_lan = deepcopy(self._device_list_cache)

    @final
    async def __update_devices_from_lan_async(
            self,
            lan_list: dict[str, dict],
            filter_dids: Optional[list[str]] = None
    ) -> None:
        """更新设备"""
        for did, info in self._device_list_cache.items():
            if filter_dids and did not in filter_dids:
                continue
            state_old: Optional[bool] = info.get('online', None)
            lan_state_old: Optional[bool] = self._device_list_lan.get(did, {}).get('online', None)
            lan_state_new: Optional[bool] = None
            device_new = lan_list.pop(did, None)
            if device_new:
                lan_state_new = device_new.get('online', None)
                # 更新缓存设备信息
                info.update({**device_new, 'online': state_old})
                # 更新局域网设备
                self._device_list_lan[did] = device_new
            else:
                # 设备删除
                self._device_list_lan[did]['online'] = None
            if lan_state_old == lan_state_new:
                # Lan　在线状态没有变更
                continue
            # Update sub from
            self.__update_device_msg_sub(did=did)
            state_new: Optional[bool] = self.__check_device_state(lan_state_new)
            if state_old == state_new:
                # 在线状态没有变更
                continue
            info['online'] = state_new
            # 设备状态更改回调
            sub = self._sub_device_state.get(did, None)
            if sub and sub.handler:
                sub.handler(
                    did,
                    AIoTDeviceState.ONLINE if state_new else AIoTDeviceState.OFFLINE,
                    sub.handler_ctx
                )
        # 新设备
        self._device_list_lan.update(lan_list)
        # 更新存储
        if not await self._storage.save_async(
                domain='aiot_devices',
                name=f'{self._uname}_{self._lan_server}',
                data=self._device_list_cache
        ):
            _LOGGER.error('save device list to cache failed')

    @final
    async def __refresh_lan_devices_async(self) -> None:
        _LOGGER.debug('refresh lan devices, %s, %s', self._uname, self._lan_server)
        if self._refresh_lan_devices_timer:
            self._refresh_lan_devices_timer.cancel()
            self._refresh_lan_devices_timer = None
        try:
            result = await self._http.get_devices_async()
        except Exception as err:  # pylint: disable=broad-exception-caught
            _LOGGER.error('refresh lan devices failed, %s', err)
            self._refresh_lan_devices_timer = self._main_loop.call_later(
                REFRESH_LAN_DEVICES_RETRY_DELAY,
                lambda: self._main_loop.create_task(self.__refresh_lan_devices_async())
            )
            return
        if not result and 'devices' not in result:
            self.__show_client_error_notify(
                message=self._i18n.translate('aiot.client.device_lan_error'),  # type: ignore
                notify_key='device_lan'
            )
            return
        else:
            self.__show_client_error_notify(message=None, notify_key='device_lan')
        lan_list: dict[str, dict] = result['devices']
        await self.__update_devices_from_lan_async(lan_list=lan_list)

        self.__request_show_devices_changed_notify()

    @final
    async def __refresh_lan_device_with_dids_async(self, dids: list[str]) -> None:
        _LOGGER.debug('refresh lan device with dids, %s', dids)
        lan_list = await self._http.get_devices_with_dids_async(dids=dids)
        if lan_list is None:
            _LOGGER.error('cloud http get_dev_list_async failed, %s', dids)
            return
        await self.__update_devices_from_lan_async(lan_list=lan_list, filter_dids=dids)
        self.__request_show_devices_changed_notify()

    def __request_refresh_lan_devices(self, immediately=False) -> None:
        """请求刷新局域网设备"""
        _LOGGER.debug('request refresh lan devices, %s, %s', self._uname, self._lan_server)
        delay_sec: int = 0 if immediately else REFRESH_LAN_DEVICES_DELAY
        if self._refresh_lan_devices_timer:
            self._refresh_lan_devices_timer.cancel()
        self._refresh_lan_devices_timer = self._main_loop.call_later(
            delay_sec,
            lambda: self._main_loop.create_task(self.__refresh_lan_devices_async())
        )

    @final
    async def __refresh_props_from_lan(self, patch_len: int = 150) -> bool:
        if not self._network.network_status:
            return False

        if len(self._refresh_props_list) < patch_len:
            request_list = self._refresh_props_list
            self._refresh_props_list = {}
        else:
            request_list = {}
            for _ in range(patch_len):
                key, value = self._refresh_props_list.popitem()
                request_list[key] = value
        try:
            results = await self._http.get_props_async(params=list(request_list.values()))
            if not results:
                raise AIoTClientError('get_props_async failed')
            for result in results:
                if (
                        'macAddr' not in result
                        or 'endPointPort' not in result
                        or 'aamCmd' not in result
                        or 'aamPropName' not in result
                        or 'aamPropValue' not in result
                ):
                    continue
                # 请求成功，从列表弹出
                request_list.pop(
                    f'{result["macAddr"]}|{result['endPointPort']}|{result["aamCmd"]}|{result["aamPropName"]}', None
                )
                self.__on_prop_msg(params=result, ctx=None)
            if request_list:
                _LOGGER.info('refresh props failed, cloud, %s', list(request_list.keys()))
                request_list = None
            return True
        except Exception as err:  # pylint:disable=broad-exception-caught
            _LOGGER.error('refresh props error, cloud, %s, %s', err, traceback.format_exc())
            # 将失败的请求添加回列表
            self._refresh_props_list.update(request_list)
            return False

    @final
    async def __refresh_props_handler(self) -> None:
        """刷新属性值"""
        if not self._refresh_props_list:
            return
        # Lan
        if await self.__refresh_props_from_lan():
            self._refresh_props_retry_count = 0
            if self._refresh_props_list:
                self._refresh_props_timer = self._main_loop.call_later(
                    REFRESH_PROPS_DELAY,
                    lambda: self._main_loop.create_task(self.__refresh_props_handler())
                )
            else:
                self._refresh_props_timer = None
            return

        # 尝试三次，如果失败三次，清空列表.
        if self._refresh_props_retry_count >= 3:
            self._refresh_props_list = {}
            self._refresh_props_retry_count = 0
            if self._refresh_props_timer:
                self._refresh_props_timer.cancel()
                self._refresh_props_timer = None
            _LOGGER.info('refresh props failed, retry count exceed')
            return
        self._refresh_props_retry_count += 1
        _LOGGER.info('refresh props failed, retry, %s', self._refresh_props_retry_count)
        self._refresh_props_timer = self._main_loop.call_later(
            REFRESH_PROPS_RETRY_DELAY,
            lambda: self._main_loop.create_task(self.__refresh_props_handler())
        )

    @final
    def __show_client_error_notify(self, message: Optional[str], notify_key: str = '') -> None:
        """显示客户端错误通知"""
        if message:
            self._persistence_notify(
                f'{DOMAIN}{self._uname}{self._lan_server}{notify_key}error',
                self._i18n.translate(key='aiot.client.aimy_home_error_title'),  # type: ignore
                self._i18n.translate(
                    key='aiot.client.aimy_home_error',
                    replace={
                        'nick_name': self._entry_data.get('nick_name', DEFAULT_NICK_NAME),
                        'uname': self._uname,
                        'lan_server': self._lan_server,
                        'message': message
                    }
                )
            )  # type: ignore
        else:
            self._persistence_notify(
                f'{DOMAIN}{self._uname}{self._lan_server}{notify_key}error',
                None, None)

    @final
    def __show_devices_changed_notify(self) -> None:
        """显示设备列表更改通知"""
        self._show_devices_changed_notify_timer = None
        if self._persistence_notify is None:
            return

        message_add: str = ''
        count_add: int = 0
        message_del: str = ''
        count_del: int = 0
        message_offline: str = ''
        count_offline: int = 0

        # 新设备
        if 'add' in self._display_devs_notify:
            for did, info in {**self._device_list_lan}.items():
                if did in self._device_list_cache:
                    continue
                count_add += 1
                message_add += (
                    f'- {info.get("name", "unknown")} ({did}, '
                    f'{info.get("model", "unknown")})\n'
                )
        # 获取不可用和离线设备
        home_name_del: Optional[str] = None
        home_name_offline: Optional[str] = None
        for did, info in self._device_list_cache.items():
            online: Optional[bool] = info.get('online', None)
            home_name_new = info.get('home_name', 'unknown')
            if online:
                # 跳过在线设备
                continue
            if 'del' in self._display_devs_notify and online is None:
                # 设备不存在
                if home_name_del != home_name_new:
                    message_del += f'\n[{home_name_new}]\n'
                    home_name_del = home_name_new
                count_del += 1
                message_del += (
                    f'- {info.get("name", "unknown")} ({did}, '
                    f'{info.get("room_name", "unknown")})\n'
                )
                continue
            if 'offline' in self._display_devs_notify:
                # 设备离线
                if home_name_offline != home_name_new:
                    message_offline += f'\n[{home_name_new}]\n'
                    home_name_offline = home_name_new
                count_offline += 1
                message_offline += (
                    f'- {info.get("name", "unknown")} ({did}, '
                    f'{info.get("room_name", "unknown")})\n'
                )

        message = ''
        if 'add' in self._display_devs_notify and count_add:
            message += self._i18n.translate(
                key='aiot.client.device_list_add',
                replace={
                    'count': count_add,
                    'message': message_add
                }
            )  # type: ignore
        if 'del' in self._display_devs_notify and count_del:
            message += self._i18n.translate(
                key='aiot.client.device_list_del',
                replace={
                    'count': count_del,
                    'message': message_del
                }
            )  # type: ignore
        if 'offline' in self._display_devs_notify and count_offline:
            message += self._i18n.translate(
                key='aiot.client.device_list_offline',
                replace={
                    'count': count_offline,
                    'message': message_offline
                }
            )  # type: ignore
        if message != '':
            msg_hash = hash(message)
            if msg_hash == self._display_notify_content_hash:
                # 通知内容无更改，返回
                _LOGGER.debug('device list changed notify content no change, return')
                return
            network_status = self._i18n.translate(
                key='aiot.client.network_status_online'
                if self._network.network_status
                else 'aiot.client.network_status_offline'
            )
            self._persistence_notify(
                self.__gen_notify_key('dev_list_changed'),
                self._i18n.translate('aiot.client.device_list_changed_title'),  # type: ignore
                self._i18n.translate(
                    key='aiot.client.device_list_changed',
                    replace={
                        'nick_name': self._entry_data.get('nick_name', DEFAULT_NICK_NAME),
                        'uname': self._uname,
                        'lan_server': self._lan_server,
                        'network_status': network_status,
                        'message': message
                    }
                )
            )  # type: ignore
            self._display_notify_content_hash = msg_hash
            _LOGGER.debug('show device list changed notify, add %s, del %s, offline %s', count_add, count_del,
                          count_offline)
        else:
            self._persistence_notify(self.__gen_notify_key('dev_list_changed'), None, None)

    @final
    def __request_show_devices_changed_notify(self, delay_sec: float = 6) -> None:
        if not self._display_devs_notify:
            return
        if not self._mips_lan:
            return
        if self._show_devices_changed_notify_timer:
            self._show_devices_changed_notify_timer.cancel()
        self._show_devices_changed_notify_timer = self._main_loop.call_later(
            delay_sec,
            self.__show_devices_changed_notify
        )

    @final
    def __show_central_state_changed_notify(self, connected: bool) -> None:
        conn_status: str = (
            self._i18n.translate('aiot.client.central_state_connected')
            if connected else
            self._i18n.translate('aiot.client.central_state_disconnected'))
        self._persistence_notify(
            self.__gen_notify_key('central_state_changed'),
            self._i18n.translate('aiot.client.central_state_changed_title'),
            self._i18n.translate(
                key='aiot.client.central_state_changed',
                replace={
                    'nick_name': self._entry_data.get('nick_name', DEFAULT_NICK_NAME),
                    'uname': self._uname,
                    'lan_server': self._lan_server,
                    'conn_status': conn_status
                }
            )
        )


@staticmethod
async def get_aiot_instance_async(
        hass: HomeAssistant,
        entry_id: str,
        entry_data: Optional[dict] = None,
        persistent_notify: Optional[Callable[[str, str, str], None]] = None
) -> AIoTClient:
    if entry_id is None:
        raise AIoTClientError('invalid entry_id')
    aiot_client = hass.data[DOMAIN].get('aiot_clients', {}).get(entry_id, None)
    if aiot_client:
        _LOGGER.info('instance exist, %s', entry_id)
        return aiot_client
    # 创建新实例
    if not entry_data:
        raise AIoTClientError('entry data is None')
    # 启动运行循环
    loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
    if not loop:
        raise AIoTClientError('loop is None')
    # AIoT 存储
    storage: Optional[AIoTStorage] = hass.data[DOMAIN].get('aiot_storage', None)
    if not storage:
        storage = AIoTStorage(root_path=entry_data['storage_path'], loop=loop)
        hass.data[DOMAIN]['aiot_storage'] = storage
        _LOGGER.info('create aiot_storage instance')
    global_config: dict = await storage.load_user_config_async(
        uname='global_config',
        lan_server='all',
        keys=['network_detect_addr', 'net_interfaces', 'enable_subscribe']
    )
    # AIoT 网络
    network_detect_addr: dict = global_config.get('network_detect_addr', {})
    network: Optional[AIoTNetwork] = hass.data[DOMAIN].get('aiot_network', None)
    if not network:
        network = AIoTNetwork(
            ip_addr_list=network_detect_addr.get('ip', []),
            url_addr_list=network_detect_addr.get('url', []),
            refresh_interval=NETWORK_REFRESH_INTERVAL,
            loop=loop
        )
        hass.data[DOMAIN]['aiot_network'] = network
        await network.init_async()
        _LOGGER.info('create aiot_network instance')
    # AIoT 服务
    mips_service: Optional[MipsService] = hass.data[DOMAIN].get('mips_service', None)
    if not mips_service:
        aiozc = await zeroconf.async_get_async_instance(hass)
        mips_service = MipsService(aiozc=aiozc, loop=loop)
        hass.data[DOMAIN]['mips_service'] = mips_service
        await mips_service.init_async()
        _LOGGER.info('create mips_service instance')
    # AIoT 客户端
    aiot_client = AIoTClient(
        entry_id=entry_id,
        entry_data=entry_data,
        network=network,
        storage=storage,
        mips_service=mips_service,
        loop=loop
    )
    aiot_client.persistent_notify = persistent_notify
    hass.data[DOMAIN]['aiot_clients'].setdefault(entry_id, aiot_client)
    _LOGGER.info('new aiot_client instance, %s, %s', entry_id, entry_data)
    await aiot_client.init_async()
    return aiot_client
