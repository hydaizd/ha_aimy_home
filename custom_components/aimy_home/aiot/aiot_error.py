# -*- coding: utf-8 -*-
from enum import Enum
from typing import Any


class AIoTErrorCode(Enum):
    """AIoT 错误码."""
    # Base error code
    CODE_UNKNOWN = -10000
    CODE_UNAVAILABLE = -10001
    CODE_INVALID_PARAMS = -10002
    CODE_RESOURCE_ERROR = -10003
    CODE_INTERNAL_ERROR = -10004
    CODE_UNAUTHORIZED_ACCESS = -10005
    CODE_TIMEOUT = -10006
    # Http error code
    CODE_HTTP_INVALID_ACCESS_TOKEN = -10030
    # AIoT mips error code
    CODE_MIPS_INVALID_RESULT = -10040
    # Config flow error code, -10100
    CODE_CONFIG_INVALID_INPUT = -10100
    CODE_CONFIG_INVALID_STATE = -10101
    # Options flow error code , -10110
    # AIoT lan error code, -10120
    CODE_LAN_UNAVAILABLE = -10120


class AIoTError(Exception):
    """AIoT error."""
    code: AIoTErrorCode
    message: Any

    def __init__(
            self,
            message: Any,
            code: AIoTErrorCode = AIoTErrorCode.CODE_UNKNOWN
    ) -> None:
        self.message = message
        self.code = code
        super().__init__(self.message)

    def to_str(self) -> str:
        return f'{{"code":{self.code.value},"message":"{self.message}"}}'

    def to_dict(self) -> dict:
        return {"code": self.code.value, "message": self.message}


class AIoTAuthError(AIoTError):
    ...


class AIoTHttpError(AIoTError):
    ...


class AIoTMipsError(AIoTError):
    ...


class AIoTDeviceError(AIoTError):
    ...


class AIoTSpecError(AIoTError):
    ...


class AIoTStorageError(AIoTError):
    ...


class AIoTClientError(AIoTError):
    ...


class AIoTEvError(AIoTError):
    ...


class MipsServiceError(AIoTError):
    ...


class AIoTConfigError(AIoTError):
    ...


class AIoTOptionsError(AIoTError):
    ...


class AIoTLanError(AIoTError):
    ...
