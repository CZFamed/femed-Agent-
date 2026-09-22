"""Vault / KMS 加密接口（契约 §6 `credentials.encrypted_payload`）。

派工单 §3 W1-A3-8 的三条硬约束，都在本模块里落地：

1. **业务层只见句柄不见明文** —— 链路上流动的是 `TokenHandle`；
   明文只在 `VaultClient.decrypt()` 返回的那一瞬间存在。
2. **密文入库** —— `credentials.encrypted_payload` 存的是本模块产出的信封格式密文。
3. **日志永不含 Token** —— `redact()` 与 `RedactingFilter` 提供兜底，
   单测里有"日志不含明文"的断言守着（这是 A3 的头号事故源）。

`InMemoryVault` 是**假 Vault**：用 stdlib 的 HMAC-SHA256 构造密钥流 + 认证标签，
让单测能验证"往返一致、篡改可发现、密文里搜不到明文"三件事。
**它不是生产级加密**（没有密钥托管、没有 HSM、没有审计），
生产必须注入真实 Vault/KMS 客户端（`VaultClient` 协议不变）。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from typing import Iterable, Protocol, runtime_checkable

from pulse.services.identity.errors import VaultError

#: 信封标识与结构：`PLV1 | key_version(1) | nonce(16) | ciphertext | tag(32)`
_MAGIC = b"PLV1"
_NONCE_LEN = 16
_TAG_LEN = 32
_BODY_PREFIX_LEN = len(_MAGIC) + 1 + _NONCE_LEN
_MIN_BLOB_LEN = _BODY_PREFIX_LEN + _TAG_LEN

#: 当前密钥版本（rotate 后递增）
DEFAULT_KEY_VERSION = 1


@dataclass(frozen=True, slots=True)
class TokenHandle:
    """Vault 句柄：指向"某账号的某版本的密钥"。**绝不包含明文。**

    `str()` / `repr()` 只输出账号 ID 与密钥版本，因此可以安全入日志。
    """

    account_id: str
    key_version: int = DEFAULT_KEY_VERSION

    def __str__(self) -> str:
        return f"<TokenHandle {self.account_id} v{self.key_version}>"

    __repr__ = __str__


@runtime_checkable
class VaultClient(Protocol):
    """Vault/KMS 客户端协议（部署层注入真实实现）。"""

    def encrypt(self, handle: TokenHandle, plaintext: bytes) -> bytes:
        """加密明文，返回可入库的密文信封。"""

    def decrypt(self, handle: TokenHandle, ciphertext: bytes) -> bytes:
        """解密信封；密文被篡改或密钥不匹配时**必须**报错。"""

    def rotate(self, handle: TokenHandle, ciphertext: bytes) -> tuple[TokenHandle, bytes]:
        """换用新版本密钥重新加密，返回 `(新句柄, 新密文)`。"""


def _xor(data: bytes, mask: bytes) -> bytes:
    """等长异或（两边长度必须一致）。"""

    return bytes(a ^ b for a, b in zip(data, mask))


class InMemoryVault:
    """内存假 Vault（单测与本地开发用）。

    Args:
        master_key: 主密钥；缺省随机生成。测试里传固定值以保证可复现。
    """

    def __init__(self, master_key: bytes | None = None) -> None:
        self._master: bytes = master_key or secrets.token_bytes(32)
        self._derived: dict[int, tuple[bytes, bytes]] = {}

    # -- 密钥派生 -----------------------------------------------------------

    def _keys(self, version: int) -> tuple[bytes, bytes]:
        """返回 `(加密密钥, 认证密钥)`；按版本缓存。"""

        if version < 1:
            raise VaultError(f"密钥版本非法：{version}")
        cached = self._derived.get(version)
        if cached is not None:
            return cached

        suffix = version.to_bytes(4, "big")
        enc = hmac.new(self._master, b"pulse-envelope-v1" + suffix, hashlib.sha256).digest()
        mac = hmac.new(self._master, b"pulse-tag-v1" + suffix, hashlib.sha256).digest()
        self._derived[version] = (enc, mac)
        return enc, mac

    @staticmethod
    def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
        """HMAC-SHA256 计数器模式密钥流。"""

        out = bytearray()
        counter = 0
        while len(out) < length:
            out += hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
            counter += 1
        return bytes(out[:length])

    # -- VaultClient 实现 ---------------------------------------------------

    def encrypt(self, handle: TokenHandle, plaintext: bytes) -> bytes:
        """加密；空明文也允许（会产出一个合法的空载荷信封）。"""

        enc_key, mac_key = self._keys(handle.key_version)
        nonce = secrets.token_bytes(_NONCE_LEN)
        body = bytes([handle.key_version]) + nonce + _xor(
            plaintext, self._keystream(enc_key, nonce, len(plaintext))
        )
        tag = hmac.new(mac_key, body, hashlib.sha256).digest()
        return _MAGIC + body + tag

    def decrypt(self, handle: TokenHandle, ciphertext: bytes) -> bytes:
        """解密；任何异常路径都抛 `VaultError`，且错误消息不含明文。"""

        if len(ciphertext) < _MIN_BLOB_LEN or not ciphertext.startswith(_MAGIC):
            raise VaultError("密文格式非法：不是本 Vault 产出的信封（拒绝解密）")

        body = ciphertext[len(_MAGIC) : -_TAG_LEN]
        tag = ciphertext[-_TAG_LEN:]
        version = body[0]
        nonce = body[1 : 1 + _NONCE_LEN]
        payload = body[1 + _NONCE_LEN :]

        if version != handle.key_version:
            raise VaultError(
                f"密钥版本不匹配：密文为 v{version}，句柄为 v{handle.key_version}"
                "（请先用 rotate() 轮换后再解密）"
            )

        enc_key, mac_key = self._keys(version)
        expected = hmac.new(mac_key, body, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            raise VaultError("密文认证失败：数据被篡改或密钥不正确")

        return _xor(payload, self._keystream(enc_key, nonce, len(payload)))

    def rotate(self, handle: TokenHandle, ciphertext: bytes) -> tuple[TokenHandle, bytes]:
        """密钥轮换：解出明文后用 v+1 重新加密，返回新的句柄与密文。"""

        plaintext = self.decrypt(handle, ciphertext)
        new_handle = TokenHandle(handle.account_id, handle.key_version + 1)
        return new_handle, self.encrypt(new_handle, plaintext)


# --------------------------------------------------------------------------
# 日志兜底：Token 永不落日志
# --------------------------------------------------------------------------

#: 替换明文时使用的占位符
REDACTED = "«redacted»"


def redact(text: str, secrets: Iterable[str]) -> str:
    """把文本里出现的敏感串替换成占位符。

    Args:
        text: 待清洗文本。
        secrets: 敏感串集合。空串会被忽略（否则会污染整段文本）。
    """

    out = text
    for secret in secrets:
        if secret:
            out = out.replace(secret, REDACTED)
    return out


class RedactingFilter(logging.Filter):
    """日志过滤器：命中敏感串时替换成 `«redacted»`，并**不阻断**日志。

    用法（部署层）：把凭证明文注册进来，

        flt = RedactingFilter()
        flt.add_secret(token)
        logging.getLogger("pulse.identity").addFilter(flt)
    """

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets: set[str] = {s for s in secrets if s}

    def add_secret(self, secret: str | None) -> None:
        """登记一个需要清洗的敏感串。"""

        if secret:
            self._secrets.add(secret)

    def discard_secret(self, secret: str | None) -> None:
        """注销敏感串（凭据轮换后旧值可移除）。"""

        self._secrets.discard(secret or "")

    @property
    def secrets_registered(self) -> int:
        """已登记的敏感串数量（仅供测试断言，不暴露内容）。"""

        return len(self._secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        """清洗 `record.msg` 与 `record.args`，永远返回 True。"""

        if not self._secrets:
            return True
        secrets = tuple(self._secrets)

        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - 兜底：格式化失败时不阻断日志
            message = str(record.msg)
        record.msg = redact(message, secrets)
        record.args = ()

        if record.exc_text:
            record.exc_text = redact(record.exc_text, secrets)
        return True


__all__ = [
    "DEFAULT_KEY_VERSION",
    "REDACTED",
    "InMemoryVault",
    "RedactingFilter",
    "TokenHandle",
    "VaultClient",
    "redact",
]
