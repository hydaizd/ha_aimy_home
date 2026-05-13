# -*- coding: utf-8 -*-

import asyncio
import hashlib
import json
import random
from os import path
from typing import Any, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import yaml
from paho.mqtt.matcher import MQTTMatcher
from slugify import slugify

AIOT_ROOT_PATH: str = path.dirname(path.abspath(__file__))


def gen_absolute_path(relative_path: str) -> str:
    """Generate an absolute path."""
    return path.join(AIOT_ROOT_PATH, relative_path)


def calc_group_id(uid: str, home_id: str) -> str:
    """Calculate the group ID based on a user ID and a home ID."""
    return hashlib.sha1(
        f'{uid}central_service{home_id}'.encode('utf-8')).hexdigest()[:16]


def load_json_file(json_file: str) -> dict:
    """Load a JSON file."""
    with open(json_file, 'r', encoding='utf-8') as f:
        return json.load(f)


def load_yaml_file(yaml_file: str) -> dict:
    """Load a YAML file."""
    with open(yaml_file, 'r', encoding='utf-8') as f:
        return yaml.load(f, Loader=yaml.FullLoader)


def randomize_int(value: int, ratio: float) -> int:
    """Randomize an integer value."""
    return int(value * (1 - ratio + random.random() * 2 * ratio))


def randomize_float(value: float, ratio: float) -> float:
    """Randomize a float value."""
    return value * (1 - ratio + random.random() * 2 * ratio)


def slugify_name(name: str, separator: str = '_') -> str:
    """Slugify a name."""
    return slugify(name, separator=separator)


def slugify_did(lan_server: str, did: str) -> str:
    """Slugify a device id."""
    return slugify(f'{lan_server}_{did}', separator='_')


# 自定义方法
def gen_device_urn(product_key: str, sku_id: str, mid_bind_id: str, endpoint: str) -> str | None:
    """生成设备urn唯一标识."""
    if mid_bind_id is None:
        return None

    if product_key is None:
        product_key = ''

    if sku_id is None:
        sku_id = ''

    if endpoint is None:
        endpoint = ''

    return f'urn:{product_key}:{sku_id}:{mid_bind_id}:{endpoint}'


def gen_device_did(mid_bind_id: str, endpoint: str) -> str:
    """生成设备id唯一标识."""
    return f'{mid_bind_id}.{endpoint}'


def get_device_product_key(urn: str) -> str:
    """获取设备 product_key"""
    urn_strs: list[str] = urn.split(':')
    return urn_strs[1]


def get_device_sku_id(urn: str) -> str:
    """获取设备 sku_id"""
    urn_strs: list[str] = urn.split(':')
    return urn_strs[2]


def get_device_mid_bind_id(urn: str) -> str:
    """获取设备 mid_bind_id"""
    urn_strs: list[str] = urn.split(':')
    return urn_strs[3]


def get_device_endpoint(urn: str) -> str:
    """获取设备 endpoint"""
    urn_strs: list[str] = urn.split(':')
    return urn_strs[4]


def get_device_did(urn: str) -> str:
    """获取设备did唯一标识"""
    urn_strs: list[str] = urn.split(':')
    return f'{urn_strs[3]}.{urn_strs[4]}'


def get_service_name(type_: str) -> str:
    """Get service name from type."""
    service_strs: list[str] = type_.split(':')
    return service_strs[4]


def get_prop_name(type_: str) -> str:
    """Get property name from type."""
    prop_strs: list[str] = type_.split(':')
    return prop_strs[3]


def get_prop_endpoint(type_: str) -> str:
    """Get property endpoint from type."""
    prop_strs: list[str] = type_.split(':')
    return prop_strs[4]


def get_prop_group_key(urn: str, service_name: str, prop_name: str) -> str | None:
    """ 获取属性组key，同组属性需要一起发送 """
    product_key = get_device_product_key(urn)
    sku_id = get_device_sku_id(urn)

    if service_name == 'set_delay_switch' and prop_name in ['OnTime', 'OffWaitTime']:
        return f'{product_key}_{sku_id}_{service_name}'
    return None


class AIoTMatcher(MQTTMatcher):
    """AIoT Pub/Sub topic matcher."""

    def iter_all_nodes(self) -> Any:
        """Return an iterator on all nodes with their paths and contents."""

        def rec(node, path_):
            # pylint: disable=protected-access
            if node._content:
                yield ('/'.join(path_), node._content)
            for part, child in node._children.items():
                yield from rec(child, path_ + [part])

        return rec(self._root, [])

    def get(self, topic: str) -> Optional[Any]:
        try:
            return self[topic]
        except KeyError:
            return None


class AIoTHttp:
    """AIoT Common HTTP API."""

    @staticmethod
    def get(
            url: str, params: Optional[dict] = None, headers: Optional[dict] = None
    ) -> Optional[str]:
        full_url = url
        if params:
            encoded_params = urlencode(params)
            full_url = f'{url}?{encoded_params}'
        request = Request(full_url, method='GET', headers=headers or {})
        content: Optional[bytes] = None
        with urlopen(request) as response:
            content = response.read()
        return str(content, 'utf-8') if content else None

    @staticmethod
    def get_json(
            url: str, params: Optional[dict] = None, headers: Optional[dict] = None
    ) -> Optional[dict]:
        response = AIoTHttp.get(url, params, headers)
        return json.loads(response) if response else None

    @staticmethod
    def post(
            url: str, data: Optional[dict] = None, headers: Optional[dict] = None
    ) -> Optional[str]:
        pass

    @staticmethod
    def post_json(
            url: str, data: Optional[dict] = None, headers: Optional[dict] = None
    ) -> Optional[dict]:
        response = AIoTHttp.post(url, data, headers)
        return json.loads(response) if response else None

    @staticmethod
    async def get_async(
            url: str, params: Optional[dict] = None, headers: Optional[dict] = None,
            loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> Optional[str]:
        # TODO: Use aiohttp
        ev_loop = loop or asyncio.get_running_loop()
        return await ev_loop.run_in_executor(
            None, AIoTHttp.get, url, params, headers)

    @staticmethod
    async def get_json_async(
            url: str, params: Optional[dict] = None, headers: Optional[dict] = None,
            loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> Optional[dict]:
        ev_loop = loop or asyncio.get_running_loop()
        return await ev_loop.run_in_executor(
            None, AIoTHttp.get_json, url, params, headers)

    @staticmethod
    async def post_async(
            url: str, data: Optional[dict] = None, headers: Optional[dict] = None,
            loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> Optional[str]:
        ev_loop = loop or asyncio.get_running_loop()
        return await ev_loop.run_in_executor(
            None, AIoTHttp.post, url, data, headers)
