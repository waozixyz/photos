# Standalone Photos follow-up

- Migrate the standalone viewer and other remaining UI imports from
  `kryon/Widgets` to the namespaced Kryon widgets.
- Reconcile the standalone root dependency graph: its locked Kryon declares
  `image_codec` at `360553150917f352e49e1beb78917370792ccaef`, while the root
  lock selects `123a1b9fe5ed2043a4eb3d3c007e9bd4cebfb95d`.
- Run the complete root `make test` with a consistent graph after those changes.
  The portable add-on has its own locked package and passing
  `make node-photos-test` suite.
