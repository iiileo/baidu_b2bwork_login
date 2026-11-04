from __future__ import annotations

import time
from io import BytesIO
from pathlib import Path

from src.logbus import log_bus

# 解码时最长边；过大时 pyzbar 会非常慢
_MAX_SIDE = 360


def decode_qr_from_image(path: str | Path) -> str | None:
    """从文件解码二维码。"""
    path = Path(path)
    if not path.exists():
        return None
    try:
        return decode_qr_from_bytes(path.read_bytes())
    except Exception as exc:  # noqa: BLE001
        log_bus.emit(f"二维码读文件失败: {exc}")
        return None


def decode_qr_from_bytes(raw: bytes) -> str | None:
    """从 PNG/JPEG bytes 解码；先缩小再识别，避免全页截图卡死。"""
    if not raw:
        return None
    t0 = time.perf_counter()
    try:
        from PIL import Image

        img = Image.open(BytesIO(raw))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        w, h = img.size
        scale = min(1.0, _MAX_SIDE / max(w, h))
        if scale < 1.0:
            img = img.resize(
                (max(1, int(w * scale)), max(1, int(h * scale))),
                Image.Resampling.BILINEAR,
            )
        text = _decode_pil(img)
        ms = (time.perf_counter() - t0) * 1000
        log_bus.emit(
            f"二维码解码耗时={ms:.0f}ms size={w}x{h} "
            f"scaled={img.size[0]}x{img.size[1]} ok={bool(text)}"
        )
        return text
    except Exception as exc:  # noqa: BLE001
        ms = (time.perf_counter() - t0) * 1000
        log_bus.emit(f"二维码解码异常耗时={ms:.0f}ms err={exc}")
        return None


def _decode_pil(img: "Image.Image") -> str | None:  # type: ignore[name-defined]
    # zxing-cpp 更快更准（若已安装）
    try:
        import zxingcpp  # type: ignore

        results = zxingcpp.read_barcodes(img)
        for r in results:
            text = getattr(r, "text", None) or str(r)
            if text:
                return str(text).strip()
    except Exception:  # noqa: BLE001
        pass

    try:
        from pyzbar.pyzbar import decode as pyzbar_decode

        for r in pyzbar_decode(img):
            data = r.data.decode("utf-8", errors="ignore").strip()
            if data:
                return data
    except Exception:  # noqa: BLE001
        pass

    return None
