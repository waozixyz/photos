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
When sync completes, the ThinkPad node holds a complete encrypted copy. Photos retrieves previews
and materializes originals when needed; its installed index is private local data.
Large libraries exchange metadata in signed pages with private local checkpoints.
The ThinkPad node needs the paged encrypted-record sync endpoint for this version.

Photos opens to a newest-first feed grouped by day. Older photos load as you
scroll, with only visible thumbnails drawn. The app follows Harmony’s colors
when hosted, including changes while it is open. Background library updates
keep browsing controls available. Browse albums, favorites, videos, hidden items and recoverable
Trash. Archive photos individually or in bulk to remove them from the home feed;
they stay in albums and search results. Browse Archive to unarchive them.
Uncategorized shows photos without album memberships. Choose Newest first or
Oldest first in galleries and albums. Search names, dates, captions, tags and albums. Open a photo to view its
original, capture time, dimensions, file size, caption, tags, albums and location.
Use the dedicated editors to change captions, tags, file names and capture dates.
Dates without an explicit time-zone offset use UTC. Left/right arrows browse
photos and Escape returns to the grid when no text field has focus.

Use Select, Ctrl-click or Shift-click to select up to 1000 items. Select loaded
selects the photos already loaded into the feed, including those outside the
visible rows; scrolling loads more. Apply favorites, hiding, Trash, restore, album
membership, corrected dates or export to the selection. Bulk property changes
set the chosen state consistently, including selections with mixed states.
Album removal keeps the photo in the library; an album can be renamed without
changing its originals or other album memberships. Removed memberships and
corrected dates survive later source rescans and are included in encrypted sync.
Empty Export destinations use `~/Pictures/Photos Export`. Source files and
synced originals are never permanently deleted by Trash.

JPEG, PNG, WebP, GIF and common video containers import directly. RAW/HEIC files
are retained even when the local image decoder cannot preview them. Video
thumbnails need ffmpeg. Videos can be stored, synced, previewed and exported;
in-app video playback is not implemented. Facial recognition, semantic image
search, photo editing, public links and phone camera backup are not implemented.
This version is a local photo library with sync, not complete Ente feature parity.
Hidden is currently a gallery filter, without Ente's authenticated lock screen.
Trash retains originals indefinitely, without automatic expiry or permanent deletion.

The Ente comparison uses its [feature overview](https://ente.com/features/),
[album behavior](https://ente.com/help/photos/features/albums-and-organization/albums),
[archive behavior](https://ente.com/help/photos/features/albums-and-organization/archive),
[metadata and editing reference](https://github.com/ente/ente/blob/main/docs/docs/photos/faq/metadata-and-editing.md)
and [export behavior](https://ente.com/help/photos/features/backup-and-sync/export).
The remaining replacement work includes:

- Mobile clients, permission-aware background camera backup and device space recovery.
- On-device face grouping, semantic search, maps, memories and smart albums.
- Photo editing, video playback, Live Photo pairing and broader HEIC/RAW previews.
- Private sharing, collaboration, public links, comments and account recovery.
  Any sharing design needs to honor the owner's ThinkPad-only sync restriction.
- Empty albums, album covers/descriptions/pinning, album list ordering,
  album archiving, and full-library continuous export with album folders.
- An authenticated hidden vault and a deliberate deletion/retention policy.
- A Ziran library/sync service. The native UI and portable app are Ziran/Kryon;
  the SQLite importer, metadata service and encrypted sync worker remain Python.

Existing unit and synthetic round-trip checks establish working flows, not a
completed backup of the owner's library or an independent cryptographic audit.

Build and check from this canonical checkout:

```sh
python3 -m venv build/venv
build/venv/bin/pip install -r requirements.txt
ziran lock
make build test window-test
PHOTOS_DAOCHI=/path/to/daochi make sync-test
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
