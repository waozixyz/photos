# Photos

The canonical checkout is `waozixyz/photos` under the Projects directory.

A standalone native photo library that also opens inside Harmony. Photos merges
local folders and Ente exports without changing their files. Identical originals
are stored once while every album membership is retained. Ente Desktop
`metadata/`, CLI `.meta/`, and adjacent Google Takeout JSON sidecars are supported.
Corrected dates, captions, original metadata and locations survive import.

The owner's merged desktop library is `/mnt/storage/Photos`, imported from
`/mnt/storage/adel` and `/mnt/storage/Album`. Private data never belongs in this
repository. Your full-quality originals remain available offline on the desktop.

Sync connects only to the pinned local Daochi instance on the ThinkPad. Desktop
access uses an authenticated SSH loopback tunnel, and the ThinkPad uses its own
loopback. Photos refuses public URLs, redirects, proxies, identity changes, and
node policies that would replicate Photos outward. Originals and metadata are
encrypted client-side using XChaCha20-Poly1305. Requests use Daochi v6 account and
device signatures; downloaded originals must match their SHA-256 and size.
The ThinkPad node stores a complete encrypted copy. Photos retrieves previews
and materializes originals when needed; its installed index is private local data.

Photos opens to a newest-first feed grouped by day. Older photos load as you
scroll, with only visible thumbnails drawn. The app follows Harmony’s colors
when hosted, including changes while it is open. Background library updates
keep browsing controls available. Browse albums, favorites, videos, hidden items and recoverable
Trash. Search names, dates, captions, tags and albums. Select a photo to view its
original, favorite it, move it to Trash, or restore it. Enter an album name,
caption, tags or export folder in the input above, then use the corresponding
action. Empty Export destinations use `~/Pictures/Photos Export`. Source files
and synced originals are never permanently deleted by Trash.

JPEG, PNG, WebP, GIF and common video containers import directly. RAW/HEIC files
are retained even when the local image decoder cannot preview them. Video
thumbnails need ffmpeg. Videos can be stored, synced, previewed and exported;
in-app video playback is not implemented. Facial recognition, semantic image
search, photo editing, public links and phone camera backup are not implemented.
This version is a local photo library with sync, not complete Ente feature parity.

Build and check from this canonical checkout:

```sh
python3 -m venv build/venv
build/venv/bin/pip install -r requirements.txt
ziran lock
make build test window-test
build/venv/bin/python tests/sync_node.py
```

`scripts/install.py` installs the standalone app, desktop entry, local library
service and `photos-sync.service`, preserving existing settings and keys.
Publish the checked portable bundle with Harmony's `scripts/publish-app.py`,
then use its Apps Install action. The installed local service supplies the same
library to both apps; this release supports Linux desktop hosts.
It must be given the pinned ThinkPad node ID before enabling sync. Never point
Photos at a public node. Uninstalling the app must retain the library and keys.
Keep a separate backup of `private/identity.json`: losing its account and content
keys makes the encrypted ThinkPad copy unreadable. Do not send keys in chat.
