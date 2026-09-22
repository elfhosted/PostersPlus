# Changelog

## [1.2.0-elf.1](https://github.com/elfhosted/PostersPlus/compare/v1.1.0-elf.7...v1.2.0-elf.1) (2026-09-22)


### Features

* a lightness slider for the tinted vignette ([c2c025f](https://github.com/elfhosted/PostersPlus/commit/c2c025ffd110ba2961387b649902cc782121f631))
* add a fallback mode to both MDBList-free rating sources ([7bcfd3e](https://github.com/elfhosted/PostersPlus/commit/7bcfd3e2d9d61d2de7b2af22a016aa22c8b90904))
* add a Padding control so the notch can be tightened without resizing the label ([9ca6bed](https://github.com/elfhosted/PostersPlus/commit/9ca6bedff1659debdcf9c2b0503eed0de5c5a45c))
* add AniList and Kitsu as anime-native art and rating sources ([b6a4b3c](https://github.com/elfhosted/PostersPlus/commit/b6a4b3cd382b9f037bf31fbd976f53dff3f9cb27))
* add QualiCache as a quality source ([a2782fb](https://github.com/elfhosted/PostersPlus/commit/a2782fb64a9cc3b7775f45b36e705c11c7a65324))
* always emit the anime id placeholders, drop the toggle ([d9ac6dd](https://github.com/elfhosted/PostersPlus/commit/d9ac6dd96a91bb820e3cda5f710e14cda1073ec9))
* **awards:** add 78th Emmy (2026) winners and nominees ([4e3ddfe](https://github.com/elfhosted/PostersPlus/commit/4e3ddfe7d1fd0726bb7b56697c68dc19917973a5))
* badge takes the poster's colour via the frosted-notch logic ([10fd116](https://github.com/elfhosted/PostersPlus/commit/10fd1169b1f935a6f148380f16e4c3174a325680))
* **cache:** send the composite's expiry in Cache-Control ([9f8098c](https://github.com/elfhosted/PostersPlus/commit/9f8098cb94676fd1ad25405c09ac4cafd1bf2115))
* composite logos on anime art, demote the foreign sash ([5171aff](https://github.com/elfhosted/PostersPlus/commit/5171affe81c6042dfd6cb8904350c1664b2d1a23))
* configurable TMDB/MDBList/TVDB API base URLs ([5c90065](https://github.com/elfhosted/PostersPlus/commit/5c90065e4b13afe5e4afa2cd8413660501bb3846))
* **configurator:** Nuvio URL format; /poster reads tmdb: stremio ids ([bd5223c](https://github.com/elfhosted/PostersPlus/commit/bd5223c8596e703e78f81c8b70f9889a54fd2c9e))
* **configurator:** refresh presets, stop stale text colour surviving rebuilds ([15d9519](https://github.com/elfhosted/PostersPlus/commit/15d9519c90df27d57af4614b95803be4bd082c7b))
* fall back to TMDB art when the anime provider misses ([2eb1991](https://github.com/elfhosted/PostersPlus/commit/2eb199160a5fc5e9012e2931a95badc85d0610d5))
* fleet-wide MDBList 429 cooldown over upstream key rotation ([4eb7675](https://github.com/elfhosted/PostersPlus/commit/4eb76755989ab674c23236d212edf642630b6e1e))
* headings on every group, retire the console strip and preview meta ([cc0617b](https://github.com/elfhosted/PostersPlus/commit/cc0617b7a969d556750f71bc477deb27dc0c1435))
* keep imdb/tmdb enrichment on anime requests ([d3ec07c](https://github.com/elfhosted/PostersPlus/commit/d3ec07c30fbcacfbac96359e0a7af48ad5e28e59))
* landscape (16:9) posters ([17e672e](https://github.com/elfhosted/PostersPlus/commit/17e672e61ec367f040b5fef00ffc0ad414356b03))
* landscape and IMDb/TMDB-keyed ids for Nuvio clients ([1d9adc9](https://github.com/elfhosted/PostersPlus/commit/1d9adc9a22470dc4d26e35f62b32dfd068214ca3))
* let operators choose the QualiCache minimum trust tier ([22bfa04](https://github.com/elfhosted/PostersPlus/commit/22bfa0413b2fad4df39b5b5e854df777a71e858b))
* let operators supply the trending list from an env-configured source ([d15effb](https://github.com/elfhosted/PostersPlus/commit/d15effb00a7ffb96fc5f0d1c01a79c1568f30e88))
* let the bullet separator apply in Year mode too ([1b8a329](https://github.com/elfhosted/PostersPlus/commit/1b8a3296bbed707b8fe1a2f3e5a28b899c9b7ea7))
* let the minimalist separator be a bullet instead of the bar ([8d3bd85](https://github.com/elfhosted/PostersPlus/commit/8d3bd850ad0f6fb157aae44b872c4f537e733407))
* link the selected title out to IMDB or TMDB ([2a9db50](https://github.com/elfhosted/PostersPlus/commit/2a9db504a4e1b3bfd7011930cb3c5253ed9e30ca))
* make IMDb ids optional at the poster boundary ([fe53ae2](https://github.com/elfhosted/PostersPlus/commit/fe53ae27c51b57f8637a76a4e2e6f7c82e527493))
* make the band's colour earn its place twice ([673772e](https://github.com/elfhosted/PostersPlus/commit/673772ebf43248d7281de485ce53ec1952e16ea9))
* make the external link a menu, with TMDB artwork shortcuts ([93d86a0](https://github.com/elfhosted/PostersPlus/commit/93d86a01639cf57f6073d6f9f6b926a3926948cb))
* **metrics:** accept METRICS_ACCESS_KEY as a Bearer token ([f1f5466](https://github.com/elfhosted/PostersPlus/commit/f1f546661b5b166bd82970eef0a04bcbf119a400))
* **metrics:** accept METRICS_ACCESS_KEY as a Bearer token ([f7def8d](https://github.com/elfhosted/PostersPlus/commit/f7def8d9ec48e425008de94dfdd3b228f9f0c047))
* **metrics:** carry [#47](https://github.com/elfhosted/PostersPlus/issues/47) Bearer-token METRICS_ACCESS_KEY onto the rebuild ([051db32](https://github.com/elfhosted/PostersPlus/commit/051db325cb1867fcd1e8102ac6514e0bd0e84d9a))
* observability, render limits, rate limiting, leader-elected jobs ([f527317](https://github.com/elfhosted/PostersPlus/commit/f5273174cd7dec0aebbeba544176f09529701984))
* **p:** accept a bare numeric TMDB id ([f869d19](https://github.com/elfhosted/PostersPlus/commit/f869d19e0c3ced0b188368289bb822bb0f33618f))
* pluggable storage/coordination/blobstore backends on v1.2.0 ([fd4be98](https://github.com/elfhosted/PostersPlus/commit/fd4be982ea6b045657ccd2d8457fdbc0c8dc2bb3))
* port Quality and Weights, regroup sash reference, restyle the priority list ([57b0209](https://github.com/elfhosted/PostersPlus/commit/57b02096c1aeaa98a9f29e80773d306ed5aa96b7))
* port the Core design language to the Rating panel ([edb51fc](https://github.com/elfhosted/PostersPlus/commit/edb51fc7c150199975633b1f315291cfb0ebafcd))
* port the design language to the Logo and Sash panels ([54868a0](https://github.com/elfhosted/PostersPlus/commit/54868a0ac32786db9e7ebfb898e6cc38c2e75805))
* poster-coloured vignettes ([dded6da](https://github.com/elfhosted/PostersPlus/commit/dded6daa250ce9446dfde383b37e5fad78abc1bf))
* public-tier lock UI, ElfHosted branding, SEO on tabbed configurator ([aa55cbc](https://github.com/elfhosted/PostersPlus/commit/aa55cbcadbec0bc039dae4e77ae7e86080f1d0af))
* **ratings:** separate anime rating weights ([68144bb](https://github.com/elfhosted/PostersPlus/commit/68144bb96236f4607b233a71664dcfce0f01565b))
* redesign the configurator shell and Core tab ([7793441](https://github.com/elfhosted/PostersPlus/commit/77934410a5f6e5369302f081b33428bb790f3dec))
* redraw the corner bookmark as a rounded folded ribbon ([7565980](https://github.com/elfhosted/PostersPlus/commit/7565980fadc390c8d2a9bdeca34c414d4c974854))
* sample the vignette's colour from the art it fades into ([5118415](https://github.com/elfhosted/PostersPlus/commit/511841508249891a6f1e194f09786b2134d86ab3))
* **sash:** date unreleased movies, fix TV awards on movies and early "Released" ([34e8cfe](https://github.com/elfhosted/PostersPlus/commit/34e8cfeeeda1311527d0fe7cba98424c626b0bfb))
* separate field and rating separators, each fully styleable ([1215f80](https://github.com/elfhosted/PostersPlus/commit/1215f809c75fe5b65dc0a87afabc60c78f740044))
* split minimalist label, notch matching, and tuned vignette defaults ([9e00f3c](https://github.com/elfhosted/PostersPlus/commit/9e00f3ca7cd7d9719f55991a377def464862865b))
* static-preset overload moat — anonymous /p route on v1.2.0 ([ea584f0](https://github.com/elfhosted/PostersPlus/commit/ea584f061735c34d5247e7661ba45381b73c2d20))
* surface Import from URL in the header, add MDBList to the link menu ([2dbaee4](https://github.com/elfhosted/PostersPlus/commit/2dbaee43b2b2f6af95960064606ad3648904467e))
* tier release-status TTL, stop the tinted vignette blurring baked-in titles ([2d0b056](https://github.com/elfhosted/PostersPlus/commit/2d0b056589cc662d6b347132396c8c4f517d48d5))
* translate the release-status and festival sashes ([88ea968](https://github.com/elfhosted/PostersPlus/commit/88ea968ac5ecf69e03d66e31674e4661643f5c8f))


### Bug Fixes

* a matched frost keeps the vignette's lightness, and flips its label ([8ef6c62](https://github.com/elfhosted/PostersPlus/commit/8ef6c6262cb0c051a65bcb2b9e799deccaaa8c98))
* address cross-model review of the v1.2.0 rebuild ([be599d9](https://github.com/elfhosted/PostersPlus/commit/be599d92bfc55b492558b5d115edd0a4be167c59))
* always derive the anime genre from the provider's own list ([566eea5](https://github.com/elfhosted/PostersPlus/commit/566eea5e0e872054bc988d87175081997e3492ca))
* anime-specific genre order, and fall back to the provider score ([b778d35](https://github.com/elfhosted/PostersPlus/commit/b778d35969ba3655e978196b04f43751323de1f1))
* antialias the badge border, unify the info strip colour ([68b10dd](https://github.com/elfhosted/PostersPlus/commit/68b10ddb18924aacb0a207941e0d79de7ca21e09))
* apply the foreign-sash demotion where the render actually reads it ([8c75927](https://github.com/elfhosted/PostersPlus/commit/8c75927d03e44391d712b51f6eeead4ac5f152c8))
* cap concurrent fresh renders, stop healthcheck zombies ([4475e7c](https://github.com/elfhosted/PostersPlus/commit/4475e7c0ba1b13f221596917e1e5efe56dc6fd25))
* cap how colourful the tint is allowed to get ([c801de7](https://github.com/elfhosted/PostersPlus/commit/c801de7b2ddc1be828480b8d749a25cb36327949))
* carry post-rebuild fork fixes onto v1.2.0 ([273691f](https://github.com/elfhosted/PostersPlus/commit/273691f07eab4dcf28d47d0d1bac37d22aa9a11d))
* carry the anime id on {id} instead of the optional placeholder form ([ef2a3d2](https://github.com/elfhosted/PostersPlus/commit/ef2a3d23b1b5548c8873643679435473bb35350e))
* claim the IMDb dataset refresh so WORKERS&gt;1 doesn't fight over it ([f728f83](https://github.com/elfhosted/PostersPlus/commit/f728f830895b85cd8d81e0b850ee89d968db00cc))
* clamp the notch padding floor so it can never grow the badge ([3fe3fab](https://github.com/elfhosted/PostersPlus/commit/3fe3fab960de9c07198c9ed1ba3f5f29e581f76b))
* close a reflected XSS and a path traversal in upstream debug endpoints ([2b5cbeb](https://github.com/elfhosted/PostersPlus/commit/2b5cbeb3aa27107a785357ff228867c051cbb13f))
* close the review gaps in the MDBList-free rating sources ([7a2e952](https://github.com/elfhosted/PostersPlus/commit/7a2e952f34bf5f26dfd6b531a1b79e77b7f2a6a6))
* **config:** an empty API base URL means the default ([cc02846](https://github.com/elfhosted/PostersPlus/commit/cc02846f89860647dc619485a615eaf8365017cb))
* copy the poster URL without the encoded-brace pasteboard flavour ([18d300f](https://github.com/elfhosted/PostersPlus/commit/18d300f9489eea09712ea6c1e54f6da0dbfe8cf5))
* **debug:** gallery style comes from constants, not the request ([d83437f](https://github.com/elfhosted/PostersPlus/commit/d83437f311bfd10b9791afda7daf3cf75e520784))
* default the quality bookmark to 30px and say the size is client-dependent ([96f7951](https://github.com/elfhosted/PostersPlus/commit/96f79516465afc7e6f522b8363dbfb0104e39138))
* derive the poster ETag from the bytes, not the cache key ([9d84d38](https://github.com/elfhosted/PostersPlus/commit/9d84d388a426c90ad439a27e01941538856fb85e))
* discard a cached trending snapshot when the configured source changes ([78700cf](https://github.com/elfhosted/PostersPlus/commit/78700cfc2f35537429cc60e02517261b7041e956))
* do not refuse to boot on a mistyped CDN_CACHE_TTL ([adbae67](https://github.com/elfhosted/PostersPlus/commit/adbae6750e1b8cf190f06b3de15e707e78ff3e43))
* don't let clients cache a poster the server refused to keep ([c4dcbbd](https://github.com/elfhosted/PostersPlus/commit/c4dcbbdda788391457c638e27eb673ec38bff6b7))
* don't let the configurator offer IMDb sources the server can't honour ([9661cef](https://github.com/elfhosted/PostersPlus/commit/9661ceffa26c67e9d9c16e1da138a30cb06d28d8))
* don't take a poster's colour out of its shadows ([eddb601](https://github.com/elfhosted/PostersPlus/commit/eddb6014aa2060b89b2bab4016b229509a5637b6))
* don't tint from a seam that is two colours, or ramp between distant ones ([b3e6854](https://github.com/elfhosted/PostersPlus/commit/b3e68546552b2498a65db583451697cb1ae4eb44))
* drop the custom-palette label, round the colour swatch, even the buttons ([e5db228](https://github.com/elfhosted/PostersPlus/commit/e5db2282cf8c03998affa506efa9fb71f80bc309))
* drop the rating from the info strip when there is none ([cdebbe3](https://github.com/elfhosted/PostersPlus/commit/cdebbe3893ed511ee8cec31dae56367f5c41c0c2))
* emit AIOMetadata's optional placeholder form for every id ([68f28a4](https://github.com/elfhosted/PostersPlus/commit/68f28a43e4a722ffe15d536ae221b4085d5106b8))
* even out how much artwork shows through the tinted band ([4146b92](https://github.com/elfhosted/PostersPlus/commit/4146b92a1d7ff227b465d3d90c10cad44533fe16))
* fit the fallback title instead of cutting it ([bdf6f18](https://github.com/elfhosted/PostersPlus/commit/bdf6f183b8fd42e047ef8fa1f2347743331963f0))
* fourth review round on the v1.2.0 rebuild ([ed0ad5a](https://github.com/elfhosted/PostersPlus/commit/ed0ad5ac5b68e98179f284fb2eb00126a8528379))
* hold chroma, not saturation, when the frost matches a vignette ([86d4af7](https://github.com/elfhosted/PostersPlus/commit/86d4af7116ce396bea61f32e0a2a109f60a2c536))
* let the Two-Tone and Blend vignette switches turn off ([94cb487](https://github.com/elfhosted/PostersPlus/commit/94cb4870b19f4398ea40fc41f329568a679ef105))
* log the canonical id on anime requests ([2e740bd](https://github.com/elfhosted/PostersPlus/commit/2e740bd7684fb87e96c0c8d92d12ad2f11806ab1))
* make the anime id placeholders opt-in again ([944f8e5](https://github.com/elfhosted/PostersPlus/commit/944f8e5a0a336a669563ed9e0f85835d730011b3))
* make the MDBList-free sources work with no MDBList key ([4f98850](https://github.com/elfhosted/PostersPlus/commit/4f988507b397c4de80088c64d03ae51051d84bb1))
* make tooltips actually usable by touch ([354b754](https://github.com/elfhosted/PostersPlus/commit/354b75453fad34fd73d40abcd2938692cd4e1dc4))
* match the notch to the colour the vignette paints, not the one it sampled ([cc164e9](https://github.com/elfhosted/PostersPlus/commit/cc164e94285bf643fd93158e3587a3f5399c2155))
* menu links opened "#" — markup was parsed after the script that fills it ([8f529bf](https://github.com/elfhosted/PostersPlus/commit/8f529bf71b683b69c8426fc62fdd184a003d157d))
* never emit the optional placeholder form for tmdb_id/imdb_id ([707b2de](https://github.com/elfhosted/PostersPlus/commit/707b2de3747dae02541aa375cc1461450fee44b4))
* nine review findings, plus a centre anchor for the minimalist strip ([edfd768](https://github.com/elfhosted/PostersPlus/commit/edfd76898c131c6e7dbb11115fda398a08134e54))
* persist the minimalist separator and centre settings ([b39fd20](https://github.com/elfhosted/PostersPlus/commit/b39fd204ba1ebdbd6f85757241175b8dfeb83a70))
* pick the vignette's colour by how much of the poster wears it ([c01a95a](https://github.com/elfhosted/PostersPlus/commit/c01a95a4912895715e07346f02e41f7e461468db))
* prefer Kitsu's genres relationship over its category tag cloud ([6b0f1f5](https://github.com/elfhosted/PostersPlus/commit/6b0f1f5db305ab68a87f23f36314f222011b8fb4))
* prefer Kitsu's genres relationship over its category tag cloud ([2cf8e03](https://github.com/elfhosted/PostersPlus/commit/2cf8e036f39d79bd1c20796bdb4aae0727a03864))
* **quality:** fold QualiCache BluRay/WEBRip into the Web badge ([2de6eb9](https://github.com/elfhosted/PostersPlus/commit/2de6eb93253c4fc7a50b22015fcc6c55cab84a08))
* read the seam as far as the band lets the art show, both sides of the edge ([9d525d8](https://github.com/elfhosted/PostersPlus/commit/9d525d85e647b81c331764a7e3fca2d205e0820b))
* restore the public-tier /poster lock and PRESET_MDBLIST_FETCH ([77cbada](https://github.com/elfhosted/PostersPlus/commit/77cbadaee24525818c8257f839b4eabe6d02c437))
* review of the Nuvio/emdb/rating-warm work ([7a8cb65](https://github.com/elfhosted/PostersPlus/commit/7a8cb657409849db8471e7c6a3bfd5ef6549eba3))
* second review round on the v1.2.0 rebuild ([da400d6](https://github.com/elfhosted/PostersPlus/commit/da400d69254713863fc788d7be74e94abab75a4c))
* stop a mode change discarding the chosen minimum quality ([e18cf87](https://github.com/elfhosted/PostersPlus/commit/e18cf87d2b674631e600da7eae2b0fe393c01577))
* stop festival sashes naming a prize the film did not win ([a44d671](https://github.com/elfhosted/PostersPlus/commit/a44d671bd1a49788d6104c1ebbcef71a7070e694))
* stop field controls from triggering their own tooltip ([65f7b40](https://github.com/elfhosted/PostersPlus/commit/65f7b4073b9f3eb3ce5fa59e15c176f02e9abf10))
* sweep the rating failure counters along with the back-offs ([6353827](https://github.com/elfhosted/PostersPlus/commit/635382761864dce51ab198911fb208ed85a8d0e1))
* version the anime metadata cache key ([2885d7d](https://github.com/elfhosted/PostersPlus/commit/2885d7dd7b1c7e0da723d4258877a869ea6d30a3))
* versioned composite blobs, and third review round ([046379d](https://github.com/elfhosted/PostersPlus/commit/046379d75ff341e27e0df3d40c8a7451bbecdffa))


### Performance Improvements

* cap anime provider concurrency per provider ([088744b](https://github.com/elfhosted/PostersPlus/commit/088744b2ef613d6a8c76af6587d1c38af80f1154))
* cut generated poster URLs to roughly a third of their length ([7cca0b0](https://github.com/elfhosted/PostersPlus/commit/7cca0b07b52eab5a34642e8e9b2046de17ac0992))
* cut resident memory and speed up text-fallback renders ([1f314e1](https://github.com/elfhosted/PostersPlus/commit/1f314e1284d7f674163bf2fe611faf91607d2eae))
* size OCR threads from the real CPU budget, drop full-canvas composites ([37fafdb](https://github.com/elfhosted/PostersPlus/commit/37fafdbd3338ceb3ca7e8ee377712d8619473c1c))


### Reverts

* keep the two-tone ramp's second colour on clusters ([8babb91](https://github.com/elfhosted/PostersPlus/commit/8babb91ac3dfbd0abb04fce5be68d06999d58857))

## v1.2.0 - 2026-09-20

This release is compared with `v1.1.0`.

### Highlights

- Added anime-native poster requests through AniList and Kitsu, with separate
  anime rating weights for MyAnimeList, AniList and Kitsu scores.
- Added a 16:9 landscape poster layout, poster-coloured vignettes, and frosted
  elements that match the painted vignette colour.
- Made IMDb ids optional: `tmdb_id` is the identity, so titles TMDB has no IMDb
  link for now render with ratings and sashes instead of failing.
- Added QualiCache as a quality source, so poster rendering answers from a
  shared cache instead of waiting on a scrape.
- Added MDBList-free rating inputs: TMDB's own vote average and IMDb's daily
  dataset, each usable as the primary source or as a fallback when MDBList has
  no value.
- Added background cache warming for trending, popular, and custom-catalog
  titles, with quota-aware MDBList spending, plus custom trending sources.
- Fixed festival sashes naming a top prize the film did not win; top prizes are
  now checked against Wikidata-built lists verified edition by edition.
- Redesigned the configurator with new presets, row tooltips, a title-link
  menu, and generated URLs about a third of their previous length.
- Hardened the server for cold-catalog bursts: fresh renders queue behind an
  admission cap, and the healthcheck no longer leaves zombie processes.
- Added Brazilian Portuguese translations and translated the remaining sash
  vocabulary in every shipped language.

### Anime

- Added anime-native poster requests through AniList and Kitsu. Clients that
  supply `anilist_id`, `kitsu_id`, or an AIOMetadata-compatible `{id}` can use
  the provider's cover art, title, genres, air dates, lifecycle status, and
  community score without converting the title to a TMDB or IMDb id.
- Added an off-by-default Anime IDs configurator option for AIOMetadata poster
  URLs. Anime-native ids are also carried through to compatible quality sources,
  so stream-quality badges continue to work when no IMDb id exists.
- Anime requests now keep any accompanying TMDB and IMDb ids for logos, MDBList
  ratings, awards, age ratings, release data, and other enrichment. AniList and
  Kitsu scores participate in the normal weighted-rating pipeline and default
  to zero weight.
- Anime cover art can receive a TMDB logo by default. If an anime provider is
  unavailable or misses a title, rendering temporarily falls back to TMDB art
  instead of a genre canvas without caching the degraded result.
- Improved anime provider caching, concurrency limits, genre selection, request
  identity, and placeholder handling. Definitive misses are negative-cached,
  while throttles and transient provider failures are not.

### Poster Rendering

- Added a dedicated 16:9 poster layout through `shape=landscape`, with backdrop
  artwork, height-relative sizing, a unified bottom information band, and clear
  top corners for client overlays. `landscape_art` selects textless or original
  artwork and `badge_pos` controls the age-rating badge position.
- Added poster-coloured top and bottom vignettes with saturation, blur,
  lightness, two-colour ramp, and blend controls. Tint selection now samples the
  artwork near the visible seam, rejects shadow-only and conflicting colours,
  and limits excessive chroma for more consistent results across a shelf.
- Frosted notches and bars can match the colour actually painted by a tinted
  vignette. Matching preserves the vignette's lightness and falls back to the
  normal frost colour when the band does not have a reliable tint.
- Posters confirmed to contain a baked-in title use a plain black vignette
  instead of blurring and tinting the title inside the artwork.
- Expanded Minimalist mode with a Split layout, optional centring, and separate
  field and rating separators. Pip, bullet, and rating-star treatments are
  exposed only where they apply, including the score-coloured separator in Year
  mode.
- Added independent notch padding so the space above and below a label can be
  tightened without shrinking the font, changing the badge width, or moving the
  notch.
- The release-status sash now shows the date an unreleased movie arrives, and
  where, when TMDB has published one — `Oct 16 Cinema`, `Oct 23 Streaming`, or
  `Dec 2027 Cinema` when it is a year or more away — instead of a bare
  `Cinema` / `Production`. In cinemas the date is the next digital or disc
  release; in production it is the first release anywhere. Translated in every
  shipped language, and switchable off with the new Release Status: Show Date
  toggle (`release_status_dates=false`).

### Configurator

- Redesigned the configurator with rounded panels, sentence-case group headings,
  text tabs, consistent spacing and controls, a cleaner preview panel, and
  refreshed preset and import dialogs across every settings tab.
- Reworked inline help into row tooltips that also work on touch devices, and
  improved control grouping, contrast, button styling, colour swatches, and the
  sash-priority editor.
- Moved Import from URL into the header and added a title-link menu with IMDb,
  TMDB artwork, MDBList, and SIMKL shortcuts. Fixed menu links that could open
  `#` before their targets were initialized.
- Generated poster URLs and presets no longer carry an `imdb_id` placeholder,
  which previously discarded the whole URL for any title without an IMDb link.
  A title with no linked IMDb id is now reported as a normal state rather than an
  error, previews load from the TMDB id alone, and the result is remembered for a
  week so the resolver is not re-run on every load.
- Plex and Jellyfin sync no longer skip library items that have a TMDB id but no
  IMDb id. Quality badges from the local file continue to work for those items,
  and an `imdb_id` baked into a copied recipe URL can no longer be applied to
  items it does not belong to.
- Wait for Quality is now sent for the Combined badge mode, which offered the
  toggle but left it out of the generated URL.

### Quality

- Added QualiCache as a quality source: set `QUALITY_SOURCE=qualicache` and
  `QUALICACHE_URL` (plus `QUALICACHE_API_KEY` if QualiCache sets an access key).
  QualiCache crawls Stremio addons in the background and answers from its own
  cache, so poster rendering no longer waits on a scrape and one instance can
  serve PostersPlus and other clients at once.
- Titles QualiCache hasn't collected yet report as pending rather than failed.
  The poster is served without badges and the composite isn't cached, so a later
  request picks the badges up — and a cold title no longer counts against the
  quality source's failure budget the way a real outage does.
- QualiCache `BLURAY` and `WEBRIP` answers now fold into the silver Web badge.
  Older shows whose best trusted release is an encode rather than a remux or
  WEB-DL (*Lost*, *Futurama*) previously showed no quality badge at all, since
  the source token was dropped and a resolution alone is never drawn. Only a
  true remux keeps gold. Tokens with no PostersPlus equivalent (`8K`, `1440P`,
  `720P`, `SD`, `HDTV`) are still dropped rather than approximated.
- Quality backend selection now runs through one dispatcher instead of being
  repeated at each call site. `/status` reports the active backend as
  `quality_source`.
- The Quality Bookmark badge mode now seeds Badge Size at 30 rather than 16.
  At 16 the corner mark was barely visible at the poster sizes most clients
  render at, so the mode looked like it hadn't worked. The right value varies
  by client, which the mode's tooltip now says.

### Ratings

- Added separate rating weights for anime. `anime_movie_weights` and
  `anime_tv_weights` take the same `source:weight` list as `movie_weights` /
  `tv_weights` and apply to any title carrying a MyAnimeList, AniList or Kitsu
  rating — MDBList supplies the MyAnimeList score for anime it knows, so this
  covers anime requested by ordinary TMDB/IMDb id as well as the anime-native
  path. Both are opt-in: a URL naming neither scores its anime with the movie
  and TV weights exactly as before, so nothing changes for existing URLs. The
  source lists are what MDBList actually returns for anime: anime films carry
  every movie source, while anime series never carry a Metacritic critic score
  or a Roger Ebert review (dropped) but do often carry Letterboxd (added). The
  Weights tab gains a **Separate Anime Weights** toggle that reveals the two
  groups, and `debug=1` now reports `is_anime` and the `rating_weights` used.
- Added an MDBList-free way to source two of the weighted rating inputs.
  `tmdb_rating_source=direct` uses TMDB's own vote average — already fetched
  alongside genre/year/credits, so it costs nothing extra and needs no MDBList
  key. `imdb_rating_source=dataset` sources the IMDb weight from IMDb's own
  free, no-key, daily-refreshed non-commercial dataset
  (`title.ratings.tsv.gz`), downloaded and refreshed on a schedule
  (`IMDB_DATASET_ENABLED`, `IMDB_DATASET_REFRESH_HOURS`,
  `IMDB_DATASET_MIN_VOTES`, `IMDB_DATASET_PATH`) and looked up locally with no
  per-title network call. Both default to the existing MDBList-sourced
  behaviour and are exposed as dropdowns on the Weights tab, and either can be
  used with zero MDBList key configured.
- Both settings also take `fallback`, which keeps MDBList as the source of
  truth and consults the local source only when MDBList has no value for that
  title. That covers a hard gap — a rate-limited or exhausted key, a timeout,
  every configured key cooling down — and a soft gap, where MDBList answered
  but carried no score for the title (or one `RATING_MIN_VOTES` filtered out),
  with the same rule. `tmdb_rating_source=fallback` needs no server-side setup
  at all, which makes it the cheapest way to keep scores alive through an
  MDBList outage. This is a different layer from `fallback_to_imdb`: that one
  fires when the *weights* score nothing and reaches for whatever `imdb` value
  is present, whereas these put a value there for it to find. They compose.
- The configurator now disables the two dataset-backed IMDb source options
  when `IMDB_DATASET_ENABLED` is off server-side, and coerces an imported URL
  that names one back to `mdblist`. Selecting an option the server can't
  honour produced a URL that looked configured and silently scored `N/A`.
  `/server-caps` already reported the state; nothing was reading it.
- Only one worker per interval downloads the IMDb dataset. With `WORKERS` > 1
  every worker ran its own copy of the refresh loop against the same
  database, so the losers of the table swap failed with `database is locked`
  and — worse — kept a stale row count, which left them reporting an empty
  dataset on `/server-caps` and splitting the composite cache signature.
- Corrected `.env.example`'s `MDBLIST_API_KEY` documentation, which called it
  `[Required]`; it has been optional in the request path for some time (see
  the "IMDb ids are now optional" entry above) and is now spelled out exactly
  which sashes and the score are unavailable without it.

### Metadata And Caching

- Poster responses now advertise the composite's own expiry, so a caching client
  keeps a trending-sashed poster for a day and a settled title for the full
  `COMPOSITE_CACHE_TTL`. A configured `CDN_CACHE_TTL` acts as a ceiling the
  deadline can lower, `CDN_CACHE_TTL=auto` drops the ceiling, and `0` still
  sends no `Cache-Control`. `304` responses carry the same freshness.

- IMDb ids are now optional. `tmdb_id` is the required identity — it selects the
  artwork and metadata — and `imdb_id` is optional enrichment. Titles TMDB has no
  IMDb link for previously returned an error and, through AIOMetadata, lost their
  poster entirely because a required placeholder with no value discards the whole
  URL. Existing URLs that send both ids are unchanged.
- Ratings, awards, keywords, and age ratings are now looked up through MDBList's
  TMDB route when no IMDb id is available, so TMDB-only titles keep their score
  and sashes. A title MDBList does not know still renders from TMDB metadata with
  an `N/A` score.
- Stream-quality lookups now resolve their id after metadata, so a title whose URL
  omits `imdb_id` still gets quality badges via the IMDb id TMDB itself supplies.
  Anime keeps its provider-native id for these lookups. Titles with no IMDb id
  anywhere skip the lookup rather than issuing one nothing can answer; an explicit
  `quality=` override is unaffected.
- Rating cache, coalescing, and back-off state are now keyed on one immutable
  per-request identity (`tmdb:<id>` when there is no IMDb id) rather than the raw
  `imdb_id` parameter. Cache warming writes the same identity the request path
  reads. Metahub logo fallback, digital-release detection, and IMDb links run only
  when an IMDb id is actually available.
- `/poster?debug=1` now reports the resolved identities — `canonical_id`,
  `rating_provider`, `rating_media_id`, `quality_id`, and `effective_imdb_id`.
- Added `TRENDING_SOURCE_MOVIE` and `TRENDING_SOURCE_TV` so operators can replace
  TMDB's global trending list with an MDBList page or a TMDB-shaped endpoint.
  The configured order drives both trending sashes and cache warming, enabling
  regional or service-specific rankings.
- Custom trending sources now isolate movie and TV entries, reject rows without
  numeric TMDB ids, follow canonical MDBList URLs, refresh cleanly when the
  configured source changes, and avoid exposing credentials or query strings in
  cache signatures and logs.
- Release-status caches now use status-aware lifetimes: active, in-production,
  and cinema titles refresh quickly, while ended, cancelled, physical, and
  established streaming releases remain cached longer. Known release dates set
  the next refresh boundary directly.
- Composite posters now expire no later than the release data rendered into
  them. Disk and in-memory cache entries share the same deadline, and cache
  warming reuses the trending snapshot it already fetched.
- Rating-provider failure counters are now pruned together with their expired
  backoff state.
- Renamed the award sash labels so winners and nominees no longer share the
  same text: "Best Picture" / "Golden Globe" became "Oscar Winner" / "Oscar
  Nominee" and "Globe Winner" / "Globe Nominee", in every shipped language.
  A new `sash_winner_star` toggle prefixes winners with a star, replacing the
  old heuristic that guessed from the shared label.
- Fixed movies wearing TV awards. TMDB movie and TV ids are separate
  namespaces, but the Emmy and Golden Globe id lists were searched as one, so
  *Back to the Future* (movie/105) inherited *Sex and the City*'s Emmy and
  *Donnie Darko* (movie/141) inherited *Cheers*'s. Lookups now use the film or
  TV lists by media type, and cached rating rows rebuild their Globe / Emmy
  labels on read so existing rows correct themselves.
- Fixed unreleased movies reading `Streaming`. TMDB flips a film to `Released`
  ahead of its first date, and limited-theatrical and festival-premiere dates
  were not being read at all, so a title like *You Can See Everything* (two
  festival premieres, limited release in October) had no dates to contradict
  the flag. Limited releases now count as theatrical, a future premiere counts
  as proof the film is not out, and cached rows that recorded no dates are
  re-fetched once.

### Performance And Reliability

- Reduced startup memory by loading genre fallback backgrounds on demand into a
  bounded cache instead of decoding the whole gallery, and reduced per-thread
  SQLite page-cache memory. Fallback fonts are now cached as well.
- Made fallback-title rendering faster and more reliable by starting font
  fitting from a monotonic width search, fitting long titles rather than cutting
  them off, and ellipsizing every landscape fallback line that needs it.
- Reduced score and quality-bar composition work by drawing only the affected
  strips instead of repeatedly compositing full-canvas layers.
- OCR thread sizing now respects the container's actual cgroup CPU quota rather
  than the host CPU count. `TEXTLESS_DETECTION_CONCURRENCY` now defaults to `1`
  to avoid slower scans and roughly 50 MB of unnecessary memory per idle
  session; larger values remain available for cold-cache library sweeps.
- Landscape requests no longer wait for quality data the layout does not render,
  and transient custom-trending failures use a short retry cooldown rather than
  refetching once per poster.
- A burst of uncached poster requests — a cold catalog or tabbed grid asking
  for 50+ posters in a second — no longer fails en masse with `PoolTimeout`.
  Fresh renders now queue behind a per-worker admission cap
  (`POSTER_RENDER_CONCURRENCY`, default `8`); cache hits and requests coalesced
  onto an in-flight render are never held back. The upstream connection pool is
  sized from that cap, and a request waits up to 10 seconds for a connection
  rather than 5. `/stats` reports `renders_active`, `renders_queued` and
  `render_slots`.
- Cache warming no longer drains a free MDBList key's daily quota. MDBList's
  limit is 1,000 requests per key per day (more on paid tiers), not a burst
  limit, and the default `CACHE_WARM_MDBLIST_BUDGET` of 500 took half of it in
  one cycle — leaving live poster requests to 429 for the rest of the day.
  Every MDBList response reports the remaining quota, and the warmer now
  reads it: it stops spending a key once its remaining requests fall to
  `CACHE_WARM_MDBLIST_RESERVE` (default `300`), moving to `MDBLIST_API_KEY_2`
  when that key still has room, and never drags live traffic off a key that
  is merely at its reserve. A quota 429, which carries no `Retry-After`, now
  parks the key until MDBList's own reset time instead of retrying hourly
  against a key that is dead until midnight UTC. `/stats` reports each key's
  `daily_limit`, `daily_remaining` and `quota_reset_at`.
- The container no longer accumulates zombie `python3` processes under load.
  The Docker healthcheck ran through a shell, so a probe that overran its 5s
  timeout on a busy host left an orphaned `python3` that nothing reaped. The
  probe now runs without a shell, imports less, and gets 10s; `tini` is PID 1
  so any orphan is reaped regardless.

### Configurator

- Generated poster URLs are about a third of their previous length - roughly
  1500 characters down to 450 on the shipped presets. Some metadata services
  truncate or reject URLs past 2000 characters, and most of what was there
  restated settings the server would have chosen anyway. Three changes get it
  there: parameters already at their default are left out, `sash_priority` is
  sent as a diff against the default order, and rating sources weighted at zero
  are no longer named. The server parses the result identically and every URL
  generated before this keeps working unchanged.
- `sash_priority` now accepts a diff form: `default,-cult,festival@0` removes
  the cult sash and promotes the festival one, instead of listing all thirty
  slots. The full list is still accepted and still means what it always did.
- The defaults the configurator omits are read from the server at load time
  rather than restated in the page, so they cannot drift apart. If the server
  cannot be reached the full-length URL is generated instead.
- Replaced the ten shipped presets with a new set — tinted minimalist,
  colour-matched bar/notch/sash, and rating-bar variants — with WebP
  screenshots.
- Fixed a rating or sash text colour that, once typed, came back after every
  container rebuild even after being cleared. The reset-on-load skipped hex
  text boxes and an imported URL that omitted the parameter left the old value
  in place; both now clear to the server default.

### Fixes And Documentation

- Fixed festival sashes naming a top prize the film did not win. MDblist tags a
  title `festival-cannes-winner` if it won *anything* at Cannes, and that was
  read as "Palme d'Or" — so the whole 2023 slate, from the Grand Prix winner
  down to the Un Certain Regard one, wore a Palme d'Or sash — nine films, of
  which one had won it. Every festival in the list had the same fault.
  The top prize is now looked up by TMDB id against a list built from Wikidata,
  and the keyword only supports the weaker claim it can actually carry: a title
  that won something at Cannes but not the Palme reads "Cannes Winner". A top
  prize now also shows when MDblist is unreachable, since the list is local.
  Cached titles convert on first startup without re-fetching anything.
- Cross-checked every top-prize list against the festivals' own winners tables,
  edition by edition. That restored 34 winners the first source had no record
  of — Joker's 2019 Golden Lion, four recent Locarno Leopards, Cannes' 1946
  eleven-way tie — each of which had been showing the weaker sash.
- Removed 9 films that were wearing a top prize they did not win. Three were
  Golden Bear winners for Best Short Film rather than the Golden Bear, two were
  Berlinale and Locarno sidebar prizes, and one was a mismatched id: Precious
  premiered at Sundance as "Push: Based on the Novel by Sapphire", and its Grand
  Jury Prize had landed on the unrelated 2009 science-fiction film Push, which
  wore the sash while Precious went without.
- Removed the Toronto, Busan, Rotterdam, SXSW and Tribeca festival sashes. Their
  labels — People's Choice, New Currents, Tiger Award, SXSW Jury, Tribeca AA —
  named specific prizes no available source can confirm, and unlike the five
  festivals that remain there is no list to check them against. Cannes, Venice,
  Berlin, Locarno and Sundance are unaffected.
- Fixed quality badges never appearing unless Wait for Quality was on. A poster
  served before its quality arrived was correctly kept out of the composite
  cache, but still carried an ETag identical to the finished render's, so
  clients and CDNs revalidated their badge-less copy and were told it was still
  current. Renders the server declines to keep now ship no validator and ask not
  to be stored. Clients holding a badge-less poster from before this fix keep it
  until the composite TTL lapses or the URL changes.
- Fixed missing ratings leaving an empty score in the information strip, and
  fixed fallback titles that could be clipped instead of resized to fit.
- Fixed landscape fallbacks losing their title, TV shows retaining a stale
  ended status after revival, and release sashes surviving past a newly reached
  digital-release boundary.
- Added the 78th Emmy (2026) winners and nominees to the award sash data.
- Split the oversized `.env.example` into a concise starter configuration and a
  new `ADVANCED.md` tuning reference. Added previously undocumented OCR and face
  model path overrides and corrected OCR concurrency guidance and defaults.

### Localization

- Added Brazilian Portuguese (`pt-br`) poster-output translations, contributed
  by @danilopagotto82.
- Region-qualified translation files take precedence over the bare language, so
  a `pt-br` request uses `languages/pt-br.json` rather than `languages/pt.json`.
  Selecting `pt-br` also restricts logo artwork to Brazil-tagged entries,
  falling back to English rather than to Portugal-tagged art.
- Translated the remaining fixed sash vocabulary in every shipped language: the
  release-status labels (`Physical`, `Streaming`, `Cinema`, `Production`,
  `Airing`, `Ended`, `Cancelled`) and the ten festival winner labels, from
  `Palme d'Or` through `Tribeca AA`. Previously these rendered in English on an
  otherwise translated poster.

## [1.1.0-elf.7](https://github.com/elfhosted/PostersPlus/compare/v1.1.0-elf.6...v1.1.0-elf.7) (2026-06-11)


### Bug Fixes

* stop release-please aborting auto-tag (empty component) ([8e17cd0](https://github.com/elfhosted/PostersPlus/commit/8e17cd0858ea7ed626ff4257f72a10831d50a625))
* stop release-please aborting auto-tag (empty component) ([310a2ea](https://github.com/elfhosted/PostersPlus/commit/310a2eac012874634394d2491d1a23aac341c6b5))

## [1.1.0-elf.6](https://github.com/elfhosted/PostersPlus/compare/v1.1.0-elf.5...v1.1.0-elf.6) (2026-06-11)


### Bug Fixes

* align public lock banner width with layout (v1.1.0-elf.6) ([7695ba6](https://github.com/elfhosted/PostersPlus/commit/7695ba6c2d2f4e4da7a550e320d5b779cf37bd22))
* align public lock banner width with the layout ([0d8e87d](https://github.com/elfhosted/PostersPlus/commit/0d8e87d0ddb7fb6a2860ab261c741953b0e5f3ff))

## [1.1.0-elf.5](https://github.com/elfhosted/PostersPlus/compare/v1.1.0-elf.4...v1.1.0-elf.5) (2026-06-10)


### Features

* read-only showcase for public lock view (v1.1.0-elf.5) ([986d05b](https://github.com/elfhosted/PostersPlus/commit/986d05b086ccadffafd13f5e55555009a6dac22a))
* read-only showcase for the public lock view ([d38de4f](https://github.com/elfhosted/PostersPlus/commit/d38de4ff14c44797d08479e7094f39ad4f97b5c4))

## [1.1.0-elf.4](https://github.com/elfhosted/PostersPlus/compare/v1.1.0-elf.3...v1.1.0-elf.4) (2026-06-10)


### Bug Fixes

* clean preset-only public lock view ([edfc9a4](https://github.com/elfhosted/PostersPlus/commit/edfc9a43609164d2d51f3918a7710c96c3b3c494))
* clean preset-only public lock view (v1.1.0-elf.4) ([942ecaf](https://github.com/elfhosted/PostersPlus/commit/942ecaf045c02773d268b6777ebb1aa922d5a37b))

## [1.1.0-elf.3](https://github.com/elfhosted/PostersPlus/compare/v1.1.0-elf.2...v1.1.0-elf.3) (2026-06-10)


### Bug Fixes

* real version in header What's-New chip (v1.1.0-elf.3) ([a02eb7a](https://github.com/elfhosted/PostersPlus/commit/a02eb7aa9a64ff3f3e34ccab4014de2eea0e0312))
* show real version in the header What's-New chip ([66bbbcf](https://github.com/elfhosted/PostersPlus/commit/66bbbcf8e9b92d8268896bd4624b7350bde28c25))

## [1.1.0-elf.2](https://github.com/elfhosted/PostersPlus/compare/v1.1.0-elf.1...v1.1.0-elf.2) (2026-06-10)


### Bug Fixes

* discoverable preset picker in public lock mode ([b353080](https://github.com/elfhosted/PostersPlus/commit/b353080ac129932b9db75979bf67cd05e08f05fe))
* public-tier version footer + discoverable preset picker (v1.1.0-elf.2) ([6262990](https://github.com/elfhosted/PostersPlus/commit/6262990f450b86c9f189003e9d41fdafde635d44))
* surface app version in /server-caps for the build footer ([45797ba](https://github.com/elfhosted/PostersPlus/commit/45797ba231010961aee9502fdbe838f378147834))

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
