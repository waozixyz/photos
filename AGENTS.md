# Photos

Photos is a standalone native app that also opens inside Harmony. Its canonical
repository is `/mnt/storage/Projects/waozixyz/photos`, on master. UI belongs here
and uses Kryon ImageProps.

Owner requirement (2026-10-07): import and merge `/mnt/storage/adel` and
`/mnt/storage/Album`, preserving originals, albums and Ente metadata. The full
desktop library lives in `/mnt/storage/Photos`. Sync uses ONLY the owner's own
local Daochi nodes, pinned by node identity, through loopback or SSH. Owner
decision (2026-10-23, "Move photos to the desktop node"): the account's shared
photos (entity data) live on the desktop Harmony node; the ThinkPad node keeps
the copies made before entity data, which stay untouched until every Photos
install reads the entity data and the owner agrees to delete them.
Never use Ente, a public Daochi node, a cloud service or public peer for Photos.
Never change this restriction or enable outward replication without an explicit
owner instruction. No photo content or library metadata belongs in Git or Social.

All sync data is encrypted on the client; account and content keys stay private.
Use the canonical DaochiClient encryption, account/key validation and signed
sync protocol primitives; do not create a separate Photos encryption system.
Reusable protocol and crypto changes belong in DaochiClient or the owning
upstream repository. Photos owns compatible backup framing, app storage and UI.
Preserve existing account/content keys and compatible encrypted backups. Android
Keystore may wrap the existing validated identity for local device protection.
Installation and removal preserve the library and keys. Screenshots and GUI
tests use synthetic photos on private Xvfb, scrubbing inherited display/session
variables. Run make test and git diff --check before committing.
