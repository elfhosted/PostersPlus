# Contributing

## Branches

**All pull requests target `dev`.**

`main` is release-only. It moves when a version ships, by a `dev` -> `main`
merge — never by a contributor PR. A PR opened against `main` is closed
automatically by the PR Base Guard workflow, with instructions for retargeting
it; your commits are untouched and changing the base is a two-click edit.

If you are working from a fork, branch from `dev` rather than committing to your
fork's `main`, and open the PR against `UmbraProjects:dev`.

## Adding a poster translation

Poster text (genre labels and info-sash labels) is translated per language in
`languages/<code>.json`. See the Poster Translations section of the README for
the full rules. The two that trip people up:

- **Copy `languages/en.json` whole and translate only the values.** The keys are
  the exact canonical English strings the renderer emits; a missing key falls
  back to the English string, so a partial file renders half in English.
- **Region files are not diffs.** `pt-br.json` takes precedence over `pt.json`
  per *table*, not per *key*, so anything absent from a region file falls
  through to English rather than to the base language. A region file must carry
  the full vocabulary.

Contributed languages must be Latin-script — the bundled font has no CJK or
Arabic glyphs and no right-to-left shaping.

## Before opening a PR

```bash
python3 -m pytest tests/ -q
```
