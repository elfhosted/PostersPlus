<!--
Base branch must be `dev`. PRs into `main` are closed automatically — see the
banner at the top of this form for which branch you're targeting.
-->

## What this changes

<!-- One or two sentences. Link the issue if there is one. -->

## Checklist

- [ ] Base branch is `dev`, not `main`
- [ ] `python3 -m pytest tests/ -q` passes
- [ ] For a new poster translation: `languages/<code>.json` is a full copy of
      `languages/en.json` with only the **values** translated — keys unchanged,
      none removed. Partial region files fall through to English, not to the
      base language.
