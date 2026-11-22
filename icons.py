"""Draws the Blink Sync icon (a camera glyph on a rounded tile) in different status colours."""

from PIL import Image, ImageDraw

COLOURS = {
    "ok": (37, 99, 235),        # blue
    "syncing": (22, 163, 74),   # green
    "starting": (37, 99, 235),
    "login": (217, 119, 6),     # amber
    "error": (220, 38, 38),     # red
    "paused": (107, 114, 128),  # grey
}


def draw_icon(state: str = "ok", size: int = 256) -> Image.Image:
    s = size / 64  # design on a 64px grid
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(14 * s), fill=COLOURS.get(state, COLOURS["ok"]))
    white = (255, 255, 255, 255)
    # camera body, lens and viewfinder bump
    d.rounded_rectangle([10 * s, 22 * s, 54 * s, 50 * s], radius=int(6 * s), fill=white)
    d.rounded_rectangle([22 * s, 15 * s, 38 * s, 25 * s], radius=int(3 * s), fill=white)
    d.ellipse([22 * s, 26 * s, 42 * s, 46 * s], fill=COLOURS.get(state, COLOURS["ok"]))
    d.ellipse([27 * s, 31 * s, 37 * s, 41 * s], fill=white)
    if state in ("login", "error"):
        # badge with "!"
        d.ellipse([40 * s, 2 * s, 62 * s, 24 * s], fill=white)
        d.rectangle([49.5 * s, 6 * s, 52.5 * s, 16 * s], fill=COLOURS[state])
        d.rectangle([49.5 * s, 18 * s, 52.5 * s, 21 * s], fill=COLOURS[state])
    elif state == "paused":
        d.ellipse([40 * s, 2 * s, 62 * s, 24 * s], fill=white)
        d.rectangle([46 * s, 7 * s, 49.5 * s, 19 * s], fill=COLOURS[state])
        d.rectangle([52.5 * s, 7 * s, 56 * s, 19 * s], fill=COLOURS[state])
    return img


def write_ico(path: str):
    """Multi-size .ico for the exe, shortcuts and installer."""
    draw_icon("ok", 256).save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


if __name__ == "__main__":
    import sys
    write_ico(sys.argv[1] if len(sys.argv) > 1 else "app.ico")
