"""Coil Reclaim 24/7 盯盘机器人。

把 `coil_reclaim.pine` 发出的 `coil-reclaim/v1` 数据包接进来，
按 agent/AGENT_PROMPT.md 的规则逐条复核，给出 LONG / SHORT / SKIP，
算好仓位，发到手机，记进本地库。
"""

from .models import SCHEMA, Packet, PacketError, parse_packet  # noqa: F401
from .judge import judge  # noqa: F401

__version__ = "1.0.0"
__all__ = ["SCHEMA", "Packet", "PacketError", "parse_packet", "judge", "__version__"]
