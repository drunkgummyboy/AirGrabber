"""Draw the player's taskbar-preview button icons (app/assets/thumb_*.ico; app/taskbar_buttons.py).

Solid white shapes on a clear background (drawn, no font), at the sizes Windows asks for
(16–48 px for 100–300 % scaling). Run once after a change: `python -m tools.player_thumb_icons`.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ASSETS = Path(__file__).resolve().parent.parent / "app" / "assets"
SIZES = (16, 20, 24, 32, 40, 48)
WHITE = (255, 255, 255, 255)
# Each shape on a 100 × 100 square, as polygons.
SHAPES: dict[str, list[list[tuple[float, float]]]] = {
    "play": [[(28, 16), (28, 84), (84, 50)]],
    "pause": [[(24, 16), (42, 16), (42, 84), (24, 84)], [(58, 16), (76, 16), (76, 84), (58, 84)]],
    "previous": [[(16, 18), (28, 18), (28, 82), (16, 82)], [(84, 18), (84, 82), (32, 50)]],
    "next": [[(72, 18), (84, 18), (84, 82), (72, 82)], [(16, 18), (16, 82), (68, 50)]],
}


def picture(name: str, size: int) -> Image.Image:
    big = size * 8  # drawn large, then made small: smooth edges
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for shape in SHAPES[name]:
        draw.polygon([(x * big / 100, y * big / 100) for x, y in shape], fill=WHITE)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    for name in SHAPES:
        frames = [picture(name, size) for size in SIZES]
        frames[-1].save(
            ASSETS / f"thumb_{name}.ico",
            sizes=[(size, size) for size in SIZES],
            append_images=frames[:-1],
        )
        print("drew", name)


if __name__ == "__main__":
    main()
