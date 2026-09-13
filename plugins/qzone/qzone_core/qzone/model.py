"""QQ 空间会话上下文与 API 响应模型（移植自 astrbot_plugin_qzone_lite）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .constants import QZONE_CODE_OK, QZONE_CODE_UNKNOWN, QZONE_INTERNAL_META_KEY


class QzoneContext:
    """一次登录态: uin + skey + p_skey + 原始 cookies。"""

    def __init__(
        self,
        uin: int,
        skey: str,
        p_skey: str,
        raw_cookies: dict[str, str] | None = None,
    ):
        self.uin = uin
        self.skey = skey
        self.p_skey = p_skey
        self._raw_cookies = raw_cookies or {}

    @property
    def gtk2(self) -> str:
        """g_tk 校验值(由 p_skey 哈希得出)。"""
        hash_val = 5381
        for ch in self.p_skey:
            hash_val += (hash_val << 5) + ord(ch)
        return str(hash_val & 0x7FFFFFFF)

    def cookies(self) -> dict[str, str]:
        o_uin = f"o{self.uin}"
        return {
            "uin": o_uin,
            "skey": self.skey,
            "p_skey": self.p_skey,
            "pt2gguin": self._raw_cookies.get("pt2gguin", o_uin),
            "p_uin": self._raw_cookies.get("p_uin", o_uin),
            "ptcz": self._raw_cookies.get("ptcz", ""),
            "RK": self._raw_cookies.get("RK", ""),
            "pt4_token": self._raw_cookies.get("pt4_token", ""),
            "pt_recent_uins": self._raw_cookies.get("pt_recent_uins", ""),
            "qzone_check": self._raw_cookies.get("qzone_check", ""),
        }

    def headers(self) -> dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
            ),
            "referer": f"https://user.qzone.qq.com/{self.uin}",
            "origin": "https://user.qzone.qq.com",
            "Host": "user.qzone.qq.com",
            "Connection": "keep-alive",
        }


@dataclass(slots=True)
class ApiResponse:
    ok: bool
    code: int
    message: str | None
    data: dict[str, Any]
    raw: dict[str, Any]

    @classmethod
    def from_raw(
        cls,
        raw: dict[str, Any],
        *,
        code_key: str = "code",
        msg_key: str | tuple[str, ...] = ("message", "msg"),
        data_key: str | None = None,
        success_code: int = QZONE_CODE_OK,
    ) -> "ApiResponse":
        code = raw.get(code_key, QZONE_CODE_UNKNOWN)

        message = None
        if isinstance(msg_key, tuple):
            for k in msg_key:
                if raw.get(k):
                    message = raw.get(k)
                    break
        else:
            message = raw.get(msg_key) or raw.get("data", {}).get(msg_key) or code

        if code == success_code:
            if data_key is None:
                data = dict(raw)
                data.pop(QZONE_INTERNAL_META_KEY, None)
            else:
                data = raw.get(data_key, {})
            return cls(ok=True, code=code, message=None, data=data, raw=raw)

        return cls(ok=False, code=code, message=message, data={}, raw=raw)
