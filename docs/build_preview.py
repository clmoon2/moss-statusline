"""Regenerate preview.gif with Pillow, fontconfig and local TrueType fonts."""
from functools import lru_cache
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import demo


W, H = 954, 542
PAPER = "#f1efdb"
INK = "#233328"
MUTED = "#4a6051"
SCREEN = "#17231f"
PALETTE = {
    "primary": "#f1efdc", "black": "#243426", "green": "#b6ce96",
    "yellow": "#dfc185", "red": "#ed9c86", "cyan": "#a9cec0", "status-metadata": "#acbbaa",
}
FERN = [
    "           x                  ",
    "          xx       x          ",
    "      x   x       xx          ",
    "      xx  x   xx  x        x  ",
    "       xx x  xxx  x       xx  ",
    "    x   xxx xx    x  x   xx   ",
    "    xx   xxxx    xx xx   x    ",
    "     xx   xx     x xx   xx    ",
    "      xx  xx  x  xxx   xx     ",
    "   xx   xxxx xx  xx  xxxx  xx ",
    "    xxx   xxxx   x  xxx   xx  ",
    "      xxx  xx   xx xx    xx   ",
    "      xxxxxxx   xxxx   xxx    ",
    "         xxxx  xxx   xxx   xx ",
    "           xx  xx  xxx    xx  ",
    "       xxxxxxx xxxxx   xxxx   ",
    "         xxxxx xxxx xxxx      ",
    "            xxxxxxxx          ",
    "         xxxxxxxxxxxx         ",
    "      xxxxxxxxxxxxxxxxx       ",
]


@lru_cache(maxsize=None)
def font(family, size):
    filename = subprocess.check_output(["fc-match", "-f", "%{file}", family], text=True)
    return ImageFont.truetype(filename, size)


SERIF = font("DejaVu Serif:style=Bold", 72)
SUB = font("Noto Sans:style=Regular", 24)
SCENE = font("Noto Sans:style=Bold", 33)
LABEL = font("DejaVu Sans Mono:style=Bold", 16)
MONO = font("JetBrainsMono Nerd Font Mono", 21)
BOLD = font("JetBrainsMono Nerd Font Mono:style=Bold", 21)
CELL = MONO.getlength("M")


def frame(scene):
    title, caption = demo.SCENES[scene]
    im = Image.new("RGB", (W, H), PAPER)
    draw = ImageDraw.Draw(im)
    draw.text((44, 23), "Moss", font=SERIF, fill=INK, anchor="lt")
    draw.text((46, 108), "A status line for Codex CLI", font=SUB, fill=MUTED, anchor="lt")
    for y, row in enumerate(FERN):
        for x, cell in enumerate(row):
            if cell == "x":
                px, py = W - 195 + x * 5, 21 + y * 5
                draw.rectangle((px, py, px + 4, py + 4), fill=["#728a53", "#46694c", "#314f40"][(x // 7 + y // 8) % 3])
    draw.line((46, 157, W - 46, 157), fill="#b8c1a8", width=2)
    draw.text((46, 185), title, font=SCENE, fill=INK, anchor="lt")
    draw.text((46, 235), caption, font=SUB, fill=MUTED, anchor="lt")

    left, top, right, bottom = 44, 292, W - 44, H - 38
    draw.rounded_rectangle((left, top, right, bottom), radius=14, fill=SCREEN)
    view = "COMPACT FOOTER  /  60 COLUMNS" if scene == "split" else "EXPANDED FOOTER"
    draw.text((left + 24, top + 20), view, fill="#a9bba8", font=LABEL, anchor="lt")
    draw.text((right - 24, top + 20), "SAMPLE DATA", fill="#a9bba8", font=LABEL, anchor="rt")
    draw.line((left + 24, top + 48, right - 24, top + 48), fill="#3b4c40")
    columns = 60 if scene == "split" else int((right - left - 48) // CELL)
    lines = list(demo.lines(demo.sample_tree(scene), columns))
    line_gap = 34
    area_top = top + 60
    y = area_top + (bottom - area_top - len(lines) * line_gap) // 2
    assert y >= area_top
    if scene == "split":
        assert len(lines) == 1
    for line in lines:
        x = left + 24
        for value, style in line:
            pen = BOLD if style.get("weight") == "bold" else MONO
            width = len(value) * CELL
            if bg := style.get("background"):
                draw.rectangle((int(x), y - 4, int(x + width), y + 22), fill=PALETTE[bg])
            color = PALETTE.get(style.get("color"), PALETTE["primary"])
            for char in value:
                # Use fontconfig's fallback for symbols, confined to a terminal cell.
                glyph = pen
                if not char.isascii():
                    family = f"monospace:charset={ord(char):x}"
                    glyph = font(family, 21)
                    if glyph.getlength(char) > CELL:
                        glyph = font(family, max(12, int(21 * CELL / glyph.getlength(char))))
                draw.text((x + CELL / 2, y + 16), char, font=glyph, fill=color, anchor="ms")
                x += CELL
        assert x <= right - 22
        y += line_gap
    return im


if __name__ == "__main__":
    frames = [frame(scene) for scene in demo.SCENES]
    frames[0].save(ROOT / "docs" / "preview.gif", save_all=True, append_images=frames[1:],
                   duration=[4400, 3200, 4400, 4400], loop=0, disposal=2, optimize=True)
