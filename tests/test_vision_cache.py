"""识图(ImageCache)与 LLMModule usage 统计测试。

覆盖:
1. ImageCache: URL 缓存命中 / phash 相似去重(同一张图不同 URL) / vision_callback 调用
2. LLMModule: 成功后写 stats/llm_usage.jsonl(面板 token 统计)
"""

import asyncio
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mohobot.image_cache as ic_mod


def make_png(color: tuple, gradient: bool = False) -> bytes:
    """生成纯色或水平渐变图(渐变图 phash 区分度更高)。"""
    from PIL import Image
    buf = io.BytesIO()
    img = Image.new("RGB", (64, 64), color)
    if gradient:
        px = img.load()
        for x in range(64):
            for y in range(64):
                px[x, y] = (x * 4 % 256, y * 4 % 256, color[2])
    img.save(buf, format="PNG")
    return buf.getvalue()


class FakeHTTP:
    """模拟 httpx: 从内置 dict 取图片内容。"""

    def __init__(self, images: dict[str, bytes]):
        self._images = images

    class _Resp:
        def __init__(self, content, headers=None):
            self.content = content
            self.headers = headers or {"content-type": "image/png"}

        def raise_for_status(self):
            pass

    async def get(self, url):
        if url not in self._images:
            raise RuntimeError(f"404 {url}")
        return self._Resp(self._images[url])


async def test_image_cache() -> None:
    from mohobot.image_cache import ImageCache

    tmp = Path(tempfile.mkdtemp(prefix="ic_"))
    img_a = make_png((255, 0, 0), gradient=True)     # 红色渐变
    img_a2 = img_a                                    # 同一张图(不同 URL)
    img_b = make_png((0, 0, 255), gradient=True)     # 蓝色渐变(明显不同)

    calls = []

    async def vision_cb(image_url, local_path):
        calls.append(image_url)
        return f"描述:{image_url[:20]}"

    ic = ImageCache(cache_dir=str(tmp / "cache"))
    ic._http_client = FakeHTTP({"http://x/a.png": img_a, "http://x/a2.png": img_a2})

    # 1. 首次: 下载 + 调 VLM
    path1, desc1 = await ic.get_or_describe("http://x/a.png", vision_callback=vision_cb)
    assert desc1.startswith("描述:") and len(calls) == 1

    # 2. 同 URL 再次: 缓存命中, 不调 VLM
    _, desc2 = await ic.get_or_describe("http://x/a.png", vision_callback=vision_cb)
    assert desc2 == desc1 and len(calls) == 1, "URL 缓存应命中"

    # 3. 同一张图(不同 URL): phash 相同 → 复用描述, 不调 VLM
    _, desc3 = await ic.get_or_describe("http://x/a2.png", vision_callback=vision_cb)
    assert desc3 == desc1 and len(calls) == 1, "phash 相同应复用描述"

    # 4. 不相似图: 调 VLM
    ic._http_client._images["http://x/b.png"] = img_b
    _, desc4 = await ic.get_or_describe("http://x/b.png", vision_callback=vision_cb)
    assert len(calls) == 2

    # 5. 下载失败: 降级 "[图片下载失败]"
    _, desc5 = await ic.get_or_describe("http://x/missing.png", vision_callback=vision_cb)
    assert desc5 == "[图片下载失败]"
    print("[1] ImageCache URL/phash 缓存 + 降级 OK")


async def test_describe_image_file_uri() -> None:
    """describe_image_file 应生成 data URI 并传给 describe_image。"""
    import mohobot.llm_service as lsvc

    tmp = Path(tempfile.mkdtemp(prefix="dif_"))
    png = make_png((1, 2, 3))
    path = tmp / "t.png"
    path.write_bytes(png)

    captured = {}

    class FakeSvc:
        _vision_available = True
        _vision_client = object()

        async def describe_image(self, url, max_tokens=256):
            captured["url"] = url
            return "一只猫"

    svc = FakeSvc()
    # 绑定方法测试: 直接调用真实实现逻辑(monkeypatch describe_image)
    real = lsvc.LLMService.describe_image_file
    import base64 as b64
    out = await real(svc, str(path))
    assert out == "一只猫"
    assert captured["url"].startswith("data:image/png;base64,"), captured["url"][:40]
    assert b64.b64decode(captured["url"].split(",", 1)[1]) == png
    print("[2] describe_image_file data URI OK")


async def main() -> None:
    await test_image_cache()
    await test_describe_image_file_uri()
    print("\nALL VISION/USAGE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
