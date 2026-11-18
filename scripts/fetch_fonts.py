"""下载剪辑 v2 的字幕 / 大字字体:Noto Sans SC Black(思源黑体同源,SIL Open Font License 1.1,可商用)。

    python scripts/fetch_fonts.py            # 下到 data/fonts/NotoSansSC-Black.otf(已有就跳过)
    python scripts/fetch_fonts.py --force    # 重新下载

来源是 Google / Adobe 维护的 noto-cjk 仓库(SubsetOTF/SC:只含简中字形,约 8MB);
许可证文本一起存到 data/fonts/LICENSE-NotoSansCJK.txt。下好后不用配 `EDIT_V2_FONT_FILE`,
`edit.render.font_file()` 默认就找这个文件。
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402

BASE = "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans"
FONT_URL = f"{BASE}/SubsetOTF/SC/NotoSansSC-Black.otf"
LICENSE_URL = f"{BASE}/LICENSE"
LICENSE_NAME = "LICENSE-NotoSansCJK.txt"
#: OpenType(CFF)文件头
OTF_MAGIC = b"OTTO"
MIN_BYTES = 1_000_000


def _get(url: str, timeout: float = 120.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "font-fetch/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 —— 固定的 https 地址
        return resp.read()


def _atomic_write(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, dest)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true", help="已有也重新下载")
    args = ap.parse_args()
    dest = config.edit_font_default()
    if dest.is_file() and not args.force:
        print(f"已有:{dest}")
        return 0
    print(f"下载 {FONT_URL}")
    data = _get(FONT_URL)
    if not data.startswith(OTF_MAGIC) or len(data) < MIN_BYTES:
        print(f"下到的不是 OTF 字体({len(data)} 字节),没有写入", file=sys.stderr)
        return 1
    license_text = _get(LICENSE_URL, timeout=30.0)
    if b"SIL OPEN FONT LICENSE" not in license_text.upper():
        print("许可证文本对不上(不是 SIL OFL),没有写入", file=sys.stderr)
        return 1
    _atomic_write(dest.parent / LICENSE_NAME, license_text)
    _atomic_write(dest, data)
    print(f"好了:{dest}({len(data) / 1e6:.1f} MB),许可证 {dest.parent / LICENSE_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
