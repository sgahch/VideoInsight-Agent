"""生成项目 Logo：深蓝黑渐变圆角方块 + 金色播放三角 + '视' 字。"""
from PIL import Image, ImageDraw, ImageFilter, ImageFont

SIZE = 1024
FONT_PATH = "C:/Windows/Fonts/msyhbd.ttc"


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def make_logo(path):
    # 1) 渐变背景（对角：深蓝黑 -> 墨蓝）
    img = Image.new("RGB", (SIZE, SIZE))
    px = img.load()
    top = (11, 15, 23)      # #0b0f17
    bottom = (30, 41, 59)   # #1e293b
    for y in range(SIZE):
        t = y / (SIZE - 1)
        color = lerp(top, bottom, t)
        for x in range(SIZE):
            px[x, y] = color

    # 2) 圆角遮罩
    mask = Image.new("L", (SIZE, SIZE), 0)
    md = ImageDraw.Draw(mask)
    radius = 180
    md.rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=radius, fill=255)
    img.putalpha(mask)

    # 3) 金色光晕（播放三角背后的辉光）
    glow_layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow_layer)
    cx, cy, cr = SIZE // 2, SIZE // 2 - 20, 300
    for r in range(cr, 60, -8):
        alpha = int(30 * (1 - r / cr))
        gd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(245, 158, 11, alpha))
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(30))
    img = Image.alpha_composite(img, glow_layer)

    # 4) 金色播放三角
    overlay = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    # 三角顶点（正三角形，居中偏上）
    tri = [(cx - 160, cy - 150), (cx - 160, cy + 150), (cx + 220, cy)]
    od.polygon(tri, fill=(245, 158, 11, 255))
    # 三角高光：内部稍亮的小三角
    tri_hl = [(cx - 110, cy - 100), (cx - 110, cy + 100), (cx + 150, cy)]
    od.polygon(tri_hl, fill=(251, 191, 36, 220))
    img = Image.alpha_composite(img, overlay)

    # 5) 底部金色文字「视界通」
    try:
        font = ImageFont.truetype(FONT_PATH, 96)
    except Exception:
        font = ImageFont.load_default()
    text = "视界通"
    text_layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    td = ImageDraw.Draw(text_layer)
    bbox = td.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = (SIZE - tw) // 2 - bbox[0]
    ty = cy + 260 - bbox[1]
    td.text((tx, ty), text, font=font, fill=(245, 158, 11, 255))
    img = Image.alpha_composite(img, text_layer)

    # 6) 内圈描边（提升质感）
    border = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    bd = ImageDraw.Draw(border)
    bd.rounded_rectangle([8, 8, SIZE - 9, SIZE - 9], radius=radius - 6,
                         outline=(245, 158, 11, 90), width=6)
    img = Image.alpha_composite(img, border)

    img.save(path, "PNG")
    print(f"logo saved: {path} ({img.size[0]}x{img.size[1]})")


if __name__ == "__main__":
    make_logo("docs/competition/logo.png")
