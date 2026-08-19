# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- RTSP channel enumeration now always terminates on its own; a target with no further channels no longer leaves the scan probing ever-increasing channel numbers indefinitely (previously only `--max-channels` or CTRL-C could stop it)
- Invalid or missing command-line arguments are now reported as errors instead of being printed under a `debug` label
- RTSP credentials are no longer embedded in the request-URI sent to the device; they travel only in the `Authorization` header, matching standard RTSP clients and keeping the password out of the request line
- Project metadata URLs (Homepage, Repository, Issues) now point to the maintained `github.com/shxve/pwneye` repository instead of the stale upstream, so the links shown by `pipx`/PyPI-style tooling resolve to the right place

## [1.4.0] - 2026-08-19 (Panopticon)

### Added

- `--max-channels N` to stop RTSP channel enumeration after `N` channels are found, useful for DVR/NVR targets that report a valid stream across a very wide channel range (defaults to unlimited)

### Changed

- `--timeout` and `--threads` now reject non-positive values with a clear error instead of silently degrading behavior (a zero or negative timeout previously made every probe fail instantly)

### Fixed

- RTSP probing no longer crashes on a malformed or oversized `Content-Length` response from a device; the header is parsed defensively and the response body is bounded
- RTSP credentials containing `@`, `:`, or `/` are now percent-encoded, so special-character passwords produce valid stream URLs instead of being probed against the wrong host
- Interactive prompts now abort cleanly when standard input is closed (e.g. piped or otherwise non-interactive runs) instead of raising an uncaught `EOFError`
- A missing or unrunnable `ffmpeg` / `ffplay` is now reported as an error instead of aborting recording, snapshot, or live preview with a traceback
- The per-target cache is written atomically, so an interrupt or crash mid-write can no longer corrupt it; cache files are also created owner-only to keep saved credentials private
- Terminal log output no longer breaks when device banners, stream URLs, or error text contain brace characters

## [1.3.2] - 2026-07-14 (Panopticon)

### Added

- ONVIF local-network discovery now accepts an optional network interface with `--discover INTERFACE`, making it easier to steer WS-Discovery toward the intended target subnet on multi-homed hosts ([thanks Darix Deros for the suggestion](https://www.linkedin.com/in/knx/))

### Changed

- ONVIF discovery messaging is now more explicit about the target network in use, including the selected interface subnet when it can be resolved

### Fixed

- Replaced shell-based network interface resolution helpers with a library-backed implementation for more reliable cross-platform interface selection on macOS and Linux

## [1.3.1] - 2026-07-09 (Panopticon)

### Fixed

- Fixed packaging and runtime icon loading for the dedicated viewer so the custom application icon is used correctly in installed builds instead of the default PyQt icon

## [1.3.0] - 2026-07-04 (Panopticon)

### Added

- In-viewer `Snapshot` and `Record` controls inside the dedicated RTSP client, with native notifications and the same default output behavior used by the CLI
- ONVIF PTZ movement from the dedicated viewer through `WASD` when the target exposes PTZ support
- ONVIF PTZ movement from the CLI through repeatable `--move direction,duration` sequences
- ONVIF Snapshot URI extraction during the post-auth enumeration phase
- Shortened protocol-skip flags `-so` for `--skip-onvif` and `-sr` for `--skip-rtsp`
- Custom application icon for the dedicated viewer instead of the default PyQt runtime icon
- Additional MOTD entries

### Changed

- CLI messaging is more explicit when ONVIF or RTSP are skipped at user request
- Project documentation was updated for `1.3.0`, including the dedicated client media actions and ONVIF camera movement workflow

### Fixed

- The startup GitHub update check no longer blocks the tool indefinitely when the host has local network access but no real internet connectivity
- Fixed snapshot path resolution so `--snapshot FILENAME.jpg` is saved under the target-specific snapshot directory instead of the generic snapshots root
- Fixed snapshot and recording filename collisions so existing output files are no longer overwritten and instead receive a sequential suffix
- Improved general viewer and workflow polish across the dedicated client, ONVIF PTZ controls, and CLI/media handling

## [1.2.0] - 2026-05-25 (Panopticon)

### Added

- Dedicated RTSP viewer client with a single-window live interface, per-stream status badges, focused view, zoom support, and improved handling for DVR / NVR-style multi-channel targets
- `--legacy` RTSP preview mode to fall back to `ffplay` instead of the dedicated client when the system utilities are preferred
- ONVIF factory-reset support via `--reset`
- ONVIF deface support via `--deface`, combining stream darkening and OSD text reuse to place a custom message on the target stream
- ONVIF deface restore support via `--undeface`, using a saved restoration profile to revert previous stream changes
- Interactive ONVIF shell support via `--shell`, exposing the underlying ONVIF capabilities for advanced users
- Dedicated restore-profile storage for ONVIF deface / undeface flows inside the existing cache layout
- Cache cleanup support via `--clear-cache`

### Changed

- ONVIF post-auth extraction now highlights offensively useful capabilities such as OSD deface support and factory-reset availability
- RTSP multi-channel handling has been extended beyond simple channel selection to support opening all discovered feeds inside the dedicated client
- Cache management is now surfaced more clearly in the CLI and `--help`, including explicit cache cleanup and more visible runtime directory initialization
- Startup and runtime initialization have been refined to make dependency checks, update checks, cache setup, and media directories more explicit and easier to understand
- The README and top-level project documentation were revised to cover the dedicated client, ONVIF administrative actions, deface / undeface support, and updated installation / update guidance

### Fixed

- Improved dedicated viewer stability across single-stream and multi-channel workflows, including reconnect behavior, focus switching, keyboard controls, and client shutdown
- Improved RTSP and ONVIF cache interactions so cached findings, fresh scans, and explicit credentials behave more predictably across repeated runs
- Multiple workflow and messaging fixes across ONVIF post-auth actions, RTSP preview routing, multi-channel selection, and update reporting

## [1.1.0] - 2026-05-03 (Panopticon)

### Added

- RTSP snapshot support via `--snapshot`, including timestamped default filenames and integration with the existing preview / no-video flows
- Manual RTSP connection string support via `--connection-string` / `-cn`, with file input support and compatibility with templated multi-channel paths such as `{channel}`
- First public iteration of RTSP multi-channel support, including:
  - `--multi-channel` path prioritization
  - channel-aware RTSP template expansion
  - interactive channel selection
  - per-target caching of discovered channels
- Runtime update checks against the latest GitHub release, plus an explicit `--check-updates` mode
- MOTD support with randomized startup messages loaded from the local knowledge base

### Changed

- Automatic recording and snapshot outputs are now stored under per-target directories inside `~/.pwneye/recordings` and `~/.pwneye/snapshots`
- RTSP target resolution now supports user-specified connection strings as first-class inputs, reducing noise when the stream path is already known
- Cache handling is more explicit when credentials are passed on the command line, avoiding misleading reuse messages and clarifying when cached results are ignored
- ONVIF reboot feedback is now more descriptive and performs an automatic reachability check after the reboot request is sent
- General RTSP and ONVIF flow messaging has been refined to reduce redundancy and make scanning state easier to understand
- `--help` output has been revised to make fallback behaviors, optional inputs, and RTSP targeting options clearer

### Fixed

- Improved `CTRL-C` handling across interactive prompts, scanning loops, recording, and channel enumeration to reduce stack traces and inconsistent exits
- Improved post-discovery RTSP handling so preview, snapshot, recording, and multi-channel flows behave more consistently across flag combinations
- Improved cache interactions for targets that were already known, especially when the user forces fresh scans or supplies explicit credentials

## [1.0.0] - 2026-04-05 (Panopticon)

### Added

- First public release of `pwneye`
- ONVIF local-network discovery via `--discover`, with continuous probing and live output for newly discovered devices
- ONVIF service probing before bruteforce to reduce wasted requests on non-ONVIF ports
- Multithreaded ONVIF authentication with live progress output showing ports and credentials being tested
- ONVIF support for single credentials or username/password wordlists passed directly on the command line
- ONVIF post-auth enumeration for device information, configured users, network configuration, media profiles, and RTSP stream URIs
- ONVIF reboot support via `--reboot`
- RTSP port discovery with prioritization of the most common RTSP ports first
- RTSP banner retrieval and banner-based vendor identification
- RTSP vendor listing via `--list-vendors`
- Vendor-aware RTSP bruteforce using the built-in RTSP knowledge base
- Exhaustive RTSP fallback using the full path database when vendor identification fails or vendor-specific paths do not work
- Multithreaded RTSP bruteforce with live spinner output and per-attempt connection visibility
- RTSP preview support via `ffplay`
- RTSP recording support via `ffmpeg`, including timestamped default filenames under `~/.pwneye/recordings`
- Dedicated RTSP banner mode via `--banner`
- Per-target cache support for successful ONVIF and RTSP findings under `~/.pwneye/cache`
- Caching of RTSP banners and ONVIF-discovered manufacturer hints to improve later targeting
- Credential file support for ONVIF and RTSP username/password arguments
- Improved interrupt handling across ONVIF, RTSP, preview, recording, and interactive prompts
- Project branding for the `v1.0.0_panopticon` release line
