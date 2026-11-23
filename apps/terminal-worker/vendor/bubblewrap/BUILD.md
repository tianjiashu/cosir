# Rebuilding the standalone helper

These are the complete vendored C sources used by `cosir-bwrap`. The Cargo build
recipe is `apps/terminal-worker/build.rs`; the Rust argv entry wrapper is
`apps/terminal-worker/src/bwrap.rs`. Both are adapted from OpenAI Codex as recorded
in `../openai-codex/SOURCE.md`. The C sources retain their upstream license.

On Linux with a C compiler, pkg-config and libcap development headers, an
equivalent standalone C entry point can also be built in a separate directory:

```sh
printf '#pragma once\n#define PACKAGE_STRING "bubblewrap built for Cosir"\n' > config.h
cc -D_GNU_SOURCE -I. -I/path/to/these/sources \
  /path/to/these/sources/bubblewrap.c /path/to/these/sources/bind-mount.c \
  /path/to/these/sources/network.c /path/to/these/sources/utils.c \
  $(pkg-config --cflags --libs libcap) -o cosir-bwrap
```

The helper is a separate, replaceable executable next to `terminal-worker`, not a
setuid binary. No runtime fallback to an unrelated system binary is used.
