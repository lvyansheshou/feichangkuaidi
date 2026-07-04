"""日志压缩与关键信息提取系统。

模块结构:
    datatypes   — FrameRecord / MatchLog 数据容器
    parser      — LogParser: 文本日志 → 结构化 MatchLog
    compressor  — LogCompressor: digest / compact / archive 三种模式
    restorer    — DigestRestorer: 从 digest JSON 还原 MatchLog

用法:
    from log_compressor import LogParser, LogCompressor, DigestRestorer

    # 解析日志
    ml = LogParser.parse_file("match.log")

    # 压缩
    c = LogCompressor("logs")
    digest = c.digest("logs/match.log")

    # 批量
    c.batch_digest()

    # 还原
    matches = DigestRestorer.batch_restore("logs/digests")
"""

from .datatypes import FrameRecord, MatchLog
from .parser import LogParser
from .compressor import LogCompressor
from .restorer import DigestRestorer

__all__ = [
    "FrameRecord",
    "MatchLog",
    "LogParser",
    "LogCompressor",
    "DigestRestorer",
]
