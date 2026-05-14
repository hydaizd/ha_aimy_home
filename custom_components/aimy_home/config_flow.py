# -*- coding: utf-8 -*-

import asyncio
import hashlib
import json
import logging
import secrets
import traceback
from typing import Any, Optional

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.instance_id import async_get

from .aiot.aiot_client import AIoTClient, get_aiot_instance_async
from .aiot.aiot_error import AIoTAuthError, AIoTError, AIoTConfigError
from .aiot.aiot_i18n import AIoTI18n
from .aiot.aiot_lan import AIoTAuthClient, AIoTHttpClient
from .aiot.aiot_network import AIoTNetwork
from .aiot.aiot_storage import AIoTStorage
from .aiot.const import (
    DOMAIN,
    NETWORK_REFRESH_INTERVAL,
    DEFAULT_INTEGRATION_LANGUAGE,
    AUTH_CLIENT_ID,
    DEFAULT_NICK_NAME,
    DEFAULT_COVER_DEAD_ZONE_WIDTH
)

_LOGGER = logging.getLogger(__name__)


class AimyHomeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """处理艾美智空间盒子的配置流程."""
    VERSION = 1
    _config_entry: config_entries.ConfigEntry
    _main_loop: asyncio.AbstractEventLoop
    _aiot_network: AIoTNetwork
    _aiot_storage: Optional[AIoTStorage]
    _aiot_i18n: AIoTI18n
    _aiot_auth: Optional[AIoTAuthClient]
    _aiot_http: Optional[AIoTHttpClient]

    _storage_path: str
    # 虚拟id
    _virtual_did: str
    _uname: str
    _uuid: str
    _action_debug: bool
    _display_devices_changed_notify: list[str]

    _lan_server: str
    _integration_language: str
    _cover_dz_width: int
    _auth_info: dict
    _nick_name: str

    # Config cache
    _cc_home_info: dict
    _cc_network_detect_addr: str
    _cc_task_auth: Optional[asyncio.Task[None]]
    _cc_config_rc: Optional[str]

    # 新增自定义
    _password: str

    def __init__(self) -> None:
        self._main_loop = asyncio.get_running_loop()
        self._lan_server = ''
        self._integration_language = DEFAULT_INTEGRATION_LANGUAGE
        self._cover_dz_width = DEFAULT_COVER_DEAD_ZONE_WIDTH
        self._storage_path = ''
        self._virtual_did = ''
        self._uname = ''
        self._uuid = ''  # MQTT client id
        self._action_debug = False
        self._display_devices_changed_notify = ['add', 'del', 'offline']
        self._auth_info = {}
        self._nick_name = DEFAULT_NICK_NAME
        self._cc_network_detect_addr = ''
        self._aiot_auth = None
        self._aiot_http = None

        self._cc_home_info = {}
        self._cc_task_auth = None
        self._cc_config_rc = None

        # 新增自定义
        self._password = ''

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """处理初始步骤."""
        self.hass.data.setdefault(DOMAIN, {})
        # 虚拟id
        if not self._virtual_did:
            self._virtual_did = str(secrets.randbits(64))
            self.hass.data[DOMAIN].setdefault(self._virtual_did, {})
        # 　存储路径
        if not self._storage_path:
            self._storage_path = self.hass.config.path('.storage', DOMAIN)
        # AIoT 存储
        self._aiot_storage = self.hass.data[DOMAIN].get('aiot_storage', None)
        if not self._aiot_storage:
            self._aiot_storage = AIoTStorage(root_path=self._storage_path, loop=self._main_loop)
            self.hass.data[DOMAIN]['aiot_storage'] = self._iot_storage
            _LOGGER.info('async_step_user, create aiot storage, %s', self._storage_path)

        # AIoT 网络
        network_detect_addr = (await self._aiot_storage.load_user_config_async(
            uname='global_config',
            lan_server='all',
            keys=['network_detect_addr']
        )).get('network_detect_addr', {})
        self._cc_network_detect_addr = ','.join(network_detect_addr.get('ip', []) + network_detect_addr.get('url', []))
        self._aiot_network = self.hass.data[DOMAIN].get('aiot_network', None)
        if not self._aiot_network:
            self._aiot_network = AIoTNetwork(
                ip_addr_list=network_detect_addr.get('ip', []),
                url_addr_list=network_detect_addr.get('url', []),
                refresh_interval=NETWORK_REFRESH_INTERVAL,
                loop=self._main_loop
            )
            self.hass.data[DOMAIN]['aiot_network'] = self._aiot_network
            await self._aiot_network.init_async()
            _LOGGER.info('async_step_user, create iot network')

        return await self.async_step_auth_config(user_input)

    async def async_step_auth_config(self, user_input: Optional[dict] = None):
        if user_input:
            self._lan_server = user_input.get('lan_server', self._lan_server)
            self._uname = user_input.get('uname', self._uname)
            self._password = user_input.get('password', self._password)

            # 生成实例uuid
            ha_uuid = await async_get(self.hass)
            if not ha_uuid:
                raise AbortFlow(reason='ha_uuid_get_failed')
            self._uuid = hashlib.sha256(
                f'{ha_uuid}.{self._virtual_did}.{self._lan_server}'.encode('utf-8')
            ).hexdigest()[:32]
            self._integration_language = user_input.get('integration_language', DEFAULT_INTEGRATION_LANGUAGE)

            self._aiot_i18n = AIoTI18n(lang=self._integration_language, loop=self._main_loop)
            await self._aiot_i18n.init_async()

            try:
                return await self.async_step_auth(user_input)
            except Exception as err:
                _LOGGER.error('async_step_auth_config, %s', err)
                return await self.__show_auth_config_form(str(err))
        return await self.__show_auth_config_form('')

    async def __show_auth_config_form(self, reason: str):
        """显示认证配置表单."""
        return self.async_show_form(
            step_id="auth_config",
            data_schema=vol.Schema({
                vol.Required('lan_server', default=""): str,
                vol.Required('uname', default="admin"): str,
                vol.Required('password', default="admin"): str,
            }),
            errors={"base": reason},
        )

    async def async_step_auth(self, user_input: Optional[dict] = None):
        try:
            if not self._aiot_auth:
                _LOGGER.info('async_step_auth, lan_server: %s', self._lan_server)
                aiot_auth = AIoTAuthClient(
                    lan_server=self._lan_server,
                    uuid=self._uuid,
                    loop=self._main_loop
                )
                self.hass.data[DOMAIN][self._virtual_did]['auth_state'] = aiot_auth.state
                self.hass.data[DOMAIN][self._virtual_did]['i18n'] = self._aiot_i18n
                _LOGGER.info('async_step_auth, lan_server: %s', self._lan_server)
                self._aiot_auth = aiot_auth
        except Exception as err:  # pylint: disable=broad-exception-caught
            _LOGGER.error('async_step_auth, %s', err)
            return self.async_show_progress_done(next_step_id='auth_error')

        if self._cc_task_auth is None:
            self._cc_task_auth = self.hass.async_create_task(self.__check_auth_async())

        if self._cc_task_auth.done():
            if (error := self._cc_task_auth.exception()):
                _LOGGER.error('task_auth exception, %s', error)
                self._cc_config_rc = str(error)
                return self.async_show_progress_done(next_step_id='auth_error')
            if self._aiot_auth:
                await self._aiot_auth.deinit_async()
                self._aiot_auth = None
            return self.config_flow_done()

        # pylint: disable=unexpected-keyword-arg
        return self.async_show_progress_done(next_step_id='auth_error')

    async def config_flow_done(self):
        return self.async_create_entry(
            title=f'智空间盒子 ({self._lan_server})',
            data={
                'virtual_did': self._virtual_did,
                'uuid': self._uuid,
                'integration_language': self._integration_language,
                'storage_path': self._storage_path,
                'uname': self._uname,
                'nick_name': self._nick_name,
                'lan_server': self._lan_server,
                'action_debug': self._action_debug,
                'cover_dead_zone_width': self._cover_dz_width,
                'display_devices_changed_notify': self._display_devices_changed_notify,
                'password': self._password,
            })

    async def __check_auth_async(self) -> None:
        """检查认证是否成功."""
        # 从aiot_auth获取access_token和user_info
        if not self._auth_info:
            try:
                if not self._aiot_auth:
                    raise AIoTConfigError('auth_client_error')
                auth_info = await self._aiot_auth.get_access_token_async()
                if not self._aiot_http:
                    self._aiot_http = AIoTHttpClient(
                        lan_server=self._lan_server,
                        client_id=AUTH_CLIENT_ID,
                        access_token=auth_info['access_token']
                    )
                else:
                    self._aiot_http.update_http_header(access_token=auth_info['access_token'])
                self._auth_info = auth_info
                try:
                    self._nick_name = (await self._aiot_http.get_user_info_async() or {}).get('name', self._nick_name)
                except (AIoTAuthError, json.JSONDecodeError):
                    self._nick_name = DEFAULT_NICK_NAME
                    _LOGGER.error('get nick name failed')
            except Exception as err:
                _LOGGER.error('get_access_token, %s, %s', err, traceback.format_exc())
                raise AIoTConfigError('get_token_error') from err

        # 获取设备信息
        try:
            if not self._aiot_http:
                raise AIoTConfigError('http_client_error')
            self._cc_home_info = (await self._aiot_http.get_devices_async())
            _LOGGER.info('get_homeinfos response: %s', self._cc_home_info)
            # Save auth_info
            if not (await self._iot_storage.update_user_config_async(
                    uname=self._username,
                    lan_server=self._lan_server,
                    config={'auth_info': self._auth_info}
            )):
                raise AIoTError('aiot_storage.update_user_config_async error')
        except Exception as err:
            _LOGGER.error('save_auth_info error, %s, %s', err, traceback.format_exc())
            raise AIoTConfigError('save_auth_info_error') from err

        if self._aiot_http:
            await self._aiot_http.deinit_async()
            self._aiot_http = None
        _LOGGER.info('__check_auth_async: %s', self._virtual_did)

    # 显示安装错误信息
    async def async_step_auth_error(self, user_input=None):
        if self._cc_config_rc is None:
            return await self.async_step_auth()
        if self._cc_config_rc.startswith('Flow aborted: '):
            raise AbortFlow(reason=self._cc_config_rc.replace('Flow aborted: ', ''))
        error_reason = self._cc_config_rc
        self._cc_config_rc = None
        return self.async_show_form(
            step_id='auth_error',
            data_schema=vol.Schema({}),
            last_step=False,
            errors={'base': error_reason},
        )
    #
    # @staticmethod
    # @callback
    # def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
    #     return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(config_entries.OptionsFlow):
    """ 使用户能够在 UI 界面上随时修改已配置集成的可选参数。 """
    # pylint: disable=unused-argument
    # pylint: disable=inconsistent-quotes
    _config_entry: config_entries.ConfigEntry
    _main_loop: asyncio.AbstractEventLoop
    _aiot_client: AIoTClient

    _aiot_network: AIoTNetwork
    _aiot_storage: AIoTStorage
    _aiot_auth: AIoTAuthClient
    _aiot_http: AIoTHttpClient
    _aiot_i18n: AIoTI18n

    _entry_data: dict
    _virtual_did: str
    _uid: str
    _storage_path: str
    _lan_server: str

    _aiot_storage: AIoTStorage

    def __init__(self, config_entry: config_entries.ConfigEntry):
        self._config_entry = config_entry
        self._main_loop = asyncio.get_event_loop()

        _LOGGER.info(
            'options init, %s, %s, %s, %s',
            config_entry.entry_id, config_entry.unique_id, config_entry.data, config_entry.options
        )

    async def async_step_init(self, user_input=None):
        """初始化选项流程."""
        self.hass.data.setdefault(DOMAIN, {})

        try:
            # AIoT client
            self._aiot_client = await get_aiot_instance_async(hass=self.hass, entry_id=self._config_entry.entry_id)
            if not self._aiot_client:
                raise AIoTConfigError('invalid aiot client')

            # AIoT storage
            self._aiot_storage = self._aiot_client.aiot_storage
            if not self._aiot_storage:
                raise AIoTConfigError('invalid aiot storage')

            # Check token
            if not await self._aiot_client.refresh_auth_info_async():
                _LOGGER.info('refresh auth info error')

            return await self.async_step_config_options()

        except AIoTConfigError as err:
            raise AbortFlow(reason='options_flow_error', description_placeholders={'error': str(err)}) from err
        except AbortFlow as err:
            raise err
        except Exception as err:
            _LOGGER.error('async_step_init error, %s, %s', err, traceback.format_exc())
            raise AbortFlow(reason='re_add', description_placeholders={'error': str(err)}) from err
