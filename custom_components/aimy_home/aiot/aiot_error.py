# -*- coding: utf-8 -*-
from typing import Any


class AIoTError(Exception):
    """AIoT error."""
    message: Any

    def __init__(
            self,
            message: Any
    ) -> None:
        self.message = message
        super().__init__(self.message)

    def to_str(self) -> str:
        return f'{{"message":"{self.message}"}}'

    def to_dict(self) -> dict:
        return {"message": self.message}


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
