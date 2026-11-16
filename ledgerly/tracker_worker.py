"""Background worker that refreshes due tracking records."""

from __future__ import annotations

import logging
import threading
from typing import Any

from config import TRACKER_BATCH_SIZE, TRACKER_IDLE_SECONDS, TRACKER_LEASE_SECONDS
from tracking_db import claim_due_shipments, get_shipment, init_db, record_check_failure
from tracking_service import refresh_ttn
from alerts import notify_admin


LOGGER = logging.getLogger(__name__)


def tracker_loop(bot: Any, stop_event: threading.Event) -> None:
    init_db()
    LOGGER.info("Tracking worker started")
    from ukrposhta_tracker import UkrTracker
    ukr_tracker=UkrTracker()
    while not stop_event.is_set():
        ukr_tracker.tick(bot)
        try:
            rows = claim_due_shipments(TRACKER_BATCH_SIZE, TRACKER_LEASE_SECONDS,nova_only=True)
        except Exception:
            LOGGER.exception("Could not claim tracking work")
            stop_event.wait(10)
            continue
        if not rows:
            stop_event.wait(TRACKER_IDLE_SECONDS)
            continue
        for row in rows:
            if stop_event.is_set():
                break
            ttn = row["ttn"]
            try:
                shipment = get_shipment(ttn) or row
                ok, _snapshot, changed, mode = refresh_ttn(
                    bot,
                    ttn,
                    preferred_account=shipment.get("profile_name") or None,
                    source_text=shipment.get("source_text") or "",
                    description=shipment.get("description") or "",
                    shipment_kind=shipment.get("shipment_kind") or "Наложка",
                    cost_text=shipment.get("cost_text") or "",
                    owner_name=shipment.get("account_name") or None,
                    source_user_id=shipment.get("source_user_id") or None,
                    source_user_name=shipment.get("source_user_name") or None,
                    source_chat_id=shipment.get("source_chat_id") or None,
                    source_thread_id=shipment.get("source_thread_id") or None,
                    source_message_id=shipment.get("source_message_id") or None,
                    olx_order_id=shipment.get("olx_order_id") or None,
                    send_to_1c=True,
                )
                if ok:
                    LOGGER.info("Checked TTN %s; changed=%s", ttn, changed)
                else:
                    LOGGER.warning("Tracking lookup failed for %s: %s", ttn, mode)
                    latest = get_shipment(ttn) or {}
                    failures = int(latest.get("consecutive_failures") or 0)
                    if failures in {3, 6, 12}:
                        notify_admin(
                            bot,
                            f"ТТН {ttn}: {failures} невдалих перевірок поспіль ({mode}).",
                        )
            except Exception as exc:
                LOGGER.exception("Tracking refresh failed for %s", ttn)
                failures = record_check_failure(ttn, type(exc).__name__)
                if failures in {3, 6, 12}:
                    notify_admin(
                        bot,
                        f"ТТН {ttn}: {failures} помилок поспіль ({type(exc).__name__}).",
                    )
        stop_event.wait(2)
    LOGGER.info("Tracking worker stopped")


def start_tracker(bot: Any) -> tuple[threading.Thread, threading.Event]:
    stop_event = threading.Event()
    thread = threading.Thread(
        target=tracker_loop,
        args=(bot, stop_event),
        name="tracking-worker",
        daemon=True,
    )
    thread.start()
    return thread, stop_event

