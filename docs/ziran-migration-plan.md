# Photos: 100% Ziran migration plan

Scope: finish migrating standalone Photos and its tests to Ziran, absorb the
Breeze Photos features, and coordinate removal of Breeze's duplicate app.
The authenticated parent coordinator's continuation handoff also confirms the
owner's requirements that Photos be independent and not use Harmony. These
scope additions are retained separately from the original inventory request.
This task remains planning only; the parent coordinates implementation.
Do not commit/push this plan separately while the full implementation and its
required local suite are incomplete. No node/account replacement or data deletion
is implied by removing the application's Harmony integration.

Source inventory: 2026-11-05, Photos 37fbff3 and Breeze f1cc5be4. Keep this
document as the migration record and update completion evidence as work lands.

## Ground rules for every contract

- **Dependency B0:** the initial Kryon graph/resolver mismatch was resolved
  through the normal installed toolchain and package commands; the integrated
  local suite now runs. Active canonical AVIF/WebP work may change dependency
  declarations, requiring a normal `ziran lock` refresh. Coordinate upstream
  ownership and never edit Kryon or vendor copies from this task. Remaining
  development work still needs upstream integration before reproducible publishing; see current
  continuation evidence below. Complete and validate the full migration before
  committing application work.
- **Compatible, restartable migrations:** keep
  `library.sqlite` schema, table/column names, settings keys and JSON values;
  `packed` JSON (sorted keys, `,`/`:` separators, UTF-8 not escaped);
  `atomic` writes (temp file, fsync, rename); `private/identity.json`,
  `device.json`, `hidden-password.json`, the app-lock file, `.worker.lock`;
  the recovery file (`MAGIC` + `crypt`); `~/.config/photos/config.json` keys;
  entity/annotation/legacy collection names and unknown record fields; the
  socket `$XDG_RUNTIME_DIR/kryon/services/photos.sock`. Preserve the account's
  shared entity data on the desktop node and the retained ThinkPad copies.
  Any schema addition needs an older-database fixture, an idempotent migration
  and rollback evidence. Live library: `/mnt/storage/Photos`, 0700; never use it
  as a test fixture or print its records, account keys or content keys.
- **Architecture:** one Ziran executable, `photos-service`, replaces
  `service.py` and `worker.py`. It serves UI requests in-process; with
  `--daemon` it is the import/sync/export worker. Modules that are already Ziran
  pipe helpers (sync, crypto, SQLite query/edit, vault, access, import writers)
  get exported functions that the service calls in-process. Their `main` stays
  only where Android or a test still uses it. Helpers that link heavy native
  libraries (`photo_decode`, `image_edit`, `video_edit`, `semantic_infer`,
  `face_*`, `detect_text`, `detect_qr`, `motion_photo`, `read_metadata`)
  stay separate processes, started with `std/process_run_linux`, so a crash
  stays isolated as it does today.
- **Porting:** each port adds a new Ziran module and a Ziran test that
  replaces the Python test named in its contract. It does not edit or delete
  Python. Python keeps running the installed app until cutover (C51) and is
  deleted at the end (C50), after desktop and Android parity is verified.
- **Tests:** unit tests are `tests/<name>_test.zi`, run with
  `build-queue -- make ztest-<name>`; the whole suite is
  `build-queue -- make ztest`. Integration tests that
  need an emulator or a real node are `tests/<name>_it_test.zi`, run with
  `make ztest-<name>_it` and left out of `make ztest`. GUI tests are
  `tests/<name>_gui_test.zi`, run with `make zgui-<name>`, only on private
  Xvfb with the inherited display, auth and session variables removed and
  desktop recovery disabled. Use synthetic photos only. All build/test commands
  below are target names: run them through `build-queue --`, including Gradle
  and upstream package tests. Test outputs use existing ignored build folders.
- **File ownership:** C02 owns the initial shared test types and Makefile
  targets; C41, then C43, then C50 serialize later Makefile changes. C27 owns
  manifest and lock integration for all upstream contracts, including Android;
  use `ziran add/update`, never hand-edit locks or commit local overrides.
  C09 precedes C32 and C52 on app/mobile files; C19 precedes C32 on import.
  C50 alone updates AGENTS.md, README.md and CHANGELOG.md at final cleanup.
  Every test path belongs to its listed contract. See the ready sets below.
- **Each contract finishes with:** its test passing with the exact command and
  output reported, the applicable integrated local suite passing, and
  `git diff --check`. Finish and validate the whole assigned implementation
  before one coherent commit per affected repository, then push as
  Waozi <waozi@proton.me> without AI co-author attribution. No partial commits
  or commits with blocked required local tests. GitHub CI and installed/released
  verification are separate evidence, never inferred from local tests.
- The live project session query returned `Agent read unavailable` on
  continuation. The saved plan session is stopped; check managed active/warm
  tasks again before a coordinator launches implementation contracts.

## 1. Inventory

Status: **A** = thin adapter over an existing Ziran executable (delete once
its caller is Ziran); **P** = real logic to port; **U** = needs an upstream
Ziran/Kryon package first.

### Runtime Python (`photos/`, 6,632 lines)

All are called by the installed `photos.service` (`service.py`) and
`photos-sync.service` (`worker.py --daemon`) systemd user units through
`worker.process`. The Ziran app reaches them only through the socket
(`src/transport.zi`). Android uses none of them.

| Module | Lines | Does | Existing Ziran | Status | Contract |
|---|---|---|---|---|---|
| library.py | 653 | schema and idempotent migrations, settings, paths, Pillow previews, import, albums, details, query, export | library_query, library_edit, album_* | P | C14–C19 |
| sync.py | 895 | Daochi v6 entity sync client: login, put/get chunks, entity/annotation records, legacy move, retire, materialize | sync_*, entity_crypto, node_policy | P | C33–C36 |
| worker.py | 601 | request router (`process`, `library_request`, `ui_query`), daemon loop, CLI | — | P | C39, C40 |
| service.py | 268 | adapter behind the native broker, socket activation, lifecycle | photos_service.zi (broker) | P | C41 |
| people.py | 345 | face pipeline orchestration, catalog, edits, index export/import | face_infer/pixels/match, people_* | P | C26 |
| semantic.py | 279 | CLIP orchestration; `text_tensor` (HF tokenizers), `image_tensor` (numpy, Pillow) | semantic_infer, semantic, semantic_index | P+U | C27 (U: C03, C04; X7 only if needed) |
| recovery.py | 271 | recovery file save/read/restore/reset/route | recovery_crypto.zi | P | C22 |
| importing.py | 260 | metadata reader process, walk, merge, copy | import_*, read_metadata | P | C19 |
| video.py | 252 | video jobs, ffprobe, render, frame preview (Pillow) | video_edit.zi | P | C24 |
| app_lock.py | 212 | app lock settings, grant/revoke/state/route | app_lock_policy.zi | P | C20 |
| native_edit.py | 205 | authorization in front of the SQLite writer | library_edit.zi | A | C23 |
| export.py | 199 | resumable one-way folder export | — | P | C31 |
| guest.py | 190 | read-only presentation scope | guest_scope.zi (policy) | P | C21 |
| motion.py | 174 | embedded motion detect/extract/separate | motion_photo.zi | P | C25 |
| editing.py | 156 | raster edit options/render/preview/save | image_edit.zi | P | C23 |
| collage.py | 152 | collage prepare/check/render/save | collage_layout/pixels | P | C29 |
| vault.py | 151 | hidden-gallery password and state | vault_auth.zi | P | C20 |
| slideshow.py | 135 | slide selection, background, frame | presentation.zi | P | C29 |
| ocr.py | 131 | OCR models, languages, parse | detect_text.zi | A | C28 |
| access_sessions.py | 130 | client of the native access registry | access_sessions.zi | A | C20 |
| service_units.py | 126 | systemd unit text, desktop node address | — | P | C42 |
| live.py | 100 | Live Photo pairing/separation | live_photo.zi (SQL) | P | C25 |
| sync_http.py | 92 | loopback encrypted request framing | sync_http.zi | A | C08 |
| qr.py | 79 | QR scan and parse | detect_qr.zi | A | C28 |
| entity_crypto.py | 73 | seal/open entity chunks | entity_crypto.zi | A | C07 |
| sync_crypto.py | 66 | encrypt/decrypt, device keys | sync_crypto.zi | A | C07 |
| decoding.py | 63 | HEIF/RAW display assets | photo_decode.zi | A | C16 |
| places.py | 59 | named areas as saved albums | geo.zi, locations.zi | P | C30 |
| account_sign.py | 56 | ML-DSA-44 keys/sign/verify | account_sign.zi | A | C07 |
| sync_transaction.py | 52 | signed v6 transactions | sync_transaction.zi | A | C08 |
| auto_albums.py | 50 | people-rule albums | people_albums.zi | P | C26 |
| owner_account.py | 44 | reads the account from desktop Inbe (`~/.local/share/inbe/inbe.db`) | — | P | C06 |
| sync_account.py | 44 | login/registration builders | sync_account.zi | A | C08 |
| sync_pages.py | 35 | paged-sync merge pipe | sync_page.zi | A | C08 |
| query.py | 33 | query engine adapter | library_query.zi | A | C18 |
| `__init__.py` | 1 | package marker | — | delete | C50 |

### Scripts

| Script | Does | Called by | Ziran target | Contract |
|---|---|---|---|---|
| install.py (300) | copy build, create venv, write units, optional Harmony catalog | `make install` | `src/install.zi`, no venv or Harmony registration | C42 |
| service.py, worker.py (7 each) | launchers | systemd units | `photos-service`, `photos-service --daemon` | C41 |
| face/semantic/text-models.py | download pinned public models, check SHA-256 | `make *-models` | `src/tools/fetch_models.zi` | C10 |
| provision-node.py | register Photos on a local Daochi node | manual | `src/tools/provision_node.zi` | C11 |
| map_image.py | Natural Earth GeoJSON → `assets/world.png` (Pillow) | manual | `src/tools/map_image.zi` | C12 |
| android-crypto.py | build libsodium for Android ABIs | `make android` | CMake recipe in `droid/app/src/main/cpp/CMakeLists.txt` | C13 |
| android-fixtures.py | synthetic emulator recovery file (PyNaCl) | `make android-test-fixtures` | `src/tools/android_fixtures.zi` | C13 |

### Python tests (83 files) and C test helpers

- 45 `test_*.py` unit tests. Each is ported by the contract that ports
  its module (named in that contract).
- 25 `*_window.py` files plus `window.py`, `portable.py` and
  `scroll_performance.py` drive the app with xdotool and read the
  `_HARMONY_APP_STATE` X property. They become Ziran GUI tests in C43–C47.
- `hosted.py`, `host_integration.py`, `host_lifecycle.py`: keep relevant
  readiness/close/restart assertions in standalone tests (C44); remove host-only
  tests in C50.
- Fixture builders `codec_fixtures.py`, `motion_fixtures.py`,
  `qr_fixtures.py`, `owner_fixture.py` become builders in `tests/kit.zi`
  (C02, extended by C16/C25/C28/C06). A fixture that needs an encoder Ziran
  lacks (HEIC, RAW) is committed as a small synthetic binary under
  `tests/fixtures/`. C02 owns `tests/kit.zi`; later contracts put additional
  builders in their own `<feature>_fixtures.zi`, avoiding parallel kit edits.
- `sync_node.py` → C49. `android_gallery.py`, `android_upload_node.py` → C48.
- `test_backup_*.py` and `backup_staging_memory_test.c` → C53.
  `scroll_frame_probe.c` → C44. `gallery_text_test.sh` becomes a Ziran source/saved-IR driver in C44.
- `requirements.txt` (Pillow, PyNaCl, pqcrypto, numpy, onnxruntime,
  tokenizers) is deleted in C50. ONNX Runtime is already loaded from Ziran
  through its C API (`-ldl`).

### Android `droid/` (8 production Java files, 1 Java test, no Kotlin)

The interim Python-free desktop milestone is not 100% Ziran completion.
Android's `CMakeLists.txt` uses
`HOST_PYTHON` to run Kryon's `generate_android_sources.py` and
`build_android_runtime.py` (upstream, X4). `scripts/android-crypto.py` →
C13. D1–D3 port all handwritten Java before C50:

| Java | Does | Phase 2 target |
|---|---|---|
| PinnedNode.java (154) | loopback HTTP to the pinned node over USB | Ziran socket HTTP in `src/android_upload.zi` (D1) |
| DeviceGallery (506), BackupQueue (550), DeviceBackup (196), DeviceLock (139), FontAsset (52) | MediaStore, Keystore, document picker, device auth, fonts | Ziran over `kryon/AndroidJni` (D2) |
| PhotosActivity (388), BackupScanJob (53) | manifest components (Activity, JobService) | Kryon-generated stubs (X6), then D3 |
| androidTest/PhotosGalleryProbe (784) | instrumentation test | D3, after X6 |

### Harmony couplings to remove without moving user data

| Coupling | Standalone replacement | Contract |
|---|---|---|
| AGENTS.md/README hosting and publishing instructions | standalone Photos desktop/Android installation | C50 |
| `src/desktop.zi` `_HARMONY_APP_STATE` and Python GUI readers | `_PHOTOS_APP_STATE` with Ziran private-display readers | C05, C43 |
| Hosted targets and `hosted.py`, `host_integration.py`, `host_lifecycle.py` | retain relevant readiness/close/restart coverage in standalone Ziran tests, then delete host-only tests | C05, C44, C50 |
| `install.py --harmony` catalog writes and Harmony bundle discovery | Ziran installer and desktop entry | C42 |
| `--shared-node`/proxy units referring to the desktop Harmony node | retain address, pin and shared entity records; use independently managed Daochi service | C37, C42, X2 |
| `provision-node.py` Harmony-specific naming | generic owner-node provision CLI, same protocol and private-node policy | C11 |
| Kryon `_HARMONY_APP_THEME`/`_HARMONY_APP_COMPACT` reads | upstream neutral properties or explicit standalone profile | X3 |
| Harmony catalog/default slot and portable publishing | remove Photos entry from the hosting surface through its owning task | X1 |

Kryon's `zx` player, `KRYON_SERVICE_ROOT` and the local socket location are
reusable Kryon capabilities and remain. Keep recovery disabled in private
display tests even when removing Harmony-specific environment names.

The saved draft's statement that no Photos data reached the desktop node was
not established by current evidence. Installed state is unknown. X2/C37/C51
must verify actual node/process/data ownership without exposing private records.
Prefer retaining the same node identity and data store under an independent
service. If copying is required, verify the destination and keep the source;
never select a new node or delete old data on an assumed default.

**Python outside this repo that Photos' build uses (X4):** Kryon
`scripts/generate_android_sources.py`, `build_android_runtime.py`,
`android_scaffold.py` (`cmake/KryonAndroid.cmake`), `renderer_artifact.py`
(run by `src/project/cli_linux.zi:611`); Python in the Makefiles of
`ziranlang/ziran`, `kryonlabs/kryon` and the `sqlite`, `oqs`, `avif`
packages. X4 covers only the tooling invoked by Photos' build, not an
unbounded rewrite of every dependency's developer tooling.

### Canonical reusable packages inspected

- `ziranlang/packages/daochi-client`: existing `crypto_native` implements
  SHA-256/HMAC, XChaCha20-Poly1305, Argon2id, random bytes and clearing;
  `keys`, `entity`, `record`, `transaction`, `wire`, `client`, `sync_flow`
  cover identity validation, encrypted chunks and signed/paged protocol.
  C07/C08 reuse these exports; only Photos framing/storage stays here.
- `ziranlang/packages/crypto` exports crypto/AES/bigint; `oqs/Oqs` exposes
  ML-DSA-44/ML-KEM-768; `sqlite/SQLite` already supports binding, busy timeout
  and backup. No app-local replacement packages are needed for these APIs.
- `std/net_http` and `std/net_http_curl_linux` already serve `sync_http.zi`.
  HTTP policy belongs in Photos; reusable networking stays upstream.
- `ziranlang/packages/image-codec` exists and uses `std/image.Image`;
  AVIF and WebP have separate codec packages. Reuse this boundary for C04;
  do not put a codec or generic resizer in Photos. AVIF integration remains
  with its active upstream owner, subject to B0.
- No CLIP tokenizer or general inference package was found in the canonical
  package directory listing. C03 and X7 propose those packages. Existing
  `semantic_infer.zi` uses ONNX Runtime's C API via `dlopen`; extract the
  reusable loader/session/tensor layer upstream while retaining model-specific
  behavior and process isolation here. Also cover face/OCR inference users.
- Read before implementing: Ziran `docs/USAGE.md`, Kryon `docs/API.md`, and
  each affected package's entry instructions. Manifest exports show source
  availability, not proof that downstream compilation currently works.

## 2. Breeze Photos features missing here

Source: Breeze `git show HEAD:<path>` at f1cc5be4.

| Feature | Breeze source | Here today | Contract |
|---|---|---|---|
| Day headings: Today/Yesterday, localized date template with `{d}{dd}{m}{mm}{M}{y}` and month names | `apps/photos/photos_view.zi` `DayHeading`, `MonthKey`, `DayNumber`, `DayBefore`, `MonthDays` | English only (`mobile_app.zi:117`) | C09 |
| UI strings in Breeze's 12 languages: `photos_today`, `_yesterday`, `_date`, `_tab_library`, `_tab_trash`, `_trash_note`, `_not_here`, `_previous`, `_next`, `_all`, `_added`, `_already_added`, `_add_failed`, `_change_failed` | Breeze locale files (`git grep photos_today`) | English only | C09 |
| Achievements: 10 entries, metrics `photo.original` (backfill), `photo.import`, `photo.open`, 12 locales, icons | `apps/photos/photos_achievements.zi` `AchievementPhotoBackfill`, `AchievementPhotoImported`; `src/apps/achievement_media.zi` `AchievementPhotoOpened`; `apps/photos/achievements.json`, `achievement-locales/*.json` | none | C32 |
| Entity record fields `day` (camera-clock capture day; Diary files `photos/YYYY/MM/DD/<sha256>.<ext>`), `taken`, `capture` | `src/apps/entity_photos.zi` header, `src/apps/media_store.zi` `MediaEntityEntry`, `MediaWhen`, `MediaTakenAt` | writes `taken_at` only | C34 |
| Pick photos on Android and add them to the account library | `photos_view.zi` `MediaPickHost`, `FollowPicks` | camera backup and granted MediaStore only | C52 |
| Read Breeze 1.0.x `cell.media.*` records (`private.inbe.v1.media`) not yet moved | `media_store.zi` `MediaParseLegacy`, `src/apps/media_diary_photos.zi` | none | C38, only if Breeze stops the move (X5) |
| Trash/restore, sync status, original on demand | `MediaUpdateHost`, `MediaStatusHost`, `MediaAssetHost` | already present | — |
| Per-account media folders, sidebar icon `assets/photos/sidebar.shape` | `media_store.zi` `MediaDirectory` | not needed (one owner account; `assets/photos.svg`) | — |

## 3. Contracts

Format: behavior · files · signature · test · deps. Signatures are the
public entry point; helpers stay private. "Ports" names the Python it
replaces; the new behavior must match it, including the error texts the UI
shows.

### Foundation contracts (dependencies determine readiness)

- **C01 Golden fixtures.** Run the current Python service on a synthetic
  library and record the request/response JSON for every action in
  `worker.library_request`, `recovery.route`, `app_lock.route`,
  `guest.route` and `vault`. Also dump the schema plus synthetic rows as
  `library-v0.1.59.sql` and save a Python-made recovery file and a sealed
  entity record. · `tests/golden_capture.py` (temporary; deleted by C50),
  `tests/fixtures/golden/**` · — · `build/venv/bin/python tests/golden_capture.py`
  writes deterministic schema/responses twice with no diff; randomized
  encryption fixtures are compared by cross-language decryption · uses existing `build/` helpers;
  otherwise B0.
- **C02 Ziran test kit.** Temp library dirs, synthetic JPEG/PNG writers
  (`std/jpeg`, `std/png_linux`), a SQL fixture loader, assertions; Makefile
  rules `ztest-%`, `ztest`, `ztool-%` (`src/tools/%.zi` → `build/tools/%`) with
  per-target configurable linker libraries; the basic kit must not require
  unrelated heavy dependencies. `ztest` excludes integration and GUI tests. · `tests/kit.zi`,
  `tests/kit_test.zi`, Makefile · `Kit.Library(dir: string) -> string`,
  `Kit.Jpeg(path: string, w: s32, h: s32, rgb: u32) -> bool` · `make ztest-kit`
  · B0.
- **C03 Upstream CLIP tokenizer package.** Check `ziranlang/packages/*`
  first. If none exists, create `ziranlang/packages/clip-tokenizer`: byte-level
  BPE from `tokenizer.json`, truncate/pad to 77, BOS 49406, EOS/pad 49407. ·
  new package repo · `Encode(tokenizer: *Tokenizer, text: string, out: []s64) -> bool`
  · package `make check`, with ids for 20 phrases matching the HF tokenizer
  (fixtures generated once, committed) · —.
- **C04 Upstream image resize.** Check `image_codec` first. Add or reuse an
  RGB8 bicubic shortest-edge resize, center crop and EXIF orientation. ·
  `ziranlang/packages/image-codec/src/` · `ResizeShortest(src: Image, edge: s32) -> Image`,
  `CenterCrop(src: Image, w: s32, h: s32) -> Image` · package tests, within 1/255
  of Pillow BICUBIC on fixtures · —.
- **C05 Standalone desktop integration.** Rename the state property to
  `_PHOTOS_APP_STATE` and update private-display readers; preserve readiness,
  close and restart behavior without host interaction. Final removal of
  host-only tests/targets and doc updates belongs to C50. · `src/desktop.zi`,
  `tests/*.py` (property name only), `tests/standalone_contract_test.zi` ·
  `StateProperty() -> string` · `make ztest-standalone_contract` and
  `make window-test` on private Xvfb · C02. Do not remove recovery disabling.
- **C06 Owner account.** Ports `owner_account.py`: read the account
  public/private key from Inbe's SQLite and validate against Photos' identity;
  keys never printed. · `src/owner_account.zi`, `tests/owner_account_test.zi`
  (ports `owner_fixture.py`) · `Load(database: string) -> OwnerAccount, string`
  · `make ztest-owner_account` · C02.
- **C07 In-process crypto.** Export library functions next to the existing
  pipe `main`s. · `src/sync_crypto.zi`, `entity_crypto.zi`, `account_sign.zi`,
  `recovery_crypto.zi`, tests · `EntitySeal(key: []u8, plain: []u8, out: *Vec(u8)) -> bool`,
  `EntityOpen(...)`, `SyncEncrypt/Decrypt`, `DeviceSignature`, `AccountSign/Verify`,
  `RecoveryCrypt(password: []u8, data: []u8, seal: bool, out: *Vec(u8)) -> bool`
  · `make ztest-sync_crypto ztest-entity_crypto ztest-account_sign ztest-recovery_crypto`
  (ports test_sync_crypto, test_entity_crypto, test_account_sign; opens C01's
  Python-made recovery file and sealed record) · C02.
- **C08 In-process transport.** Same for HTTP framing, page merge,
  transactions, account requests, node policy. · `src/sync_http.zi`,
  `sync_page.zi`, `sync_transaction.zi`, `sync_account.zi`, `node_policy.zi`,
  tests · `HttpRequest(frame: SyncFrame) -> SyncResponse, string`,
  `ApplyPage(db: *void, page: []u8, cursor: string) -> bool`,
  `BuildTransaction(...)`, `LoginRequest(...)`, `RegistrationRequest(...)` ·
  `make ztest-sync_http ztest-sync_page ztest-sync_transaction ztest-sync_account`
  (ports test_sync_http, test_sync_pages, test_sync_transaction,
  test_sync_account) · C02.
- **C09 Localized day headings (Breeze).** `DayHeading` with Breeze's
  templates and month names, the photos_* strings in 12 languages, wired into
  desktop and mobile; include every Photos locale key used by the source,
  including empty/loading/picker/trash notices and month names. · `src/day_heading.zi`,
  `assets/locales/*.json`, `src/app.zi`, `src/mobile_app.zi`,
  `src/mobile_media.zi`, `tests/day_heading_test.zi` ·
  `DayHeading(day: s32, today: s32, locale: string) -> string` ·
  `make ztest-day_heading` (Today, Yesterday, de/ja/ru templates, leap days) · C02.
- **C10 Model fetch tool.** Ports the three `*-models.py`: read
  `assets/*-MODELS.json`, download, check SHA-256, place atomically in
  `build/models/<kind>`. · `src/tools/fetch_models.zi`,
  `tests/fetch_models_test.zi` · `Fetch(manifest: string, dest: string) -> bool, string`
  · `make ztest-fetch_models` (local `file://` fixture, bad hash rejected) · C02.
- **C11 Provision-node tool.** Ports `provision-node.py` with a generic
  `--origin/--env` (H9); loopback/private address only. ·
  `src/tools/provision_node.zi`, test · `Provision(origin: string, env: string) -> bool, string`
  · `make ztest-provision_node` (fake loopback node) · C02.
- **C12 Map image tool.** Ports `map_image.py`. · `src/tools/map_image.zi`, test
  · `Render(geojson: string, output: string, w: s32, h: s32) -> bool` ·
  `make ztest-map_image` (output matches `assets/world.png` dimensions and land
  pixel count within 0.5%) · C02.

### Wave 1: library core

- **C14 Library store.** Ports `Library.__init__`, `setting`, `set_setting`:
  mkdir 0700, WAL, timeout 60 s, the exact schema and every idempotent
  migration, including `album_catalog_migrated`. · `src/library_store.zi`,
  `src/service_types.zi` (Config/NodePin shared types),
  test · `Open(root: string) -> *Library, string`,
  `Setting(lib: *Library, key: string) -> string`, `SetSetting(...) -> bool`,
  `Close(lib: *Library)` · `make ztest-library_store` (fresh DB, C01's 0.1.59
  dump migrated twice, rows unchanged) · C01, C02.
- **C15 Library files.** Ports `packed`, `atomic`, `digest_file`,
  `photo`/`original`/`thumbnail`/`preview`/`sidecar`. Previews come from a new
  `thumbnail` op in `image_edit` (oriented RGB JPEG, same size as today). ·
  `src/library_files.zi`, `src/image_edit.zi`, test ·
  `Packed(json: string) -> string`, `Atomic(path: string, data: []u8) -> bool`,
  `Preview(lib: *Library, id: string) -> string, string` ·
  `make ztest-library_files` (ports the path/preview parts of test_library) · C14.
- **C16 Decode service.** Ports `decoding.py`. · `src/decode_service.zi`,
  `tests/fixtures/codec/*`, test · `Display(lib: *Library, photo: PhotoRow) -> string, string`,
  `Supported(ext: string) -> bool` · `make ztest-decode_service` (ports
  test_decoding, codec_fixtures) · C15.
- **C17 Albums.** Ports `album_name`, `rename_album`, `_ensure_album`,
  `album_details`, `edit_album`, `album_list`, `edit`/`edit_many`/`_edit`
  (in-process `library_edit`), `details`. · `src/library_albums.zi`, test ·
  `AlbumList(lib: *Library, request: string) -> string`, `RenameAlbum(...)`,
  `Edit(lib: *Library, ids: []string, change: string) -> string` ·
  `make ztest-library_albums` (album parts of test_library and the single-photo
  `Library.export` path) · C15. Also expose the existing writer in
  `src/library_edit.zi`; C23 calls it.
- **C18 Gallery query.** Ports `worker.ui_query`, `query.page`/`validate`,
  `Library.query`/`_query`, calling `library_query` in-process. ·
  `src/gallery_query.zi`, test · `UiQuery(lib: *Library, request: string) -> string`
  · `make ztest-gallery_query` (ports test_search, test_storage, test_similar,
  test_discovery; matches C01 goldens) · C14.
- **C19 Import.** Ports `import_file`, `import_folders` and `importing.py`
  (persistent `read_metadata` reader, walk, merge, copy, sidecar, camera,
  album metadata), driving the existing import executables. Sources stay
  read-only. · `src/library_import.zi`, test ·
  `ImportFolders(lib: *Library, sources: []string) -> ImportResult` ·
  `make ztest-library_import` (ports test_importing, test_camera_metadata) ·
  C15, C17.
- **C13 Android build without Python.** libsodium for the ABIs as a CMake
  recipe; emulator fixtures in Ziran. The Makefile rewiring waits for C50. ·
  `droid/app/src/main/cpp/CMakeLists.txt`, `src/tools/android_fixtures.zi` ·
  `main` writes `build/android/fixtures/{recovery,expected}.bin`,
  `build/android/node-seed.bin` · `make ztool-android_fixtures` then
  `make android` · C07.
- **C53 Backup tests.** Ports test_backup_identity, test_backup_staging,
  test_backup_staging_memory, test_backup_upload and
  `backup_staging_memory_test.c` into Ziran tests against the existing
  `backup_*.zi`. · `tests/backup_*_test.zi` · — · `make ztest-backup_identity`
  and the other three · C02, C07.

### Wave 2: features (parallel; each touches only its own files)

- **C20 Access.** Ports `access_sessions.py` (in-process registry), `vault.py`,
  `app_lock.py`. · `src/access_registry.zi`, `src/vault.zi`, `src/app_lock.zi`,
  tests · `AppLockRoute(lib: *Library, request: string) -> string, bool`,
  `VaultAuthenticate(lib: *Library, password: []u8, setup: bool) -> string, string`
  · `make ztest-access_registry ztest-vault ztest-app_lock` (ports
  test_access_sessions, test_vault, test_app_lock) · C14.
- **C21 Guest.** Ports `guest.py`. · `src/guest.zi`, test ·
  `GuestRoute(lib: *Library, request: string) -> string, bool` ·
  `make ztest-guest` (ports test_guest) · C18, C20.
- **C22 Recovery.** Ports `recovery.py`; file format byte-identical; restore
  only into an empty library with the worker stopped. · `src/recovery.zi`, test
  · `RecoveryRoute(lib: *Library, request: string, config: Config) -> string, bool`
  · `make ztest-recovery` (ports test_recovery; restores C01's Python-made
  file) · C06, C07, C20.
- **C23 Edits.** Ports `editing.py` and `native_edit.py`. ·
  `src/edit_service.zi`, test · `ImagePreview(...)`, `ImageSave(...) -> string`,
  `Change(lib: *Library, request: string) -> string` · `make ztest-edit_service`
  (ports test_editing, test_native_edit) · C15, C17.
- **C24 Video.** Ports `video.py` (job thread, ffprobe, render through
  `video_edit`, frame preview). · `src/video_service.zi`, test ·
  `VideoStart(...) -> string`, `VideoState(lib: *Library, job: string, cancel: bool) -> string`
  · `make ztest-video_service` (ports test_video) · C15.
- **C25 Live and motion.** Ports `live.py` and `motion.py`. ·
  `src/live_motion.zi`, `tests/fixtures/motion/*`, test ·
  `LiveDetect(lib: *Library)`, `LiveSeparate(...)`, `MotionDetect(...)`,
  `MotionExtract(...)` · `make ztest-live_motion` (ports test_live,
  test_motion, motion_fixtures) · C17.
- **C26 People.** Ports `people.py` and `auto_albums.py`. ·
  `src/people_service.zi`, `src/auto_albums.zi`, tests ·
  `PeopleKick(lib: *Library)`, `PeopleState(...) -> string`,
  `PeopleIndexBlob(lib: *Library, id: string) -> string`,
  `AcceptPeopleIndex(...) -> bool` · `make ztest-people_service ztest-auto_albums`
  (ports test_people, test_manual_people, test_native_people,
  test_auto_albums, test_face_import) · C16, C17.
- **C27 Semantic.** Ports `semantic.py`; tokenizer from C03, tensor from C04 +
  C16; adds the package dependencies. · `src/semantic_service.zi`, test,
  `ziran.toml`, `ziran.lock`, `ziran.local.toml` · `SemanticKick(lib: *Library)`,
  `SemanticSearch(lib: *Library, text: string, request: string) -> string`,
  `SemanticIndexBlob(lib: *Library, id: string) -> string` ·
  `make ztest-semantic_service` (ports test_semantic, test_semantic_index;
  tensors match C01's within 1e-4) · C03, C04, C16, C18. X7 is conditional on a demonstrated migration need. Integrate all upstream package dependencies here,
  including Android, using Ziran package commands rather than hand-editing locks.
- **C28 OCR and QR.** Ports `ocr.py` and `qr.py`; results stay in memory. ·
  `src/scan_service.zi`, `tests/fixtures/qr/*`, test ·
  `TextScan(lib: *Library, id: string) -> string`, `QrScan(...) -> string` ·
  `make ztest-scan_service` (ports test_ocr, test_qr, qr_fixtures) · C16.
- **C29 Collage and slideshow.** Ports `collage.py` and `slideshow.py`. ·
  `src/collage_service.zi`, `src/slideshow_service.zi`, tests ·
  `CollageRender(...)`, `CollageSave(...) -> string`,
  `SlideFrame(lib: *Library, request: string) -> string` ·
  `make ztest-collage_service ztest-slideshow_service` (ports test_collage,
  test_slideshow) · C16, C18.
- **C30 Places.** Ports `places.py`. · `src/places_service.zi`, test ·
  `PlaceCreate(lib: *Library, value: string, id: string) -> string`,
  `PlaceUpdate(...)` · `make ztest-places_service` (ports test_places,
  test_locations) · C17.
- **C31 Export.** Ports `export.py`: resumable, removes only unchanged files
  it owns, cancels on revision change. · `src/export.zi`, test ·
  `ExportRun(lib: *Library, dest: string, sources: []string, materialize: Materializer, cancelled: () -> bool) -> ExportResult`
  · `make ztest-export` (ports test_export) · C15.
- **C32 Achievements (Breeze).** 10 achievements with Breeze's ids and
  metrics: backfill `photo.original` from captured originals, record
  `photo.import` on import and `photo.open` when the viewer resolves an
  original; a new `achievements` table (migration-safe); an Achievements
  screen; 12 locales and icons copied from Breeze HEAD. ·
  `src/achievements.zi`, `assets/achievements/**`, `src/model.zi`, calls in
  `src/library_import.zi` and `src/app.zi`, schema migration in
  `src/library_store.zi`, test · `AchievementBackfill(lib: *Library) -> s32`,
  `AchievementRecord(lib: *Library, metric: string, subject: string, day: s32) -> bool`
  · `make ztest-achievements` · C09, C14, C19.
- **C52 Android add-from-picker (Breeze).** System photo picker → existing
  staging/encryption/upload; notices from C09. · `droid/.../DeviceGallery.java`,
  `src/android_queue.zi`, `src/mobile_app.zi` ·
  `PickAdded(results: []PickedItem) -> s32` · `make android` plus emulator
  check in C48 · C09.

### Wave 3: sync (parallel with wave 2)

- **C33 Sync client.** Ports `Client.__init__`, `raw`, `check_node`,
  `origin`, `login`, `header`, `request`, `record_key`, `put`, `put_content`,
  `get_content`, `fetch_blob` and the module helpers (`encode`…`text`). Pinned
  loopback/private nodes only; the docstring loses Harmony. ·
  `src/sync_client.zi`, test · `ClientOpen(lib: *Library, config: Config) -> *SyncClient, string`,
  `PutContent(c: *SyncClient, id: string, chunks: ChunkSource) -> []Blob, s64`
  · `make ztest-sync_client` (fake loopback node fixture) · C06, C07, C08, C14.
- **C34 Entity records (+ Breeze fields).** Ports `prepare_entity` …
  `accept_album`. Adds `day` (camera-clock `YYYY-MM-DD`), `taken` and
  `capture` exactly as Breeze writes them; keeps unknown fields. ·
  `src/sync_entity.zi`, test · `PhotoValue(c: *SyncClient, photo: PhotoRow, entity: string) -> string`,
  `IncomingPhoto(...)`, `IncomingAlbum(...)`, `IncomingAnnotation(...)` ·
  `make ztest-sync_entity` (round-trips C01's Python records; a Breeze-shaped
  record keeps every field) · C26, C27, C33.
- **C35 Legacy move.** Ports `legacy_*`, `verify`, `retire`,
  `switch_account`; old records stay readable until 2027-10-24; chunks retire
  only with `retire_legacy_chunks`, after re-download and hash check. ·
  `src/sync_legacy.zi`, test · `LegacyExchange(c: *SyncClient) -> s32, string`
  · `make ztest-sync_legacy` · C33.
- **C36 Sync run.** Ports `exchange`, `sync`, `materialize`, `download`. ·
  `src/sync_run.zi`, test · `Sync(c: *SyncClient) -> s32, string`,
  `Materialize(c: *SyncClient, id: string) -> string, string` ·
  `make ztest-sync_run` · C34, C35.
- **C37 Entity node migration (Harmony independence).** Copy every account
  entity photo/album record and chunk from one pinned node to another, verify
  each hash on the destination, never delete on the source; resumable;
  progress in `settings`. · `src/sync_migrate.zi`, test ·
  `MigrateEntityNode(lib: *Library, from: NodePin, to: NodePin) -> MigrateResult`
  · `make ztest-sync_migrate` (two fake nodes) · C36, X2.
- **C38 (conditional) Breeze legacy media.** Only if X5 says Breeze no longer
  moves `cell.media.*`: read `private.inbe.v1.media` records and blobs and
  write entity records. · `src/sync_breeze_legacy.zi`, test ·
  `BreezeLegacyMove(c: *SyncClient) -> s32, string` ·
  `make ztest-sync_breeze_legacy` · C33, X5.
- **C49 Sync integration.** Ports `tests/sync_node.py`: a real isolated Daochi
  node (`ziran pkg path daochi`) on loopback, a full upload/download/legacy
  move. · `tests/sync_node_it_test.zi` · — · `make ztest-sync_node_it` · C36.

### Wave 4: service and install (sequential)

- **C39 Dispatcher.** Ports `worker.process` and `library_request`: every
  action routed to C17–C31/C36 with guest/recovery/app-lock ordering
  unchanged; JSON identical to C01. · `src/service_dispatch.zi`, test ·
  `Process(request: string, config: Config) -> string` ·
  `make ztest-service_dispatch` (replays every golden) · C14–C31, C36.
- **C40 Worker loop.** Ports `configuration`, `daemon` (flock
  `.worker.lock`), `run_worker` (import every 300 s or on request, sync,
  export with revision checks and 30 s retry, semantic/people kick, 30×1 s
  wait), `--once`, `--import-folders`; never logs HTTP bodies or keys. ·
  `src/worker_loop.zi`, test · `RunWorker(lib: *Library, config: Config, once: bool) -> s32`
  · `make ztest-worker_loop` · C19, C26, C27, C31, C36.
- **C41 Service executable.** `photos_service.zi` calls `Process` in-process
  (no socketpair to Python); socket activation with `LISTEN_PID/LISTEN_FDS`
  and the listener path/mode/uid checks from `service.py`; idle exit;
  `--daemon` runs C40; Makefile `local-service` LDLIBS updated. ·
  `src/photos_service.zi`, Makefile, test · `main` ·
  `make ztest-photos_service` (ports test_service) · C39, C40.
- **C42 Installer.** Ports `install.py` and `service_units.py`: copy
  executables to `~/.local/lib/photos`; units `photos.socket`,
  `photos.service` (`ExecStart=…/photos-service`),
  `photos-sync.service` (`… --daemon`), the existing tunnel kept, an optional
  entity-node proxy renamed (H7); keep config, ports and keys; remove the venv
  only after the new service answers a health request; no Harmony
  registration (H6). · `src/install.zi`, `src/service_units.zi`, test ·
  `Install(prefix: string, options: InstallOptions) -> bool, string` ·
  `make ztest-install` (temp HOME, `--no-systemctl`; ports
  test_service_units) · C41, C05, C37.

### Wave 5: Ziran GUI tests on private Xvfb

- **C43 Xvfb kit.** Start a private Xvfb with inherited
  DISPLAY/WAYLAND_DISPLAY/XAUTHORITY/DBUS_SESSION_BUS_ADDRESS/SESSION_MANAGER
  removed; launch the app with a temp `PHOTOS_CONFIG` and
  `XDG_RUNTIME_DIR`; read `_PHOTOS_APP_STATE`; click/type via XTest FFI
  (no xdotool); save screenshots of the private display only to
  `build/screenshots`; Makefile `zgui-%`. · `tests/xvfb_kit.zi`, Makefile ·
  `Launch(fixture: string, portable: bool) -> *AppUnderTest`,
  `Click(app: *AppUnderTest, label: string) -> bool` · `make zgui-kit` · C05, C02.
- **C44 Gallery GUI.** Port `window`, `refresh`, `heartbeat` (+close),
  `swipe`, `viewer`, `historic`, `portable`, `scroll_performance` and
  `scroll_frame_probe.c`; each native and portable. ·
  `tests/{gallery,refresh,heartbeat,swipe,viewer,historic,scroll}_gui_test.zi`
  · `make zgui-gallery` etc. · C41, C43.
- **C45 Media GUI.** Port `crop`, `video`, `playback`, `collage`, `slideshow`,
  `motion`, `codec`. · `tests/{crop,video,playback,collage,slideshow,motion,codec}_gui_test.zi`
  · `make zgui-crop` etc. · C41, C43.
- **C46 Security GUI.** Port `app_lock`, `guest`, `recovery`,
  `recovery_reset`. · `tests/{app_lock,guest,recovery,recovery_reset}_gui_test.zi`
  · `make zgui-app_lock` etc. · C41, C43.
- **C47 Discovery GUI.** Port `people`, `semantic`, `similar`, `text`, `qr`,
  `places`, `map`, `discovery`, `storage`. · `tests/{people,semantic,similar,text,qr,places,map,discovery,storage}_gui_test.zi`
  · `make zgui-people` etc. · C41, C43.
- **C48 Android tests.** Port `android_gallery.py` and
  `android_upload_node.py` (test-owned emulator, isolated node, adb through
  `std/process_run_linux`); covers C52. · `tests/android_gallery_it_test.zi`,
  `tests/android_upload_it_test.zi` · — · `make ztest-android_gallery_it
  ztest-android_upload_it` · C13, C41, C52.

### Wave 6: cutover and the final step

- **C51 Desktop cutover.** On a read-only snapshot copy of
  `/mnt/storage/Photos/library.sqlite` (SQLite backup API, temp dir), compare
  gallery/album/search answers from the Python and Ziran services. Then
  install with C42 and check the live service and sync status. Originals and
  keys are never written. · — · — · the comparison has no differences; `systemctl --user status photos.socket photos-sync.service` shows the Ziran executable · C42, C44–C47.
- **C50 Delete Python; Ziran-only Makefile and CI.** Delete `photos/`,
  `scripts/*.py`, `tests/*.py`, `tests/__pycache__`, `photos/__pycache__`,
  `requirements.txt`, the `PYTHON` variable and `build/venv`, the old
  `test_*`/`*-window-test` targets, and `tests/golden_capture.py`. Rewire the
  `install`, `*-models`, `android*`, `sync-test` targets to Ziran. Add
  `make check-no-python` (fails if `git ls-files '*.py'` is non-empty or the
  Makefile mentions python) to `make test`. Add
  `.github/workflows/test.yml` using `ziranlang/ziran/.github/actions/setup`
  with `fetch: true`: `make test` and the `zgui-*` tests under xvfb-run. Update
  AGENTS.md, README, CHANGELOG `## [Unreleased]`; retain this plan with
  completion evidence. Remove retired handwritten Java/C test drivers after
  D1–D3 and C44/C53 pass; preserve active installed outputs. The retired venv
  is removed only after C51 verifies health. · listed
  files · — · `make test`, `make check-no-python`, and the GitHub run green on
  the pushed commit (separate evidence) · all mandatory C contracts, D1–D3,
  X dependencies and C38 if X5 requires it. Desktop-only Python removal is not
  100% Ziran completion.

### Android completion (before final C50)

- **D1 Pinned Android HTTP client.** Port `PinnedNode.java` request/login,
  bounded streamed transfers, pin checks and retry to `src/android_upload.zi`;
  reuse DaochiClient/std HTTP rather than copy Java policy. ·
  `AndroidRequest(config: NodePin, request: string) -> string, string` ·
  `tests/android_transport_it_test.zi`; `make ztest-android_transport_it`,
  `make android`, C48 Android upload test · C48, C27.
- **D2 Android platform adapters.** Port `DeviceGallery.java`, `BackupQueue.java`,
  `DeviceBackup.java`, `DeviceLock.java`, `FontAsset.java` via `kryon/AndroidJni`:
  MediaStore permissions/cursors, picker URIs, queue persistence, Keystore,
  auth and font loading. · `src/android_gallery.zi`, `src/android_device.zi`,
  `src/android_queue.zi`, `src/android_lock.zi`, `src/android_font.zi` ·
  `GalleryPage(request: string) -> string`, `QueueResume(root: string) -> bool`,
  `DeviceUnlock(request: string) -> string` · `tests/android_device_it_test.zi`;
  `make ztest-android_device_it`, C48, fresh APK private-emulator test · D1, C52.
  Preserve existing identity bytes and queue state; no fallback handwritten Java.
- **D3 Android lifecycle and instrumentation.** Replace handwritten
  `PhotosActivity.java`, `BackupScanJob.java`, `PhotosGalleryProbe.java` with
  generated OS entry stubs invoking Ziran lifecycle/job/test functions. ·
  `src/android_lifecycle.zi`, `tests/android_lifecycle_it_test.zi`, Android
  manifest and component declarations; remove old Java only after replacement
  compiles · `ActivityEvent(event: s32)`, `RunBackupJob(job: s32) -> bool` ·
  `make android`, `make ztest-android_lifecycle_it`, C48 · X6, D2. Validate
  recreation, permission cancellation, background job and resume after process
  death on a test-owned private emulator; physical phone tests use USB Moto.

### External dependency contracts (owning upstream repositories only)

- **X1 Harmony hosting removal.** Remove Photos hosting/catalog/publish entries
  from `harness/harmony/assets/apps/catalog.json` and
  `client/src/app_defaults.zi`; retire only Photos' registration, preserving
  other apps and all private library files. · catalog/default lookup · run
  Harmony's relevant catalog/default tests and integrated local suite from its
  own current instructions · C42, C51. Parent routes to existing Harmony owner.
- **X2 Independent desktop Daochi service.** Inspect current service/process,
  node identity and store ownership first. Prefer the same store and pin under
  Daochi's independent installer; do not change the owner's selected node.
  If a verified copy is necessary, C37 performs it and preserves the source. ·
  Daochi installer/service configuration in its canonical repository, no Photos
  data in Git · service startup/health endpoint · its current local service tests,
  then Photos `make ztest-sync_node_it` against an isolated node. Done only when
  Photos connects without a Harmony process dependency; never infer that from a
  saved config or claim that old data is absent.
- **X3 Neutral Kryon host settings (conditional proposal).** In Kryon, change
  `system_theme_host_linux.zi` and `background_window_linux.zi` to expose neutral
  theme/compact properties or an explicit standalone profile. · existing host
  setting readers · current Kryon host tests and `make push-check` · coordinate
  with Kryon's active owner, C27 integrates after upstream validation. No
  downstream copies or uncoordinated shared-file changes.
- **X4 Python-free Photos build path.** In Kryon's project tool, port the
  invoked source-generator/runtime-builder/scaffold/artifact entry points from
  `generate_android_sources.py`, `build_android_runtime.py`,
  `android_scaffold.py`, `renderer_artifact.py`. Keep app policy in Photos. ·
  canonical Kryon project-tool source and its tests ·
  `GenerateAndroid(config: string) -> bool`,
  `BuildAndroidRuntime(config: string) -> bool`,
  `ResolveRendererArtifact(request: string) -> string` · upstream Android
  generation tests, `make push-check`, then Photos `make android`. This is a
  dependency contract, not permission for an unbounded dependency-tool rewrite.
- **X5 Breeze removal coordination.** In the separately owned Breeze task,
  remove the duplicate Photos implementation only once standalone feature/data
  parity is available; keep Diary's photo path and shared entity interfaces.
  Confirm `media_diary_photos.zi` continues migrating `cell.media.*`; otherwise
  activate C38. · Breeze Photos/media call sites and removal tests · existing
  media lookup/update API · Breeze's current entity/Diary tests and Photos
  C38/C49 fixtures. Optional server discovery must launch standalone Photos.
- **X6 Kryon-generated Android entry components.** Extend the existing Android
  generator/JNI surface to produce minimal Activity/JobService/instrumentation
  stubs that call Ziran exports. · canonical generator/templates, JNI host and
  generator tests · `GenerateComponents(config: string) -> bool` · upstream
  minimal Android consumer test and `make push-check`; Photos D3 integration.
  Serialize with X4 and X3 if their actual files overlap; generated Java is output.
- **X7 Reusable inference package (conditional proposal).** No canonical general inference package
  was found. Extract generic ONNX loader/session/tensor ownership from
  `src/semantic_infer.zi`, `src/face_infer.zi`, `src/detect_text.zi` into a
  canonical Ziran package (proposed `ziranlang/packages/onnx`); retain
  model-specific processing in Photos. · new package source/tests; thin
  integrations in those three Photos helpers are owned by C27 after package
  validation · `OpenSession(model: string) -> *Session, string`,
  `Run(session: *Session, inputs: []Tensor) -> []Tensor, string`,
  `CloseSession(session: *Session)` · newly defined package `make test` with a
  small synthetic model, failure/lifetime/bounds cases; Photos semantic/face/OCR
  tests compare numerical output. Upstream first, then C27 lock integration.

## Exact entry points and test paths

Public signatures above containing `...` are completed here; these are proposed
interfaces, not claims about existing export names. For every `ztest-NAME`, its
owner creates `tests/NAME_test.zi`; `zgui-NAME` owns `tests/NAME_gui_test.zi`.
CLI and test program entry points are `main :: () -> s32`. Shared `Library` and
`PhotoRow` types belong to C14's library_store, `Config`/`NodePin` to service_types.
Named request/result records belong to the module exposing that operation.

| Contract | Complete proposed entry point(s) |
|---|---|
| C07 | `EntityOpen(key: []u8, sealed: []u8, out: *Vec(u8)) -> bool`; `SyncCrypt(key: []u8, data: []u8, seal: bool, out: *Vec(u8)) -> bool`; `AccountSign(secret: []u8, message: []u8, out: *Vec(u8)) -> bool`; `AccountVerify(public: []u8, message: []u8, signature: []u8) -> bool`; device signing reuses `sync_sign.SignDevice` |
| C08 | `BuildTransaction(request: string, identity: []u8) -> string, string`; `LoginRequest(challenge: string, identity: []u8) -> string, string`; `RegistrationRequest(request: string, identity: []u8) -> string, string` |
| C13 | `WriteFixtures(dest: string) -> bool`; `tests/android_fixtures_test.zi`; `make ztest-android_fixtures` before `make android` |
| C14 | `SetSetting(lib: *Library, key: string, value: string) -> bool` |
| C17 | `RenameAlbum(lib: *Library, old: string, name: string) -> string`; `ExportPhoto(lib: *Library, id: string, dest: string) -> string` |
| C23 | `ImagePreview(lib: *Library, request: string) -> string`; `ImageSave(lib: *Library, request: string) -> string` |
| C24 | `VideoStart(lib: *Library, request: string) -> string` |
| C25 | `LiveDetect(lib: *Library) -> s32`; `LiveSeparate(lib: *Library, id: string) -> string`; `MotionDetect(lib: *Library, id: string) -> string`; `MotionExtract(lib: *Library, id: string) -> string` |
| C26 | `PeopleState(lib: *Library) -> string`; `AcceptPeopleIndex(lib: *Library, id: string, value: string) -> bool` |
| C28 | `QrScan(lib: *Library, id: string) -> string` |
| C29 | `CollageRender(lib: *Library, request: string) -> string`; `CollageSave(lib: *Library, request: string) -> string` |
| C30 | `PlaceUpdate(lib: *Library, value: string, id: string) -> string` |
| C34 | `IncomingPhoto(c: *SyncClient, record: string) -> bool`; `IncomingAlbum(c: *SyncClient, record: string) -> bool`; `IncomingAnnotation(c: *SyncClient, record: string) -> bool` |
| C42 | `Units(prefix: string, options: InstallOptions) -> string` in `src/service_units.zi`; quoted paths, activation, deferred sync and existing proxy/tunnel options tested |

C07/C08 import existing DaochiClient protocol/crypto exports and Oqs; they do not
reimplement crypto. C07 depends on C01 for cross-language encrypted fixtures;
C08 depends on C07. C22 must test wrong passwords, interrupted restore and rollback.
C03's new package creates `make check`; C04 uses existing
`sh tests/contracts.sh`, `sh tests/corpus.sh --verify` plus a new
`sh tests/resize.sh`. C53 explicitly runs `make ztest-backup_identity
ztest-backup_staging ztest-backup_staging_memory ztest-backup_upload`.
C44 additionally ports `tests/gallery_text_test.sh` into
`tests/gallery_text_driver_test.zi` with native/portable source/saved-IR parity.
Only C02 edits tests/kit.zi; later contracts add their own fixture modules.

## Readiness, file ownership and counts

Ready independent preparation: C01 (synthetic goldens), C02 (test kit/Makefile),
C03 (new tokenizer package), C04 (existing image-codec resize). Their actual files do not overlap. B0 blocks current Photos builds,
not the act of implementing independent source contracts; report local tests as
pending until the precise dependency failure is resolved, and do not commit early.

After C02, C06/C09/C10/C11/C12 can start independently; C07 also needs C01.
C53 follows C07.
After C01/C02, C14 supplies shared library/types. Then C15 precedes C17/C18;
C16 follows C15, C19 follows C15/C17. C08 follows C07. C13 needs C07/X4.
C05 follows C02. C20–C31 may run only when each listed dependency is met;
C33 follows C06/C07/C08/C14; C34 follows C26/C27/C33; C35 follows C33;
C36 follows C34/C35. C37 follows C36/X2. C38 is conditional on X5.

Shared-file serialization: C02 -> C41 -> C43 -> C50 for Makefile;
C09 -> C32 -> C52 for app/mobile UI; C19 -> C32 for library_import;
C14 -> C32 for library_store; C52 -> D1 -> D2 -> D3 for Android adapters
(with D1's C48 prerequisite); C27 alone integrates package manifests/locks and
inference helper imports, never editing another worker's uncommitted upstream.
C32 follows C19/C09/C14 before touching their files. C05 changes only property
names in old GUI readers; finish C05 before C44 ports those readers. C43 does
not require desktop cutover and can be prepared after C02/C05.

Final chain: feature/sync contracts -> C39/C40 -> C41 -> C42 -> C44–C49;
C52 and C48 precede D1–D3; complete D3 and rerun Android integration before
C51 cutover, then C50 final cleanup. C42 also needs C37. X1 cleanup follows
verified independent installation; do not delete source node data as cleanup.

There are **63 named contracts**: C01–C53 (53), D1–D3 (3), X1–X7 (7).
C38 runs only if X5 requires it. X3 and X7 are proposals, not authorized generic
refactors: implement only when an actual application migration dependency
requires them. With those three conditional items omitted, the default path
has 60 contracts.
C03/C04 and X contracts have upstream owners; D1–D3 are required for the
100% Ziran goal. These counts describe the plan, not completed implementation.

## Continuation evidence (2026-11-05)

The document inventory validator passed: 63 unique contract IDs and all 36
runtime Python modules covered. C02 is implemented in tests/kit.zi,
tests/kit_test.zi and Makefile; `build-queue -- make ztest-kit` compiled and ran
successfully. It covers synthetic private libraries, JPEG/PNG files, SQLite
dumps and scoped cleanup that preserves symlink targets. The integrated headless
`build-queue -- make test` then passed the 416 existing Python tests and kit.

Initial package resolution failed before compilation because Photos' locked
Kryon graph was older than its selected local manifest. The normal installed
resolver was also older than current canonical unborn-override support.
Canonical `build-queue -- make all` and `build-queue -- sh tests/packages.sh
build/bin/ziran` passed, including 18 reported package/cache cases. The existing
installed bootstrap payload was refreshed in place (10 tools plus
std/include/templates), with an ignored recovery snapshot in the owning Ziran
repository. SHA-256 verification confirmed the queue shim, saved launcher and
build-tools mapping remained unchanged. No upstream source was edited and no
alternate compiler or package bypass was used.

Normal `ziran lock` now succeeds through the unchanged launcher, with ignored
canonical image-codec/avif/webp overrides. This is a development lock, not a
release lock. Image-codec, WebP and AVIF now have real upstream commits; the
latest 19-package graph has no unborn identities. AVIF's dav1d dependency was
incorporated by a normal lock refresh. Local overrides still select active
working sources, including the process-channel change below; upstream
integration and reproducible source validation remain required before
publishing. No upstream commits were fabricated.

C06 is implemented in src/owner_account.zi and tests/owner_account_test.zi.
`build-queue -- make ztest-owner_account` compiled and executed successfully.
The port retains read-only lookup, one complete settings user, last-value
semantics, lowercase hex encoding and the existing three user-facing error
messages. It derives the same public-key account id, and uses DaochiClient's
canonical private-key proof as required by the repository's key-validation
policy. Correct-length mismatched private keys now fail that proof (the Python
constructor previously checked lengths and digest only). It never writes a new
identity or reads the owner's actual database in tests. Caller-owned key buffers
are explicitly cleared, and temporary decoded/private buffers are wiped.

C06's linker configuration is in Makefile; `make test` now includes the Ziran
unit aggregate. `build-queue -- make ztest` passed all seven current Ziran unit
programs; the final integrated headless `build-queue -- make test` passed those
units plus 416 existing Python tests in 121.763 seconds. `git diff --check` passed. Nothing in Photos is committed,
pushed, released or installed; no GitHub checks have run for these uncommitted
application changes. The installed toolchain update is separate from Photos
application installation.

C01 persisted-store fixtures are captured in tests/fixtures/golden:
library-v0.1.59.sql, library-legacy.sql, library-legacy-migrated.sql and
library-expectations.json, generated by tests/golden_capture.py. Two independent
captures were byte-identical; all three SQL dumps passed integrity checks, and
30 existing library baseline tests passed. The legacy fixture covers seven
missing migrated columns across photos/albums/people, album-catalog backfill,
unknown JSON values and preserved table rows/BLOBs. Protocol and recovery
goldens remain pending; this is only C01's persisted-store subset.

C09 is implemented by the date-heading specialist: shared day formatting,
12 locale assets and desktop/mobile call sites. The focused Ziran unit and
desktop `make build` passed. Android source checking passed with the Android
defines; this is not full APK/device validation.

C10's Ziran model downloader and synthetic tests passed the exact
`build-queue -- make ztest-fetch_models ztool-fetch_models ZTOOL_ARGS=--help`.
It handles the three existing manifests, offline sources, streamed SHA-256,
cache validation, licenses, private staging and atomic publication. Shared
integration added the direct canonical crypto dependency through `ziran add`
and tool module discovery in the Ziran unit rule. Python model scripts remain
until final cutover.

C14 is implemented in src/library_store.zi, src/service_types.zi and its Ziran
unit test. Current and legacy synthetic SQL fixtures match the Python baseline
for every table, column and row, including large integers, BLOBs and unknown
JSON fields. Tests cover schema rollback/retry, double reopen, album tombstones
and renamed-ID collisions, Unicode trimming, numeric false markers, private
roots, WAL and the 60-second busy timeout. Trailing slashes and missing/../
components normalize before directory creation; existing symlink/.. semantics
are preserved. Native root/error allocations have explicit ownership and
consumer error helpers. No owner library or credentials were used.

The coordinated `build-queue -- make ztest` passed ten Ziran unit programs.
The headless `build-queue -- make test` passed those units and all 416 existing
Python regressions in 121.181 seconds. C15 file/preview and C17 album ports are
the next implementation work; C18 query and C07 crypto proceed in separate
managed scopes. C04's reusable resize functions already exist as
std/image.ResizeLanczos, ResizeBicubic and Thumbnail; no new upstream resize
implementation is needed without a demonstrated parity gap. The Python/Java
and service cutovers, full platform validation and installed verification are
still pending. Nothing in Photos is committed, pushed, released or installed.

C01's additional synthetic crypto fixtures and C07's four in-process crypto
ports were verified independently: sync/entity/account-sign/recovery Ziran
units passed; Python opened every Ziran-produced crypto output; existing pipe
helpers rebuilt; 39 baseline crypto/recovery tests passed. Fixture recapture
was byte-stable. Existing framing, account validation and CLI entry points are
preserved; no owner keys, nodes or real library data were used. Parent accepted
this subset, and the same crypto specialist continues C08 transport. This does
not complete recovery UI/service orchestration or the full C01 request corpus.

C15 now supplies library_files.zi and the image_edit thumbnail operation.
The focused `build-queue -- make ztest-library_files ztest-library_store`
passed binary/empty streaming hashes, atomic writes and permissions, full photo
row/path reads, orientation, RGB conversion, 640-pixel preview bounds, cache
reuse, original preservation and native sidecar metadata. Trash timestamps stay
s64, with a 2^32 regression. Helpers are resolved from an explicit trusted app
root or beside the service executable, never arbitrary cwd/ancestor basenames.
Native preview work scrubs display/session variables and uses owned channels,
deadlines and a 64-KiB stdout limit. Synthetic failures, stalls, stdout/stderr
floods and a cwd helper trap all passed; temporary artifacts are removed.

This needed a minimal canonical std/process_channel_linux extension: up to
32 arguments, and optional discard_stderr (default false; merge+discard is
rejected). Native source and saved-IR tests passed at 17/32/33 arguments,
binary I/O, all closed-standard-descriptor masks, silent diagnostics and owned
group shutdown. The adjacent wait/pidfd/fallback/interruption suite passed
with no descriptor growth. Existing callers keep their behavior. Only this
stdlib module, its existing regression script and its USAGE paragraph were
changed upstream by this task; unrelated Ziran work is preserved, uncommitted.
C17 album/metadata edits are next; the last full aggregate run remains the
416-test result above until current parallel library/query/transport ports
are ready together.


C17 catalog and core edit ports now run in-process. Album projections,
pinned/Unicode/created ordering, smart/place counts and hidden exclusion pass
native regressions. Album edits reuse the existing writer, preserve identities,
normalize names/searches, and retain a caller transaction. Photo edits share
Live Photo expansion, authorization, expiry checks, writes and commit with the
existing CLI. Native tests cover denial, actual expiry during SQL and batch
rollback; the CLI's 13 existing tests pass. The real Hidden registry/guard
adapter passes grant/revoke/locked-photo tests. C18's 87 oracle cases now use
this actual catalog and C20 access providers. C17 details/export provider
integration is still pending; this is acceptance of the core subset only.

The canonical SQLite package now exports SqliteGetAutocommit, with explicit
null behavior and initial/BEGIN/COMMIT/ROLLBACK conformance tests. Its local
source/saved-IR native package suite passed. All app and dependency source
changes remain uncommitted while the full migration is incomplete.

Canonical JSON now owns escaped/generated/copied bytes per node, deep-copies
keys and values, and exposes explicit JsonReset and fallible caller-owned Into
accessors/output. Registered erasers wipe private buffers and failed partial
parses before release. Source/saved-IR C/Go/VM lifetime tests, native bounded
allocation/erasure/fault tests, sanitizers and the float formatting oracle pass.
The compiler experiment was reverted; no compiler or installed payload changes
are needed. JsonSetCopy owns temporary keys without application node coupling.

C14/C15/C17 callers now reset documents and use Into accessors for local text;
their native storage/files/catalog/edit functional tests pass. Local editor
geo/query/rename buffers also retain vector ownership through SQLite calls.
The editor now exposes ApplyAlbumInto/ApplyPhotosInto and the album policy
EditManyInto/EditManyAuthorizedInto, with caller-owned empty/reusable vectors.
A true return means complete JSON; inspect saved/updated/error for the action
outcome. Success replies are built before committing SQL. The dedicated native
heap fixture ran 5000 real photo/album success/error replies with zero heap
growth after warm-up. Legacy string wrappers remain for command-line callers;
persistent code must use Into and never guess which strings to free. Catalog,
row/path and dispatcher lifetime acceptance still requires its own repeated
allocation proof before C39; this editor proof does not establish whole-service
readiness.

The native Curl adapter now accepts named-header values through 8191 bytes,
including the validated name, separator and NUL in its staging buffer. Existing
built-in-header bounds, validation, direct proxy bypass and redirect policy are
unchanged. Canonical loopback tests and the normal Photos provision/HTTP/client/
legacy native targets pass the 4096/8191 accepted and 8192 rejected cases.

C16 now has a decoder service and frozen synthetic HEIC, rotated HEIC, DNG,
rotated DNG and AVIF binaries. It shares C15 helper lookup, bounded channels and
dimension updates, and validates private PNG output before publication. Its
native codec/privacy/failure suite passes actual codec pixels, rotation,
alpha, immutable originals, thumbnail/cache behavior, validation/timeout cleanup,
SQL rollback, real concurrent Hidden revocation, concurrent row hiding and token
expiry. C23 must use its real token/registry seam when wiring the dispatcher.
C17 details/export provider integration and full Python removal remain pending.

Native import facade local verification (C19). `src/library_import.zi` now exposes caller-owned `ImportFile`, `ImportFolders`, `ImportResultInto` and explicit `ClearImportResult`. A true return means processing finished; callers inspect the six counters and per-path failures. The facade supervises the existing native metadata reader, digest/copy and transactional metadata writers; uses exact size/mtime-nanosecond/sidecar-stamp cache keys; keeps sources read-only; rejects library ancestry; streams bounded no-follow discovery; restores album/Takeout/private automatic provenance; and invokes the real per-file Motion provider and batch-end LiveDetect. The native import unit covers duplicates, Unicode/oversized integers, cache invalidation, invalid sidecars/rules, links/FIFOs, transaction rollback/retry, stalled/flooding writers and discovery, plus explicit provider warning retention. Canonical temporary-directory/list/tree ownership was fixed upstream: source and saved native filesystem tests each retain zero heap bytes over 2000 create/list/remove cycles; 64 actual failed-import retries and 4000 failure-record resets also retain zero. Local commands: `build-queue -- sh tests/std_file_tree_linux.sh build/bin/ziran` in Ziran, and `build-queue -- make ztest-library_import ztest-library_files ztest-live_motion` in Photos; all passed. The later duplicate/Takeout/provenance extension also passes `build-queue -- make ztest-library_import`. These are local unit/integration results; C39/C40 dispatcher adoption, final Python deletion, whole-application/platform validation and installation remain pending.

The current canonical SQLite binding now provides typed scalar/destructor callbacks,
callback user data and binary-value/result APIs. Its native source/saved tests prove
connection-local contexts and exactly-once destruction after replacement, deletion,
close and a real failed registration. Existing common APIs pass C/Go source/saved
on the current canonical toolchain. The previous locked compiler cannot build the
new callback test; final immutable toolchain/package pins remain a release gate.

Store setup now releases resolver intermediates, query builders, migration album
names/identifiers, markers and packed settings. Its real legacy/current/rollback
fixtures pass; native allocation tracking and ASAN/UBSAN/LSan pass 256 open/close
cycles and 302 allocation failures plus recovery with no retained bytes. Callers
release owned setting/packed/open-error results using `Store.ReleaseResult`.

C16 now uses bounded cancellation during helper output and exit waits. Canonical
`CancelChannel` terminates the owned unreaped process group before joining,
including an exited parent with a descendant holding stdout; a separate unrelated
test process stays alive. Photos passes 240 cancellation/recovery cycles per native
mode with stable descriptors and zero retained bytes. Checked photo reads record
only their ten owned string columns; `ClearPhoto` releases those owners safely on
partial allocation failure and leaves borrowed fixture strings untouched. Display
uses caller buffers internally and transfers an owned path only on success.
Native decoder tests pass 2,000 row/cached/rejected cycles, eight real HEIC source
success/failure cycles and 101 allocation failures plus recovery with zero retained
bytes. Existing codec pixels, orientation, alpha, original immutability, rollback
and concurrent privacy tests also pass. C27's actual semantic-service unit also passes RAW helper cancellation and real
decoder retry with the original intact. C27's final lifetime/selected-suite review,
C39/C40 dispatcher adoption, full application/platform validation, Python removal
and final publication/installation remain separate pending work.
