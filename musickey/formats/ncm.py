"""网易云音乐 NCM 流式解密器。"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import BinaryIO

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from ..ciphers import ncm_stream_bytes, xor_ncm_file
from ..errors import FormatError
from ..logging_setup import get_logger
from ..model import KNOWN_CONTAINERS, DecodeResult, sniff_container
from .base import DecodeOptions, Decoder

logger = get_logger("musickey.formats.ncm")

MAGIC = b"CTENFDAM"
CORE_KEY = b"hzHRAmso5kInbaxW"
META_KEY = b"#14ljk_!\\]&0U<'("
META_PREFIX = b"163 key(Don't modify):"


class NcmDecoder(Decoder):
    name = "网易云音乐 NCM"
    extensions = frozenset({".ncm"})

    def decode_to(
        self,
        path: Path,
        out: BinaryIO,
        opts: DecodeOptions,
        progress=None,
    ) -> DecodeResult:
        if path.stat().st_size < 0x2C:
            raise FormatError(f"{path.name}: 文件过短，不是合法的 NCM")

        with path.open("rb") as fp:
            if fp.read(len(MAGIC)) != MAGIC:
                raise FormatError(f"{path.name}: 不是合法的 NCM 文件")
            fp.seek(10)

            key_len = int.from_bytes(fp.read(4), "little")
            if key_len <= 0 or key_len > 1 << 20:
                raise FormatError(f"{path.name}: NCM 密钥长度非法")
            key_blob = bytes(value ^ 0x64 for value in fp.read(key_len))
            try:
                key_plain = unpad(AES.new(CORE_KEY, AES.MODE_ECB).decrypt(key_blob), 16)
                rc4_key = key_plain[17:]
            except Exception as exc:
                raise FormatError(f"{path.name}: NCM 核心密钥解密失败") from exc
            if not rc4_key:
                raise FormatError(f"{path.name}: NCM 核心密钥为空")

            meta_len = int.from_bytes(fp.read(4), "little")
            meta_blob = b""
            if meta_len:
                if meta_len > 64 << 20:
                    raise FormatError(f"{path.name}: NCM 元数据过大")
                meta_blob = bytes(value ^ 0x63 for value in fp.read(meta_len))

            tags, meta_format = self._parse_meta(meta_blob)

            fp.read(5)  # 固定间隙
            cover: bytes | None = None
            cover_header = fp.read(8)
            if len(cover_header) == 8:
                image_space = int.from_bytes(cover_header[:4], "little")
                image_size = int.from_bytes(cover_header[4:], "little")
                if image_size and image_size <= 128 << 20:
                    cover = fp.read(image_size)
                fp.seek(image_space - image_size, 1)
            audio_offset = fp.tell()

            audio_total = path.stat().st_size - audio_offset
            logger.info(
                "NCM 解析: %s 总大小=%.2f MB 密钥=%d 元数据=%d 封面=%s 音频偏移=%d 音频=%.2f MB",
                path.name,
                path.stat().st_size / (1024 * 1024),
                key_len,
                len(meta_blob),
                f"{len(cover) / (1024 * 1024):.2f} MB" if cover else "无",
                audio_offset,
                max(0, audio_total) / (1024 * 1024),
            )
            fp.seek(audio_offset)
            box = ncm_stream_bytes(rc4_key)
            first = fp.read(64)
            if not first:
                raise FormatError(f"{path.name}: 音频流为空")
            first_decoded = bytes(
                value ^ box[(index + 1) & 0xFF] for index, value in enumerate(first)
            )
            container = meta_format if meta_format in KNOWN_CONTAINERS else sniff_container(first_decoded)
            out.write(first_decoded)
            xor_ncm_file(fp, audio_total, out, box, start_offset=len(first), progress=progress)

        return DecodeResult(
            container=container,
            tags=tags,
            cover=cover,
            source="ncm",
        )

    @staticmethod
    def _parse_meta(blob: bytes) -> tuple[dict[str, str], str]:
        if not blob.startswith(META_PREFIX):
            return {}, ""
        try:
            payload = base64.b64decode(blob[len(META_PREFIX) :])
            plain = unpad(AES.new(META_KEY, AES.MODE_ECB).decrypt(payload), 16)
            if not plain.startswith(b"music:"):
                return {}, ""
            data = json.loads(plain[6:].decode("utf-8", "replace"))
        except Exception:
            return {}, ""
        tags: dict[str, str] = {}
        if isinstance(data.get("musicName"), str):
            tags["title"] = data["musicName"]
        artists = data.get("artist") or []
        if isinstance(artists, list) and artists and isinstance(artists[0], (list, tuple)):
            if artists[0]:
                tags["artist"] = str(artists[0][0])
        if isinstance(data.get("album"), str):
            tags["album"] = data["album"]
        return tags, str(data.get("format") or "").lower()
