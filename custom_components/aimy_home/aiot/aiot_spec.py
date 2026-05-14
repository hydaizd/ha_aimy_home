# -*- coding: utf-8 -*-
import asyncio
import logging
import platform
from typing import Any, Union, Optional

from slugify import slugify

from .aiot_error import AIoTSpecError, AIoTHttpError
from .aiot_storage import AIoTStorage
from .common import (
    AIoTHttp,
    get_prop_group_key
)
from .const import (
    HTTP_API_PORT,
    DEFAULT_INTEGRATION_LANGUAGE
)

_LOGGER = logging.getLogger(__name__)


class AIoTSpecValueRange:
    """AIoT 范围值类."""
    min_: int
    max_: int
    step: int | float

    def __init__(self, value_range: Union[dict, list]) -> None:
        if isinstance(value_range, dict):
            self.load(value_range)
        elif isinstance(value_range, list):
            self.from_spec(value_range)
        else:
            raise AIoTSpecError('invalid value range format')

    def load(self, value_range: dict) -> None:
        if 'min' not in value_range or 'max' not in value_range or 'step' not in value_range:
            raise AIoTSpecError('invalid value range')
        self.min_ = value_range['min']
        self.max_ = value_range['max']
        self.step = value_range['step']

    def from_spec(self, value_range: list) -> None:
        if len(value_range) != 3:
            raise AIoTSpecError('invalid value range')
        self.min_ = value_range[0]
        self.max_ = value_range[1]
        self.step = value_range[2]

    def dump(self) -> dict:
        return {'min': self.min_, 'max': self.max_, 'step': self.step}

    def __str__(self) -> str:
        return f'[{self.min_}, {self.max_}, {self.step}'


class AIoTSpecValueListItem:
    """AIoT 规范值列表项类."""
    # NOTICE: bool type without name
    name: str
    # Value
    value: Any
    # Descriptions after multilingual conversion.
    description: str

    def __init__(self, item: dict) -> None:
        self.load(item)

    def load(self, item: dict) -> None:
        if 'value' not in item or 'description' not in item:
            raise AIoTSpecError('invalid value list item, %s')

        self.name = item.get('name', None)
        self.value = item['value']
        self.description = item['description']

    @staticmethod
    def from_spec(item: dict) -> 'AIoTSpecValueListItem':
        if 'name' not in item or 'value' not in item or 'description' not in item:
            raise AIoTSpecError('invalid value list item, %s')
        # Slugify name and convert to lower-case.
        cache = {
            'name': slugify(text=item['name'], separator='_').lower(),
            'value': item['value'],
            'description': item['description']
        }
        return AIoTSpecValueListItem(cache)

    def dump(self) -> dict:
        return {
            'name': self.name,
            'value': self.value,
            'description': self.description
        }

    def __str__(self) -> str:
        return f'{self.name}: {self.value} - {self.description}'


class AIoTSpecValueList:
    """AIoT 规范值列表类."""
    # pylint: disable=inconsistent-quotes
    items: list[AIoTSpecValueListItem]

    def __init__(self, value_list: list[dict]) -> None:
        if not isinstance(value_list, list):
            raise AIoTSpecError('invalid value list format')
        self.items = []
        self.load(value_list)

    @property
    def names(self) -> list[str]:
        return [item.name for item in self.items]

    @property
    def values(self) -> list[Any]:
        return [item.value for item in self.items]

    @property
    def descriptions(self) -> list[str]:
        return [item.description for item in self.items]

    @staticmethod
    def from_spec(value_list: list[dict]) -> 'AIoTSpecValueList':
        result = AIoTSpecValueList([])
        dup_desc: dict[str, int] = {}
        for item in value_list:
            # Handle duplicate descriptions.
            count = 0
            if item['description'] in dup_desc:
                count = dup_desc[item['description']]
            count += 1
            dup_desc[item['description']] = count
            if count > 1:
                item['name'] = f'{item["name"]}_{count}'
                item['description'] = f'{item["description"]}_{count}'

            result.items.append(AIoTSpecValueListItem.from_spec(item))
        return result

    def load(self, value_list: list[dict]) -> None:
        for item in value_list:
            self.items.append(AIoTSpecValueListItem(item))

    def to_map(self) -> dict:
        return {item.value: item.description for item in self.items}

    def get_value_by_description(self, description: str) -> Any:
        for item in self.items:
            if item.description == description:
                return item.value
        return None

    def get_description_by_value(self, value: Any) -> Optional[str]:
        for item in self.items:
            if item.value == value:
                return item.description
        return None

    def dump(self) -> list:
        return [item.dump() for item in self.items]


class _AIoTSpecBase:
    """AIoT 规范基类."""
    # nnd：属性对应aam_prop_name，服务对应aam_cmd
    nnd: str
    type_: str
    description: str
    description_trans: Optional[str]
    proprietary: bool
    name: str

    # External params
    platform: Optional[str]
    entity_category: Optional[str]

    spec_id: int

    def __init__(self, spec: dict) -> None:
        self.nnd = spec['nnd']
        self.type_ = spec['type']
        self.description = spec['description']

        self.description_trans = spec.get('description_trans', None)
        self.proprietary = spec.get('proprietary', False)
        self.name = spec.get('name', 'aimy')

        self.platform = None
        self.entity_category = None

        self.spec_id = hash(f'{self.type_}.{self.nnd}')

    def __hash__(self) -> int:
        return self.spec_id

    def __eq__(self, value) -> bool:
        return self.spec_id == value.spec_id


class AIoTSpecProperty(_AIoTSpecBase):
    """AIoT 规范属性类."""
    unit: Optional[str]
    precision: int
    expr: Optional[str]

    _format_: type
    _value_range: Optional[AIoTSpecValueRange]
    _value_list: Optional[AIoTSpecValueList]

    service: 'AIoTSpecService'

    # 　自定义
    group_key: Optional[str]  # 关联属性组key，同组属性需要一起发送

    def __init__(
            self,
            spec: dict,
            service: 'AIoTSpecService',
            format_: str,
            unit: Optional[str] = None,
            value_range: Optional[dict] = None,
            value_list: Optional[list[dict]] = None,
            precision: Optional[int] = None,
            expr: Optional[str] = None
    ) -> None:
        super().__init__(spec=spec)
        self.service = service  # 示例：在 IoTSpecParser 解析属性时
        self.format_ = format_
        self.unit = unit
        self.value_range = value_range
        self.value_list = value_list
        self.precision = precision if precision is not None else 1
        self.expr = expr

    @property
    def format_(self) -> type:
        return self._format_

    @format_.setter
    def format_(self, value: str) -> None:
        self._format_ = {
            'string': str,
            'str': str,
            'bool': bool,
            'float': float
        }.get(value, int)

    @property
    def value_range(self) -> AIoTSpecValueRange | None:
        return self._value_range

    @value_range.setter
    def value_range(self, value: Union[dict, list, None]) -> None:
        """Set value-range, precision."""
        if not value:
            self._value_range = None
            return
        self._value_range = AIoTSpecValueRange(value_range=value)
        if isinstance(value, list):
            step_: str = format(value[2], '.10f').rstrip('0').rstrip('.')
            self.precision = len(step_.split('.')[1]) if '.' in step_ else 0

    @property
    def value_list(self) -> AIoTSpecValueList | None:
        return self._value_list

    @value_list.setter
    def value_list(self, value: Union[list[dict], AIoTSpecValueList, None]) -> None:
        if not value:
            self._value_list = None
            return
        if isinstance(value, list):
            self._value_list = AIoTSpecValueList(value_list=value)
        elif isinstance(value, AIoTSpecValueList):
            self._value_list = value

    def eval_expr(self, src_value: Any) -> Any:
        if not self.expr:
            return src_value
        try:
            # pylint: disable=eval-used
            return eval(self.expr, {'src_value': src_value})
        except Exception as err:  # pylint: disable=broad-exception-caught
            _LOGGER.error('eval expression error, %s, %s, %s', src_value, self.expr, err)
            return src_value

    def value_format(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            if self.format_ == int:
                value = int(float(value))
            elif self.format_ == float:
                value = float(value)
        if self.format_ == bool:
            return bool(value in [True, 1, 'True', 'true', '1'])
        return value

    def value_precision(self, value: Any) -> Any:
        if value is None:
            return None
        if self.format_ == float:
            return round(value, self.precision)
        if self.format_ == int:
            if self.value_range is None:
                return int(round(value))
            return int(
                round(value / self.value_range.step) * self.value_range.step)
        return value

    def dump(self) -> dict:
        return {
            'type': self.type_,
            'name': self.name,
            'description': self.description,
            'description_trans': self.description_trans,
            'format': self.format_.__name__,
            'unit': self.unit,
            'value_range': (self._value_range.dump() if self._value_range else None),
            'value_list': self._value_list.dump() if self._value_list else None,
            'precision': self.precision,
            'expr': self.expr,
        }

    def get_default_value(self) -> Any:
        """Get默认值."""
        if self._format_ == str:
            return ''
        elif self._format_ == bool:
            return False
        else:
            return 0


class AIoTSpecEvent(_AIoTSpecBase):
    """AIoT 规范事件类."""
    argument: list[AIoTSpecProperty]
    service: 'AIoTSpecService'

    def __init__(
            self,
            spec: dict,
            service: 'AIoTSpecService',
            argument: list[AIoTSpecProperty] | None = None) -> None:
        super().__init__(spec=spec)
        self.argument = argument or []
        self.service = service

        self.spec_id = hash(f'e.{self.name}.{self.service.nnd}.{self.nnd}')

    def dump(self) -> dict:
        return {
            'type': self.type_,
            'name': self.name,
            'nnd': self.nnd,
            'description': self.description,
            'description_trans': self.description_trans,
            'proprietary': self.proprietary,
            'argument': [prop.nnd for prop in self.argument],
        }


class AIoTSpecAction(_AIoTSpecBase):
    """AIoT 规范操作类."""
    in_: list[AIoTSpecProperty]
    out: list[AIoTSpecProperty]
    service: 'AIoTSpecService'

    def __init__(
            self,
            spec: dict,
            service: 'AIoTSpecService',
            in_: list[AIoTSpecProperty] | None = None,
            out: list[AIoTSpecProperty] | None = None
    ) -> None:
        super().__init__(spec=spec)
        self.in_ = in_ or []
        self.out = out or []
        self.service = service

        self.spec_id = hash(f'a.{self.name}.{self.service.nnd}.{self.nnd}')

    def dump(self) -> dict:
        return {
            'type': self.type_,
            'name': self.name,
            'nnd': self.nnd,
            'description': self.description,
            'description_trans': self.description_trans,
            'in': [prop.nnd for prop in self.in_],
            'out': [prop.nnd for prop in self.out],
            'proprietary': self.proprietary,
        }


class AIoTSpecService(_AIoTSpecBase):
    """AIoT 规范服务类."""
    properties: list[AIoTSpecProperty]
    events: list[AIoTSpecEvent]
    actions: list[AIoTSpecAction]

    def __init__(self, spec: dict) -> None:
        super().__init__(spec=spec)
        self.properties = []
        self.events = []
        self.actions = []

    def dump(self) -> dict:
        return {
            'type': self.type_,
            'name': self.name,
            'nnd': self.nnd,
            'description': self.description,
            'description_trans': self.description_trans,
            'proprietary': self.proprietary,
            'properties': [prop.dump() for prop in self.properties],
            'events': [event.dump() for event in self.events],
            'actions': [action.dump() for action in self.actions],
        }


class AIoTSpecInstance:
    """AIoT 规范实例类."""
    urn: str
    name: str
    description: str
    description_trans: str
    services: list[AIoTSpecService]

    # External params
    platform: str
    device_class: Any
    icon: str

    def __init__(
            self,
            urn: str,
            name: str,
            description: str,
            description_trans: str
    ) -> None:
        self.urn = urn
        self.name = name
        self.description = description
        self.description_trans = description_trans
        self.services = []

    @staticmethod
    def load(specs: dict) -> 'AIoTSpecInstance':
        instance = AIoTSpecInstance(
            urn=specs['urn'],
            name=specs['name'],
            description=specs['description'],
            description_trans=specs['description_trans'])
        for service in specs['services']:
            spec_service = AIoTSpecService(spec=service)
            for prop in service['properties']:
                spec_prop = AIoTSpecProperty(
                    spec=prop,
                    service=spec_service,
                    format_=prop['format'],
                    unit=prop['unit'],
                    value_range=prop['value_range'],
                    value_list=prop['value_list'],
                    precision=prop.get('precision', None),
                    expr=prop.get('expr', None)
                )
                spec_service.properties.append(spec_prop)
            for event in service['events']:
                spec_event = AIoTSpecEvent(spec=event, service=spec_service)
                arg_list: list[AIoTSpecProperty] = []
                for pnnd in event['argument']:
                    for prop in spec_service.properties:
                        if prop.nnd == pnnd:
                            arg_list.append(prop)
                            break
                spec_event.argument = arg_list
                spec_service.events.append(spec_event)
            for action in service['actions']:
                spec_action = AIoTSpecAction(
                    spec=action,
                    service=spec_service,
                    in_=action['in']
                )
                in_list: list[AIoTSpecProperty] = []
                for pnnd in action['in']:
                    for prop in spec_service.properties:
                        if prop.nnd == pnnd:
                            in_list.append(prop)
                            break
                spec_action.in_ = in_list
                out_list: list[AIoTSpecProperty] = []
                for pnnd in action['out']:
                    for prop in spec_service.properties:
                        if prop.nnd == pnnd:
                            out_list.append(prop)
                            break
                spec_action.out = out_list
                spec_service.actions.append(spec_action)
            instance.services.append(spec_service)
        return instance

    def dump(self) -> dict:
        return {
            'urn': self.urn,
            'name': self.name,
            'description': self.description,
            'description_trans': self.description_trans,
            'services': [service.dump() for service in self.services]
        }


class AIoTSpecParser:
    """AIoT 规范解析器."""
    _DOMAIN: str = 'aiot_specs'
    _lang: str
    _storage: AIoTStorage
    _entry_data: Optional[dict]
    _main_loop: asyncio.AbstractEventLoop

    _init_done: bool

    def __init__(
            self,
            lang: Optional[str],
            storage: AIoTStorage,
            entry_data: Optional[dict] = None,
            loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        self._lang = lang or DEFAULT_INTEGRATION_LANGUAGE
        self._storage = storage
        self._entry_data = entry_data
        self._main_loop = loop or asyncio.get_running_loop()

        self._init_done = False

    async def init_async(self) -> None:
        if self._init_done is True:
            return
        self._init_done = True

    async def deinit_async(self) -> None:
        self._init_done = False

    async def parse(
            self,
            urn: str,
            skip_cache: bool = False
    ) -> AIoTSpecInstance | None:
        if not skip_cache:
            cache_result = await self.__cache_get(urn=urn)
            if isinstance(cache_result, dict):
                _LOGGER.debug('get from cache, %s', urn)
                return AIoTSpecInstance.load(specs=cache_result)

        # 重试3次
        for index in range(3):
            try:
                return await self.__parse(urn=urn)
            except Exception as err:
                _LOGGER.error('parse error, retry, %d, %s, %s', index, urn, err)
        return None

    async def refresh_async(self, urn_list: list[str]) -> int:
        """MUST await init first !!!"""
        if not urn_list:
            return False
        success_count = 0
        for index in range(0, len(urn_list), 5):
            batch = urn_list[index:index + 5]
            task_list = [
                self._main_loop.create_task(self.parse(urn=urn, skip_cache=True))
                for urn in batch
            ]
            results = await asyncio.gather(*task_list)
            success_count += sum(1 for result in results if result is not None)
        return success_count

    async def __cache_get(self, urn: str) -> Optional[dict]:
        if platform.system() == 'Windows':
            urn = urn.replace(':', '_')
        return await self._storage.load_async(
            domain=self._DOMAIN,
            name=f'{urn}_{self._lang}',
            type_=dict
        )  # type: ignore

    async def __cache_set(
            self,
            urn: str,
            data: dict
    ) -> bool:
        if platform.system() == 'Windows':
            urn = urn.replace(':', '_')
        return await self._storage.save_async(
            domain=self._DOMAIN,
            name=f'{urn}_{self._lang}',
            data=data
        )

    async def __get_instance(self, urn: str) -> dict | None:
        """获取产品实例."""
        if not self._entry_data:
            raise AIoTSpecError('entry data is None')
        urn_strs: list[str] = urn.split(':')
        lan_server = self._entry_data['lan_server']
        res_obj = await AIoTHttp.get_json_async(
            url=f'http://{lan_server}:{HTTP_API_PORT}/api/basic/iot-spec-v1/instance',
            params={
                "productKey": urn_strs[1],
                "skuId": urn_strs[2],
            })

        if 'data' not in res_obj:
            raise AIoTHttpError('invalid response result')
        return res_obj['data']

    async def __parse(self, urn: str) -> AIoTSpecInstance:
        _LOGGER.debug('parse urn, %s', urn)

        # Load spec instance
        instance = await self.__get_instance(urn=urn)
        if not isinstance(instance, dict):
            raise AIoTSpecError(f'invalid urn instance, {urn}')

        # Parse device type
        spec_instance: AIoTSpecInstance = AIoTSpecInstance(
            urn=urn,
            name="hhhhhhhhhh",
            description='bbbbbbbbbbbbbbbb',
            description_trans="tesdfdfjpafjafjpafja"
        )

        urn_service_instance = instance.get('services', [])

        # Parse services
        for service in urn_service_instance:
            if 'type' not in service or 'description' not in service:
                _LOGGER.error('invalid service, %s', service)
                continue

            spec_service: AIoTSpecService = AIoTSpecService(spec=service)
            spec_service.name = spec_service.nnd

            for property_ in service.get('properties', []):
                if 'type' not in property_ or 'description' not in property_ or 'format' not in property_:
                    continue
                property_['description'] = f'{service['description']} | {property_['description']}'
                unit = property_.get('unit', None)
                spec_prop: AIoTSpecProperty = AIoTSpecProperty(
                    spec=property_,
                    service=spec_service,
                    format_=property_['format'],
                    unit=unit if unit != 'none' else None
                )
                spec_prop.name = spec_prop.nnd
                # 为None时则根据format判断平台类型
                spec_prop.platform = self._get_platform(property_)
                # 获取属性组key
                spec_prop.group_key = get_prop_group_key(urn, spec_service.nnd, spec_prop.nnd)

                if 'value-list' in property_:
                    spec_prop.value_list = property_['value-list']
                if 'value-range' in property_:
                    spec_prop.value_range = property_['value-range']

                spec_service.properties.append(spec_prop)
            spec_instance.services.append(spec_service)
        await self.__cache_set(urn=urn, data=spec_instance.dump())
        return spec_instance

    def _get_platform(self, property_: dict) -> str | None:
        """ 获取ha平台类型，取值只有0和1的属性转化为switch布尔型 """
        if property_['format'] in ['enum', 'int_enum'] and 'value-list' in property_ and len(
                property_['value-list']) == 2:
            values = []
            for value_info in property_['value-list']:
                values.append(value_info['value'])
            sort_values = sorted(values)
            if sort_values == [0, 1] or sort_values == ['0', '1']:
                return 'switch'
        return None
