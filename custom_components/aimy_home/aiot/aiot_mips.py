# -*- coding: utf-8 -*-

import asyncio
import json
import logging
import ssl
import struct
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Callable, Optional, final, Coroutine

from paho.mqtt.client import (
    MQTT_ERR_SUCCESS,
    MQTT_ERR_NO_CONN,
    MQTT_ERR_UNKNOWN,
    Client,
    MQTTv5,
    MQTTMessage
)

from .aiot_error import AIoTMipsError
# pylint: disable=relative-beyond-top-level
from .common import AIoTMatcher
from .const import (
    MYHOME_MQTT_KEEPALIVE,
)

_LOGGER = logging.getLogger(__name__)


class _MipsMsgTypeOptions(Enum):
    """AIoT Pub/Sub message type."""
    ID = 0
    RET_TOPIC = auto()
    PAYLOAD = auto()
    FROM = auto()
    MAX = auto()


class _MipsMessage:
    """AIoT Pub/Sub message."""
    mid: int = 0
    msg_from: Optional[str] = None
    ret_topic: Optional[str] = None
    payload: Optional[str] = None

    @staticmethod
    def unpack(data: bytes) -> '_MipsMessage':
        mips_msg = _MipsMessage()
        data_len = len(data)
        data_start = 0
        data_end = 0
        while data_start < data_len:
            data_end = data_start + 5
            unpack_len, unpack_type = struct.unpack('<IB', data[data_start:data_end])
            unpack_data = data[data_end:data_end + unpack_len]
            #  string end with \x00
            match unpack_type:
                case _MipsMsgTypeOptions.ID.value:
                    mips_msg.mid = int.from_bytes(unpack_data, byteorder='little')
                case _MipsMsgTypeOptions.RET_TOPIC.value:
                    mips_msg.ret_topic = str(unpack_data.strip(b'\x00'), 'utf-8')
                case _MipsMsgTypeOptions.PAYLOAD.value:
                    mips_msg.payload = str(unpack_data.strip(b'\x00'), 'utf-8')
                case _MipsMsgTypeOptions.FROM.value:
                    mips_msg.msg_from = str(unpack_data.strip(b'\x00'), 'utf-8')
                case _:
                    pass
            data_start = data_end + unpack_len
        return mips_msg

    @staticmethod
    def pack(
            mid: int,
            payload: str,
            msg_from: Optional[str] = None,
            ret_topic: Optional[str] = None
    ) -> bytes:
        if mid is None or payload is None:
            raise AIoTMipsError('invalid mid or payload')
        pack_msg: bytes = b''
        # mid
        pack_msg += struct.pack('<IBI', 4, _MipsMsgTypeOptions.ID.value, mid)
        # msg_from
        if msg_from:
            pack_len = len(msg_from)
            pack_msg += struct.pack(
                f'<IB{pack_len}sx', pack_len + 1,
                _MipsMsgTypeOptions.FROM.value, msg_from.encode('utf-8')
            )
        # ret_topic
        if ret_topic:
            pack_len = len(ret_topic)
            pack_msg += struct.pack(
                f'<IB{pack_len}sx', pack_len + 1,
                _MipsMsgTypeOptions.RET_TOPIC.value, ret_topic.encode('utf-8')
            )
        # payload
        pack_len = len(payload)
        pack_msg += struct.pack(
            f'<IB{pack_len}sx', pack_len + 1,
            _MipsMsgTypeOptions.PAYLOAD.value, payload.encode('utf-8')
        )
        return pack_msg

    def __str__(self) -> str:
        return f'{self.mid}, {self.msg_from}, {self.ret_topic}, {self.payload}'


@dataclass
class _MipsRequest:
    """AIoT Pub/Sub request."""
    mid: int
    on_reply: Callable[[str, Any], None]
    on_reply_ctx: Any
    timer: Optional[asyncio.TimerHandle]


@dataclass
class _MipsBroadcast:
    """AIoT Pub/Sub broadcast."""
    topic: str
    """
    param 1: msg topic
    param 2: msg payload
    param 3: handle_ctx
    """
    handler: Callable[[str, str, Any], None]
    handler_ctx: Any

    def __str__(self) -> str:
        return f'{self.topic}, {id(self.handler)}, {id(self.handler_ctx)}'


@dataclass
class _MipsState:
    """AIoT Pub/Sub state."""
    key: str
    """
    str: key
    bool: mips connect state
    """
    handler: Callable[[str, bool], Coroutine]


class AIoTDeviceState(Enum):
    """AIoT device state define."""
    DISABLE = 0
    OFFLINE = auto()
    ONLINE = auto()


@dataclass
class MipsDeviceState:
    """AIoT Pub/Sub device state."""
    did: Optional[str] = None
    """handler
    str: did
    AIoTDeviceState: online/offline/disable
    Any: ctx
    """
    handler: Optional[Callable[[str, AIoTDeviceState, Any], None]] = None
    handler_ctx: Any = None


class _MipsClient(ABC):
    # pylint: disable=unused-argument
    MQTT_INTERVAL_S = 1
    MIPS_QOS: int = 2
    UINT32_MAX: int = 0xFFFFFFFF
    MIPS_RECONNECT_INTERVAL_MIN: float = 10
    MIPS_RECONNECT_INTERVAL_MAX: float = 600
    MIPS_SUB_PATCH: int = 300
    MIPS_SUB_INTERVAL: float = 1
    main_loop: asyncio.AbstractEventLoop
    _logger: Optional[logging.Logger]
    _client_id: str
    _host: str
    _port: int
    _username: Optional[str]
    _password: Optional[str]
    _ca_file: Optional[str]
    _cert_file: Optional[str]
    _key_file: Optional[str]

    _mqtt_logger: Optional[logging.Logger]
    _mqtt: Optional[Client]
    _mqtt_fd: int
    _mqtt_timer: Optional[asyncio.TimerHandle]
    _mqtt_state: bool

    _event_connect: asyncio.Event
    _event_disconnect: asyncio.Event
    _internal_loop: asyncio.AbstractEventLoop
    _mips_thread: Optional[threading.Thread]
    _mips_reconnect_tag: bool
    _mips_reconnect_interval: float
    _mips_reconnect_timer: Optional[asyncio.TimerHandle]
    _mips_state_sub_map: dict[str, _MipsState]
    _mips_state_sub_map_lock: threading.Lock
    _mips_sub_pending_map: dict[str, int]
    _mips_sub_pending_timer: Optional[asyncio.TimerHandle]

    def __init__(
            self,
            client_id: str,
            host: str,
            port: int,
            username: Optional[str] = None,
            password: Optional[str] = None,
            ca_file: Optional[str] = None,
            cert_file: Optional[str] = None,
            key_file: Optional[str] = None,
            loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        # MUST run with running loop
        self.main_loop = loop or asyncio.get_running_loop()
        self._logger = None
        self._client_id = client_id
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._ca_file = ca_file
        self._cert_file = cert_file
        self._key_file = key_file

        self._mqtt_logger = None
        self._mqtt_fd = -1
        self._mqtt_timer = None
        self._mqtt_state = False
        self._mqtt = None

        # Mips init
        self._event_connect = asyncio.Event()
        self._event_disconnect = asyncio.Event()
        self._mips_thread = None
        self._mips_reconnect_tag = False
        self._mips_reconnect_interval = 0
        self._mips_reconnect_timer = None
        self._mips_state_sub_map = {}
        self._mips_state_sub_map_lock = threading.Lock()
        self._mips_sub_pending_map = {}
        self._mips_sub_pending_timer = None
        # DO NOT start the thread yet. Do that on connect

    @property
    def client_id(self) -> str:
        return self._client_id

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @final
    @property
    def mips_state(self) -> bool:
        """获取 mips 连接状态 """
        if self._mqtt:
            return self._mqtt.is_connected()
        return False

    def connect(self, thread_name: Optional[str] = None) -> None:
        """mips 连接"""
        # Start mips thread
        if self._mips_thread:
            return
        self._internal_loop = asyncio.new_event_loop()
        self._mips_thread = threading.Thread(target=self.__mips_loop_thread)
        self._mips_thread.daemon = True
        self._mips_thread.name = (self._client_id if thread_name is None else thread_name)
        self._mips_thread.start()

    async def connect_async(self) -> None:
        """mips 异步连接"""
        self.connect()
        await self._event_connect.wait()

    def disconnect(self) -> None:
        """mips 断开连接"""
        if not self._mips_thread:
            return
        self._internal_loop.call_soon_threadsafe(self.__mips_disconnect)
        self._mips_thread.join()
        self._mips_thread = None
        self._internal_loop.close()

    async def disconnect_async(self) -> None:
        """mips 异步断开连接"""
        self.disconnect()
        await self._event_disconnect.wait()

    @final
    def deinit(self) -> None:
        self.disconnect()

        self._logger = None
        self._username = None
        self._password = None
        self._ca_file = None
        self._cert_file = None
        self._key_file = None
        self._mqtt_logger = None
        with self._mips_state_sub_map_lock:
            self._mips_state_sub_map.clear()
        self._mips_sub_pending_map.clear()
        self._mips_sub_pending_timer = None

    @final
    async def deinit_async(self) -> None:
        await self.disconnect_async()

        self._logger = None
        self._username = None
        self._password = None
        self._ca_file = None
        self._cert_file = None
        self._key_file = None
        self._mqtt_logger = None
        with self._mips_state_sub_map_lock:
            self._mips_state_sub_map.clear()
        self._mips_sub_pending_map.clear()
        self._mips_sub_pending_timer = None

    def update_mqtt_password(self, password: str) -> None:
        self._password = password
        if self._mqtt:
            self._mqtt.username_pw_set(username=self._username, password=self._password)

    def log_debug(self, msg, *args, **kwargs) -> None:
        if self._logger:
            self._logger.debug(f'{self._client_id}, ' + msg, *args, **kwargs)

    def log_info(self, msg, *args, **kwargs) -> None:
        if self._logger:
            self._logger.info(f'{self._client_id}, ' + msg, *args, **kwargs)

    def log_error(self, msg, *args, **kwargs) -> None:
        if self._logger:
            self._logger.error(f'{self._client_id}, ' + msg, *args, **kwargs)

    def enable_logger(self, logger: Optional[logging.Logger] = None) -> None:
        self._logger = logger

    def enable_mqtt_logger(self, logger: Optional[logging.Logger] = None) -> None:
        self._mqtt_logger = logger
        if self._mqtt:
            if logger:
                self._mqtt.enable_logger(logger=logger)
            else:
                self._mqtt.disable_logger()

    @final
    def sub_mips_state(
            self,
            key: str,
            handler: Callable[[str, bool], Coroutine]
    ) -> bool:
        """订阅 mips 状态"""
        if isinstance(key, str) is False or handler is None:
            raise AIoTMipsError('invalid params')
        state = _MipsState(key=key, handler=handler)
        with self._mips_state_sub_map_lock:
            self._mips_state_sub_map[key] = state
        self.log_debug(f'mips register mips state, {key}')
        return True

    @final
    def unsub_mips_state(self, key: str) -> bool:
        """取消订阅 mips 状态"""
        if isinstance(key, str) is False:
            raise AIoTMipsError('invalid params')
        with self._mips_state_sub_map_lock:
            del self._mips_state_sub_map[key]
        self.log_debug(f'mips unregister mips state, {key}')
        return True

    @abstractmethod
    def sub_prop(
            self,
            did: str,
            handler: Callable[[dict, Any], None],
            aam_cmd: Optional[str] = None,
            aam_prop_name: Optional[int] = None,
            handler_ctx: Any = None
    ) -> bool:
        ...

    @abstractmethod
    def unsub_prop(
            self,
            did: str,
            aam_cmd: Optional[str] = None,
            aam_prop_name: Optional[str] = None
    ) -> bool:
        ...

    @abstractmethod
    def sub_event(
            self,
            did: str,
            handler: Callable[[dict, Any], None],
            aam_cmd: Optional[str] = None,
            aam_prop_name: Optional[str] = None,
            handler_ctx: Any = None
    ) -> bool:
        ...

    @abstractmethod
    def unsub_event(
            self,
            did: str,
            aam_cmd: Optional[str] = None,
            aam_prop_name: Optional[str] = None
    ) -> bool:
        ...

    @abstractmethod
    def _on_mips_message(
            self,
            topic: str,
            payload: bytes
    ) -> None:
        ...

    @abstractmethod
    def _on_mips_connect(
            self,
            rc: int,
            props: dict
    ) -> None:
        ...

    @abstractmethod
    def _on_mips_disconnect(
            self,
            rc: int,
            props: dict
    ) -> None:
        ...

    @final
    def _mips_sub_internal(self, topic: str) -> None:
        """mips 订阅"""
        self.__thread_check()
        if not self._mqtt or not self._mqtt.is_connected():
            self.log_error(f'mips sub when not connected, {topic}')
            return

        if topic not in self._mips_sub_pending_map:
            self._mips_sub_pending_map[topic] = 0
        if not self._mips_sub_pending_timer:
            self._mips_sub_pending_timer = self._internal_loop.call_later(
                0.01,
                self.__mips_sub_internal_pending_handler,
                topic
            )

    @final
    def _mips_unsub_internal(self, topic: str) -> None:
        """mips 取消订阅"""
        self.__thread_check()
        if not self._mqtt or not self._mqtt.is_connected():
            self.log_debug(f'mips unsub when not connected, {topic}')
            return
        try:
            result, mid = self._mqtt.unsubscribe(topic=topic)
            if (result == MQTT_ERR_SUCCESS) or (result == MQTT_ERR_NO_CONN):
                self.log_debug(f'mips unsub internal success, {result}, {mid}, {topic}')
                return
            self.log_error(f'mips unsub internal error, {result}, {mid}, {topic}')
        except Exception as err:  # pylint: disable=broad-exception-caught
            # Catch all exception
            self.log_error(f'mips unsub internal error, {topic}, {err}')

    @final
    def _mips_publish_internal(
            self,
            topic: str,
            payload: str | bytes,
            wait_for_publish: bool = False,
            timeout_ms: int = 10000
    ) -> bool:
        """mips publish message.
        NOTICE: Internal function, only mips threads are allowed to call

        """
        self.__thread_check()
        if not self._mqtt or not self._mqtt.is_connected():
            return False
        try:
            handle = self._mqtt.publish(topic=topic, payload=payload, qos=self.MIPS_QOS)
            # self.log_debug(f'_mips_publish_internal, {topic}, {payload}')
            if wait_for_publish is True:
                handle.wait_for_publish(timeout_ms / 1000.0)
            return True
        except Exception as err:  # pylint: disable=broad-exception-caught
            # Catch other exception
            self.log_error(f'mips publish internal error, {err}')
        return False

    def __thread_check(self) -> None:
        if threading.current_thread() is not self._mips_thread:
            raise AIoTMipsError('illegal call')

    def __mqtt_read_handler(self) -> None:
        self.__mqtt_loop_handler()

    def __mqtt_write_handler(self) -> None:
        self._internal_loop.remove_writer(self._mqtt_fd)
        self.__mqtt_loop_handler()

    def __mqtt_timer_handler(self) -> None:
        self.__mqtt_loop_handler()
        if self._mqtt:
            self._mqtt_timer = self._internal_loop.call_later(
                self.MQTT_INTERVAL_S, self.__mqtt_timer_handler)

    def __mqtt_loop_handler(self) -> None:
        try:
            # If the main loop is closed, stop the internal loop immediately
            if self.main_loop.is_closed():
                self.log_debug('The main loop is closed, stop the internal loop.')
                if not self._internal_loop.is_closed():
                    self._internal_loop.stop()
                return
            if self._mqtt:
                self._mqtt.loop_read()
            if self._mqtt:
                self._mqtt.loop_write()
            if self._mqtt:
                self._mqtt.loop_misc()
            if self._mqtt and self._mqtt.want_write():
                self._internal_loop.add_writer(
                    self._mqtt_fd, self.__mqtt_write_handler)
        except Exception as err:  # pylint: disable=broad-exception-caught
            # Catch all exception
            self.log_error(f'__mqtt_loop_handler, {err}')
            raise err

    def __mips_loop_thread(self) -> None:
        self.log_info('mips_loop_thread start')
        # mqtt init for API_VERSION2,
        # callback_api_version=CallbackAPIVersion.VERSION2,
        self._mqtt = Client(client_id=self._client_id, protocol=MQTTv5)
        self._mqtt.enable_logger(logger=self._mqtt_logger)
        # Set mqtt config
        if self._username:
            self._mqtt.username_pw_set(username=self._username, password=self._password)
        if (
                self._ca_file
                and self._cert_file
                and self._key_file
        ):
            self._mqtt.tls_set(
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
                ca_certs=self._ca_file,
                certfile=self._cert_file,
                keyfile=self._key_file
            )
        else:
            self._mqtt.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
        self._mqtt.tls_insecure_set(True)
        self._mqtt.on_connect = self.__on_connect
        self._mqtt.on_connect_fail = self.__on_connect_failed
        self._mqtt.on_disconnect = self.__on_disconnect
        self._mqtt.on_message = self.__on_message
        # Connect to mips
        self.__mips_start_connect_tries()
        # Run event loop
        self._internal_loop.run_forever()
        self.log_info('mips_loop_thread exit!')

    def __on_connect(self, client, user_data, flags, rc, props) -> None:
        if not self._mqtt:
            _LOGGER.error('__on_connect, but mqtt is None')
            return
        if not self._mqtt.is_connected():
            _LOGGER.error('__on_connect, but mqtt is disconnected')
            return
        self.log_info(f'mips connect, {flags}, {rc}, {props}')
        self.__reset_reconnect_time()
        self._mqtt_state = True
        self._internal_loop.call_soon(self._on_mips_connect, rc, props)
        with self._mips_state_sub_map_lock:
            for item in self._mips_state_sub_map.values():
                if item.handler is None:
                    continue
                self.main_loop.call_soon_threadsafe(self.main_loop.create_task, item.handler(item.key, True))
        # Resolve future
        self.main_loop.call_soon_threadsafe(self._event_connect.set)
        self.main_loop.call_soon_threadsafe(self._event_disconnect.clear)

    def __on_connect_failed(self, client: Client, user_data: Any) -> None:
        self.log_error('mips connect failed')
        # 尝试重连
        self.__mips_try_reconnect()

    def __on_disconnect(self, client, user_data, rc, props) -> None:
        if self._mqtt_state:
            (self.log_info if rc == 0 else self.log_error)(f'mips disconnect, {rc}, {props}')
            self._mqtt_state = False
            if self._mqtt_timer:
                self._mqtt_timer.cancel()
                self._mqtt_timer = None
            if self._mqtt_fd != -1:
                self._internal_loop.remove_reader(self._mqtt_fd)
                self._internal_loop.remove_writer(self._mqtt_fd)
                self._mqtt_fd = -1
            # Clear retry sub
            if self._mips_sub_pending_timer:
                self._mips_sub_pending_timer.cancel()
                self._mips_sub_pending_timer = None
            self._mips_sub_pending_map = {}
            self._internal_loop.call_soon(
                self._on_mips_disconnect, rc, props)
            # Call state sub handler
            with self._mips_state_sub_map_lock:
                for item in self._mips_state_sub_map.values():
                    if item.handler is None:
                        continue
                    self.main_loop.call_soon_threadsafe(self.main_loop.create_task, item.handler(item.key, False))

        # 尝试重连
        self.__mips_try_reconnect()
        # 设置事件
        self.main_loop.call_soon_threadsafe(self._event_disconnect.set)
        self.main_loop.call_soon_threadsafe(self._event_connect.clear)

    def __on_message(
            self,
            client: Client,
            user_data: Any,
            msg: MQTTMessage
    ) -> None:
        self._on_mips_message(topic=msg.topic, payload=msg.payload)

    def __mips_sub_internal_pending_handler(self, ctx: Any) -> None:
        if not self._mqtt or not self._mqtt.is_connected():
            _LOGGER.error('mips sub internal pending, but mqtt is None or disconnected')
            return
        subbed_count = 1
        for topic in list(self._mips_sub_pending_map.keys()):
            if subbed_count > self.MIPS_SUB_PATCH:
                break
            count = self._mips_sub_pending_map[topic]
            if count > 3:
                self._mips_sub_pending_map.pop(topic)
                self.log_error(f'retry mips sub internal error, {topic}')
                continue
            subbed_count += 1
            result = mid = None
            try:
                result, mid = self._mqtt.subscribe(topic, qos=self.MIPS_QOS)
                if result == MQTT_ERR_SUCCESS:
                    self._mips_sub_pending_map.pop(topic)
                    self.log_debug(f'mips sub internal success, {topic}')
                    continue
            except Exception as err:  # pylint: disable=broad-exception-caught
                # Catch all exception
                self.log_error(f'mips sub internal error, {topic}. {err}')
            self._mips_sub_pending_map[topic] = count + 1
            self.log_error(f'retry mips sub internal, {count}, {topic}, {result}, {mid}')

        if len(self._mips_sub_pending_map):
            self._mips_sub_pending_timer = self._internal_loop.call_later(
                self.MIPS_SUB_INTERVAL,
                self.__mips_sub_internal_pending_handler,
                None
            )
        else:
            self._mips_sub_pending_timer = None

    def __mips_connect(self) -> None:
        """mip 连接"""
        if not self._mqtt:
            _LOGGER.error('__mips_connect, but mqtt is None')
            return
        result = MQTT_ERR_UNKNOWN
        if self._mips_reconnect_timer:
            self._mips_reconnect_timer.cancel()
            self._mips_reconnect_timer = None
        try:
            # Try clean mqtt fd before mqtt connect
            if self._mqtt_timer:
                self._mqtt_timer.cancel()
                self._mqtt_timer = None
            if self._mqtt_fd != -1:
                self._internal_loop.remove_reader(self._mqtt_fd)
                self._internal_loop.remove_writer(self._mqtt_fd)
                self._mqtt_fd = -1
            result = self._mqtt.connect(
                host=self._host,
                port=self._port,
                clean_start=True,
                keepalive=MYHOME_MQTT_KEEPALIVE
            )
            self.log_info(f'__mips_connect success, {result}')
        except (TimeoutError, OSError) as error:
            self.log_error('__mips_connect, connect error, %s', error)

        if result == MQTT_ERR_SUCCESS:
            socket = self._mqtt.socket()
            if socket is None:
                self.log_error('__mips_connect, connect success, but socket is None')
                self.__mips_try_reconnect()
                return
            self._mqtt_fd = socket.fileno()
            self.log_debug(f'__mips_connect, _mqtt_fd, {self._mqtt_fd}')
            self._internal_loop.add_reader(self._mqtt_fd, self.__mqtt_read_handler)
            if self._mqtt.want_write():
                self._internal_loop.add_writer(self._mqtt_fd, self.__mqtt_write_handler)
            self._mqtt_timer = self._internal_loop.call_later(self.MQTT_INTERVAL_S, self.__mqtt_timer_handler)
        else:
            self.log_error(f'__mips_connect error result, {result}')
            self.__mips_try_reconnect()

    def __mips_try_reconnect(self, immediately: bool = False) -> None:
        """mips 尝试重连"""
        if self._mips_reconnect_timer:
            self._mips_reconnect_timer.cancel()
            self._mips_reconnect_timer = None
        if not self._mips_reconnect_tag:
            return
        interval: float = 0
        if not immediately:
            interval = self.__get_next_reconnect_time()
            self.log_error('mips try reconnect after %ss', interval)
        self._mips_reconnect_timer = self._internal_loop.call_later(interval, self.__mips_connect)

    def __mips_start_connect_tries(self) -> None:
        """mips 开始连接重试"""
        self._mips_reconnect_tag = True
        self.__mips_try_reconnect(immediately=True)

    def __mips_disconnect(self) -> None:
        """mips 断开连接"""
        self._mips_reconnect_tag = False
        if self._mips_reconnect_timer:
            self._mips_reconnect_timer.cancel()
            self._mips_reconnect_timer = None
        if self._mqtt_timer:
            self._mqtt_timer.cancel()
            self._mqtt_timer = None
        if self._mqtt_fd != -1:
            self._internal_loop.remove_reader(self._mqtt_fd)
            self._internal_loop.remove_writer(self._mqtt_fd)
            self._mqtt_fd = -1
        # Clear retry sub
        if self._mips_sub_pending_timer:
            self._mips_sub_pending_timer.cancel()
            self._mips_sub_pending_timer = None
        self._mips_sub_pending_map = {}
        if self._mqtt:
            self._mqtt.disconnect()
            self._mqtt = None
        self._internal_loop.stop()

    def __get_next_reconnect_time(self) -> float:
        """获取下次重连时间间隔"""
        if self._mips_reconnect_interval < self.MIPS_RECONNECT_INTERVAL_MIN:
            self._mips_reconnect_interval = self.MIPS_RECONNECT_INTERVAL_MIN
        else:
            self._mips_reconnect_interval = min(
                self._mips_reconnect_interval * 2,
                self.MIPS_RECONNECT_INTERVAL_MAX)
        return self._mips_reconnect_interval

    def __reset_reconnect_time(self) -> None:
        """重置重连时间间隔"""
        self._mips_reconnect_interval = 0


class MipsLanClient(_MipsClient):
    """AIoT 发布/订阅 Lan 客户端"""
    # pylint: disable=unused-argument
    # pylint: disable=inconsistent-quotes
    _msg_matcher: AIoTMatcher

    def __init__(
            self,
            uuid: str,
            lan_server: str,
            app_id: str,
            token: str,
            port: int = 8883,
            loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        self._msg_matcher = AIoTMatcher()
        super().__init__(
            client_id=f'ha.{uuid}',
            host=f'{lan_server}',
            port=port,
            username=app_id,
            password=token,
            loop=loop
        )

    @final
    def disconnect(self) -> None:
        super().disconnect()
        self._msg_matcher = AIoTMatcher()

    def update_access_token(self, access_token: str) -> bool:
        if not isinstance(access_token, str):
            raise AIoTMipsError('invalid token')
        self.update_mqtt_password(password=access_token)
        return True

    @final
    def sub_prop(
            self,
            did: str,
            handler: Callable[[dict, Any], None],
            aam_cmd: Optional[str] = None,
            aam_prop_name: Optional[str] = None,
            handler_ctx: Any = None
    ) -> bool:
        """订阅属性"""
        if not isinstance(did, str) or handler is None:
            raise AIoTMipsError('invalid params')

        # 包含子设备入网事件、离网事件、属性消息、事件消息、设备离线、设备在线
        topic: str = '/aam/subgateway/sub/event/#'

        def on_prop_msg(topic: str, payload: str, ctx: Any) -> None:
            try:
                msg: dict = json.loads(payload)
            except json.JSONDecodeError:
                self.log_error(f'on_prop_msg, invalid msg, {topic}, {payload}')
                return

            info: dict = msg.get('body')
            if (
                    not isinstance(msg.get('params', None), dict)
                    or 'siid' not in msg['params']
                    or 'piid' not in msg['params']
                    or 'value' not in msg['params']
            ):
                self.log_error(f'on_prop_msg, invalid msg, {topic}, {payload}')
                return
            if handler:
                self.log_debug('on properties_changed, %s', payload)
                handler(msg['params'], ctx)

        return self.__reg_broadcast_external(topic=topic, handler=on_prop_msg, handler_ctx=handler_ctx)

    @final
    def unsub_prop(
            self,
            did: str,
            aam_cmd: Optional[str] = None,
            aam_prop_name: Optional[str] = None
    ) -> bool:
        """取消订阅属性"""
        if not isinstance(did, str):
            raise AIoTMipsError('invalid params')
        topic: str = '/aam/subgateway/sub/event/#'
        return self.__unreg_broadcast_external(topic=topic)

    @final
    def sub_event(
            self,
            did: str,
            handler: Callable[[dict, Any], None],
            aam_cmd: Optional[str] = None,
            aam_prop_name: Optional[str] = None,
            handler_ctx: Any = None
    ) -> bool:
        """订阅事件"""
        if not isinstance(did, str) or handler is None:
            raise AIoTMipsError('invalid params')
        # Spelling error: event_occured
        topic: str = (
            f'device/{did}/up/event_occured/'
            f'{"#" if aam_cmd is None or aam_prop_name is None else f"{aam_cmd}/{aam_prop_name}"}')

        def on_event_msg(topic: str, payload: str, ctx: Any) -> None:
            try:
                msg: dict = json.loads(payload)
            except json.JSONDecodeError:
                self.log_error(f'on_event_msg, invalid msg, {topic}, {payload}')
                return
            if (
                    not isinstance(msg.get('params', None), dict)
                    or 'siid' not in msg['params']
                    or 'eiid' not in msg['params']
                    or 'arguments' not in msg['params']
            ):
                self.log_error(f'on_event_msg, invalid msg, {topic}, {payload}')
                return
            if handler:
                self.log_debug('on on_event_msg, %s', payload)
                msg['params']['from'] = 'cloud'
                handler(msg['params'], ctx)

        return self.__reg_broadcast_external(topic=topic, handler=on_event_msg, handler_ctx=handler_ctx)

    @final
    def unsub_event(
            self,
            did: str,
            aam_cmd: Optional[str] = None,
            aam_prop_name: Optional[str] = None
    ) -> bool:
        """取消订阅事件"""
        if not isinstance(did, str):
            raise AIoTMipsError('invalid params')
        # Spelling error: event_occured
        topic: str = (
            f'device/{did}/up/event_occured/'
            f'{"#" if aam_cmd is None or aam_prop_name is None else f"{aam_cmd}/{aam_prop_name}"}')
        return self.__unreg_broadcast_external(topic=topic)

    @final
    def sub_device_state(
            self,
            did: str,
            handler: Callable[[str, AIoTDeviceState, Any], None],
            handler_ctx: Any = None
    ) -> bool:
        """订阅设备在线状态"""
        if not isinstance(did, str) or handler is None:
            raise AIoTMipsError('invalid params')
        topic: str = f'device/{did}/state/#'

        def on_state_msg(topic: str, payload: str, ctx: Any) -> None:
            msg: dict = json.loads(payload)
            # {"device_id":"xxxx","device_name":"米家智能插座3   ","event":"online",
            # "model": "cuco.plug.v3","timestamp":1709001070828,"uid":xxxx}
            if msg is None or 'device_id' not in msg or 'event' not in msg:
                self.log_error(f'on_state_msg, recv unknown msg, {payload}')
                return
            if msg['device_id'] != did:
                self.log_error(f'on_state_msg, err msg, {did}!={msg["device_id"]}')
                return
            if handler:
                self.log_debug('cloud, device state changed, %s', payload)
                handler(
                    did,
                    AIoTDeviceState.ONLINE if msg['event'] == 'online' else AIoTDeviceState.OFFLINE,
                    ctx
                )

        return self.__reg_broadcast_external(topic=topic, handler=on_state_msg, handler_ctx=handler_ctx)

    @final
    def unsub_device_state(self, did: str) -> bool:
        """取消订阅设备在线状态"""
        if not isinstance(did, str):
            raise AIoTMipsError('invalid params')
        topic: str = f'device/{did}/state/#'
        return self.__unreg_broadcast_external(topic=topic)

    def __reg_broadcast_external(
            self,
            topic: str,
            handler: Callable[[str, str, Any], None],
            handler_ctx: Any = None
    ) -> bool:
        self._internal_loop.call_soon_threadsafe(self.__reg_broadcast, topic, handler, handler_ctx)
        return True

    def __unreg_broadcast_external(self, topic: str) -> bool:
        self._internal_loop.call_soon_threadsafe(self.__unreg_broadcast, topic)
        return True

    def __reg_broadcast(
            self,
            topic: str,
            handler: Callable[[str, str, Any], None],
            handler_ctx: Any = None
    ) -> None:
        if not self._msg_matcher.get(topic=topic):
            sub_bc: _MipsBroadcast = _MipsBroadcast(topic=topic, handler=handler, handler_ctx=handler_ctx)
            self._msg_matcher[topic] = sub_bc
            self._mips_sub_internal(topic=topic)
        else:
            self.log_debug(f'mips cloud re-reg broadcast, {topic}')

    def __unreg_broadcast(self, topic: str) -> None:
        if self._msg_matcher.get(topic=topic):
            del self._msg_matcher[topic]
            self._mips_unsub_internal(topic=topic)

    def _on_mips_connect(self, rc: int, props: dict) -> None:
        """sub topic."""
        for topic, _ in list(self._msg_matcher.iter_all_nodes()):
            self._mips_sub_internal(topic=topic)

    def _on_mips_disconnect(self, rc: int, props: dict) -> None:
        """unsub topic."""
        pass

    def _on_mips_message(self, topic: str, payload: bytes) -> None:
        """
        NOTICE thread safe, this function will be called at the **mips** thread
        """
        # broadcast
        bc_list: list[_MipsBroadcast] = list(self._msg_matcher.iter_match(topic))
        if not bc_list:
            return
        # The message from the cloud is not packed.
        payload_str: str = payload.decode('utf-8')
        # self.log_debug(f"on broadcast, {topic}, {payload}")
        for item in bc_list or []:
            if item.handler is None:
                continue
            # NOTICE: call threadsafe
            self.main_loop.call_soon_threadsafe(item.handler, topic, payload_str, item.handler_ctx)
