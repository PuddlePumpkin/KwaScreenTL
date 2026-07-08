"""Convert docs/appicon.png to Launcher/appicon.ico (multi-res)."""
import sys, os
from PIL import Image

src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "docs", "AppIcon.png")
dst = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "Launcher", "AppIcon.ico")

if not os.path.exists(src):
    print(f"[icon] {src} not found, skipping")
    sys.exit(0)

img = Image.open(src)
# Ensure square — centre-crop if needed
sz = min(img.size)
img = img.crop(((img.width - sz) // 2, (img.height - sz) // 2,
                (img.width + sz) // 2, (img.height + sz) // 2))

sizes = [16, 20, 24, 28, 32, 40, 48, 64, 96, 128, 256]
img.save(dst, format="ICO", sizes=[(s, s) for s in sizes])
print(f"[icon] {dst} ({sz}x{sz} source → {', '.join(f'{s}x{s}' for s in sizes)})")
