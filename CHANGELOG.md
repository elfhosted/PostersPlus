# Changelog

## [1.1.0-elf.1](https://github.com/elfhosted/PostersPlus/compare/v1.0.3-elf.2...v1.1.0-elf.1) (2026-06-10)


### Features

* add fake textless review report ([446dbb7](https://github.com/elfhosted/PostersPlus/commit/446dbb7105b9f7b70dfd193079cfa0ffc297540f))
* fleet-wide MDBList 429 cooldown over upstream key rotation ([7fac294](https://github.com/elfhosted/PostersPlus/commit/7fac2941537e6960c8603eae8d78099e62e69706))
* observability, render limits, rate limiting, leader-elected jobs ([2a968bc](https://github.com/elfhosted/PostersPlus/commit/2a968bc091f34e467266e7de68b0b1ade57763fc))
* pluggable storage/coordination/blobstore backends on v1.1.0 ([b040a1d](https://github.com/elfhosted/PostersPlus/commit/b040a1d408c9b6fffbf66a08160fde1c139e56c4))
* PRESET_MDBLIST_FETCH — opt-in MDBlist fallback for /p endpoint ([#30](https://github.com/elfhosted/PostersPlus/issues/30)) ([55469b7](https://github.com/elfhosted/PostersPlus/commit/55469b77c90096260e32c1a91f8db71dd93fa682))
* public-tier lock UI, ElfHosted branding, SEO on tabbed configurator ([4f0e04b](https://github.com/elfhosted/PostersPlus/commit/4f0e04b1d28430df3ddbe136409e6372842b5823))
* static-preset overload moat — anonymous /p route on v1.1.0 ([5170828](https://github.com/elfhosted/PostersPlus/commit/5170828c717edb057062146b1de81ce02af08755))


### Bug Fixes

* address codex pre-merge review (CDN 302 on /poster, compose volume) ([f5c233b](https://github.com/elfhosted/PostersPlus/commit/f5c233bc7b398a6738faec9797f30606fd29a463))
* derive preset genre from TMDB when rating is uncached ([#28](https://github.com/elfhosted/PostersPlus/issues/28)) ([9d1b242](https://github.com/elfhosted/PostersPlus/commit/9d1b24291e35e9bddfb4db2946a85115e575c6c4))


### Miscellaneous Chores

* align fork release line to upstream v1.1.0 ([a31bfeb](https://github.com/elfhosted/PostersPlus/commit/a31bfeb1fdf8df47d1efd4a6e5f2ad7186299352))

## v1.1.0 - 2026-06-09

This release is compared with the original `v1.0.0` release. It also includes
the maintenance fixes published in the v1.0.x releases.

### Highlights

- Added several new poster layouts, including Frosted Bar, Minimalist, Clean,
  and expanded quality and age-rating treatments.
- Rebuilt textless-poster validation around the PP-OCRv5 Mobile detector, with
  background scanning and load controls for live installations.
- Added smarter TMDB poster selection so a tiny number of votes cannot easily
  promote a badly rated poster over substantially better artwork.
- Expanded artwork selection with original-art controls, language-aware poster
  matching, improved logo fallback, and face- and saliency-aware cropping.
- Added a preset gallery and reorganized the configurator into a mobile-friendly
  tabbed interface.
- Added poster-output translations for French, Portuguese, and Italian.

### Poster Rendering

- Added independent top and bottom vignette controls with Off, Low, Medium, and
  High strengths.
- Added Frosted Bar mode with:
  - Frosted, black, silver, gold, and rating-focused styles.
  - Optional rating, year, and sash content.
  - Rating progress and out-of-ten display variants.
  - Poster-derived tinting that can be shared with the sash notch.
- Expanded Minimalist mode with improved title, metadata, and fallback-art
  presentation.
- Added Clean mode for a restrained score and metadata layout.
- Added poster-derived sash colors and an option to match the Frosted Bar.
- Added diagonal, notch, and hidden sash display modes.
- Added filled and frosted notch styles.
- Split sash sizing into dedicated width, height, inset, and font controls.
- Added winner-star treatment for selected award sashes.
- Added release-status sashes for BluRay, Streaming, Cinema, and Production.
- Added greyscale treatments for Cinema and Production releases.
- Added options to keep release artwork in color when stream quality is known,
  or use greyscale when no quality is available.
- Added six quality display choices covering the quality notch, quality with
  age rating, badge row, combined text badge, age rating only, and hidden output.
- Added a minimum quality threshold (`badge_min_score`) to all quality display
  modes. When set, the badge is suppressed for streams whose quality score falls
  below the configured value; no-data states are always rendered regardless.
- Added age-rating badges and tracking.
- Improved score bars, badge alignment, spacing, gradients, metadata placement,
  and long-title handling across layouts.

### Artwork And Logos

- Added a Primary or Top Rated source selector for original artwork.
- Added language-aware poster selection, including support for original artwork
  in the requested language.
- Improved logo selection priority across requested, native, original, and text
  fallbacks.
- Added Metahub as an additional logo fallback.
- Added configurable logo sizing and safer contrast and stretch behavior.
- Added face-aware and saliency-aware backdrop cropping.
- Added text-aware crop selection to reduce accidental clipping of useful
  artwork.
- Added minimalist and photoreal genre fallback backgrounds.
- Improved title fallback rendering when no usable logo is available.
- Added a fallback gallery endpoint for reviewing generated title and genre
  artwork.

### Textless Poster Detection

- Replaced the previous EAST detector with PP-OCRv5 Mobile.
- Added title-aware OCR rules to detect posters incorrectly marked as textless
  while avoiding rejection solely for a matching standalone logo.
- Added specialized handling for wide, low-contrast, repeated, and
  design-integrated text.
- Added versioned detection signatures so tuning changes invalidate stale OCR
  results automatically.
- Added a deduplicated cache-volume report of TMDB posters that OCR identifies
  as incorrectly marked textless, including direct review links.
- Added request coalescing so simultaneous requests for the same poster share a
  single scan.
- Added a dedicated text-detection executor with configurable concurrency.
- Added foreground vote gating to keep uncached burst traffic responsive:
  - Posters at or below the configured vote limit are scanned during the request.
  - Posters above the limit are served without caching the composite and queued
    for an idle background scan.
  - Once the background scan completes, later requests use the cached detection
    result and can cache the completed composite normally.
- Bundled the compact detector model in standard Docker builds by default.

### TMDB Poster Selection

- Added a minimum-vote preference when ranking textless poster candidates.
- Preserved the previous selection behavior when no candidate reaches the
  minimum vote count.
- Added a maximum score-drop safeguard so vote confidence cannot promote a
  heavily downvoted poster over much better-rated artwork.
- Included the ranking policy in cache signatures so selection-setting changes
  take effect without manual cache removal.

### Ratings

- Added a minimum vote count for rating providers. Scores with fewer than 10
  votes are ignored by default.
- Exempted Roger Ebert from the vote minimum because its source represents a
  single critic rating.
- Added a per-configuration "Fallback to IMDb" toggle. When enabled, IMDb is
  used only if the selected weighted sources produce no score.
- Improved normalization, missing-provider handling, and provider metadata
  caching.
- Added MDBList secondary API key rotation and rate-limit backoff.
- Improved cache invalidation when rating policy or provider metadata changes.
- Refined the default movie weighting toward Letterboxd with Trakt as a
  low-weight fallback.

### Quality And Release Data

- Added Stremio scraper support as an alternative quality source.
- Improved AIOStreams quality parsing and quality-token normalization.
- Improved digital release synchronization and release-status prioritization.
- Improved background quality refresh behavior and failure handling.
- Added server capability reporting so the configurator can hide unsupported
  options cleanly.

### Configurator

- Rebuilt the configurator as a tabbed interface covering Core, Rating, Logo,
  Sash, Quality, and Weights settings.
- Added a preset gallery with ready-made poster styles.
- Added settings persistence in the browser.
- Restored importing an existing Posters Plus URL for editing.
- Added editable values alongside range sliders.
- Added expanded preview and crop simulation.
- Added controls for original artwork, poster language behavior, logo sizing,
  sash styles, Frosted Bar, age ratings, release colors, and the IMDb fallback.
- Added a light/dark mode toggle to the header. Preference persists in the
  browser across sessions.
- Improved responsive and mobile layouts.
- Improved generated URL handling when the server is accessed over a LAN.
- Added a composite-cache toggle for testing and troubleshooting.
- Updated default values: top vignette defaults to Medium (was High),
  minimalist rating horizontal position defaults to 0.065 (was 0.05), match
  notch color for Frosted Bar modes is enabled by default, diagonal sash height
  defaults to 0.135 (was 0.12), diagonal sash corner distance defaults to 1.20
  (was 1.15), minimum quality threshold defaults to score 5 for Badge Row /
  Quality Notch / Combined Text Badge modes and score 2 for Quality Age Rating
  mode, and IMDb fallback is enabled by default.
- Updated preset gallery: all presets now include the IMDb fallback setting.
- Updated Primary Client selector label to list Plex and Jellyfin alongside
  Stremio TV and Nuvio, reflecting the shared flush-edge inset profile.

### Plex and Jellyfin Sync

- Added `plex_sync.py`, a companion script that reads a Plex library, derives
  quality tokens from each title's actual media file metadata, and pushes
  PostersPlus-generated posters back as library covers.
- Added `jellyfin_sync.py`, a companion script with the same workflow for
  Jellyfin libraries, using the Jellyfin REST API directly without a
  third-party SDK.
- Both scripts detect resolution, HDR format, audio codec, and release type
  (Remux, WEB-DL) from file paths and stream display titles.
- Both scripts include an `--inspect` mode that logs derived quality tokens for
  every library title without writing any posters, making it easy to audit
  token derivation against known titles before a full sync.
- TV show quality is derived from a representative episode selected by watch
  progress, air date, and episode count.

### Localization

- Added French, Portuguese, and Italian poster-output translations.
- Added translated genre and sash labels.
- Poster translations follow the selected logo language.
- Missing translation keys fall back to English individually.

### Performance And Reliability

- Changed the default Uvicorn worker count from 2 to 1. A single worker avoids
  loading duplicate OCR models and was faster in testing for typical installs.
- Set text-detection concurrency to 2 by default.
- Added in-flight request coalescing for expensive shared work.
- Hardened SQLite use with WAL mode, busy timeouts, retry handling, and safer
  multi-request cache writes.
- Added cache pruning, reclaim, and vacuum maintenance.
- Improved metadata, logo, poster, rating, and composite cache invalidation.
- Improved handling of stale cache rebuilds and large request bursts.
- Improved Docker builds for amd64 and arm64, including reliable multi-platform
  `latest` publishing.
- Added more detailed diagnostics for text detection, artwork selection, cache
  behavior, quality lookup, and render timing.

### Fixes

- Fixed several false-positive and false-negative text detections found during
  broad real-world poster testing.
- Fixed stale OCR results surviving detector or threshold changes.
- Fixed cases where a skipped textless scan could be treated as a final cached
  decision.
- Fixed duplicate work when many requests asked for the same uncached poster.
- Fixed edge cases in poster ranking with very small or negative vote samples.
- Fixed logo fallback, logo contrast, and oversized-logo edge cases.
- Fixed missing or malformed metadata causing incomplete poster renders.
- Fixed score-bar totals, normalization text, and missing-provider behavior.
- Fixed sash and quality visibility interactions.
- Fixed configurator spacing, slider, dropdown, preview, and mobile layout
  issues.
- Fixed backdrop crop centering on false-positive face detections, where a
  large low-confidence background blob could outrank a smaller, genuinely
  detected face on bounding-box size alone.
- Fixed Docker workflow races that could publish an older image as `latest`.

### Upgrade Notes

#### Recommended defaults

```env
WORKERS=1
TEXTLESS_DETECTION_CONCURRENCY=2
TEXTLESS_DETECTION_MAX_VOTES=3000
RATING_MIN_VOTES=10
TMDB_POSTER_MIN_VOTES=3
TMDB_POSTER_MAX_SCORE_DROP=1.0
PPOCR_BOX_THRESHOLD=0.70
PPOCR_WIDE_BOX_THRESHOLD=0.30
PPOCR_WIDE_MIN_ASPECT=3.0
PPOCR_WIDE_MIN_AREA=0.01
PPOCR_WIDE_MIN_Y=0.55
TEXTLESS_SCAN_TOP=0.08
BAKE_PPOCR_MODEL=true
```

- Keep `WORKERS x TEXTLESS_DETECTION_CONCURRENCY` at or below the number of
  available CPU cores unless the host has been tested under realistic load.
- Larger values can improve short bursts on powerful systems, but also increase
  CPU contention, memory use, duplicate model memory across workers, and
  pressure on SQLite.
- `TEXTLESS_DETECTION_MAX_VOTES` controls the foreground speed versus immediate
  detection tradeoff. Lower values defer more scans; higher values scan more
  posters before responding.

#### Text detector migration

- EAST has been replaced by PP-OCRv5 Mobile.
- Existing EAST settings such as `TEXTLESS_MIN_BOXES`, `EAST_INPUT_WIDTH`,
  `EAST_INPUT_HEIGHT`, `EAST_MODEL_URL`, `EAST_MODEL_PATH`, and
  `BAKE_EAST_MODEL` are no longer used.
- Standard Docker images include the PP-OCR detector model. Builds with
  `BAKE_PPOCR_MODEL=false` download it into the model cache at runtime.

#### Compatibility

- Existing v1.0 poster URLs remain supported.
- Legacy sash and quality parameters continue to map to their current
  equivalents.
- The `combined_badge_min_score` URL parameter is accepted as a fallback for
  `badge_min_score` so existing Combined Text Badge URLs continue to work.
- Compact mode, which appeared during v1.1 development, was replaced by Frosted
  Bar before release.
- Cache schema migrations run automatically.
- Rating, artwork-selection, OCR, and composite signatures automatically refresh
  results affected by changed policies.
- The IMDb fallback is stored in the generated configuration URL and does not
  require a server environment variable.

### Included v1.0.x Maintenance

- Corrected release and Docker publishing workflows.
- Fixed showcase and documentation links.
- Improved multi-platform image publishing and `latest` tag consistency.
