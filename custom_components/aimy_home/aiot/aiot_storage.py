# -*- coding: utf-8 -*-
import asyncio
import hashlib
import json
import logging
import os
import shutil
import traceback
from enum import Enum, auto
from pathlib import Path
from typing import Optional, Any, Union

_LOGGER = logging.getLogger(__name__)


class AIoTStorageType(Enum):
    LOAD = auto()
    LOAD_FILE = auto()
    SAVE = auto()
    SAVE_FILE = auto()
    DEL = auto()
    DEL_FILE = auto()
    CLEAR = auto()


class AIoTStorage:
    """文件管理."""
    _main_loop: asyncio.AbstractEventLoop
    _file_future: dict[str, tuple[AIoTStorageType, asyncio.Future]]

    _root_path: str

    def __init__(
            self,
            root_path: str,
            loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        """初始化文件管理."""
        self._main_loop = loop or asyncio.get_running_loop()
        self._file_future = {}

        self._root_path = os.path.abspath(root_path)
        os.makedirs(self._root_path, exist_ok=True)

        _LOGGER.debug('root path, %s', self._root_path)

    def __get_full_path(
            self,
            domain: str,
            name: str,
            suffix: str
    ) -> str:
        """获取文件完整路径."""
        return os.path.join(self._root_path, domain, f'{name}.{suffix}')

    def __add_file_future(
            self,
            key: str,
            op_type: AIoTStorageType,
            fut: asyncio.Future
    ) -> None:
        """添加文件异步操作."""

        def fut_done_callback(fut: asyncio.Future):
            del fut
            self._file_future.pop(key, None)

        fut.add_done_callback(fut_done_callback)
        self._file_future[key] = op_type, fut

    def __load(
            self,
            full_path: str,
            type_: type = bytes,
            with_hash_check: bool = True
    ) -> Union[bytes, str, dict, list, None]:
        """加载文件."""
        if not os.path.exists(full_path):
            _LOGGER.debug('load error, file does not exist, %s', full_path)
            return None
        if not os.access(full_path, os.R_OK):
            _LOGGER.error('load error, file not readable, %s', full_path)
            return None
        try:
            with open(full_path, 'rb') as r_file:
                r_data: bytes = r_file.read()
                if r_data is None:
                    _LOGGER.error('load error, empty file, %s', full_path)
                    return None
                data_bytes: bytes
                # Hash check
                if with_hash_check:
                    if len(r_data) <= 32:
                        return None
                    data_bytes = r_data[:-32]
                    hash_value = r_data[-32:]
                    if hashlib.sha256(data_bytes).digest() != hash_value:
                        _LOGGER.error('load error, hash check failed, %s', full_path)
                        return None
                else:
                    data_bytes = r_data
                if type_ == bytes:
                    return data_bytes
                if type_ == str:
                    return str(data_bytes, 'utf-8')
                if type_ in [dict, list]:
                    return json.loads(data_bytes)
                _LOGGER.error('load error, unsupported data type, %s', type_.__name__)
                return None
        except (OSError, TypeError) as e:
            _LOGGER.error('load error, %s, %s', e, traceback.format_exc())
            return None

    def load(
            self,
            domain: str,
            name: str,
            type_: type = bytes
    ) -> Union[bytes, str, dict, list, None]:
        """加载文件."""
        full_path = self.__get_full_path(domain=domain, name=name, suffix=type_.__name__)
        return self.__load(full_path=full_path, type_=type_)

    async def load_async(
            self,
            domain: str,
            name: str,
            type_: type = bytes
    ) -> Union[bytes, str, dict, list, None]:
        """异步加载文件."""
        full_path = self.__get_full_path(domain=domain, name=name, suffix=type_.__name__)
        if full_path in self._file_future:
            # Waiting for the last task to be completed
            op_type, fut = self._file_future[full_path]
            if op_type == AIoTStorageType.LOAD:
                if not fut.done():
                    return await fut
            else:
                await fut
        fut = self._main_loop.run_in_executor(None, self.__load, full_path, type_)
        if not fut.done():
            self.__add_file_future(full_path, AIoTStorageType.LOAD, fut)
        return await fut

    def __save(
            self,
            full_path: str,
            data: Union[bytes, str, dict, list, None],
            cover: bool = True,
            with_hash: bool = True
    ) -> bool:
        """保存文件."""
        if data is None:
            _LOGGER.error('save error, save data is None')
            return False
        if os.path.exists(full_path):
            if not cover:
                _LOGGER.error('save error, file exists, cover is False')
                return False
            if not os.access(full_path, os.W_OK):
                _LOGGER.error('save error, file not writeable, %s', full_path)
                return False
        else:
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
        try:
            w_bytes: bytes
            if isinstance(data, bytes):
                w_bytes = data
            elif isinstance(data, str):
                w_bytes = data.encode('utf-8')
            elif isinstance(data, (dict, list)):
                w_bytes = json.dumps(data).encode('utf-8')
            else:
                _LOGGER.error('save error, unsupported data type, %s', type(data).__name__)
                return False
            with open(full_path, 'wb') as w_file:
                w_file.write(w_bytes)
                if with_hash:
                    w_file.write(hashlib.sha256(w_bytes).digest())
            return True
        except (OSError, TypeError) as e:
            _LOGGER.error('save error, %s, %s', e, traceback.format_exc())
            return False

    def save(
            self,
            domain: str,
            name: str,
            data: Union[bytes, str, dict, list, None]
    ) -> bool:
        """保存文件."""
        full_path = self.__get_full_path(domain=domain, name=name, suffix=type(data).__name__)
        return self.__save(full_path=full_path, data=data)

    async def save_async(
            self,
            domain: str,
            name: str,
            data: Union[bytes, str, dict, list, None]
    ) -> bool:
        """异步保存文件."""
        full_path = self.__get_full_path(domain=domain, name=name, suffix=type(data).__name__)
        if full_path in self._file_future:
            # Waiting for the last task to be completed
            fut = self._file_future[full_path][1]
            await fut
        fut = self._main_loop.run_in_executor(None, self.__save, full_path, data)
        if not fut.done():
            self.__add_file_future(full_path, AIoTStorageType.SAVE, fut)
        return await fut

    def __remove(self, full_path: str) -> bool:
        """删除文件."""
        item = Path(full_path)
        if item.is_file() or item.is_symlink():
            item.unlink()
        return True

    def remove(
            self,
            domain: str,
            name: str,
            type_: type
    ) -> bool:
        """删除文件."""
        full_path = self.__get_full_path(domain=domain, name=name, suffix=type_.__name__)
        return self.__remove(full_path=full_path)

    async def remove_async(
            self,
            domain: str,
            name: str,
            type_: type
    ) -> bool:
        """异步删除文件."""
        full_path = self.__get_full_path(
            domain=domain, name=name, suffix=type_.__name__)
        if full_path in self._file_future:
            # Waiting for the last task to be completed
            op_type, fut = self._file_future[full_path]
            if op_type == AIoTStorageType.DEL:
                if not fut.done():
                    return await fut
            else:
                await fut
        fut = self._main_loop.run_in_executor(None, self.__remove, full_path)
        if not fut.done():
            self.__add_file_future(full_path, AIoTStorageType.DEL, fut)
        return await fut

    def __remove_domain(self, full_path: str) -> bool:
        """删除域名."""
        path_obj = Path(full_path)
        if path_obj.exists():
            # Recursive deletion
            shutil.rmtree(path_obj)
        return True

    def remove_domain(self, domain: str) -> bool:
        """删除域名."""
        full_path = os.path.join(self._root_path, domain)
        return self.__remove_domain(full_path=full_path)

    async def remove_domain_async(self, domain: str) -> bool:
        """异步删除域名."""
        full_path = os.path.join(self._root_path, domain)
        if full_path in self._file_future:
            # Waiting for the last task to be completed
            op_type, fut = self._file_future[full_path]
            if op_type == AIoTStorageType.DEL:
                if not fut.done():
                    return await fut
            else:
                await fut
        # Waiting domain tasks finish
        for path, value in self._file_future.items():
            if path.startswith(full_path):
                await value[1]
        fut = self._main_loop.run_in_executor(
            None, self.__remove_domain, full_path)
        if not fut.done():
            self.__add_file_future(full_path, AIoTStorageType.DEL, fut)
        return await fut

    def get_names(
            self,
            domain: str,
            type_: type
    ) -> list[str]:
        """获取所有文件名."""
        path: str = os.path.join(self._root_path, domain)
        type_str = f'.{type_.__name__}'
        names: list[str] = []
        for item in Path(path).glob(f'*{type_str}'):
            if not item.is_file() and not item.is_symlink():
                continue
            names.append(item.name.replace(type_str, ''))
        return names

    def file_exists(
            self,
            domain: str,
            name_with_suffix: str
    ) -> bool:
        """检查文件是否存在."""
        return os.path.exists(os.path.join(self._root_path, domain, name_with_suffix))

    def save_file(
            self,
            domain: str,
            name_with_suffix: str,
            data: bytes
    ) -> bool:
        """保存文件."""
        if not isinstance(data, bytes):
            _LOGGER.error('save file error, file must be bytes')
            return False
        full_path = os.path.join(self._root_path, domain, name_with_suffix)
        return self.__save(full_path=full_path, data=data, with_hash=False)

    async def save_file_async(
            self,
            domain: str,
            name_with_suffix: str,
            data: bytes
    ) -> bool:
        """异步保存文件."""
        if not isinstance(data, bytes):
            _LOGGER.error('save file error, file must be bytes')
            return False
        full_path = os.path.join(self._root_path, domain, name_with_suffix)
        if full_path in self._file_future:
            # 等待最后一个任务完成
            fut = self._file_future[full_path][1]
            await fut
        fut = self._main_loop.run_in_executor(
            None, self.__save, full_path, data, True, False)
        if not fut.done():
            self.__add_file_future(full_path, AIoTStorageType.SAVE_FILE, fut)
        return await fut

    def load_file(
            self,
            domain: str,
            name_with_suffix: str
    ) -> Optional[bytes]:
        """加载文件."""
        full_path = os.path.join(self._root_path, domain, name_with_suffix)
        return self.__load(full_path=full_path, type_=bytes, with_hash_check=False)  # type: ignore

    async def load_file_async(
            self,
            domain: str,
            name_with_suffix: str
    ) -> Optional[bytes]:
        """异步加载文件."""
        full_path = os.path.join(self._root_path, domain, name_with_suffix)
        if full_path in self._file_future:
            # 等待最后一个任务完成
            op_type, fut = self._file_future[full_path]
            if op_type == AIoTStorageType.LOAD_FILE:
                if not fut.done():
                    return await fut
            else:
                await fut
        fut = self._main_loop.run_in_executor(
            None, self.__load, full_path, bytes, False)
        if not fut.done():
            self.__add_file_future(full_path, AIoTStorageType.LOAD_FILE, fut)
        return await fut  # type: ignore

    def remove_file(
            self,
            domain: str,
            name_with_suffix: str
    ) -> bool:
        """删除文件."""
        full_path = os.path.join(self._root_path, domain, name_with_suffix)
        return self.__remove(full_path=full_path)

    async def remove_file_async(
            self,
            domain: str,
            name_with_suffix: str
    ) -> bool:
        """异步删除文件."""
        full_path = os.path.join(self._root_path, domain, name_with_suffix)
        if full_path in self._file_future:
            # Waiting for the last task to be completed
            op_type, fut = self._file_future[full_path]
            if op_type == AIoTStorageType.DEL_FILE:
                if not fut.done():
                    return await fut
            else:
                await fut
        fut = self._main_loop.run_in_executor(None, self.__remove, full_path)
        if not fut.done():
            self.__add_file_future(full_path, AIoTStorageType.DEL_FILE, fut)
        return await fut

    def clear(self) -> bool:
        """清空存储."""
        root_path = Path(self._root_path)
        for item in root_path.iterdir():
            if item.is_file() or item.is_symlink():
                item.unlink()
            elif item.is_dir():
                shutil.rmtree(item)
        return True

    async def clear_async(self) -> bool:
        """异步清空存储."""
        if self._root_path in self._file_future:
            op_type, fut = self._file_future[self._root_path]
            if op_type == AIoTStorageType.CLEAR and not fut.done():
                return await fut
        # Waiting all future resolve
        for value in self._file_future.values():
            await value[1]

        fut = self._main_loop.run_in_executor(None, self.clear)
        if not fut.done():
            self.__add_file_future(
                self._root_path, AIoTStorageType.CLEAR, fut)
        return await fut

    def update_user_config(
            self,
            uname: str,
            lan_server: str,
            config: Optional[dict[str, Any]],
            replace: bool = False
    ) -> bool:
        """更新用户配置."""
        if config is not None and len(config) == 0:
            # Do nothing
            return True

        config_domain = 'aiot_config'
        config_name = f'{uname}_{lan_server}'
        if config is None:
            # Remove config file
            return self.remove(domain=config_domain, name=config_name, type_=dict)
        if replace:
            # Replace config file
            return self.save(domain=config_domain, name=config_name, data=config)
        local_config = (self.load(domain=config_domain, name=config_name, type_=dict)) or {}
        local_config.update(config)  # type: ignore
        return self.save(domain=config_domain, name=config_name, data=local_config)

    async def update_user_config_async(
            self,
            uname: str,
            lan_server: str,
            config: Optional[dict[str, Any]],
            replace: bool = False
    ) -> bool:
        """更新用户配置."""
        if config is not None and len(config) == 0:
            # Do nothing
            return True

        config_domain = 'aiot_config'
        config_name = f'{uname}_{lan_server}'
        if config is None:
            # Remove config file
            return await self.remove_async(domain=config_domain, name=config_name, type_=dict)
        if replace:
            # Replace config file
            return await self.save_async(domain=config_domain, name=config_name, data=config)
        local_config = (await self.load_async(domain=config_domain, name=config_name, type_=dict)) or {}
        local_config.update(config)  # type: ignore
        return await self.save_async(domain=config_domain, name=config_name, data=local_config)

    def load_user_config(
            self,
            uname: str,
            lan_server: str,
            keys: Optional[list[str]] = None
    ) -> dict[str, Any]:
        """加载用户配置."""
        if isinstance(keys, list) and len(keys) == 0:
            # Do nothing
            return {}
        config_domain = 'aiot_config'
        config_name = f'{uname}_{lan_server}'
        local_config = (self.load(domain=config_domain, name=config_name, type_=dict))
        if not isinstance(local_config, dict):
            return {}
        if keys is None:
            return local_config
        return {
            key: local_config[key] for key in keys
            if key in local_config}

    async def load_user_config_async(
            self,
            uname: str,
            lan_server: str,
            keys: Optional[list[str]] = None
    ) -> dict:
        """加载用户配置."""
        if isinstance(keys, list) and len(keys) == 0:
            # Do nothing
            return {}
        config_domain = 'aiot_config'
        config_name = f'{uname}_{lan_server}'
        local_config = (await self.load_async(domain=config_domain, name=config_name, type_=dict))
        if not isinstance(local_config, dict):
            return {}
        if keys is None:
            return local_config
        return {
            key: local_config[key] for key in keys
            if key in local_config}

    def gen_storage_path(
            self,
            domain: Optional[str] = None,
            name_with_suffix: Optional[str] = None
    ) -> str:
        """生成文件路径."""
        result = self._root_path
        if domain:
            result = os.path.join(result, domain)
            if name_with_suffix:
                result = os.path.join(result, name_with_suffix)
        return result
