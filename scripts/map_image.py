#!/usr/bin/env python3
"""Render public-domain land polygons to a bundled offline coordinate map.

Input: Natural Earth ne_110m_land.geojson. This only reads public map data;
it never reads the photo library. Run explicitly when updating the bitmap.
"""
import argparse
import json
from pathlib import Path
from PIL import Image, ImageDraw

parser = argparse.ArgumentParser()
parser.add_argument('geojson', type=Path)
parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1] / 'assets/world.png')
args = parser.parse_args()
width, height = 4096, 2048
image = Image.new('RGB', (width, height), '#183440')
draw = ImageDraw.Draw(image)
project = lambda point: ((point[0] + 180) * width / 360, (90 - point[1]) * height / 180)
for feature in json.loads(args.geojson.read_bytes())['features']:
    geometry = feature['geometry']
    polygons = [geometry['coordinates']] if geometry['type'] == 'Polygon' else geometry['coordinates']
    for polygon in polygons:
        for index, ring in enumerate(polygon):
            draw.polygon([project(p) for p in ring], fill='#56766d' if index == 0 else '#183440')
for longitude in range(-180, 181, 30):
    x = (longitude + 180) * width / 360
    draw.line((x, 0, x, height), fill='#385256', width=1)
for latitude in range(-90, 91, 30):
    y = (90 - latitude) * height / 180
    draw.line((0, y, width, y), fill='#385256', width=1)
args.output.parent.mkdir(parents=True, exist_ok=True)
image.save(args.output, optimize=True)
