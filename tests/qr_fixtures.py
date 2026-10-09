"""Synthetic QR pixels are committed as binary PNGs, with no generator dependency."""
from pathlib import Path
from PIL import Image

FIXTURES = Path(__file__).parent / 'fixtures'
CONTENTS = ['https://example.invalid/photos?album=Summer&day=1',
            'Hola, fotografías\nLocal "only" \\ 🙂',
            'WIFI:T:WPA;S:Photos Test;P:synthetic-qr-only;;']


def qr_image(path):
    with Image.open(FIXTURES / 'qr-multiple.png') as image:
        image.save(path)
