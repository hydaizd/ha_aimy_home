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
    """生成绝对路径"""
    return path.join(AIOT_ROOT_PATH, relative_path)

def load_json_file(json_file: str) -> dict:
    """加载json文件"""
    with open(json_file, 'r', encoding='utf-8') as f:
        return json.load(f)

def slugify_name(name: str, separator: str = '_') -> str:
    """Slugify 一个名字"""
    return slugify(name, separator=separator)


def slugify_did(lan_server: str, did: str) -> str:
    """Slugify 一个设备id"""
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


def gen_device_did(mid_bind_id: str, endpoint: str) -> str | None:
    """生成设备id唯一标识."""
    if mid_bind_id is None:
        return None
    if endpoint is None:
        endpoint = ''
    return f'{mid_bind_id}.{endpoint}'


def gen_prop_group_key(urn: str, snnd: str, pnnd: str) -> str | None:
    """ 生成属性组key，同组属性需要一起发送 """
    urn_strs: list[str] = urn.split(':')
    product_key = urn_strs[1]
    sku_id = urn_strs[2]

    if snnd == 'set_delay_switch' and pnnd in ['OnTime', 'OffWaitTime']:
        return f'{product_key}_{sku_id}_{snnd}'
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
    """AIoT 通用 HTTP API."""
    @staticmethod
    def get(
            url: str,
            params: Optional[dict] = None,
            headers: Optional[dict] = None
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
            url: str,
            params: Optional[dict] = None,
            headers: Optional[dict] = None
    ) -> Optional[dict]:
        response = AIoTHttp.get(url, params, headers)
        return json.loads(response) if response else None

    @staticmethod
    def post(
            url: str,
            data: Optional[dict] = None,
            headers: Optional[dict] = None
    ) -> Optional[str]:
        pass

    @staticmethod
    def post_json(
            url: str,
            data: Optional[dict] = None,
            headers: Optional[dict] = None
    ) -> Optional[dict]:
        response = AIoTHttp.post(url, data, headers)
        return json.loads(response) if response else None

    @staticmethod
    async def get_async(
            url: str,
            params: Optional[dict] = None,
            headers: Optional[dict] = None,
            loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> Optional[str]:
        # TODO: Use aiohttp
        ev_loop = loop or asyncio.get_running_loop()
        return await ev_loop.run_in_executor(None, AIoTHttp.get, url, params, headers)

    @staticmethod
    async def get_json_async(
            url: str,
            params: Optional[dict] = None,
            headers: Optional[dict] = None,
            loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> Optional[dict]:
        ev_loop = loop or asyncio.get_running_loop()
        return await ev_loop.run_in_executor(None, AIoTHttp.get_json, url, params, headers)

    @staticmethod
    async def post_async(
            url: str,
            data: Optional[dict] = None,
            headers: Optional[dict] = None,
            loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> Optional[str]:
        ev_loop = loop or asyncio.get_running_loop()
        return await ev_loop.run_in_executor(None, AIoTHttp.post, url, data, headers)
