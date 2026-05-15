# -*- coding: utf-8 -*-

import asyncio
import hashlib
import json
import logging
import secrets
import traceback
from typing import Any, Optional

import homeassistant.helpers.config_validation as cv
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
from .aiot.aiot_mdns import MipsService
from .aiot.aiot_network import AIoTNetwork
from .aiot.aiot_storage import AIoTStorage
from .aiot.const import (
    DOMAIN,
    NETWORK_REFRESH_INTERVAL,
    DEFAULT_INTEGRATION_LANGUAGE,
    AUTH_CLIENT_ID,
    DEFAULT_NICK_NAME,
    DEFAULT_COVER_DEAD_ZONE_WIDTH,
    INTEGRATION_LANGUAGES,
    MIN_COVER_DEAD_ZONE_WIDTH,
    MAX_COVER_DEAD_ZONE_WIDTH
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
            self.hass.data[DOMAIN]['aiot_storage'] = self._aiot_storage
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
                    client_id=AUTH_CLIENT_ID,
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
            self._cc_config_rc = str(err)
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
        return self.async_show_progress(
            step_id='auth',
            progress_action='auth',
            description_placeholders={},
            progress_task=self._cc_task_auth,  # type: ignore
        )

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
                'password': self._password
            })

    async def __check_auth_async(self) -> None:
        """检查认证是否成功."""
        # 从aiot_auth获取access_token和user_info
        if not self._auth_info:
            try:
                if not self._aiot_auth:
                    raise AIoTConfigError('auth_client_error')
                auth_info = await self._aiot_auth.get_access_token_async(self._uname, self._password)
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
            if not (await self._aiot_storage.update_user_config_async(
                    uname=self._uname,
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
    _mips_service: MipsService
    _aiot_auth: AIoTAuthClient
    _aiot_http: AIoTHttpClient
    _aiot_i18n: AIoTI18n

    _entry_data: dict
    _virtual_did: str
    _uname: str
    _storage_path: str
    _lan_server: str

    _integration_language: str
    _nick_name: str
    _action_debug: bool
    _display_devs_notify: list[str]

    _auth_info: dict
    _devices_add: list[str]
    _devices_remove: list[str]

    # Config options
    _lang_new: str
    _nick_name_new: Optional[str]
    _action_debug_new: bool
    _update_devices: bool
    _opt_network_detect_cfg: bool
    _opt_check_network_deps: bool
    _cover_width_new: int

    _need_reload: bool

    # Config cache
    _cc_task_auth: Optional[asyncio.Task[None]]
    _cc_config_rc: Optional[str]
    _cc_network_detect_addr: str

    # 自定义
    _password: str

    def __init__(self, config_entry: config_entries.ConfigEntry):
        self._config_entry = config_entry
        self._main_loop = asyncio.get_event_loop()

        self._entry_data = dict(config_entry.data)
        self._virtual_did = self._entry_data['virtual_did']
        self._uname = self._entry_data['uname']
        self._storage_path = self._entry_data['storage_path']
        self._lan_server = self._entry_data['lan_server']
        self._integration_language = self._entry_data.get('integration_language', DEFAULT_INTEGRATION_LANGUAGE)
        self._cover_dz_width = self._entry_data.get('cover_dead_zone_width', DEFAULT_COVER_DEAD_ZONE_WIDTH)
        self._nick_name = self._entry_data.get('nick_name', DEFAULT_NICK_NAME)
        self._action_debug = self._entry_data.get('action_debug', False)

        self._auth_info = {}

        self._devices_add = []
        self._devices_remove = []

        self._lang_new = self._integration_language
        self._nick_name_new = None
        self._action_debug_new = False
        self._cover_width_new = self._cover_dz_width
        self._update_user_info = False
        self._update_devices = False
        self._opt_network_detect_cfg = False
        self._opt_check_network_deps = False

        self._need_reload = False

        self._cc_task_auth = None
        self._cc_config_rc = None
        self._cc_network_detect_addr = ''

        self._password = self._entry_data['password']

        _LOGGER.info(
            'options init, %s, %s, %s, %s',
            config_entry.entry_id, config_entry.unique_id, config_entry.data, config_entry.options
        )

    async def async_step_init(self, user_input=None):
        """初始化选项流程."""
        self.hass.data.setdefault(DOMAIN, {})
        self.hass.data[DOMAIN].setdefault(self._virtual_did, {})
        try:
            # AIoT client
            self._aiot_client = await get_aiot_instance_async(hass=self.hass, entry_id=self._config_entry.entry_id)
            if not self._aiot_client:
                raise AIoTConfigError('invalid aiot client')
            # AIoT network
            self._aiot_network = self._aiot_client.aiot_network
            if not self._aiot_network:
                raise AIoTConfigError('invalid aiot network')
            # AIoT storage
            self._aiot_storage = self._aiot_client.aiot_storage
            if not self._aiot_storage:
                raise AIoTConfigError('invalid aiot storage')
            # Mips service
            self._mips_service = self._aiot_client.mips_service
            if not self._mips_service:
                raise AIoTConfigError('invalid mips service')
            # AIoT auth
            self._aiot_auth = self._aiot_client.aiot_auth
            if not self._aiot_oauth:
                raise AIoTConfigError('invalid aiot oauth')
            # AIoT http
            self._aiot_http = self._aiot_client.aiot_http
            if not self._aiot_http:
                raise AIoTConfigError('invalid aiot http')
            self._aiot_i18n = self._aiot_client.aiot_i18n
            if not self._aiot_i18n:
                raise AIoTConfigError('invalid aiot i18n')

            # Check token
            if not await self._aiot_client.refresh_auth_info_async():
                # 检查网络
                if not await self._aiot_network.get_network_status_async():
                    raise AbortFlow(reason='network_connect_error', description_placeholders={})
                self._need_reload = True
                return await self.async_step_auth_config()
            return await self.async_step_config_options()
        except AIoTConfigError as err:
            raise AbortFlow(reason='options_flow_error', description_placeholders={'error': str(err)}) from err
        except AbortFlow as err:
            raise err
        except Exception as err:
            _LOGGER.error('async_step_init error, %s, %s', err, traceback.format_exc())
            raise AbortFlow(reason='re_add', description_placeholders={'error': str(err)}) from err

    async def async_step_auth_config(self, user_input=None):
        if user_input:
            return await self.async_step_auth(user_input)

        return self.async_show_form(
            step_id="auth_config",
            data_schema=vol.Schema({
                vol.Required('lan_server', default=""): str,
                vol.Required('uname', default="admin"): str,
                vol.Required('password', default="admin"): str,
            }),
            description_placeholders={
                'cloud_server': '智空间主机',
            },
        )

    async def async_step_auth(self, user_input=None):
        try:
            if self._cc_task_auth is None:
                self.hass.data[DOMAIN][self._virtual_did]['auth_state'] = self._aiot_auth.state
                self.hass.data[DOMAIN][self._virtual_did]['i18n'] = self._aiot_i18n
                _LOGGER.info('async_step_auth, %s')
                self._cc_task_auth = self.hass.async_create_task(self.__check_auth_async())
                _LOGGER.info('async_step_auth, %s', self._virtual_did)

            if self._cc_task_auth.done():
                if (error := self._cc_task_auth.exception()):
                    _LOGGER.error('task_auth exception, %s', error)
                    self._cc_config_rc = str(error)
                    self._cc_task_auth = None
                    return self.async_show_progress_done(next_step_id='auth_error')
                return self.async_show_progress_done(next_step_id='config_options')
        except Exception as err:  # pylint: disable=broad-exception-caught
            _LOGGER.error('async_step_auth error, %s, %s', err, traceback.format_exc())
            self._cc_config_rc = str(err)
            return self.async_show_progress_done(next_step_id='auth_error')
        # pylint: disable=unexpected-keyword-arg
        return self.async_step_auth_config()

    async def __check_auth_async(self) -> None:
        # Get access_token and user_info from miot_oauth
        if not self._auth_info:
            auth_info: dict = {}
            try:
                auth_info = await self._aiot_oauth.get_access_token_async(self._uname, self._password)
            except Exception as err:
                _LOGGER.error('get_access_token, %s, %s', err, traceback.format_exc())
                raise AIoTConfigError('get_token_error') from err
            # Check uid
            m_http: AIoTHttpClient = AIoTHttpClient(
                lan_server=self._lan_server,
                client_id=AUTH_CLIENT_ID,
                access_token=auth_info['access_token'],
                loop=self._main_loop)
            del m_http
            self._miot_http.update_http_header(access_token=auth_info['access_token'])
            if not await self._aiot_storage.update_user_config_async(
                    uname=self._uname,
                    lan_server=self._lan_server,
                    config={'auth_info': auth_info}):
                raise AbortFlow('storage_error')
            self._auth_info = auth_info

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

    async def async_step_config_options(self, user_input=None):
        if not user_input:
            return self.async_show_form(
                step_id='config_options',
                data_schema=vol.Schema({
                    # Integration configure
                    vol.Required(
                        'integration_language',
                        default=self._integration_language  # type: ignore
                    ): vol.In(INTEGRATION_LANGUAGES),
                    vol.Required(
                        'update_user_info',
                        default=self._update_user_info  # type: ignore
                    ): bool,
                    vol.Required(
                        'network_detect_config',
                        default=self._opt_network_detect_cfg  # type: ignore
                    ): bool,
                    # Device info configure
                    vol.Required(
                        'update_devices',
                        default=self._update_devices  # type: ignore
                    ): bool,
                    vol.Required(
                        'display_devices_changed_notify',
                        default=self._display_devs_notify  # type: ignore
                    ): cv.multi_select(
                        self._miot_i18n.translate('config.device_state')
                    ),  # type: ignore
                    # Entity info configure
                    vol.Required(
                        'action_debug',
                        default=self._action_debug  # type: ignore
                    ): bool,
                    vol.Optional(
                        'cover_dead_zone_width',
                        default=self._cover_dz_width  # type: ignore
                    ): vol.All(vol.Coerce(int), vol.Range(
                        min=MIN_COVER_DEAD_ZONE_WIDTH,
                        max=MAX_COVER_DEAD_ZONE_WIDTH)),
                }),
                errors={},
                description_placeholders={
                    'nick_name': self._nick_name,
                    'uname': self._uname,
                    'lan_server': '智空间主机',
                    'instance_id': f'ha.{self._entry_data["uuid"]}'
                },
                last_step=False,
            )
        # Check network
        if not await self._aiot_network.get_network_status_async():
            raise AbortFlow(reason='network_connect_error', description_placeholders={})
        self._lang_new = user_input.get('integration_language', self._integration_language)
        self._update_user_info = user_input.get('update_user_info', self._update_user_info)
        self._update_devices = user_input.get('update_devices', self._update_devices)
        self._action_debug_new = user_input.get('action_debug', self._action_debug)
        self._display_devs_notify = user_input.get('display_devices_changed_notify', self._display_devs_notify)
        self._opt_network_detect_cfg = user_input.get('network_detect_config', self._opt_network_detect_cfg)
        self._cover_width_new = user_input.get('cover_dead_zone_width', self._cover_dz_width)

        return await self.async_step_update_user_info()

    async def async_step_update_user_info(self, user_input=None):
        if not self._update_user_info:
            return await self.async_step_homes_select()
        if not user_input:
            nick_name_new = (
                    await self._aiot_http.get_user_info_async() or {}).get(
                'miliaoNick', DEFAULT_NICK_NAME)
            return self.async_show_form(
                step_id='update_user_info',
                data_schema=vol.Schema({
                    vol.Required('nick_name', default=nick_name_new): str
                }),
                description_placeholders={
                    'nick_name': self._nick_name
                },
                last_step=False
            )

        self._nick_name_new = user_input.get('nick_name')
        return await self.async_step_homes_select()
