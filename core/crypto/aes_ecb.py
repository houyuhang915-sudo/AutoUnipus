"""AES-128-ECB 零填充解密.

U校园答案接口返回的密文采用零填充(NoPadding),与 PKCS7 不兼容。
pycryptodome 在 ECB 模式下不会自动添加/去除填充,只需手动剥掉尾部零字节。

参考:yuanarcsin/unipus_auto 的 modules/unipus-api.js
"""
from __future__ import annotations

from Crypto.Cipher import AES


_BLOCK_SIZE = 16  # AES-128


def decrypt_aes128_ecb_zero_pad(hex_cipher: str, key_str: str) -> str:
    """AES-128-ECB 解密,密钥为 UTF-8 字符串,密文为 hex 字符串.

    Args:
        hex_cipher: hex 字符串(2 个 hex 字符 = 1 字节)
        key_str:    任意长度,会被截断或用 \\x00 pad 到 16 字节

    Returns:
        解密后的 UTF-8 字符串(已去除尾部 \\x00 填充)
    """
    if not hex_cipher:
        return ""

    key_bytes = key_str.encode("utf-8")
    # 自动 pad / 截断到 16 字节,避免短 key 直接报错
    if len(key_bytes) < _BLOCK_SIZE:
        key_bytes = key_bytes.ljust(_BLOCK_SIZE, b"\x00")
    elif len(key_bytes) > _BLOCK_SIZE:
        key_bytes = key_bytes[:_BLOCK_SIZE]

    cipher_bytes = bytes.fromhex(hex_cipher)
    if len(cipher_bytes) % _BLOCK_SIZE != 0:
        raise ValueError(
            f"密文长度({len(cipher_bytes)})不是 {_BLOCK_SIZE} 的倍数,"
            f"无法 ECB 解密"
        )

    cipher = AES.new(key_bytes, AES.MODE_ECB)
    plain = cipher.decrypt(cipher_bytes)

    # 零填充:剥掉尾部所有 \x00
    plain = plain.rstrip(b"\x00")
    return plain.decode("utf-8", errors="replace")
