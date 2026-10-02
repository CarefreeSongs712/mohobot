"""mohobot 适配层：核心代码只通过这里接触宿主框架。"""

from .components import (
    Image,
    MessageChain,
    MessageEventResult,
    Plain,
    Segment,
    to_onebot_segments,
)
from .config_store import ConfigStore
from .event import HostBridge, RawEvent, StealerEvent
from .log import logger
from .models import EmbeddingProvider, ModelEndpoint, ModelGateway, ModelNotConfigured

__all__ = [
    "ConfigStore",
    "EmbeddingProvider",
    "HostBridge",
    "Image",
    "MessageChain",
    "MessageEventResult",
    "ModelEndpoint",
    "ModelGateway",
    "ModelNotConfigured",
    "Plain",
    "RawEvent",
    "Segment",
    "StealerEvent",
    "logger",
    "to_onebot_segments",
]
