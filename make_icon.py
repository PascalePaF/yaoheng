"""Generate the same 曜衡 brand mark used by the in-app sidebar."""

from pathlib import Path

from PIL import Image, ImageDraw


SIZE = 1024
ORANGE = (255, 157, 46, 255)
DARK = (23, 23, 23, 255)
TRANSPARENT = (0, 0, 0, 0)


def scaled_points(points: list[tuple[float, float]]) -> list[tuple[int, int]]:
    scale = SIZE / 42
    return [(round(x * scale), round(y * scale)) for x, y in points]


image = Image.new("RGBA", (SIZE, SIZE), TRANSPARENT)
draw = ImageDraw.Draw(image)
hexagon = scaled_points([(21, 2), (38, 11), (38, 31), (21, 40), (4, 31), (4, 11)])
draw.polygon(hexagon, fill=DARK)
draw.line(hexagon + [hexagon[0]], fill=ORANGE, width=48, joint="curve")

# Angular horizon/peak and the centered focus diamond mirror YaohengApp._draw_logo.
draw.line(scaled_points([(10, 27), (21, 8), (32, 27)]), fill=ORANGE, width=72, joint="curve")
draw.polygon(scaled_points([(21, 17), (27, 23), (21, 29), (15, 23)]), fill=ORANGE)

output_dir = Path(__file__).resolve().parent
png_target = output_dir / "app.png"
ico_target = output_dir / "app.ico"
resampling = getattr(Image, "Resampling", Image).LANCZOS
icon = image.resize((256, 256), resampling)
icon.save(png_target, format="PNG", optimize=True)
icon.save(
    ico_target, format="ICO",
    sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
)
print(ico_target)
print(png_target)
