# Changelog

## [1.0.3-elf.1](https://github.com/elfhosted/PostersPlus/compare/v1.0.3...v1.0.3-elf.1) (2026-05-26)


### Bug Fixes

* fall back to TMDB-derived genre when MDBlist is unreachable ([#24](https://github.com/elfhosted/PostersPlus/issues/24)) ([e735b94](https://github.com/elfhosted/PostersPlus/commit/e735b94c6a00e245170a5ae4ecd09a6a7581aa5d))

## [1.0.1](https://github.com/elfhosted/PostersPlus/compare/v1.0.0...v1.0.1) (2026-05-26)


### Bug Fixes

* gate preview expand on mobile viewport to avoid desktop scroll lock ([#22](https://github.com/elfhosted/PostersPlus/issues/22)) ([938a9c0](https://github.com/elfhosted/PostersPlus/commit/938a9c08b1eaf3785b470bccd55eadb35a38f364))

## [1.0.0](https://github.com/elfhosted/PostersPlus/compare/v0.6.0...v1.0.0) (2026-05-26)


### Features

* build-version footer + align to upstream v1.0.0 ([41a85cf](https://github.com/elfhosted/PostersPlus/commit/41a85cf8256e9bfe7e13abb65334fe87feb7adc4))
* build-version footer + align to upstream v1.0.0 ([c40cd5c](https://github.com/elfhosted/PostersPlus/commit/c40cd5ccb39d1a759acafb9a92f1539576e47999))

## [0.6.0](https://github.com/elfhosted/PostersPlus/compare/v0.5.2...v0.6.0) (2026-05-26)


### Features

* add sponsor-the-upstream-developer link ([5889a3d](https://github.com/elfhosted/PostersPlus/commit/5889a3df6f94b8e78c16661250fa0008fe44e4ec))
* add sponsor-the-upstream-developer link ([96a6098](https://github.com/elfhosted/PostersPlus/commit/96a6098724488ad33d7142062b1c9c4387c0e683))

## [0.5.2](https://github.com/elfhosted/PostersPlus/compare/v0.5.1...v0.5.2) (2026-05-26)


### Bug Fixes

* reload preview when preset dropdown changes ([ec49d30](https://github.com/elfhosted/PostersPlus/commit/ec49d307e8c46a4825a919e940703d5501a258d2))
* reload preview when the preset dropdown changes ([392e03c](https://github.com/elfhosted/PostersPlus/commit/392e03ce6776196012bf384c313e28af3a30684f))

## [0.5.1](https://github.com/elfhosted/PostersPlus/compare/v0.5.0...v0.5.1) (2026-05-26)


### Bug Fixes

* propagate blob-put failures so orphan metadata rows aren't written ([d968dae](https://github.com/elfhosted/PostersPlus/commit/d968dae12bcf0432ff608b1c34aef7155b7b2ed7))
* propagate blob-put failures so orphan metadata rows aren't written ([53a2c22](https://github.com/elfhosted/PostersPlus/commit/53a2c2229ecbcf04d3844bb3a9ff951e44c39c71))

## [0.5.0](https://github.com/elfhosted/PostersPlus/compare/v0.4.0...v0.5.0) (2026-05-26)


### Features

* selective port of upstream v1.0.0 — backdrop fallback, MDBlist semaphore, range clamps, log redaction ([dc224e5](https://github.com/elfhosted/PostersPlus/commit/dc224e5a7c0a750b18e877f3c9e9fa2554ae9523))

## [0.4.0](https://github.com/elfhosted/PostersPlus/compare/v0.3.1...v0.4.0) (2026-05-26)


### Features

* re-tune presets to leverage cheaper mode-4 tier bar ([bc64f9c](https://github.com/elfhosted/PostersPlus/commit/bc64f9cf4a22df27f96ce21d4be82cae038c0926))


### Bug Fixes

* cherry-pick upstream crash fix when no rating data (e26d204) ([460851b](https://github.com/elfhosted/PostersPlus/commit/460851b2f3051dd0bf908dcda073df5ed94a8343))

## [0.3.1](https://github.com/elfhosted/PostersPlus/compare/v0.3.0...v0.3.1) (2026-05-25)


### Bug Fixes

* point store CTA at the Posters+ product page ([226ee24](https://github.com/elfhosted/PostersPlus/commit/226ee245c93ef7e577878a0d9f15d3879b8a9e9c))
* point store CTA at the Posters+ product page ([095132d](https://github.com/elfhosted/PostersPlus/commit/095132db8d4702381afd27d14903bf3e23426188))

## [0.3.0](https://github.com/elfhosted/PostersPlus/compare/v0.2.0...v0.3.0) (2026-05-25)


### Features

* brand tagline + Stremio addons CTA + UTM-tagged elfhosted links ([c560289](https://github.com/elfhosted/PostersPlus/commit/c56028996fbab7b5a2d27ece47bf4a19a7ad9c6e))
* informational public-tier banner; drop access-key unlock UI ([c122a36](https://github.com/elfhosted/PostersPlus/commit/c122a367a76649529b0eedb10cf08312bfc1b43e))
* Phase 11 — anonymous CDN-cacheable preset endpoint ([4920db3](https://github.com/elfhosted/PostersPlus/commit/4920db3051dbaaddbbe10c3b239af89c2f0db2d2))
* public-tier preset-only lock + anonymous TMDB proxy gating ([b7246f9](https://github.com/elfhosted/PostersPlus/commit/b7246f9957cdd0f0d06087308efbff97ce8407e8))
* SEO + LLM discovery — meta, JSON-LD, noscript, llms.txt ([2bf6e64](https://github.com/elfhosted/PostersPlus/commit/2bf6e644e5557089b0533ba59dc3cb852f193db8))
* use real ElfHosted logo in configurator header ([6f6cd7f](https://github.com/elfhosted/PostersPlus/commit/6f6cd7f645b403a003d5d90e69a77761887baddb))


### Bug Fixes

* lock-mode silently bypassed by null-deref in updateKeyBadges ([90d07d2](https://github.com/elfhosted/PostersPlus/commit/90d07d2a59833d4d683fdf5d9767fbfc42ec2352))

## [0.2.0](https://github.com/elfhosted/PostersPlus/compare/v0.1.0...v0.2.0) (2026-05-24)


### Features

* move composite poster bytes to object storage; revert TMDB to FS ([298957b](https://github.com/elfhosted/PostersPlus/commit/298957b4d68275ad7bb11f0e1a893b42cd3342e0))
