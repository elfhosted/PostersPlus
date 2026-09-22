# digital_release.py
"""
Polls r/movieleaks every 24 hours via the Arctic Shift archive API to build a
set of IMDB IDs for movies that have recently hit digital/streaming.

Arctic Shift is used instead of Reddit's JSON endpoint because Reddit blocks
unauthenticated requests from datacenter IPs (Oracle, AWS, GCP, etc.).

Posts younger than MIN_AGE_DAYS are skipped — mod cleanup usually completes
within a few hours, so a 1-day hold filters any noise before it reaches the DB.
Entries older than MAX_AGE_DAYS are pruned by the regular cache prune loop.
"""
import asyncio
import logging
import re
import time
from contextlib import suppress

import httpx

from cache import add_digital_releases
import coordination as coord
from config import DIGITAL_RELEASE_MAX_AGE_DAYS, DIGITAL_RELEASE_MIN_AGE_DAYS

logger = logging.getLogger(__name__)

_ARCTIC_SHIFT_URL = "https://arctic-shift.photon-reddit.com/api/posts/search"
# Match tt + 1-10 digits, with a negative lookahead so we don't grab the first
# 10 chars of a longer numeric run (TMDB / IMDB IDs are at most 10 digits).
_IMDB_RE          = re.compile(r"tt\d{1,10}(?!\d)")
_LIMIT            = 100   # posts per page
_MAX_PAGES        = 10    # hard cap — 1 000 posts covers 30 days of sub activity
_POLL_INTERVAL    = 86400  # seconds (24 h)
_PAGE_PAUSE       = 1.0    # seconds between paginated requests


async def _fetch_page(
    client: httpx.AsyncClient,
    after_ts: int,
    before_ts: int,
) -> list[dict]:
    """
    Fetch one page of posts from Arctic Shift between after_ts and before_ts
    (both Unix timestamps), newest first.
    """
    try:
        resp = await client.get(
            _ARCTIC_SHIFT_URL,
            params={
                "subreddit": "movieleaks",
                "after":     after_ts,
                "before":    before_ts,
                "limit":     _LIMIT,
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        return resp.json().get("data", [])
    except Exception as exc:
        logger.warning(f"Digital release: Arctic Shift fetch failed: {exc}")
        return []


async def sync_digital_releases(client: httpx.AsyncClient) -> int:
    """
    Fetch r/movieleaks posts within the trust window, extract IMDB IDs, and
    persist them.  Returns the number of new entries added to the DB.

    The window is [now - MAX_AGE_DAYS, now - MIN_AGE_DAYS], queried with
    timestamp-based pagination so we always get exactly the posts we want
    regardless of whether the cache is empty or already populated.
    """
    now        = int(time.time())
    before_ts  = now - DIGITAL_RELEASE_MIN_AGE_DAYS * 86400   # exclude posts < 1 day old
    after_ts   = now - DIGITAL_RELEASE_MAX_AGE_DAYS * 86400   # exclude posts > 30 days old

    entries: list[tuple[str, int]] = []
    cursor     = before_ts   # walk backwards through time

    for page_num in range(_MAX_PAGES):
        posts = await _fetch_page(client, after_ts=after_ts, before_ts=cursor)
        if not posts:
            break

        for post in posts:
            created_utc = post.get("created_utc")
            if not created_utc:
                # No timestamp → can't decide age accurately. Skip rather than
                # store with posted_at=0 (which would be pruned next sweep anyway).
                continue
            posted_at = int(created_utc)
            # Scan body, title AND url — posters sometimes put the IMDB link in
            # the title or as the submission URL rather than the self-text body.
            haystack = " ".join((
                post.get("selftext", "") or "",
                post.get("title", "") or "",
                post.get("url", "") or "",
            ))
            for imdb_id in set(_IMDB_RE.findall(haystack)):
                entries.append((imdb_id, posted_at))

        if len(posts) < _LIMIT:
            break   # last page — no need to paginate further

        # Advance cursor to just before the oldest post on this page
        cursor = int(posts[-1].get("created_utc", cursor)) - 1

        if page_num < _MAX_PAGES - 1:
            await asyncio.sleep(_PAGE_PAUSE)

    added = add_digital_releases(entries)
    logger.info(
        f"Digital release sync: {added} new entries added "
        f"({len(entries)} valid posts scanned)"
    )
    return added


async def digital_release_poll_loop(
    client: httpx.AsyncClient,
    initial_sync_done: asyncio.Event | None = None,
) -> None:
    """
    Background task: initial sync shortly after startup, then every 24 h.

    If *initial_sync_done* is given, it's set once the first sync attempt
    finishes (success or failure) — other startup tasks (e.g. cache warming)
    can wait on it so they don't run concurrently with this one.

    ElfHosted fork: leader-elected via the coordination layer so multi-replica
    deployments only have one replica polling the source. With the default
    in-process coordinator each worker leads its own iteration (matches
    upstream).
    """
    lease_ttl = float(_POLL_INTERVAL + 3600)  # one polling cycle + headroom
    lease_token: str | None = None

    await asyncio.sleep(60)   # let the service finish warming up first
    try:
        while True:
            if lease_token is None:
                lease_token = await coord.try_acquire_lease(
                    coord.LEASE_DIGITAL_RELEASE, lease_ttl,
                )
                if lease_token is not None:
                    logger.info("Acquired digital-release-poll lease")
            elif not await coord.refresh_lease(
                coord.LEASE_DIGITAL_RELEASE, lease_token, lease_ttl,
            ):
                logger.info("Digital-release-poll lease lost")
                lease_token = None

            try:
                if lease_token is not None:
                    try:
                        await sync_digital_releases(client)
                    except Exception as exc:
                        logger.error(f"Digital release poll loop error: {exc}")
                else:
                    logger.debug(
                        "Digital release poll skipped — another replica holds the lease"
                    )
            finally:
                # Set on EVERY path, including the one where this replica never
                # held the lease. Whoever waits on this event is waiting for
                # "the feed has had its chance", not for this replica to have
                # been the one to run it — a follower that never sets it would
                # hang the startup tasks gated on it for the whole poll
                # interval, on every replica but one.
                if initial_sync_done is not None and not initial_sync_done.is_set():
                    initial_sync_done.set()

            await asyncio.sleep(_POLL_INTERVAL)
    finally:
        if lease_token is not None:
            with suppress(Exception):
                await coord.release_lease(coord.LEASE_DIGITAL_RELEASE, lease_token)
