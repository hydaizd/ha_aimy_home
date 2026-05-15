# -*- coding: utf-8 -*-

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Optional

import aiohttp

from .aiot_error import AIoTAuthError, AIoTHttpError
# pylint: disable=relative-beyond-top-level
from .common import gen_device_urn, gen_device_did
from .const import (
    UNSUPPORTED_MODELS,
    HTTP_API_TIMEOUT,
    HTTP_API_PORT,
)

_LOGGER = logging.getLogger(__name__)


class AIoTAuthClient:
    """auth agent url, default: product env."""
    _main_loop: asyncio.AbstractEventLoop
    _session: aiohttp.ClientSession
    _auth_host: str
    _client_id: int
    _device_id: str
    _state: str

    def __init__(
            self,
            client_id: str,
            lan_server: str,
            uuid: str,
            loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        self._main_loop = loop or asyncio.get_running_loop()
        if client_id is None or client_id.strip() == '':
            raise AIoTAuthError('invalid client_id')
        if not lan_server:
            raise AIoTAuthError('invalid lan_server')
        if not uuid:
            raise AIoTAuthError('invalid uuid')

        self._client_id = int(client_id)
        self._auth_host = f'{lan_server}:{HTTP_API_PORT}'
        self._device_id = f'ha.{uuid}'
        self._state = hashlib.sha1(f'd={self._device_id}'.encode('utf-8')).hexdigest()
        self._session = aiohttp.ClientSession(loop=self._main_loop)

    @property
    def state(self) -> str:
        return self._state

    async def deinit_async(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def __get_token_async(self, data) -> dict:
        headers = {'content-type': 'application/json'}
        req_url = f'http://{self._auth_host}/api/basic/user/login'
        if data.get('refresh_token', None):
            # 刷新token：使用refreshToken接口，带Authorization头
            req_url = f'http://{self._auth_host}/api/basic/user/refreshToken'
            headers['Authorization'] = data['refresh_token']

        http_res = await self._session.post(
            url=req_url,
            json=data if not data.get('refresh_token', None) else None,
            headers=headers,
            timeout=HTTP_API_TIMEOUT
        )
        if http_res.status == 401:
            raise AIoTAuthError('unauthorized(401)')
        if http_res.status != 200:
            raise AIoTAuthError(f'invalid http status code, {http_res.status}')

        res_str = await http_res.text()
        res_obj = json.loads(res_str)
        if (
                not res_obj
                or res_obj.get('success', None) != True
                or 'data' not in res_obj
        ):
            raise AIoTAuthError(f'invalid http response, {res_str}')

        token = res_obj.get("data")
        return {
            'access_token': token,
            'refresh_token': token,
            'expires_ts': int(time.time() + 8 * 3600 * 0.7)
        }

    async def get_access_token_async(self, username: str, password: str) -> dict:
        if not isinstance(username, str) or not isinstance(password, str):
            raise AIoTAuthError('invalid username or password')

        md5 = hashlib.md5()
        md5.update(password.encode())

        return await self.__get_token_async(data={
            'client_id': self._client_id,
            'username': username,
            'passwd': md5.hexdigest(),
            'device_id': self._device_id
        })

    async def refresh_access_token_async(self, refresh_token: str) -> dict:
        """刷新token"""
        if not isinstance(refresh_token, str):
            raise AIoTAuthError('invalid refresh_token')

        return await self.__get_token_async(data={
            'client_id': self._client_id,
            'refresh_token': refresh_token,
        })


class AIoTHttpClient:
    """AIoT http client."""
    # pylint: disable=inconsistent-quotes
    GET_PROP_AGGREGATE_INTERVAL: float = 0.2
    GET_PROP_MAX_REQ_COUNT = 150
    _main_loop: asyncio.AbstractEventLoop
    _session: aiohttp.ClientSession
    _host: str
    _base_url: str
    _client_id: str
    _access_token: str

    _get_prop_timer: Optional[asyncio.TimerHandle]
    _get_prop_list: dict[str, dict]

    def __init__(
            self,
            lan_server: str,
            client_id: str,
            access_token: str,
            loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        self._main_loop = loop or asyncio.get_running_loop()
        self._host = f'{lan_server}:10088'
        self._base_url = ''
        self._client_id = ''
        self._access_token = ''

        self._get_prop_timer = None
        self._get_prop_list = {}

        if (
                not isinstance(lan_server, str)
                or not isinstance(client_id, str)
                or not isinstance(access_token, str)
        ):
            raise AIoTHttpError('invalid params')

        self.update_http_header(
            lan_server=lan_server,
            client_id=client_id,
            access_token=access_token
        )

        self._session = aiohttp.ClientSession(loop=self._main_loop)

    async def deinit_async(self) -> None:
        if self._get_prop_timer:
            self._get_prop_timer.cancel()
            self._get_prop_timer = None
        for item in self._get_prop_list.values():
            fut: Optional[asyncio.Future] = item.get('fut', None)
            if fut:
                fut.cancel()
        self._get_prop_list.clear()
        if self._session and not self._session.closed:
            await self._session.close()

    def update_http_header(
            self,
            lan_server: Optional[str] = None,
            client_id: Optional[str] = None,
            access_token: Optional[str] = None
    ) -> None:
        if isinstance(lan_server, str):
            self._base_url = f'http://{self._host}'
        if isinstance(client_id, str):
            self._client_id = client_id
        if isinstance(access_token, str):
            self._access_token = access_token

    @property
    def __api_request_headers(self) -> dict:
        return {
            'Host': self._host,
            'X-Client-BizId': 'haapi',
            'Content-Type': 'application/json',
            'Authorization': self._access_token,
            'X-Client-AppId': self._client_id,
        }

    # pylint: disable=unused-private-member
    async def __aimy_home_api_get_async(
            self,
            url_path: str,
            params: dict,
            timeout: int = HTTP_API_TIMEOUT
    ) -> dict:
        http_res = await self._session.get(
            url=f'{self._base_url}{url_path}',
            params=params,
            headers=self.__api_request_headers,
            timeout=timeout
        )
        if http_res.status == 401:
            raise AIoTHttpError('aimy_home api get failed, unauthorized(401)')
        if http_res.status != 200:
            raise AIoTHttpError(f'aimy_home api get failed, {http_res.status}, 'f'{url_path}, {params}')
        res_str = await http_res.text()
        res_obj: dict = json.loads(res_str)
        if not res_obj.get('success', None):
            raise AIoTHttpError(f'invalid response, {res_obj.get("success", None)}, 'f'{res_obj.get("msg", "")}')
        _LOGGER.debug('aimy_home api get, %s%s, %s -> %s', self._base_url, url_path, params, res_obj)
        return res_obj

    async def __aimy_home_api_post_async(
            self,
            url_path: str,
            data: dict,
            timeout: int = HTTP_API_TIMEOUT
    ) -> dict:
        http_res = await self._session.post(
            url=f'{self._base_url}{url_path}',
            json=data,
            headers=self.__api_request_headers,
            timeout=timeout
        )
        if http_res.status == 401:
            raise AIoTHttpError('aimy_home api get failed, unauthorized(401)')
        if http_res.status != 200:
            raise AIoTHttpError(f'aimy_home api post failed, {http_res.status}, 'f'{url_path}, {data}')
        res_str = await http_res.text()
        res_obj: dict = json.loads(res_str)
        if not res_obj.get('success', None):
            raise AIoTHttpError(f'invalid response, {res_obj.get("success", None)}, 'f'{res_obj.get("msg", "")}')
        _LOGGER.debug('aimy_home api post, %s%s, %s -> %s', self._base_url, url_path, data, res_obj)
        return res_obj

    async def get_user_info_async(self) -> dict:
        http_res = await self._session.get(
            url=f'{self._base_url}/api/basic/user/info',
            headers=self.__api_request_headers,
            timeout=HTTP_API_TIMEOUT
        )

        res_str = await http_res.text()
        res_obj = json.loads(res_str)
        if (
                not res_obj
                or not res_obj.get('success', None)
                or 'data' not in res_obj
                or 'name' not in res_obj['data']
        ):
            raise AIoTAuthError(f'invalid http response, {http_res.text}')

        return res_obj['data']

    async def __get_device_list_page_async(self, dids: list[str]) -> dict[str, dict]:
        mid_bind_ids = []
        for did in dids:
            did_strs: list[str] = did.split('.')
            if did_strs[0] not in mid_bind_ids:
                mid_bind_ids.append(did_strs[0])

        req_data: dict = {
            "isAll": "true",  # 不分页
            "orderBy": "productTypeClass",
            "isShow": "true",
            'midBindIds': mid_bind_ids,
        }
        device_infos: dict = {}
        res_obj = await self.__aimy_home_api_get_async(
            url_path='/api/basic/device/endpoint_page',
            params=req_data
        )
        if 'data' not in res_obj:
            raise AIoTHttpError('invalid response result')
        res_obj = res_obj['data']

        for device in res_obj.get('items', []) or []:
            product_key = device.get('productKey', None)
            sku_id = device.get('skuId', None)
            endpoint = device.get('endpoint', None)
            mid_bind_id = device.get('midBindId', None)
            group_id = device.get('groupId', None)

            did = gen_device_did(mid_bind_id, endpoint)
            name = device.get('name', None)
            urn = gen_device_urn(product_key, sku_id, mid_bind_id, endpoint)
            model = device.get('skuId', None)
            if did is None or name is None:
                _LOGGER.info('invalid device, lan, %s', device)
                continue
            if urn is None or model is None:
                _LOGGER.info('missing the urn|model field, cloud, %s', device)
                continue
            if model in UNSUPPORTED_MODELS:
                _LOGGER.info('ignore unsupported model %s, lan, %s', model, did)
                continue
            device_infos[did] = {
                'did': did,
                'urn': urn,
                'name': name,
                'model': model,
                'online': device.get('onlineState', False),
                'group_id': group_id,
                'ep_name': device.get('endpointName', ''),
                'version': device.get('version', '')
            }
        return device_infos

    async def get_devices_with_dids_async(self, dids: list[str]) -> Optional[dict[str, dict]]:
        results: list[dict[str, dict]] = await asyncio.gather(*[self.__get_device_list_page_async(dids=dids)])
        devices = {}
        for result in results:
            if result is None:
                return None
            devices.update(result)
        return devices

    async def get_devices_async(self) -> dict[str, dict]:
        """获取设备列表."""
        results = await self.get_devices_with_dids_async(dids=[])
        if results is None:
            raise AIoTHttpError('get devices failed')
        return {
            'devices': results
        }

    async def get_props_async(self, params: list) -> list:
        """
        params = [
            {"did": "xxxx", "snnd": 2, "pnnd": 1},
            {"did": "xxxxxx", "snnd": 2, "pnnd": 2}
        ]
        """
        if len(params) == 1:
            # 单个请求
            param = params[0]
            did_strs: list[str] = param['did'].split('.')
            res_obj = await self.__aimy_home_api_get_async(
                url_path='/api/basic/device/props',
                params={
                    'midBindId': did_strs[0],
                    'endpoint': did_strs[1],
                    'aamPropNames': param['pnnd']
                },
            )
            if 'data' not in res_obj:
                raise AIoTHttpError('invalid response result')
            return res_obj['data']
        else:
            # 　批量请求
            mid_bind_ids = []
            for param in params:
                did_strs: list[str] = param['did'].split('.')
                if did_strs[0] not in mid_bind_ids:
                    mid_bind_ids.append(did_strs[0])

            props_list = []
            for mid_bind_id in mid_bind_ids:
                res_obj = await self.__aimy_home_api_get_async(
                    url_path='/api/basic/device/props',
                    params={
                        'midBindId': mid_bind_id,
                    },
                )
                if 'data' in res_obj:
                    for result in res_obj['data']:
                        for param in params:
                            if (
                                    param['did'] == f'{result['macAddr']}.{result['endPointPort']}'
                                    and param['pnnd'] == result['aamPropName']
                            ):
                                props_list.append(result)
            return props_list

    async def __get_prop_async(
            self,
            did: str,
            snnd: str,
            pnnd: str
    ) -> Any:
        results = await self.get_props_async(
            params=[{'did': did, 'snnd': snnd, 'pnnd': pnnd}]
        )
        if not results:
            return None
        result = results[0]
        if 'aamPropValue' not in result:
            return None
        return result['aamPropValue']

    async def __get_prop_handler(self) -> bool:
        props_req: set[str] = set()
        props_buffer: list[dict] = []

        for key, item in self._get_prop_list.items():
            if item.get('tag', False):
                continue
            # 最大请求数
            if len(props_req) >= self.GET_PROP_MAX_REQ_COUNT:
                break
            item['tag'] = True
            props_buffer.append(item['param'])
            props_req.add(key)

        if not props_buffer:
            _LOGGER.error('get prop error, empty request list')
            return False
        results = await self.get_props_async(props_buffer)

        for result in results:
            if not all(key in result for key in ['macAddr', 'endPointPort', 'aamPropName', 'aamPropValue']):
                continue
            key = f'{result["macAddr"]}.{result["endPointPort"]}.{result["aamPropName"]}'
            prop_obj = self._get_prop_list.pop(key, None)
            if prop_obj is None:
                _LOGGER.info('get prop error, key not exists, %s', result)
                continue
            prop_obj['fut'].set_result(result['aamPropValue'])
            props_req.remove(key)

        for key in props_req:
            prop_obj = self._get_prop_list.pop(key, None)
            if prop_obj is None:
                continue
            prop_obj['fut'].set_result(None)
        if props_req:
            _LOGGER.info('get prop from cloud failed, %s', props_req)

        if self._get_prop_list:
            self._get_prop_timer = self._main_loop.call_later(
                self.GET_PROP_AGGREGATE_INTERVAL,
                lambda: self._main_loop.create_task(self.__get_prop_handler())
            )
        else:
            self._get_prop_timer = None
        return True

    async def get_prop_async(
            self,
            did: str,
            snnd: str,
            pnnd: str,
            immediately: bool = False
    ) -> Any:
        if immediately:
            return await self.__get_prop_async(did=did, snnd=snnd, pnnd=pnnd)
        key: str = f'{did}.{snnd}.{pnnd}'
        prop_obj = self._get_prop_list.get(key, None)
        if prop_obj:
            return await prop_obj['fut']
        fut = self._main_loop.create_future()
        self._get_prop_list[key] = {
            'param': {'did': did, 'snnd': snnd, 'pnnd': pnnd},
            'fut': fut
        }
        if self._get_prop_timer is None:
            self._get_prop_timer = self._main_loop.call_later(
                self.GET_PROP_AGGREGATE_INTERVAL,
                lambda: self._main_loop.create_task(self.__get_prop_handler())
            )

        return await fut

    async def set_prop_async(self, params: dict) -> dict:
        """控制设备."""
        res_obj = await self.__aimy_home_api_post_async(
            url_path='/api/basic/device/ctrl',
            data=params,
            timeout=15
        )
        if 'data' not in res_obj:
            raise AIoTHttpError('invalid response result')

        return res_obj['data']

    async def action_async(
            self,
            did: str,
            snnd: str,
            annd: str,
            in_list: list[dict]
    ) -> dict:
        did_strs: list[str] = did.split('.')

        # 非标准动作参数
        res_obj = await self.__aimy_home_api_post_async(
            url_path='/api/basic/device/ctrl',
            data={
                "cmd": snnd,
                "midBindId": did_strs[0],
                "endpointId": did_strs[1],
            },
            timeout=15
        )
        if 'data' not in res_obj:
            raise AIoTHttpError('invalid response result')

        return res_obj['data']

    # 自定义
    async def get_device_instance_async(self, product_key: str, sku_id: str) -> dict:
        """获取产品功能."""
        req_params = {
            "productKey": product_key,
            "skuId": sku_id,
        }
        res_obj = await self.__aimy_home_api_get_async(
            url_path='/api/basic/iot-spec-v1/instance',
            params=req_params
        )
        if 'data' not in res_obj:
            raise AIoTHttpError('invalid response result')
        return res_obj['data']
