# Photos

The canonical checkout is `waozixyz/photos` under the Projects directory.

A standalone native photo library that also opens inside Harmony. Photos merges
local folders and Ente exports without changing their files. Identical originals
are stored once while every album membership is retained. Ente Desktop
`metadata/`, CLI `.meta/`, and adjacent Google Takeout JSON sidecars are supported.
Corrected dates, captions, original metadata and locations survive import.
Original copying runs in a headless Ziran importer: it streams and verifies the
complete SHA-256 before atomically publishing a private copy, rejects symbolic
links, and preserves an existing destination if its bytes differ. Interrupted
copies are cleaned up by the local import adapter. Ordinary original imports
merge metadata, album definitions and the exact source-cache stamp in one native
Ziran transaction, taking a fresh snapshot after copying. Unknown sidecar values,
explicit nulls, large integers and long captions survive unchanged. Failed or
interrupted writes roll back the photo, albums and source-cache record together;
metadata updates retain a newer backup stamp. Folder discovery also runs in a
headless Ziran helper, streaming sorted entries without following folder links.
It excludes provider metadata and Git folders, reports unreadable folders and
stops its own walker if the import consumer closes. Nonregular media files such
as named pipes are skipped. A persistent headless Ziran reader extracts
camera dates, dimensions and valid GPS from JPEG, PNG, WebP, TIFF/BigTIFF and
HEIC containers. EXIF original dates take precedence over modified dates;
fractional seconds and explicit time-zone offsets are retained, with UTC used
when an offset is absent or malformed. Historic camera dates survive export
and encrypted restore, including dates before 1970. Provider and album sidecars
are read there too, with UTF-8, UTF-16 and UTF-32 supported and unknown JSON values
preserved. Unreadable, linked, oversized or malformed sidecars produce import
warnings while originals remain intact. One reader serves an import batch;
failed codecs are bounded and the next request can restart the reader.
Album definitions publish in one native Ziran transaction, including their
complete description, cover, pinning, archive state, saved search and automatic
people rules. Existing definitions retain their later edits and backup stamps;
failed or interrupted publication rolls back the whole definition.
Automatic membership provenance for imports without face indexes joins the
same native photo/album/source-cache transaction. It retains the current album
identities, later manual membership changes and the people responsible for the
original additions. Invalid rules warn without discarding the original.
Private face groups, names, birthdays, corrections and manual tags publish in
that same native transaction, including their automatic membership provenance.
Later person edits and completed scans survive rescans; malformed indexes warn
while retaining originals. The complete library-service port remains in progress.
Some non-TIFF RAW camera formats still lack capture-field
extraction even when their pixels can be viewed.

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
the available filters. Capture-date filters accept years 0001 through 9999, including dates before
1970. Date corrections, timeline jumps and memories also accept historic dates;
calendar leap days are checked and date filters use UTC capture days.
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
excluded. Play Memory Lane presents eligible photos across all its years in
capture order, with year and optional age captions. Public Memory Lane links
and curated stories remain unfinished.

Slideshow plays an album's photos and Live Photo stills across the Photos window;
videos are excluded. In Albums, choose Slideshow albums, select up to 64 regular,
smart or place albums, then Play selected albums. A photo in several selected
albums appears once. Search filters still apply. In order uses oldest-first
capture order; Shuffle gives a stable shuffled cycle with Previous and Next.
Choose 5, 10, 15 or 30 seconds, or 1, 5 or 10 minutes per photo. Pause or resume,
switch between Black and Blurred backgrounds, or hide controls. Tap the middle
to show controls and the sides to browse; Space pauses, arrow keys browse and
Escape stops. Pausing keeps privacy checks active. Hidden and Trash stay excluded
even while Hidden is unlocked; Memory Lane also excludes archived and ignored
items. A privacy or membership change ends access, and a stalled or unavailable
service clears the displayed image within its short access lease. Photos already
stored on the device work offline; missing originals can come only from the
pinned ThinkPad. An unavailable original stops playback with a message. Live
Photo motion playback and music during slideshows remain unfinished.

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
Favorites, Hidden, Archive, Trash, restore, captions, tags, album memberships,
capture dates, locations and file names are written by the native Ziran SQLite
editor in one transaction. Date and coordinate parsing runs there too. Album
creation, renaming, descriptions, covers, pinning, archiving, deletion of empty
albums and saved-search edits use the same native writer. Renaming an album
retains its identity and automatic people provenance, including Hidden and Trash
members, and rolls back if migrating any member fails. Live Photo components are checked
together, and expired Hidden access or an interrupted pre-save operation rolls
back the selection. The local authorization adapter retains unlock tokens in
the existing service process and grants a short access lease through a private pipe.
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
both records agree. Embedded Android Motion Photos also appear as one item.
Modern JPEG XMP directories, legacy Google MicroVideo and Samsung SEF JPEGs, and modern HEIC/AVIF
`mpvd` containers are recognized during import or source rescan. Play Live Photo
extracts a private local playback asset while the entire combined original stays
unchanged in backup and export. Separate files saves a video copy, preserving
dates, captions, tags, albums and privacy; the original combined file remains
intact and later rescans keep the separation. Raster edited copies contain only
the still image. Clips from 0.05 through ten seconds and up to 128 MiB are accepted
with local FFmpeg codecs. The Ziran reader checks namespaced XMP, byte ranges and
complete MP4 boxes; stale flags, invalid ranges and malformed files stay ordinary
retained photos. It uses libxml2 and libheif without external XML documents or
network requests, following the
[Android Motion Photo format](https://developer.android.com/media/platform/motion-photo-format).
Samsung JPEG trailers are read through validated SEFH/SEFT directory entries;
Samsung HEIC `sefd` trailers use their validated absolute video pointer, with
only the clip extracted for playback. Camera flags and container item properties
can be XML attributes or scalar elements, independently of their namespace prefix.
Extended JPEG XMP segments are recombined, including out-of-order chunks and
properties split between the standard and extended packet. Incomplete or overlapping
chunks, inconsistent metadata, a mismatched packet digest and invalid pointers
are rejected. Extended packets are limited to one MiB and one referenced stream.
Source rescans recheck older imports once with the new reader. Complete originals,
including camera trailers, remain in backup and exports. Native phone Live Photo
capture remains unfinished.

Guest view lets you hand over the desktop app with only an opened photo or video,
a selection of up to 1000 items, or the current album visible. Choose Guest view
from the gallery or viewer footer. Browse the permitted items, zoom photos and
play videos or Live Photo motion; library navigation, information, settings,
editing, export and sharing actions are unavailable. Hidden and Trash are always
excluded. Later privacy or album membership changes revoke access, and an
unavailable local service clears the displayed items within a short access lease.
The scope survives app and service restarts. Smart albums retain the search rule
chosen when Guest view starts; renames follow the album's identity.
The read-only Ziran gallery engine checks selected identities, current memberships,
saved rules and both Live Photo components in one snapshot. Selections retain
their chosen order across pages, including selections of 1000 items. Temporary
selection identities stay in memory and never become library tables or backup
records. Native checks reject malformed scopes and ignore owner-gallery filters
that could broaden guest access. The local adapter still stores the scope,
checks passwords and dispatches permitted viewing requests.
Set up App lock or a local Hidden password first, then use Leave Guest view and
your app credential (or the Hidden password when app lock is disabled) to return
to the library. The credential stays masked and does not appear in app
inspection state, process arguments or logs. Entering Guest view locks all local
Hidden sessions; leaving does not unlock Hidden or change originals or backup keys.
This implements local desktop handover. Ente's mobile
[Guest view](https://www.ente.photos/help/photos/features/utilities/guest-view)
uses device authentication; system authentication and phone gestures remain
unfinished here. Guest view is an app access restriction; local originals retain
their existing storage format.

App lock adds opening protection to the standalone app and Harmony's Photos
view. Open App lock in the sidebar and choose Set app PIN, Set app password, or
enable with an existing Hidden password. An app PIN contains 4 through 12 ASCII
digits and preserves leading zeroes; an app password contains at least eight
characters. Confirm it before saving. Separate credentials do not require a
Hidden password. They use the native Argon2id verifier, with five failed attempts
per minute. Changing the method or credential locks every local app session.
Unlock Photos uses a separate
local app session and leaves Hidden locked. Lock now closes viewing and editing
and revokes local app sessions. Turn off app lock also requires your password.
The enabled setting and app credential verifier survive restart and stay local
to this device. A service restart, changed app credential or expired session
requires authentication again. Changing Hidden's password also relocks an app
using that password. Hidden's password stays unchanged when setting an app PIN
or password. Guest view uses the current app credential for authenticated exit
when app lock is enabled, and the Hidden password otherwise.
Permission checks continue while viewing; an unavailable service clears photos,
playback, extracted text and other previews within a short access lease. A stale
query or permission response cannot reopen the library after it locks.
Originals, account keys and encrypted ThinkPad backup remain unchanged.
Choose Lock after leaving Photos: Immediately, 5 seconds, 15 seconds, 1 minute,
5 minutes or 30 minutes. Losing keyboard focus starts the background timer;
returning before the deadline cancels it. An expired deadline locks before a
returning focus event or delayed authentication can reveal the library. The
delay stays on this device and survives restart and app credential changes.
Guest view keeps its restricted gallery when focus changes and still requires
authentication to leave. System authentication and phone app-switcher hiding
remain unfinished compared with [Ente app lock](https://ente.com/help/photos/features/account/app-lock).
The screen and its access-expiry decisions are Ziran/Kryon; the local session
adapter still uses Python and the existing native Argon2id verifier.

This version is a local photo library with sync, not complete Ente feature parity.

The local Photos endpoint is a headless Ziran service. It authenticates the
connecting user's OS identity, accepts at most 16 simultaneous connections,
and handles fragmented request frames without blocking other clients. Each
request or response is limited to 1 MiB of valid UTF-8 JSON. Incomplete requests
and stalled replies expire after 60 seconds. Its socket is private to this user;
opening another instance preserves the active endpoint and unrelated files.
Photo/chunk and album/metadata encryption, authenticated decryption, metadata
nonce generation and resumable chunk nonce derivation now run in a headless
Ziran helper over a private binary channel. It retains the existing
XChaCha20-Poly1305 backup format and content keys. An altered tag, account,
address, key or nonce returns no plaintext. A missing or interrupted helper
pauses that operation and preserves originals and keys. Ed25519 device seed
generation, public keys and transaction signatures also run in Ziran and retain
existing device identities. Post-quantum account signing, network
transport/checkpoints and the library/session adapter still use Python.

Create collage combines two through six visible photos or Live Photo stills.
Select them in a gallery, then choose Create collage. Switch between balanced
grid, rows, columns or a leading-photo layout and square, landscape or portrait
shapes. Swap photos lets you tap two tiles to exchange them; tap the first again
to cancel that swap. Drag a tile to frame its image, and use the wheel or Zoom
buttons to magnify it up to four times. Previous photo, Next photo and Reset
frame provide keyboard-independent framing controls. Save copy adds a separate
PNG to Collages, at 2048 pixels on its longest side, with white borders and
transparency composited on white. If that name belongs to a smart or deleted
album, a numbered Collages album preserves the earlier definition. The copy
enters normal encrypted backup and exports.
Preview and saved pixels share the same Ziran layout and framing calculations.
Original files, their metadata and albums stay unchanged; repeating the same
collage keeps the earlier copy's later edits. Hidden, Trash, videos and animated
images are excluded, and access is checked again before saving. Previews clear
when privacy changes or the local service stops responding. Phone pinch gestures,
long-press swapping, device-gallery saving and system sharing remain unfinished.
The selection and arrangement behavior follows Ente's
[collage guide](https://ente.com/help/photos/features/utilities/collage).
Detect QR opens an on-demand local scanner for photos, screenshots and Live Photo
stills outside Trash. Click a detected box or use Previous code and Next code,
then Copy contents to copy that code's complete text, including Unicode and
line breaks. Escape and Back return to the photo, including while a scan is
running. Hidden requires its current local unlock. Access is rechecked while
results are open; privacy changes or a stalled service clear the viewer's decoded
contents within its short access lease. Contents deliberately copied remain on
the system clipboard. Scanning never opens a link, launches another app or makes
a payment. The QR payload is never cached on disk, added to a search index or
sent through encrypted backup.

The headless Ziran helper uses the installed ZBar C engine and GdkPixbuf, with
EXIF orientation and transparent backgrounds handled locally. HEIC/RAW photos
use the existing local decoder. Animated images use their first frame. Scanning
downscales images above 4096 pixels on an edge and accepts up to 32 codes with
32 KiB of combined UTF-8 text; binary and unsupported text payloads are reported
without displaying partial contents. Small, damaged or unusual codes may be
missed; crop a copy to scan a smaller area. Detection makes no network requests;
a missing original is restored only through the pinned ThinkPad client. QR
discovery indexing, phone long-press scanning, link-opening, native sharing and
payment actions remain unfinished. This follows Ente's
[QR behavior](https://ente.photos/help/photos/features/utilities/qr-codes-in-photos),
with those platform actions still explicitly tracked as gaps.

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
of Live Photos. Failed files remain stored and can be retried. Video-content search,
automatic curation and a Ziran tokenizer/indexing service
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

Tag person assigns a named person to a photo, Live Photo still or video even when
no face was detected. Use it in the viewer or with a selection of up to 1000
visible items. Existing names reuse that person; each item supports up to 64
manual people. Remove person tag removes only the manual assignment and retains
detected faces. Not this person in a person's gallery removes manual assignments
as well as correcting detected faces. Manual tags appear in People, name searches,
automatic people albums and eligible still-photo Memory Lane timelines; they
survive source rescans, encrypted ThinkPad backup and export/reimport. Tags do not
mark a photo's face scan complete. Hidden, Trash and Live Photo motion components
cannot be manually tagged, and a mixed private selection is rejected together.

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
- Advanced photo editing, encrypted video streaming, animated editing previews, broader Motion Photo formats, HDR display and broader codec/camera coverage.
- Private sharing, collaboration, public links, comments and account recovery.
  Any sharing design needs to honor the owner's ThinkPad-only sync restriction.
- System authentication, Hidden password recovery and a deliberate deletion/retention policy.
- A complete Ziran library/sync service. The native UI, portable app, gallery/search
  engine, image processor, password verifier, photo/album-property writers,
  original copying, folder discovery, camera/provider/album-sidecar reading
  and ordinary import metadata/source-cache writes are Ziran/Kryon.
  Album-definition import publication, automatic-membership import writes and
  private face-index import writes are native too. Local peer authentication,
  socket framing, connection limits and deadlines also run in Ziran. The remaining
  automatic index mutations and app/Hidden session adapter use Python. Sync content
  encryption/decryption, chunk/metadata nonces and Ed25519 device identities/signatures
  run in Ziran. Post-quantum account key generation and ML-DSA-44 signatures
  use the pinned Oqs package through a private native helper, retaining existing
  account keys and wire signatures. HTTP framing and requests run in a bounded
  headless Ziran helper using libcurl, with numeric loopback as its only destination,
  disabled proxies, refused redirects and a fixed set of signed sync routes.
  Pinned-node identity/policy checks and sync checkpoint orchestration still use Python.

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
The private loopback sync transport needs the host's installed libcurl.
QR detection needs ZBar's installed `libzbar.so.0` and GdkPixbuf; no camera or
network recognition service is used. ZBar is distributed separately under LGPL 2.1;
its [C API reference](https://zbar.sourceforge.net/api/zbar_8h.html) describes the
local image scanner used by the Ziran helper.
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
make motion-window-test motion-portable-test
make playback-window-test playback-portable-test
make face-models people-window-test people-portable-test
make slideshow-window-test slideshow-portable-test
make qr-window-test qr-portable-test
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
