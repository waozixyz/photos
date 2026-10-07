# Photos

Photos is a standalone native app that also opens inside Harmony. Its canonical
repository is `/mnt/storage/Projects/waozixyz/photos`, on master. UI belongs here
and uses Kryon ImageProps.

Owner requirement (2026-10-07): import and merge `/mnt/storage/adel` and
`/mnt/storage/Album`, preserving originals, albums and Ente metadata. The full
desktop library lives in `/mnt/storage/Photos`. Sync uses ONLY the owner's local
Daochi node on the ThinkPad, pinned by node identity, through loopback or SSH.
Never use Ente, a public Daochi node, a cloud service or public peer for Photos.
Never change this restriction or enable outward replication without an explicit
owner instruction. No photo content or library metadata belongs in Git or Social.

All sync data is encrypted on the client; account and content keys stay private.
Installation and removal preserve the library and keys. Screenshots and GUI
tests use synthetic photos on private Xvfb, scrubbing inherited display/session
variables. Run make test and git diff --check before committing.
