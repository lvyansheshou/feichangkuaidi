"""TCP 帧编解码（协议 §1）。

帧格式：5 位十进制长度前缀（body 的 UTF-8 字节数，≤99999）+ UTF-8 JSON body。
接收端必须处理半包（缓存至完整）、粘包（按前缀循环拆多条）、中文跨包（先按字节缓存再 UTF-8 解码）。
消息边界只按长度前缀判断，不能按换行/read 次数判断。

容错：服务端偶发未引号数字键名（如 {1001:"MOVE"}），正则兜底修复后重试解析。
"""

import json
import re

import config

# 匹配未引号数字键名: { 或 , 之后、: 之前的裸数字
_UNQUOTED_NUMERIC_KEY = re.compile(r'([{,])(\s*)(-?\d+)(\s*:)')


def _repair_unquoted_keys(text: str) -> str:
    """将 {1001:"MOVE"} 修复为 {"1001":"MOVE"}，只处理裸数字键名。"""
    return _UNQUOTED_NUMERIC_KEY.sub(r'\1\2"\3"\4', text)


class FramingError(Exception):
    """帧层不可恢复错误（长度前缀非法或 JSON 解析失败），意味着字节流失步。"""


def encode_frame(envelope):
    """把消息信封 dict 序列化为 `5位长度前缀 + UTF-8 body` 字节串。

    envelope 形如 {"msg_name": ..., "msg_data": {...}}。
    """
    body = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > config.MAX_FRAME_BODY_BYTES:
        raise FramingError(
            "body too large: %d > %d bytes" % (len(body), config.MAX_FRAME_BODY_BYTES)
        )
    prefix = str(len(body)).zfill(config.LENGTH_PREFIX_WIDTH).encode("ascii")
    return prefix + body


class FrameDecoder:
    """按长度前缀从字节流增量拆帧。

    用法：
        decoder.feed(chunk)              # 喂入任意长度字节
        for envelope in decoder.frames() # 取出当前已完整的所有帧（dict）
    半包会保留在内部缓冲，等待后续 feed。
    """

    _W = config.LENGTH_PREFIX_WIDTH

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data):
        if data:
            self._buf.extend(data)

    def frames(self):
        while True:
            if len(self._buf) < self._W:
                return  # 连长度前缀都没收齐
            prefix = bytes(self._buf[: self._W])
            if not prefix.isdigit():
                raise FramingError("invalid length prefix: %r" % prefix)
            length = int(prefix)
            total = self._W + length
            if len(self._buf) < total:
                return  # body 未收齐（半包）
            body = bytes(self._buf[self._W : total])
            del self._buf[:total]
            text = body.decode("utf-8")
            try:
                yield json.loads(text)
            except (UnicodeDecodeError, json.JSONDecodeError):
                # 兜底：服务端偶发未引号数字键名 {1001:"MOVE"} → {"1001":"MOVE"}
                try:
                    repaired = _repair_unquoted_keys(text)
                    yield json.loads(repaired)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise FramingError("bad frame body: %s" % exc) from exc

    @property
    def pending_bytes(self):
        """当前缓冲中尚未组成完整帧的字节数（用于诊断）。"""
        return len(self._buf)
