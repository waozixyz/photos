"""Stage only Photos' catalog, translations and licensed achievement icons."""
import json
from pathlib import Path
import re
import shutil
import sys

root = Path(__file__).resolve().parent.parent
source = root / "portable"
destination = root / "build/package-assets/photos/assets/photos"
package = Path(sys.argv[1])
destination.mkdir(parents=True, exist_ok=True)
shutil.copyfile(source / "achievements.json", destination / "achievements.json")
shutil.copytree(source / "achievement-locales", destination / "achievement-locales", dirs_exist_ok=True)
icons = destination / "icons"
icons.mkdir(exist_ok=True)
for achievement in json.loads((source / "achievements.json").read_text())["achievements"]:
    name = achievement["icon_source"]
    if not re.fullmatch(r"mingcute_(?:regular|filled|extra)_[a-z0-9_]+", name):
        raise ValueError("invalid Photos achievement icon source")
    match = re.search(r'^Shape :: (".*");$', (package / "src" / (name + ".zi")).read_text(), re.M)
    if not match:
        raise ValueError(f"missing icon shape: {name}")
    shape = json.loads(match[1])
    if not shape or len(shape.encode()) >= 1024 or shape[0] not in "SF":
        raise ValueError(f"invalid icon shape: {name}")
    expected = f"assets/photos/icons/{achievement['id']}.shape"
    if achievement["icon"] != expected:
        raise ValueError("unexpected Photos achievement icon path")
    (icons / (achievement["id"] + ".shape")).write_text(shape)
shutil.copyfile(package / "LICENSE", icons / "LICENSE")
