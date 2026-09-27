"""生成界面资产: 从原图裁剪遥控器正面并抠除背景 -> app/assets/remote{A,B}.png

用法: python -X utf8 prep_assets.py <G20款正面图.jpg> <AppleTV款图.jpg>
原图不随仓库分发, 请自备两张遥控器商品图。
"""
import os
import sys
from collections import deque

import numpy as np
from PIL import Image

if len(sys.argv) != 3:
    sys.exit("用法: python -X utf8 prep_assets.py <G20款正面图.jpg> <AppleTV款图.jpg>")

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "assets")
os.makedirs(OUT, exist_ok=True)


def flood_bg(img, is_bg):
    """从边缘 BFS 标记背景像素(仅连通区域), 抠除后 alpha=0。"""
    h, w = img.shape[:2]
    rgb = img[:, :, :3].astype(int)
    bg = np.zeros((h, w), bool)
    dq = deque()
    for x in range(w):
        for y in (0, h - 1):
            if not bg[y, x] and is_bg(rgb[y, x]):
                bg[y, x] = True
                dq.append((y, x))
    for y in range(h):
        for x in (0, w - 1):
            if not bg[y, x] and is_bg(rgb[y, x]):
                bg[y, x] = True
                dq.append((y, x))
    while dq:
        y, x = dq.popleft()
        for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
            if 0 <= ny < h and 0 <= nx < w and not bg[ny, nx] and is_bg(rgb[ny, nx]):
                bg[ny, nx] = True
                dq.append((ny, nx))
    img[bg, 3] = 0
    return img


# --- G20 款: 白底, 正面在左半 ---
img = Image.open(sys.argv[1]).convert("RGBA")
crop = np.array(img.crop((112, 42, 340, 758)))
crop = flood_bg(crop, lambda p: p[0] > 195 and p[1] > 195 and p[2] > 195)
Image.fromarray(crop).save(os.path.join(OUT, "remoteA.png"))
print("remoteA:", crop.shape)

# --- Apple TV 款: 蓝底, 正面在左二 ---
img = Image.open(sys.argv[2]).convert("RGBA")
crop = np.array(img.crop((222, 118, 462, 800)))
crop = flood_bg(crop, lambda p: (int(p[2]) - max(int(p[0]), int(p[1]))) > 18)
Image.fromarray(crop).save(os.path.join(OUT, "remoteB.png"))
print("remoteB:", crop.shape)
