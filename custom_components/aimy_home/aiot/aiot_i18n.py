# -*- coding: utf-8 -*-

import asyncio
import logging
import os
from typing import Optional, Union

# pylint: disable=relative-beyond-top-level
from .common import load_json_file

_LOGGER = logging.getLogger(__name__)


class AIoTI18n:
    _main_loop: asyncio.AbstractEventLoop
    _lang: str
    _data: dict

    def __init__(
            self,
            lang: str,
            loop: Optional[asyncio.AbstractEventLoop]
    ) -> None:
        self._main_loop = loop or asyncio.get_event_loop()
        self._lang = lang
        self._data = {}

    async def init_async(self) -> None:
        if self._data:
            return
        data = None
        self._data = {}
        try:
            data = await self._main_loop.run_in_executor(
                None,
                load_json_file,
                os.path.join(os.path.dirname(os.path.abspath(__file__)), f'i18n/{self._lang}.json')
            )
        except Exception as err:  # pylint: disable=broad-exception-caught
            _LOGGER.error('load file error, %s', err)
            return
        # Check if the file is a valid JSON file
        if not isinstance(data, dict):
            _LOGGER.error('valid file, %s', data)
            return
        self._data = data

    async def deinit_async(self) -> None:
        self._data = {}

    def translate(
            self,
            key: str,
            replace: Optional[dict[str, str]] = None
    ) -> Union[str, dict, None]:
        result = self._data
        for item in key.split('.'):
            if item not in result:
                return None
            result = result[item]
        if isinstance(result, str) and replace:
            for k, v in replace.items():
                result = result.replace('{' + k + '}', str(v))
        return result or None
