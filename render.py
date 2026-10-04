"""ساخت تصویر میز پوکر با Pillow (بدون نیاز به فونت فارسی: بازیکن‌ها با شماره صندلی نمایش داده می‌شن)."""
import io
import math
from PIL import Image, ImageDraw, ImageFont

from poker import rank_str

W, H = 900, 600
RED = (200, 30, 40)
BLACK = (25, 25, 30)


def font(size):
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
                 "DejaVuSans-Bold.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def text_c(d, xy, s, f, fill):
    box = d.textbbox((0, 0), s, font=f)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text((xy[0] - w / 2 - box[0], xy[1] - h / 2 - box[1]), s, font=f, fill=fill)


def draw_suit(d, suit, cx, cy, s, color):
    r = s * 0.28
    if suit == "d":
        d.polygon([(cx, cy - s * .5), (cx + s * .36, cy), (cx, cy + s * .5), (cx - s * .36, cy)], fill=color)
    elif suit == "h":
        d.ellipse([cx - 2 * r, cy - 1.4 * r, cx, cy + .6 * r], fill=color)
        d.ellipse([cx, cy - 1.4 * r, cx + 2 * r, cy + .6 * r], fill=color)
        d.polygon([(cx - 1.95 * r, cy - .1 * r), (cx + 1.95 * r, cy - .1 * r), (cx, cy + 1.9 * r)], fill=color)
    elif suit == "s":
        d.ellipse([cx - 2 * r, cy - .6 * r, cx, cy + 1.4 * r], fill=color)
        d.ellipse([cx, cy - .6 * r, cx + 2 * r, cy + 1.4 * r], fill=color)
        d.polygon([(cx - 1.95 * r, cy + .1 * r), (cx + 1.95 * r, cy + .1 * r), (cx, cy - 1.9 * r)], fill=color)
        d.polygon([(cx, cy + .6 * r), (cx - .7 * r, cy + 2 * r), (cx + .7 * r, cy + 2 * r)], fill=color)
    else:  # club
        d.ellipse([cx - .9 * r, cy - 2 * r, cx + .9 * r, cy - .2 * r], fill=color)
        d.ellipse([cx - 1.9 * r, cy - .5 * r, cx - .1 * r, cy + 1.3 * r], fill=color)
        d.ellipse([cx + .1 * r, cy - .5 * r, cx + 1.9 * r, cy + 1.3 * r], fill=color)
        d.polygon([(cx, cy + .2 * r), (cx - .7 * r, cy + 2 * r), (cx + .7 * r, cy + 2 * r)], fill=color)


def draw_card(d, x, y, w, h, card=None):
    if card is None:  # پشت کارت
        d.rounded_rectangle([x, y, x + w, y + h], 6, fill=(30, 60, 150), outline=(230, 230, 240), width=2)
        d.rounded_rectangle([x + 5, y + 5, x + w - 5, y + h - 5], 4, outline=(110, 140, 220), width=2)
        return
    d.rounded_rectangle([x, y, x + w, y + h], 6, fill=(250, 250, 250), outline=(60, 60, 60), width=2)
    col = RED if card[1] in "hd" else BLACK
    f = font(int(h * 0.3))
    text_c(d, (x + w * 0.27, y + h * 0.2), rank_str(card[0]), f, col)
    draw_suit(d, card[1], x + w * 0.5, y + h * 0.62, h * 0.38, col)


def seat_positions(n):
    cx, cy, rx, ry = W / 2, H / 2 + 5, 365, 235
    pos = []
    for i in range(n):
        a = math.pi / 2 + 2 * math.pi * i / n  # صندلی ۱ پایین، ساعت‌گرد
        pos.append((cx + rx * math.cos(a), cy + ry * math.sin(a)))
    return pos


def render_table(t, reveal=False):
    img = Image.new("RGB", (W, H), (14, 20, 18))
    d = ImageDraw.Draw(img)
    d.ellipse([70, 70, W - 70, H - 70], fill=(92, 58, 30))
    d.ellipse([90, 90, W - 90, H - 90], fill=(28, 108, 68))
    d.ellipse([130, 130, W - 130, H - 130], outline=(40, 135, 88), width=3)

    f_big, f_mid, f_sm = font(30), font(22), font(18)
    cx = W // 2
    text_c(d, (cx, 205), f"POT {t.pot()}", f_big, (255, 220, 90))

    cw, ch = 70, 98
    gap = 12
    x0 = cx - (5 * cw + 4 * gap) / 2
    for i in range(5):
        x = x0 + i * (cw + gap)
        if i < len(t.board):
            draw_card(d, x, 245, cw, ch, t.board[i])
        else:
            d.rounded_rectangle([x, 245, x + cw, 245 + ch], 6, outline=(60, 150, 105), width=2)

    n = len(t.players)
    show_all = reveal or t.stage == "showdown"
    for i, (p, (sx, sy)) in enumerate(zip(t.players, seat_positions(n))):
        folded = p.folded
        active = (i == t.to_act and t.state == "hand")
        bw, bh = 150, 62
        x, y = sx - bw / 2, sy - bh / 2
        fill = (40, 40, 46) if folded else (22, 30, 44)
        outline = (255, 210, 60) if active else ((90, 90, 95) if folded else (150, 170, 200))
        d.rounded_rectangle([x, y, x + bw, y + bh], 12, fill=fill, outline=outline, width=4 if active else 2)
        label = f"#{i + 1}"
        if i == t.dealer:
            d.ellipse([x - 12, y - 12, x + 20, y + 20], fill=(250, 250, 250), outline=(0, 0, 0), width=2)
            text_c(d, (x + 4, y + 4), "D", f_sm, (0, 0, 0))
        dim = (130, 130, 135)
        text_c(d, (sx - 38, sy - 12), label, f_mid, dim if folded else (255, 255, 255))
        text_c(d, (sx + 25, sy - 12), str(p.chips), f_mid, dim if folded else (120, 230, 150))
        status = "FOLD" if folded else ("ALL-IN" if p.allin else "")
        if status:
            text_c(d, (sx, sy + 17), status, f_sm, (240, 120, 100) if not folded else dim)
        # کارت‌های بازیکن
        if p.hole and not folded:
            kw, kh = 34, 48
            ky = y - kh + 8 if sy > H / 2 else y + bh - 8
            for j, c in enumerate(p.hole):
                draw_card(d, sx - kw - 2 + j * (kw + 4), ky, kw, kh, c if show_all else None)
        # مبلغ شرط
        if p.bet and t.state == "hand":
            bx = sx + (cx - sx) * 0.5
            by = sy + (H / 2 - sy) * 0.55
            d.ellipse([bx - 24, by - 24, bx + 24, by + 24], fill=(190, 40, 50), outline=(255, 255, 255), width=3)
            text_c(d, (bx, by), str(p.bet), f_sm, (255, 255, 255))

    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    buf.name = "table.png"
    return buf
