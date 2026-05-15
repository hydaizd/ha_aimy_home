# -*- coding: utf-8 -*-
DOMAIN: str = "aimy_home"
NAME: str = "Aimy Home"

DEFAULT_NICK_NAME: str = 'Aimy'

HTTP_API_TIMEOUT: int = 30
HTTP_API_PORT: int = 10088
MYHOME_MQTT_KEEPALIVE: int = 60

NETWORK_REFRESH_INTERVAL: int = 30

AUTH_CLIENT_ID: str = '300007303440741271'

# 　定义集成支持的平台
SUPPORTED_PLATFORMS: list = [
    "switch",
    "select",
    "button",
    "number",
    # "sensor",
    # "light"
]

UNSUPPORTED_MODELS: list = []

DEFAULT_INTEGRATION_LANGUAGE: str = 'en'

DEFAULT_COVER_DEAD_ZONE_WIDTH: int = 0

# 刷新设备列表重试延迟，单位秒
REFRESH_LAN_DEVICES_RETRY_DELAY = 60
REFRESH_LAN_DEVICES_DELAY = 6
REFRESH_PROPS_DELAY = 0.2
REFRESH_PROPS_RETRY_DELAY = 3
