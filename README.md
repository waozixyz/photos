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
Oldest first in galleries and albums. Search names, dates, captions, tags and albums.
Combine filters such as `type:photo album:"Summer trip" year:2024 -tag:work`.
`is:favorite`, `is:archived`, `has:location`, `has:caption` and `has:tags` filter
properties. `from:2024-06-01 to:2024-06-30` includes both capture days in UTC;
`on:2024-06-15` selects one day. Search tips explains phrases, exclusions and
the available filters. Structured capture-date filters currently accept years
1970 through 2999; older originals remain browsable and searchable by their date text.
Search values are bound SQLite parameters and literal
percent/underscore characters are preserved. The gallery and search engine is Ziran.
Open a photo to view its
original, capture time, dimensions, file size, caption, tags, albums and location.
Use the dedicated editors to change captions, tags, file names and capture dates.
Dates without an explicit time-zone offset use UTC. Left/right arrows browse
photos and Escape returns to the grid when no text field has focus.
Jump to date accepts a capture day in YYYY-MM-DD format and scrolls the current
gallery to that day, keeping its album, search and sort. If the day is missing,
it starts at the next match in that direction, or the end of the gallery.
Opening a photo, returning and refreshing retain the jumped position. Jump to
this day in a visible photo's details opens its day in the main timeline.
Zoom + and Zoom - magnify photos, and the mouse wheel zooms around the pointer.
Drag the image to pan. Fit restores the fitted view; 1:1 displays original pixels
at one screen pixel each, using the decoder's oriented image dimensions.
Linux hosts retain original decoded pixels up to 67 megapixels within their
image memory budget. Larger images use a reduced preview, labelled Preview 1:1;
full-resolution tiled viewing for those originals remains unfinished.
Expand fills the Photos window, with Previous/Next and arrow-key browsing.
Escape returns to the normal viewer first, then to the gallery. Opening another
photo resets zoom and pan. These controls only change the view of the original.

Edit image opens a native editor with rotation, horizontal and vertical flips,
centered square/4:3/16:9 crops, brightness, contrast and saturation. Preview and
reset changes, then Save copy to add a separate PNG original to the library.
Your source photo stays untouched. Copies keep capture dates, captions, tags,
locations, albums, favorites, Hidden/Archive state, transparency and color profiles;
they are backed up and exported like other originals. Raster transformations run
in a headless Ziran processor using installed GdkPixbuf codecs. Animated images
are refused to avoid losing frames. Custom crop opens the oriented, uncropped
preview: drag over it to choose the area to keep, then Use crop to preview the
result. Full image resets the selection; Cancel keeps the previous crop.
Rotating or flipping after choosing a crop keeps the selection on the same
part of the image. Drawing, advanced filters
and RAW/HEIC editing remain unfinished. Repeating an identical edit reuses the
existing copy and preserves any later changes to its metadata or Trash state.

Edit video offers start/end trim times in seconds (up to three decimals), rotation
and centered original, square, 4:3, 16:9, 9:16 and 3:4 crops. The preview shows the
edited frame at the trim start. Keep at least one second, then Save copy to create
an H.264/AAC MP4 while leaving the source unchanged. Saving runs in the background
and can be cancelled; one video copy processes at a time per library. Copies retain
audio, capture dates, captions, tags, locations, albums and privacy settings and
are included in encrypted ThinkPad backup and export. The Ziran helper composes
codec commands without a shell; FFmpeg/FFprobe must be installed. Video copies are
re-encoded and may trim a one-pixel edge to keep even dimensions. HDR editing
and animated editing previews remain unfinished. Custom crop lets you drag an
area to keep after rotation; the selection rotates with later rotation changes.
Video crop edges align to even pixels for H.264 and may shift by one pixel.
Hidden must remain unlocked through completion to save a hidden video. Stopping
the library service stops its codec process; originals stay intact.

Edit location accepts latitude and longitude separated by a comma; an empty value
removes location. Apply it to a photo or selection. Corrections survive source
rescans and remain in encrypted sync and export sidecars. Memories shows photos
from the same capture day in earlier years, using UTC, and Choose day lets you
browse another day. Hidden, Trash and individually or album-archived photos stay
excluded. In People, Memory Lane groups a person's eligible photos by year and
offers Previous year and Next year. It requires at least four eligible photos
in each of two years. An optional Birth date adds age captions and excludes
photos before age three; leap-day birthdays use February 28 in a non-leap year.
Birth dates are preserved in the private encrypted face index and exports.
Hidden, Trash, ignored people and individually or album-archived photos stay
excluded. Timeline playback, public Memory Lane links and curated stories remain
unfinished.

Map shows GPS locations on a bundled offline world map. Zoom with buttons or
the wheel, drag to pan, and choose a numbered cluster or View photos to browse
that area. Back to map restores the previous viewport. Album map limits the
map to the current regular or smart album; the search field also filters markers.
All matching locations are clustered locally, including photos beyond the loaded
gallery page. Hidden and Trash are always excluded, even while Hidden is unlocked.
Zero coordinates and date-line edges are retained. Camera GPS is read on import;
location corrections and removals remain authoritative during later rescans.
The map makes no tile or geocoding requests. Its coarse Natural Earth land outlines
provide orientation; street detail and geographic trip curation remain unfinished.

Places names an area by its latitude, longitude and radius in kilometres. Choose
Name place in a visible photo's details to start at that photo's location, or New
place in Places to enter coordinates. Matching photos update as locations are
imported or corrected. Search a place name, use `place:"Home"`, or combine
`near:-25.3,-57.6,2` with other filters. Distance uses a spherical Earth and crosses
the date line and poles correctly; radii range from one metre to 20040 km. Hidden
and Trash stay excluded from place galleries and maps. Places appear alongside
saved albums and share their naming rules; edit an area's center and radius,
rename, pin or delete it without changing originals or memberships. Definitions
use the existing encrypted album backup and library-export format. No geocoding
service receives coordinates or searches.

Use Select, Ctrl-click or Shift-click to select up to 1000 items. Select loaded
selects the photos already loaded into the feed, including those outside the
visible rows; scrolling loads more. Apply favorites, hiding, Trash, restore, album
membership, corrected dates or export to the selection. Bulk property changes
set the chosen state consistently, including selections with mixed states.
Album removal keeps the photo in the library; an album can be renamed without
changing its originals or other album memberships. Create an empty album from
Albums, and edit its description, pin it or archive it from the album view.
Use a visible photo as its cover from that photo's details. The album list
filters active, archived and empty albums and sorts by name or creation date;
pinned albums remain first. Empty albums and album details are encrypted and
synced too. Archiving an album removes all its members from the home timeline,
including photos that also belong to other albums; search and album browsing
still include them. Delete empty album refuses any album with members, including
hidden or trashed members. Removed memberships and
corrected dates survive later source rescans and are included in encrypted sync.
Empty Export destinations use `~/Pictures/Photos Export`. Source files and
synced originals are never permanently deleted by Trash.

Save search creates a smart album from the current Photos search. Matching photos
update automatically as imports and edits change the library, without storing
duplicate originals or changing regular album memberships. Smart albums always
exclude Hidden and Trash. Their definitions are encrypted and synced to the
ThinkPad and preserved by library export/import. Edit search changes the rules;
pinning and renaming work like regular albums. Delete smart album removes only
the saved search. Membership changes, explicit covers and whole-album archiving
apply to regular albums; a smart album uses its newest matching photo as its cover.
These are saved searches; automatically curated trip albums remain unfinished.
For an ordinary album, Auto-add people opens a picker of named visible people.
Choose up to 64 people; a photo matching any selected person joins the album.
Existing matches are added on Save people, and new matches join after local face
indexing. Naming and merging people updates these rules; disabling People pauses
additions, and enabling it catches up. Rules use person identities, so a renamed
person or album keeps working. Deselect all stops additions. Keep previously
auto-added photos retains memberships; Remove previously auto-added photos
removes automatic contributions from deselected people without removing photos
that were imported or deliberately added to that album. Hidden and Trash stay
untouched by this cleanup. Manual removal stays authoritative through indexing
and source rescans. The rules and automatic-membership provenance survive
encrypted backup and folder export/import. Originals stay unchanged.

Local sync offers Export library and Continuous export. Choose a destination
outside the library and every import source. Background export writes unchanged
originals into album folders, including multiple memberships, with JSON sidecars
and album details; photos without albums go into Uncategorized. Hidden and Trash
are excluded. Empty albums are retained too. Names include short identifiers so
identically named files and sanitized album names do not collide. Later passes
update metadata and mirror album membership, renames and Trash changes. Only
unchanged files created by this export are removed; unrelated files and files
edited outside Photos are preserved. Such a conflict pauses export for review.
Stop export leaves the folder copy intact. Never use the export destination as
an import source. Export is opt-in and is not automatically enabled on the owner's
library.

JPEG, PNG, WebP, GIF and common video containers import directly. HEIC/HEIF and
AVIF use libheif; camera RAW formats including DNG, CR2/CR3, NEF, ARW, RAF, RW2,
ORF and PEF use LibRaw. A headless Ziran decoder generates oriented gallery
thumbnails and PNG display assets on opening, with alpha retained for HEIC.
The original files and embedded metadata remain unchanged. Viewing uses an
8-bit display rendering; HDR display and RAW development controls remain unfinished.
Decoder support depends on the host's installed codec plugins and camera support.
Decoding is limited to 100 megapixels and bounded memory/time; an unsupported or
damaged original stays stored and can still be backed up and exported. Existing
imports gain missing thumbnails during source rescans or when opened. Video
thumbnails need ffmpeg. Videos can be stored, synced, previewed and exported;
in-app playback is available on Linux with GStreamer and its installed codecs.
Public links and phone camera backup are not implemented.
Exported Live Photos with an image and a MOV/MP4 motion file appear as one item.
Automatic pairing requires the same folder and base name, unambiguous components,
capture dates or source file times within one day, and a motion file of one to ten
seconds. HEIC/HEIF and JPEG stills are supported. The pair shows a Live Photo label;
Play Live Photo uses the normal offline player, including audio and playback
controls. `type:live` finds paired photos. Favorites, Hidden, Archive, Trash,
captions, tags, albums and corrected dates apply to both components. Export and
encrypted ThinkPad backup retain both original files and their pairing metadata.
Separate files restores independent gallery items and survives rescans and sync.
Incomplete pairs and inconsistent privacy updates stay out of galleries until
both records agree. Embedded Android motion-photo extraction and native phone
Live Photo capture remain unfinished.

This version is a local photo library with sync, not complete Ente feature parity.
Detect text opens an on-demand local OCR viewer for photos and Live Photo stills
outside Trash. Click a word, drag across words, double-click a word to select its
recognized line, or Select all; Copy selection puts the result on the clipboard.
Escape and Back return to the photo. Switch the recognition language to English,
Spanish, Portuguese, German, Simplified Chinese or Japanese. Text and selection
never become a library search index or leave the device through backup. Results
are cached briefly in bounded local memory; Hidden results are never cached.
Access is rechecked while the viewer is open and extracted text is cleared when
a photo is hidden, trashed, locked or the local service becomes unavailable.
Text already copied deliberately remains on the system clipboard.
The headless Ziran helper uses Tesseract's local C engine and pinned Apache 2.0
language data; it needs libtesseract 5 and GdkPixbuf. Run `make text-models` to
download the verified public models, or copy them with `scripts/text-models.py
--source DIR`, then install with `scripts/install.py --text-models build/models/ocr`.
Scanning itself makes no network requests. Recognition is approximate, downscales
images larger than 4096 pixels on an edge, and rejects results beyond 1024 words
or 32 KiB of text; crop a copy to recognize a smaller area. Small, rotated or
handwritten text may be missed. Animated images use their first frame. Native
phone long-press selection and movable touch selection handles remain unfinished.

Semantic search finds image content from natural language descriptions, with
closest matches first. Enable it in Semantic search to index local photos in the
background, see progress, pause/resume processing or retry unavailable originals.
Completed indexes remain searchable while processing is paused.
Disable search stops processing and results without removing the saved index.
Hidden and Trash are excluded even while Hidden is unlocked. Encrypted ThinkPad backup includes
indexes; receiving an index does not enable processing or search on that device.
The native Ziran inference and ranking engines run locally, using verified CLIP
ViT-B/32 quantized models and ONNX Runtime with telemetry disabled. Models are
optional: run `make semantic-models` to download their pinned public assets, or
`scripts/semantic-models.py --source DIR` to copy previously downloaded assets.
Install them with `scripts/install.py --semantic-models build/models`. No photo,
query or index is sent to a model service. The CLIP model is primarily English;
results are approximate and exclude similarities below 0.20. Indexing covers
still images whose originals are present locally, including the still component
of Live Photos. Failed files remain stored and can be retried. Face recognition,
video-content search, automatic curation and a Ziran tokenizer/indexing service
remain unfinished. CLIP's MIT attribution is in `assets/CLIP-LICENSE.txt`.

People provides opt-in local face grouping. Enable People to index local photos,
pause or resume processing, and retry unavailable originals. Previously completed
groups remain browsable while paused. Name or merge assigns a name; using an
existing name merges its groups. Ignore removes a group from active People and
named-person search; Show ignored lets you unignore it. Select wrong photos in a
person's gallery, then Not this person moves their matching faces into separate
unnamed groups and retains the correction. Corrected groups cannot be silently
merged back by reusing the rejected name. Originals and album memberships stay
unchanged. Search a name or combine `person:"Ada"` with other filters. A saved
search using this filter updates as new faces join that named person.

Face detection, five-landmark alignment, embeddings and similarity matching run
in headless Ziran helpers with the pinned YuNet/SFace models and ONNX Runtime's
CPU engine; telemetry is disabled. Their licenses are in `assets/`. Recognition
is a suggestion, especially for children, small faces, occlusion or unusual
angles. Each photo supports up to 32 clear faces, with a conservative similarity
threshold; missed or excess faces remain a limitation. Photos with no detected
faces are marked processed. Videos are not face-indexed. Hidden and Trash are
excluded, even while Hidden is unlocked. Derived indexes and corrections stay in
the private local library and authenticated encrypted ThinkPad records; optional
folder exports include them in sidecars. Enable People independently on a restored
device. Disable People hides its groups and named-person search while retaining
the local index for later use. No network recognition or public node is used.

Hidden requires a separate local password, with confirmation when first created.
The native Ziran helper uses libsodium's Argon2id verifier; passwords never enter
process arguments or app inspection state. Locked photos cannot be read, exported
or changed through the local service, including hidden items in Trash and mixed
selections. Leaving Hidden or using Lock Hidden revokes the unlock; it also expires
after ten minutes and on service restart. Authentication is local to each device,
while the Hidden property remains in encrypted sync. This is an application access
lock; local originals retain their existing storage format. System biometrics and
password recovery remain unfinished. While Hidden is unlocked, Change password
accepts and confirms a replacement password, then relocks every local session.
Your originals and encrypted-backup keys stay intact. The local password is not
synced to other devices.
Trash retains originals indefinitely, without automatic expiry or permanent deletion.

The Ente comparison uses its [feature overview](https://ente.com/features/),
[album behavior](https://ente.com/help/photos/features/albums-and-organization/albums),
[archive behavior](https://ente.com/help/photos/features/albums-and-organization/archive),
[metadata and editing reference](https://github.com/ente/ente/blob/main/docs/docs/photos/faq/metadata-and-editing.md)
[video editing](https://ente.com/help/photos/features/utilities/video-editor)
and [export behavior](https://ente.com/help/photos/features/backup-and-sync/export).
Memory Lane follows Ente's [eligibility rules](https://ente.photos/help/photos/features/search-and-discovery/memory-lane)
and automatic people albums follow its [auto-add behavior](https://ente.com/help/photos/features/albums-and-organization/auto-add-people).
The remaining replacement work includes:

- Mobile clients, permission-aware background camera backup and device space recovery.
- Detailed maps, curated memories, automatic trip albums and richer face review/assignment controls.
- Advanced photo editing, encrypted video streaming, animated editing previews, embedded Android motion photos, HDR display and broader codec/camera coverage.
- Private sharing, collaboration, public links, comments and account recovery.
  Any sharing design needs to honor the owner's ThinkPad-only sync restriction.
- System authentication, Hidden password recovery and a deliberate deletion/retention policy.
- A complete Ziran library/sync service. The native UI, portable app, gallery/search
  engine, image processor and password verifier are Ziran/Kryon; the SQLite importer,
  metadata mutation service and encrypted sync worker remain Python.

Choose Play video to watch a video in the Photos window. Pause/resume, Replay,
the position slider, five-second seek buttons, volume and Mute work offline.
Space pauses or resumes; Left/Right seek five seconds. Speed cycles through
0.25x, 0.5x, 1x, 1.5x and 2x. Expand fills the app window; Escape returns to
the normal view, then exits playback. Audio and frames share the decoder clock.
The local library rechecks access while playing, and an expiring decoder lease
stops playback if those checks stall. Leaving the viewer stops audio and wipes
the decoded frame. Encrypted HLS streaming and phone playback remain unfinished;
remote originals are fully restored from the pinned ThinkPad before playback.
GStreamer playback, base/good and FFmpeg codec plugins must be installed on the
Linux host. The decoder loads these libraries when needed, so missing codecs
produce a useful message while the rest of the library remains usable.

The native image processor needs GdkPixbuf and GObject on the Linux desktop host;
Hidden authentication also needs libsodium.
The HEIC/RAW decoder needs libheif, LibRaw and `prlimit` from util-linux. Its
display assets use the same Kryon ImageProps viewer as other image formats.
Video editing and video thumbnails need FFmpeg (including FFprobe and H.264/AAC codecs).
Unit and synthetic round-trip checks validate tested flows; an independent
cryptographic audit remains outside their scope. Full-original restore verification belongs
in private receipts, rather than storing library inventories in the repository.

Build and check from this canonical checkout:

```sh
python3 -m venv build/venv
build/venv/bin/pip install -r requirements.txt
ziran lock
make build test window-test
make video-window-test video-portable-test
make crop-window-test crop-portable-test
make viewer-window-test viewer-portable-test
make map-window-test map-portable-test
make places-window-test places-portable-test
make codec-window-test codec-portable-test
make playback-window-test playback-portable-test
make face-models people-window-test people-portable-test
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
