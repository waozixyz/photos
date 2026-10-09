# Changelog

## 0.1.45

- Keep encrypted backup and restore requests on the private ThinkPad connection even when system proxy settings are configured.
- Reject redirected, malformed or oversized node responses while retaining queued changes, original photos and private account keys.
- Check existing encrypted chunks without downloading them again, and retain signed session renewal for interrupted backups.

## 0.1.44

- Keep existing account identities and encrypted backups usable through the account authentication upgrade, without replacing private keys.
- Continue signed photo and album backup requests with new and existing accounts; interrupted authentication preserves originals and private settings.

## 0.1.43

- Keep existing encrypted photo and album backups usable through the encryption upgrade, preserving account and content keys.
- Continue resumable backup uploads of unchanged originals, with changed bytes receiving distinct encrypted chunks.
- Retain existing device identities and signed backup requests through the device authentication upgrade.
- Reject altered encrypted content before returning a restored photo or album, retaining local originals when encryption cannot finish.

## 0.1.42

- Keep Photos responsive when a local client sends an incomplete request or disconnects during an operation.
- Bound simultaneous local connections and retire stalled requests, while retaining private app access and original files.
- Preserve unrelated files and active Photos endpoints when another instance tries to open the library service.

## 0.1.41

- Search and jump to historic capture dates, including photos taken before 1970 and calendar years 0001 through 9999.
- Correct older photo dates with explicit time zones and fractional seconds, preserving them through source rescans, export and encrypted restore.
- Browse memories from earlier calendar years, with privacy exclusions and original files preserved.

## 0.1.40

- Lock Photos automatically after leaving it, immediately or after 5 seconds, 15 seconds, 1 minute, 5 minutes or 30 minutes.
- Return before the selected delay to keep browsing; an expired delay clears photos and previews and requires your app credential again.
- Keep the delay on this device across restarts and credential changes, while Guest view retains its restricted gallery.

## 0.1.39

- Choose a separate app PIN or password to protect opening Photos without needing to set up Hidden first.
- Confirm new credentials before saving; changing them locks every local app session while preserving originals and backup keys.
- Authenticate leaving Guest view with your app credential when app lock is enabled, while keeping Hidden separately locked.
- Keep existing Hidden-password app locks working; background lock timers and device authentication remain unfinished.

## 0.1.38

- Protect opening Photos with your local Hidden password, using a separate app session that leaves Hidden locked.
- Lock the app manually and require authentication after restarting, changing the local password or losing the library service.
- Clear photos, playback and derived previews when app access ends, and keep delayed permission responses from reopening the library.
- Preserve originals, account keys and backup settings when enabling or disabling app lock; background lock timers and separate PIN options remain unfinished.

## 0.1.37

- Keep large Guest view selections in their chosen order across pages, including selections of 1000 photos and videos.
- Check album identity, saved search rules and paired Live Photo privacy together, so later renames keep working and privacy changes revoke viewing.
- Reject malformed guest scopes and prevent gallery options from widening guest access while preserving originals and backup records.

## 0.1.36

- Hand over Photos in Guest view with only a chosen photo, selection or album available, without exposing the rest of your library or editing actions.
- Require your local Hidden password to leave Guest view, retain the restriction after restarting the app, and keep Hidden locked when you return.
- Revoke guest viewing when an item becomes private or leaves its album, and clear previews when the local service is unavailable while preserving originals and backup keys.

## 0.1.35

- Import private face groups, names, birthdays, corrections and manual people tags together with photos, albums and source records.
- Keep later person edits and existing scans during source rescans; warn on malformed private indexes while retaining originals.
- Roll back all imported photo and private index records when a write fails or is interrupted, so retrying can finish the complete import.

## 0.1.34

- Retain automatic album memberships when importing folder exports without face indexes, including their original people rules.
- Preserve later manual album changes during source rescans instead of reintroducing removed automatic memberships.
- Keep photos, albums, automatic memberships and source records together if an import fails or is interrupted; invalid rules produce a warning while originals remain safe.

## 0.1.33

- Import complete album definitions together, preserving descriptions, covers, pinning, archive state, saved searches and automatic people rules.
- Keep later album edits and pending backup changes when an older folder export is imported again.
- Roll back an unfinished album definition when an import fails or is interrupted, leaving source photos and metadata untouched.

## 0.1.32

- Preserve camera capture dates, fractional seconds, time-zone offsets and valid GPS locations when importing JPEG, PNG, WebP, TIFF and HEIC photos.
- Retain historic camera dates through export and encrypted restore, including photos taken before 1970.
- Read provider and album sidecars in Unicode encodings while keeping unknown metadata, complete captions and later library corrections.
- Keep originals safe and report unreadable or malformed metadata; interrupted metadata reading can be retried without blocking the next import.

## 0.1.31

- Discover source photos in a predictable order while excluding provider metadata folders and linked folders.
- Report unreadable source folders so incomplete imports are visible, and skip media-named pipes without waiting for their contents.
- Preserve source files and previously imported photos if folder discovery is interrupted.

## 0.1.30

- Preserve unknown import metadata, explicit empty values, large numbers and complete captions while keeping later date, location and album corrections.
- Keep imported photos, album definitions and source records together when an import fails or is interrupted, so retrying cannot skip an unfinished photo.
- Retain pending backup updates when a changed sidecar is merged, without changing originals or private photo settings.

## 0.1.29

- Import originals through a streaming, verified copy and publish them atomically without replacing an existing stored original.
- Preserve both source files and existing library files when an import changes, fails or is interrupted; reject symbolic links and retain private copy permissions.
- Keep the library available for edits while originals copy, and merge concurrent imports without losing metadata corrections or duplicating stored originals.

## 0.1.28

- Keep album membership, corrected dates, locations and file names atomic across selections and paired Live Photos, preserving originals during invalid edits or expired Hidden access.
- Rename albums in one operation while retaining private members, automatic people rules and later source-rescan corrections.
- Preserve complete album descriptions, saved searches and queued backup changes when album edits fail or are interrupted.

## 0.1.27

- Keep photo-property edits atomic across selections and paired Live Photos, and roll back the whole edit if Hidden access expires before saving.
- Retain Unicode captions and tags, source originals and queued backup edits during interruptions or invalid selections.

## 0.1.26

- Play Samsung HEIC Motion Photos while preserving their camera metadata and complete combined originals.
- Recognize Motion Photos whose JPEG metadata is split across extended XMP segments or uses XML elements for item properties.
- Recheck previously imported photos for these formats, rejecting incomplete metadata and invalid video pointers without altering originals.

## 0.1.25

- Play older Samsung JPEG Motion Photos without altering their original trailer metadata.
- Recognize Google motion metadata written as XML elements as well as attributes, with duplicate and malformed values rejected.
- Retain separation, encrypted backup, export and privacy behavior across the additional camera formats.

## 0.1.24

- Browse Android Motion Photos with embedded video as one item, including modern JPEG, HEIC and AVIF files and legacy Google JPEGs.
- Play the motion and audio locally while retaining the unchanged combined original in encrypted ThinkPad backup and folder exports.
- Separate a motion clip into a video copy, keep captions, dates, albums and privacy settings, and preserve that choice during later imports.
- Reject malformed motion metadata and clips while retaining originals, and revoke playback when Hidden or Trash access changes.

## 0.1.23

- Create collages from two through six photos or Live Photo stills, choosing layouts and square, landscape or portrait shapes.
- Swap positions, zoom and drag each photo's framing, then save a separate PNG in Collages for normal backup and export.
- Preserve source files and albums, exclude private and animated inputs, and clear previews when access ends.

## 0.1.22

- Tag people in photos, Live Photo stills and videos without requiring a detected face, individually or in a selection.
- Find manually tagged people through search and People, use eligible photos in Memory Lane and automatic people albums, and keep assignments in encrypted backup and exports.
- Remove manual tags while retaining detected faces, originals and album memberships; keep private items out of tagging and People.

## 0.1.21

- Detect QR codes in photos, screenshots and Live Photo stills locally, select each code and copy its complete contents.
- Keep multiline and Unicode contents intact, recognize rotated images and clear QR results when privacy access expires or the library becomes unavailable.
- Leave originals unchanged and keep decoded QR contents out of library search and encrypted backup.

## 0.1.20

- Play photos and Live Photo stills from one or several albums as a slideshow, without duplicating photos that belong to multiple selected albums.
- Choose capture order or shuffle, seven time intervals, black or blurred backgrounds, and pause, resume or browse with buttons and keyboard controls.
- Play Memory Lane across eligible years with year and optional age captions, while keeping Hidden, Trash, ignored people and archived memories private.
- Clear displayed slides when access changes or the local service stops responding, and keep originals and encrypted ThinkPad backup unchanged.

## 0.1.19

- Browse a person's Memory Lane by year, with years-ago and optional birth-date captions; keep private, ignored and archived items out of the timeline.
- Jump to a capture date within the current gallery, album and search, and keep that position when opening a photo or refreshing.
- Choose named people to fill ordinary albums with existing and newly indexed matches, and keep or remove earlier automatic additions when stopping a rule.
- Preserve deliberately added memberships, manual removals, birth dates and automatic album rules in encrypted backup and folder exports while leaving originals unchanged.

## 0.1.18

- Enable local face indexing and browse people without sending photos or face data to a recognition service.
- Name people, merge groups with an existing name, ignore groups or correct selected photos while keeping originals unchanged.
- Find named people in search and retain names, face groups and corrections in encrypted ThinkPad backup and library exports; keep Hidden and Trash out of People.
- Keep large libraries responsive in Harmony while loading gallery pages and following theme changes.

## 0.1.17

- Detect text in photos and Live Photos locally, select words or lines over the image, and copy a selection or all recognized text.
- Switch between English, Spanish, Portuguese, German, Simplified Chinese and Japanese without sending images or extracted text to a service.
- Keep recognition out of Trash, honor Hidden locks and revoke extracted text when access ends, while preserving originals and backup keys.

## 0.1.16

- Describe objects, scenes and colors in Semantic search to find local photos by their image content, ordered by relevance.
- Enable local indexing, see its progress, pause and resume it, and retry unavailable originals while keeping completed indexes searchable.
- Keep Hidden and Trash out of semantic results, leave originals unchanged, and include search indexes only inside encrypted ThinkPad backup.

## 0.1.15

- Name places from a photo's location or enter an area manually, and browse matching photos as imports and location corrections update the library.
- Search by place name or combine named places and radius filters with dates, albums and other photo properties, without contacting a geocoding service.
- Change a place's center and radius, rename, pin or remove it while keeping originals and album memberships; preserve definitions in encrypted backup and library exports.

## 0.1.14

- View HEIC/HEIF and supported camera RAW photos with oriented thumbnails and full-size viewing, retaining unchanged originals for export and encrypted backup.
- Browse exported Live Photos as one item, play their motion and audio, and keep both originals in backup and export.
- Apply favorites, privacy, Trash, captions, albums and date corrections to both Live Photo components; separate the files when wanted without losing originals.
- Return from video playback to its photo details with Escape.

## 0.1.13

- Magnify photos with zoom controls or the mouse wheel, drag to pan, fit the image or inspect it at its original pixel size.
- Expand photos to fill the Photos window and keep browsing photos with Previous, Next or the arrow keys; Escape returns to the normal view.
- Keep zoom and pan bounded to the image and reset them when opening another photo, without changing original files.
- Browse photo locations on an offline world map, zoom and drag to explore, and open clustered markers or the visible area as a gallery.
- Filter maps by album or search, include camera GPS on import and keep Hidden and Trash locations off the map.
- Return from a photo with Back without closing the application.

## 0.1.12

- Drag a custom crop area in the photo or video editor, preview it and save a separate copy while preserving the original.
- Keep the selected crop area attached to the image when rotating or flipping, and restore it when cancelling another crop.
- Keep image transparency and video audio, dates, captions, locations and album memberships in cropped copies and encrypted backup.

## 0.1.11

- Watch local videos inside Photos with synchronized audio, pause/resume, seeking, playback speed and volume controls.
- Expand the video view, use Space to pause or resume and the arrow keys to seek five seconds at a time.
- Stop playback when access expires, the library becomes unavailable, a video is hidden or trashed, or you leave the viewer; keep original files intact.

## 0.1.10

- Trim videos to a start/end range, rotate them and apply centered square, portrait or landscape crops with a frame preview.
- Save edited videos as separate MP4 copies with audio, dates, captions, locations and album memberships while keeping originals intact.
- Process video copies in the background, with cancellation and Hidden-lock checks before saving; keep edited copies in encrypted backup and library export.

## 0.1.9

- Combine capture dates, media type, albums, tags, captions, favorites, archive state and location availability in searches, with quoted phrases and exclusions.
- Save searches as smart albums that update when photos are imported or their properties change; rename, pin, edit or remove the saved search while keeping originals.
- Keep smart album searches in encrypted ThinkPad backup and library exports.
- Keep Hidden photos protected if their privacy state changes while a gallery page or selection edit is being processed.
- Change Hidden's local password with confirmation while unlocked; relock all local sessions while keeping originals and backup keys intact.

## 0.1.8

- Protect Hidden with a confirmed local password and masked input, using the same lock in the standalone app and Harmony.
- Require authentication before viewing, exporting or changing hidden photos, including hidden items in Trash and mixed selections.
- Relock when leaving Hidden, pressing Lock Hidden, restarting the local service or reaching the ten-minute unlock limit, including while editing a photo.

## 0.1.7

- Preview photo rotation, horizontal and vertical flips, centered crop presets, brightness, contrast and saturation, then save a separate PNG copy while keeping the original.
- Keep capture dates, captions, tags, locations, album memberships, privacy settings, transparency and color profiles in edited copies.
- Correct or remove location coordinates for a photo or selection, preserving corrections during source rescans, export and encrypted sync.
- Browse memories from the same capture day in earlier years, or choose another day; Hidden, Trash and archived photos stay excluded.

## 0.1.6

- Create empty albums, edit descriptions, choose covers, pin favorites and browse active, archived or empty albums.
- Archive an album to remove its photos from the home timeline while keeping them searchable and available in other albums.
- Keep album details and empty albums in the encrypted ThinkPad backup.
- Export the library into album folders with originals and metadata, and keep the copy current with optional continuous export.
- Preserve files edited outside Photos and refuse export destinations that overlap import folders or the private library.

## 0.1.5

- Restore retained empty originals from their encrypted backup without treating them as unfinished uploads.

## 0.1.4

- Archive photos individually or in bulk while keeping them searchable and available in their albums.
- Browse archived photos and bring them back to the main timeline with Unarchive.
- Find photos without an album in Uncategorized; removing the last membership keeps the original safe.
- Choose oldest-first or newest-first browsing in the gallery and albums.

## 0.1.3

- Keep large ThinkPad backups moving with small encrypted metadata pages.
- Resume interrupted metadata downloads from their last verified page, preserving queued edits.

## 0.1.2

- Select multiple photos to organize, favorite, hide, restore, export or correct their dates together.
- Rename albums and remove photos from an album while keeping originals and other memberships.
- View photo details and use dedicated caption, tag, name and date editors.
- Keep corrected dates and removed album memberships when source folders are scanned again.
- Show useful errors for invalid edits and adapt selection controls to narrow windows.

## 0.1.1

- Open to a newest-first photo feed grouped by day, loading older photos as you scroll.
- Follow Harmony's colors, including theme changes while Photos is open.
- Keep browsing responsive during background library updates.

## 0.1.0

- Photos runs on its own and opens inside Harmony.
- Merge local folders and Ente exports into one library while keeping originals,
  album memberships, dates, captions and location metadata.
- Browse photos and videos by date, search, use favorites and albums, hide items,
  and recover them from Trash.
- Keep a full desktop copy and encrypted originals on the ThinkPad's local
  Daochi node, with offline edits and automatic retry.
- Export untouched originals with their metadata.
