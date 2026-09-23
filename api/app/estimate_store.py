"""The estimate book's memory, kept across API restarts.

The book lives in the API process; a deploy used to forget every
aircraft it was estimating, and they came back only as they were lost
again. Its last observations are saved every SAVE_S and when the app
stops, and restored when it starts, dropping anything past the
estimator's own maximum age.
"""
import asyncio
import datetime
import logging
import time

from .refdata_models import LiveState

log = logging.getLogger("network-api.estimates")

KEY = "estimate_book"
SAVE_S = 60.0


def save(sessionmaker, book):
    items = book.dump()
    with sessionmaker() as session:
        row = session.get(LiveState, KEY)
        now = datetime.datetime.now(datetime.timezone.utc)
        if row is None:
            session.add(LiveState(key=KEY, value=items, updated_at=now))
        else:
            row.value, row.updated_at = items, now
        session.commit()
    return len(items)


def restore(sessionmaker, book):
    with sessionmaker() as session:
        row = session.get(LiveState, KEY)
        items = row.value if row is not None else []
    return book.restore(items, time.time())


async def keep(app, book):
    """Restore once, then save every SAVE_S until cancelled; a last save
    on the way out."""
    try:
        n = await asyncio.to_thread(restore, app.state.sessionmaker, book)
        if n:
            log.info("estimates: %d aircraft restored", n)
    except Exception as exc:             # noqa: BLE001 — start empty then
        log.warning("estimates: restore failed: %s", exc)
    try:
        while True:
            await asyncio.sleep(SAVE_S)
            try:
                await asyncio.to_thread(save, app.state.sessionmaker, book)
            except Exception as exc:     # noqa: BLE001 — next round
                log.warning("estimates: save failed: %s", exc)
    except asyncio.CancelledError:
        try:
            save(app.state.sessionmaker, book)
        except Exception:                # noqa: BLE001 — shutting down
            pass
        raise
