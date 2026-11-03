from __future__ import annotations

from pathlib import Path


def decode_qr_from_image(path: str | Path) -> str | None:
    """尝试从图片解码二维码；失败返回 None。"""
    path = Path(path)
    if not path.exists():
        return None

    # 优先 zxing-cpp
    try:
        import zxingcpp  # type: ignore
        from PIL import Image

        img = Image.open(path)
        results = zxingcpp.read_barcodes(img)
        for r in results:
            text = getattr(r, "text", None) or str(r)
            if text:
                return str(text).strip()
    except Exception:  # noqa: BLE001
        pass

    # 回退 pyzbar
    try:
        from PIL import Image
        from pyzbar.pyzbar import decode as pyzbar_decode

        img = Image.open(path)
        for r in pyzbar_decode(img):
            data = r.data.decode("utf-8", errors="ignore").strip()
            if data:
                return data
    except Exception:  # noqa: BLE001
        pass

    return None
