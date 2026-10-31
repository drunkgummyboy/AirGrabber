"""Baked pictures for the spotlight band (app/ui/spotlight_band.py), in physical pixels: its
backdrop (a title's picture blurred and dimmed, fading into the page's background at the
bottom, like the info window's: app/ui/title_info.py's `compose_hero`), the glow that stands in
without a picture, the scene (the backdrop with the small poster and its shadow), the crossfade
frames between two scenes, and the controls drawn over the picture (rounded boxes, anti-aliased
by drawing them larger and scaling down).

Pure functions. The big ones (`glow_layer`, `compose_scene`, `crossfade_frames`) run in a worker
thread; the controls are small enough for the event loop. Images handed out may be shared:
read-only."""

from __future__ import annotations

from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from app.ui.title_info import BLUR_SHARE, HeroFace, compose_hero, rounded_mask

RGB = tuple[int, int, int]
RGBA = tuple[int, int, int, int]

FADE_FROM = 0.35  # the picture starts fading into the background this far down the band
GLOW_PEAK = (0.55, 0.8)  # the glow's strongest (light, dark)
GLOW_SECOND = 0.55  # the smaller glow's strength, as a share of the main one's
SHADOW_ALPHA = 0.5  # the poster's shadow at its darkest
_SUPERSAMPLE = 4
_GLOW_STEP = 8  # the glow is computed this many times smaller, then scaled up (it's soft)
_GRADIENT_EDGE = 181.0  # Image.radial_gradient's value halfway along each side


def band_face(size: tuple[int, int], bg: RGB, *, blur: bool = True) -> HeroFace:
    """The band's finish step for a picture `size` pixels big (ImageCache): blurred and dimmed
    (`blur`), fading into `bg` from FADE_FROM down to the very bottom."""
    width, height = size
    return HeroFace(
        bg=bg,
        fade_from=round(height * FADE_FROM),
        solid_from=height,
        blur=round(width * BLUR_SHARE, 1) if blur else 0.0,
    )


def _blob(size: tuple[int, int], strength: float) -> Image.Image:
    """An L ellipse filling `size`, `strength` (0–1) at its centre fading smoothly to 0 at its
    edge."""
    table = []
    for value in range(256):
        # Pillow's gradient is 0 at the centre and 181 at the middle of each side (255 in the
        # corners): 0 strength from the ellipse's edge on.
        x = min(1.0, value / _GRADIENT_EDGE)
        table.append(round(255 * strength * (1 - x * x * (3 - 2 * x))))
    return Image.radial_gradient("L").point(table).resize(size, Image.Resampling.BILINEAR)


@lru_cache(maxsize=4)
def glow_layer(size: tuple[int, int], bg: RGB, accent: RGB, dark: bool) -> Image.Image:
    """The band without a picture: `bg` with a soft radial glow of `accent` (a wide one right of
    the middle, a smaller lighter one on the left), fading into `bg` at the bottom like a
    picture would. RGB, `size` pixels."""
    width, height = size
    small = (max(1, width // _GLOW_STEP), max(1, height // _GLOW_STEP))
    sw, sh = small
    peak = GLOW_PEAK[1 if dark else 0]
    mask = Image.new("L", small, 0)
    main = _blob((max(1, round(sw * 0.9)), max(1, round(sh * 2.6))), peak)
    mask.paste(main, (round(sw * 0.72) - main.width // 2, round(sh * 0.3) - main.height // 2))
    second = _blob((max(1, round(sw * 0.5)), max(1, round(sh * 2.0))), peak * GLOW_SECOND)
    left = Image.new("L", small, 0)
    left.paste(second, (round(sw * 0.2) - second.width // 2, round(sh * 0.15) - second.height // 2))
    mask = ImageChops.lighter(mask, left)
    lighter = tuple(round(c + (255 - c) * 0.3) for c in accent)
    colour = Image.new("RGB", small, accent)
    colour.paste(Image.new("RGB", small, lighter), (0, 0), left)
    glow = Image.composite(colour, Image.new("RGB", small, bg), mask)
    glow = glow.resize(size, Image.Resampling.BICUBIC)
    return compose_hero(glow, band_face(size, bg, blur=False))


def compose_scene(
    layer: Image.Image,
    poster: Image.Image | None,
    box: tuple[int, int],
    *,
    radius: int,
    shadow_blur: int,
    shadow_offset: int,
) -> Image.Image:
    """The band's picture: `layer` (the backdrop or the glow, the band's size) with `poster`
    (RGBA, rounded) at `box` (its top-left) over a soft shadow. RGB, a new image."""
    scene = layer.convert("RGB")  # a copy, also when it is RGB already
    if poster is None:
        return scene
    x, y = box
    width, height = poster.size
    pad = max(1, shadow_blur * 3)
    shadow = Image.new("L", (width + 2 * pad, height + 2 * pad), 0)
    shadow.paste(rounded_mask((width, height), radius), (pad, pad))
    shadow = shadow.filter(ImageFilter.GaussianBlur(shadow_blur))
    shadow = shadow.point([round(value * SHADOW_ALPHA) for value in range(256)])
    left, top = x - pad, y - pad + shadow_offset
    scene.paste((0, 0, 0), (left, top, left + shadow.width, top + shadow.height), shadow)
    scene.paste(poster, (x, y), poster)
    return scene


def crossfade_frames(old: Image.Image, new: Image.Image, steps: int) -> list[Image.Image]:
    """The `steps` frames from `old` to `new` (the last one is `new`); just `new` when their
    sizes or modes differ."""
    if old.size != new.size or old.mode != new.mode or steps <= 1:
        return [new]
    frames = [Image.blend(old, new, step / steps) for step in range(1, steps)]
    frames.append(new)
    return frames


@lru_cache(maxsize=96)
def rounded_box(
    size: tuple[int, int],
    radius: float,
    fill: RGBA,
    edge: RGBA | None = None,
    border: int = 0,
) -> Image.Image:
    """A rounded rectangle `size` pixels big, `fill` inside a `border`-pixel `edge` line,
    see-through around its corners, anti-aliased. RGBA."""
    width, height = max(1, size[0]), max(1, size[1])
    scale = _SUPERSAMPLE
    big = Image.new("RGBA", (width * scale, height * scale), (*fill[:3], 0))
    draw = ImageDraw.Draw(big)
    box = (0, 0, width * scale - 1, height * scale - 1)
    corner = max(0, round(radius * scale))
    if edge is not None and border > 0:
        draw.rounded_rectangle(box, radius=corner, fill=edge)
        inset = border * scale
        inner = (inset, inset, width * scale - 1 - inset, height * scale - 1 - inset)
        if inner[2] > inner[0] and inner[3] > inner[1]:
            draw.rounded_rectangle(inner, radius=max(0, corner - inset), fill=fill)
    else:
        draw.rounded_rectangle(box, radius=corner, fill=fill)
    return big.resize((width, height), Image.Resampling.BOX)
