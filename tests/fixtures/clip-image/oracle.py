"""Test-only frozen CLIP tensors from Pillow/NumPy; no app runtime use."""
from pathlib import Path
import zlib
import numpy as np
from PIL import Image, ImageOps

root = Path(__file__).resolve().parent
cases = []
def save(name, image, **options):
    image.save(root / name, **options)
    cases.append(name)
for width, height, name in [(9, 7, 'alpha.png'), (641, 479, 'odd.png'),
                            (233, 467, 'portrait.png')]:
    y, x = np.indices((height, width))
    rgba = np.stack(((x*17+y*13)%256, (x*7+y*23)%256,
                     (x*43+y*3)%256, (x*19+y*31)%256), axis=2).astype('uint8')
    save(name, Image.fromarray(rgba))
save('black.png', Image.new('RGB', (1, 1), (0, 0, 0)))
save('white.png', Image.new('RGB', (1, 1), (255, 255, 255)))
palette = Image.new('P', (17, 11))
palette.putpalette([c for at in range(256) for c in (at, 255-at, (at*13)%256)])
palette.putdata([(at*17)%256 for at in range(17*11)])
save('palette.png', palette, transparency=17)
gray = Image.frombytes('LA', (13, 19), bytes((at*31)%256 for at in range(13*19*2)))
save('gray-alpha.png', gray)
base = Image.open(root / 'alpha.png').convert('RGB')
for orientation in range(1, 9):
    exif = Image.Exif(); exif[274] = orientation
    save(f'orientation-{orientation}.jpg', base, quality=95, subsampling=0, exif=exif)
for orientation in (6, 8):
    exif = Image.Exif(); exif[274] = orientation
    save(f'orientation-{orientation}.png', base, exif=exif)
for name in cases:
    with Image.open(root / name) as original:
        image = ImageOps.exif_transpose(original).convert('RGB')
        width, height = image.size
        shape = ((224, max(224, int(height*224/width))) if width <= height else
                 (max(224, int(width*224/height)), 224))
        image = image.resize(shape, Image.Resampling.BICUBIC)
        left, top = (image.width-224)//2, (image.height-224)//2
        image = image.crop((left, top, left+224, top+224))
        pixels = np.asarray(image, dtype=np.float32)/255.0
        pixels = (pixels-np.array([.48145466, .4578275, .40821073], dtype=np.float32))/np.array([.26862954, .26130258, .27577711], dtype=np.float32)
        tensor = pixels.transpose(2, 0, 1).astype('<f4').tobytes()
        assert len(tensor) == 602112
        (root / (name+'.f32.zlib')).write_bytes(zlib.compress(tensor, 9))
print(f'Generated {len(cases)} synthetic independent Pillow/NumPy tensors')
