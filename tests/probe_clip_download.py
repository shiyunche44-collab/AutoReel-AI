"""探测:上一步拿到的直链能不能真的下载下来。

CDN 普遍校验 Referer —— 只带默认 httpx UA 会被 403。这里要确认的是
"带了 Referer 就能下",因为那决定 V2 来源成不成立。

用法:python -m tests.probe_clip_download <直链> [<直链> ...]
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import httpx

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def probe(url: str) -> tuple[bool, str]:
    origin = f"{url.split('/')[0]}//{url.split('/')[2]}"
    headers = {**UA, "Referer": origin + "/"}
    try:
        with httpx.stream("GET", url, headers=headers, timeout=30,
                          follow_redirects=True) as resp:
            if resp.status_code != 200:
                return False, f"HTTP {resp.status_code}"
            ctype = resp.headers.get("content-type", "")
            length = int(resp.headers.get("content-length") or 0)
            dest = Path(tempfile.gettempdir()) / "probe_clip.mp4"
            written = 0
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes(chunk_size=1 << 16):
                    fh.write(chunk)
                    written += len(chunk)
                    if written > 3_000_000:   # 只要确认能下,不用下全
                        break
            ok = written > 100_000
            return ok, f"{ctype} 实收 {written // 1024}KB / 声明 {length // 1024}KB"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def main() -> int:
    urls = sys.argv[1:]
    if not urls:
        print("用法:python -m tests.probe_clip_download <直链> ...")
        return 2
    for url in urls:
        ok, info = probe(url)
        print(f"{'OK  ' if ok else 'FAIL'} {info}\n     {url[:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
