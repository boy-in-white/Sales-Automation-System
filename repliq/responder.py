#!/usr/bin/env python3
"""Repliq entry point. Legacy protocol fixtures remain for regression tests."""

import argparse
import hashlib
import json
import sys
import tempfile
import termios
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from app import AppError, Repliq, private_lock, read_json, write_private
from olx_client import OlxClient, identifier
from polling import AdaptivePolling

REPLY = "Так, в наявності."
FINAL = {"sent", "skipped", "stale", "sending", "uncertain"}


class StopRequested(Exception):
    pass


def terminal_text(value):
    return "".join(char for char in str(value) if char in "\n\t" or not unicodedata.category(char).startswith("C"))


def clear_terminal_input():
    """A reply typed before the draft is visible is not an approval."""
    try:
        if not sys.stdin.isatty():
            raise AppError("Для підтвердження потрібне звичайне вікно Terminal. Запусти файл «Запустити автовідповідач.command».")
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except (AttributeError, OSError, ValueError, termios.error):
        raise AppError("Не вдалося підготувати підтвердження. Запусти автовідповідач у звичайному вікні Terminal.") from None


def message_count(thread):
    count = thread.get("total_count")
    if isinstance(count, bool) or count is None or not str(count).isdecimal():
        return None
    return int(count)


def has_unread(thread):
    return str(thread.get("unread_count", "0")).isdecimal() and int(thread.get("unread_count", 0)) > 0


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        # Naive OLX timestamps are only compared with other timestamps in the
        # same response. Do not use the computer's timezone for their ordering.
        return parsed.replace(tzinfo=timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        raise AppError("Не вдалося визначити порядок повідомлень. Перевір цей діалог в OLX.") from None


def snapshot(messages):
    """Do not answer if the seller has already replied or chronology is ambiguous."""
    incoming, outgoing, unknown = [], [], []
    for message in messages:
        item = dict(message, _time=timestamp(message.get("created_at")), _id=identifier(message))
        direction = item.get("type")
        (incoming if direction == "received" else outgoing if direction == "sent" else unknown).append(item)
    if not incoming:
        return None
    incoming.sort(key=lambda item: (item["_time"], int(item["id"]) if str(item.get("id", "")).isdecimal() else 0))
    latest = incoming[-1]
    if any(item["_time"] >= latest["_time"] for item in outgoing + unknown):
        return None
    # Equal timestamps without a numeric order cannot be safely resolved by UUID.
    same_time = [item for item in incoming if item["_time"] == latest["_time"]]
    if len(same_time) > 1 and not all(str(item.get("id", "")).isdecimal() for item in same_time):
        raise AppError("Кілька повідомлень мають однаковий час без порядкових номерів. Перевір діалог вручну.")
    fingerprint = hashlib.sha256(json.dumps([
        (item["_id"], item.get("type"), item.get("text", ""), item.get("created_at"))
        for item in sorted(incoming + outgoing + unknown, key=lambda item: item["_id"])
    ], ensure_ascii=False).encode("utf-8")).hexdigest()
    return {"id": latest["_id"], "text": str(latest.get("text") or ""), "fingerprint": fingerprint,
            "created_at": latest.get("created_at", "")}


class Journal:
    def __init__(self, path, account_id, advert_id=None):
        self.path = Path(path)
        self.data = read_json(self.path) or {"version": 1, "accounts": {}}
        if self.data.get("version") != 1 or not isinstance(self.data.get("accounts"), dict):
            raise AppError("Журнал Repliq має невідомий формат. Автовідповідач зупинено.")
        # A new advert filter has its own baseline; records still belong to the account.
        self.account = self.data["accounts"].setdefault(str(account_id), {"baselines": {}, "records": {}})
        self.account_id = str(account_id)
        self.scope = str(advert_id or "all")
        self.baseline = self.account["baselines"].setdefault(self.scope, {"initialized": False, "seen": {}})

    def save(self):
        write_private(self.path, self.data)

    def record_key(self, thread_id, source_id):
        return str(thread_id) + ":" + str(source_id)

    def get(self, thread_id, source_id):
        return self.account["records"].get(self.record_key(thread_id, source_id), {})

    def mark(self, thread_id, source_id, status):
        self.account["records"][self.record_key(thread_id, source_id)] = {"status": status, "updated_at": time.time()}
        self.save()


class ConsoleResponder:
    def __init__(self, client, journal, input_fn=input, output=print, include_existing=False,
                 prepare_input=None, on_activity=None, on_unavailable=None):
        self.client, self.journal = client, journal
        self.input, self.output = input_fn, output
        self.include_existing = include_existing
        self.prepare_input = prepare_input if prepare_input is not None else (clear_terminal_input if input_fn is input else lambda: None)
        self.on_activity = on_activity if on_activity is not None else lambda thread_id: None
        self.on_unavailable = on_unavailable if on_unavailable is not None else lambda thread_id: None
        # Every live launch establishes a fresh boundary. An old journal or
        # an unfinished draft must never enable replies to pre-launch history.
        self.initializing = not include_existing
        if self.initializing:
            self.journal.baseline["initialized"] = False
            self.journal.baseline["thread_counts"] = {}
            self.journal.baseline.pop("thread_versions", None)

    def approval(self):
        self.prepare_input()
        while True:
            answer = self.input("Надіслати? [так / ні / вихід]: ").strip().casefold()
            if answer in ("так", "т", "yes", "y"):
                return True
            if answer in ("", "ні", "н", "no", "n"):
                return False
            if answer in ("вихід", "вийти", "q", "quit", "exit"):
                raise StopRequested()
            self.output("Введи «так», «ні» або «вихід». Enter означає «ні».")

    def scan(self, advert_id=None):
        self.output("\n[%s] Отримую список діалогів OLX…" % datetime.now().strftime("%H:%M:%S"))
        threads = self.client.threads(advert_id)
        return self._process_threads([(identifier(thread), thread) for thread in threads], full_scan=True)

    def scan_active(self, thread_ids):
        if self.initializing:
            raise AppError("Спочатку потрібна початкова перевірка списку діалогів.")
        threads = []
        for thread_id in dict.fromkeys(thread_ids):
            try:
                # Keep the exact ID used in the inbox even if the response
                # exposes an additional UUID or changes the UUID letter case.
                threads.append((str(thread_id), self.client.thread(thread_id)))
            except AppError as error:
                if error.status in (401, 403, 429):
                    raise
                self.on_unavailable(thread_id)
                self.output("Діалог %s: %s. Повертаю його до звичайної перевірки." % (terminal_text(thread_id), terminal_text(error)))
        return self._process_threads(threads, full_scan=False)

    def _process_threads(self, threads, full_scan):
        initializing = self.initializing
        count = 0
        stats = {"checked": 0, "cached": 0, "new": 0, "baseline": 0, "errors": 0, "no_candidate": 0}
        counts = self.journal.baseline.setdefault("thread_counts", {})
        if initializing:
            # The list endpoint already contains message counts. Do not fetch
            # any old conversation just to establish the live-session boundary.
            for thread_id, thread in threads:
                counts[thread_id] = message_count(thread)
            self.initializing = False
            self.journal.baseline["initialized"] = True
            self.journal.account["last_scan"] = dict(stats, completed_at=time.time(), threads=len(threads), sent=0, changed=0, baseline=len(threads), unknown_types=[])
            self.journal.save()
            self.output("Початкову перевірку завершено: зафіксовано %s діалогів, історії повідомлень не завантажувалися. Чекаю тільки нові повідомлення." % len(threads))
            missing = sum(value is None for value in counts.values())
            if missing:
                self.output("OLX не передав лічильник для %s діалогів. Їхню історію не читаю, поки не з’явиться надійний лічильник." % missing)
            return 0

        changed = []
        for thread_id, thread in threads:
            current_count = message_count(thread)
            if self.include_existing:  # Synthetic demo/test fixtures only; no live CLI option.
                changed.append((thread_id, thread, current_count))
                continue
            if current_count is None:
                # Remember that this thread existed before its first valid
                # counter; a missing counter is not permission to read history.
                counts.setdefault(thread_id, None)
                stats["cached"] += 1
                continue
            if current_count == 0 and thread_id not in counts:
                counts[thread_id] = 0
                stats["cached"] += 1
                continue
            if thread_id in counts:
                previous_count = counts[thread_id]
                if previous_count is None:
                    counts[thread_id] = current_count
                    stats["cached"] += 1
                    continue
                if current_count <= previous_count:
                    # Keep the high-water mark if the provider's count drops.
                    # A later rebound must not expose old messages as new.
                    stats["cached"] += 1
                    continue
            changed.append((thread_id, thread, current_count))

        # An idle fast tick neither prints repetitive logs nor fsyncs the journal.
        if not full_scan and not changed:
            return 0
        changed.sort(key=lambda item: not has_unread(item[1]))
        self.output("%s: %s. З новими повідомленнями: %s. Історію решти не завантажую." % (
            "Діалогів" if full_scan else "Швидко перевірено активних діалогів", len(threads), len(changed)))
        unknown_types = set()
        for position, (thread_id, thread, current_count) in enumerate(changed, start=1):
            if len(changed) > 1:
                self.output("Читаю нові повідомлення: діалог %s із %s." % (position, len(changed)))
            try:
                messages = self.client.messages(thread_id)
                stats["checked"] += 1
                current = snapshot(messages)
            except AppError as error:
                if error.status in (401, 403, 429):
                    raise
                stats["errors"] += 1
                self.on_unavailable(thread_id)
                self.output("Діалог %s: %s" % (terminal_text(thread_id), terminal_text(error)))
                continue
            # Advance only after a complete successful read; a failed request
            # is retried for this changed thread on the next list poll.
            counts[thread_id] = current_count
            if not current:
                stats["no_candidate"] += 1
                unknown_types.update(str(message.get("type"))[:40] for message in messages if message.get("type") not in ("sent", "received"))
                continue
            seen = self.journal.baseline["seen"]
            previous_id = seen.get(thread_id)
            record = self.journal.get(thread_id, current["id"])
            seen[thread_id] = current["id"]
            if previous_id == current["id"] and not self.include_existing:
                continue
            if record.get("status") in FINAL:
                continue
            stats["new"] += 1
            self.on_activity(thread_id)
            self.journal.mark(thread_id, current["id"], "pending")
            self.output("\n" + "─" * 58)
            self.output("Діалог: %s · оголошення: %s" % (terminal_text(thread_id), terminal_text(thread.get("advert_id", "—"))))
            self.output("Повідомлення покупця:")
            for line in (terminal_text(current["text"])[:4000] or "(Повідомлення без тексту — наприклад, вкладення)").splitlines():
                self.output("  │ " + line)
            self.output("\nПропонована відповідь: «%s»" % REPLY)
            if not self.approval():
                self.journal.mark(thread_id, current["id"], "skipped")
                self.output("Пропущено.")
                continue
            # Confirmation is tied to the displayed account, source message and text.
            identity = self.client.identity()
            if str(identity.get("id")) != self.journal.account_id:
                raise AppError("Обліковий запис OLX змінився. Перезапусти автовідповідач.")
            refreshed = snapshot(self.client.messages(thread_id))
            if not refreshed or refreshed["fingerprint"] != current["fingerprint"]:
                self.journal.mark(thread_id, current["id"], "stale")
                self.output("Діалог змінився після показу чернетки. Відповідь скасована; перевірю нові повідомлення.")
                continue
            # Persist intent before the non-idempotent POST. A crash or timeout
            # must never cause this message to be sent again on the next launch.
            self.journal.mark(thread_id, current["id"], "sending")
            try:
                self.client.send(thread_id, REPLY)
            except Exception as error:
                self.journal.mark(thread_id, current["id"], "uncertain")
                self.output("Немає підтвердження надсилання від OLX. Перевір діалог вручну; повторного запиту не буде.")
                if isinstance(error, AppError) and error.status in (401, 403, 429):
                    raise
                continue
            self.journal.mark(thread_id, current["id"], "sent")
            self.on_activity(thread_id)
            count += 1
            self.output("Відповідь надіслано." if not getattr(self.client, "demo", False) else "Демо: відповідь підтверджено. Справжнє повідомлення OLX не надсилалося.")
        self.journal.baseline["initialized"] = True
        self.journal.account["last_scan"] = dict(stats, completed_at=time.time(), threads=len(threads), sent=count, changed=len(changed), mode="inbox" if full_scan else "active", unknown_types=sorted(unknown_types))
        self.journal.save()
        self.output("Перевірка завершена: прочитано %s, без змін %s, нових запитів %s, надіслано %s." % (stats["checked"], stats["cached"], stats["new"], count))
        if unknown_types:
            self.output("Є невідомі типи повідомлень OLX: %s. Їх не використовую для надсилання." % terminal_text(", ".join(sorted(unknown_types))))
        return count


class DemoClient:
    demo = True

    def __init__(self):
        self.sent = []

    def identity(self):
        return {"id": "demo", "name": "Тестовий обліковий запис"}

    def threads(self, advert_id=None):
        return [{"uuid": "demo-thread", "advert_id": advert_id or "demo-pants"}]

    def messages(self, thread_id):
        return [{"uuid": "demo-message", "type": "received", "text": "Добрий день, бажаю замовити штани. Чи є в наявності?", "created_at": "2026-11-05 12:00:00"}]

    def send(self, thread_id, text):
        self.sent.append((thread_id, text))
        return {"uuid": "demo-sent"}


def run(args, state_path, client):
    with private_lock(state_path.with_suffix(".lock"), blocking=False):
        profile = client.identity()
        if profile.get("id") is None:
            raise AppError("Не вдалося визначити обліковий запис OLX.")
        print("Обліковий запис: %s (ID %s)" % (terminal_text(profile.get("name") or "OLX"), terminal_text(profile["id"])))
        journal = Journal(state_path, profile["id"], args.advert_id)
        uncertain = sum(record.get("status") in ("sending", "uncertain") for record in journal.account["records"].values())
        if uncertain:
            print("Є запити з невідомим результатом: %s. Вони не надсилатимуться повторно; перевір відповідні діалоги в OLX." % uncertain)
        schedule = AdaptivePolling(args.poll_interval, args.active_interval, args.active_window, args.max_active_chats)
        responder = ConsoleResponder(client, journal, include_existing=args.demo,
                                     on_activity=schedule.activate, on_unavailable=schedule.deactivate)
        last_mode = None
        while True:
            inbox_due, active_due = schedule.due()
            if inbox_due:
                responder.scan(args.advert_id)
                schedule.inbox_checked()
                # This inbox poll already checked active counters. Avoid an
                # immediate duplicate GET /threads/{id} in the same iteration.
                schedule.active_checked()
            elif active_due:
                responder.scan_active(active_due)
                schedule.active_checked(active_due)
            if args.once or args.demo:
                return
            mode = schedule.active_count
            if mode != last_mode:
                if mode:
                    print("Швидкий режим: активних чатів %s, перевірка кожні %s с до %s с після останньої активності. Інші чати — кожні %s с." % (
                        mode, args.active_interval, args.active_window, args.poll_interval), flush=True)
                else:
                    print("Спокійний режим: перевірка списку кожні %s с." % args.poll_interval, flush=True)
                print("Підтвердження вводь лише після запитання «Надіслати?».", flush=True)
                last_mode = mode
            delay = schedule.delay()
            if delay > 0:
                time.sleep(delay)


def _prototype_main():
    parser = argparse.ArgumentParser(description="Repliq: «Так, в наявності» лише після підтвердження в консолі.")
    parser.add_argument("--demo", action="store_true", help="Тестовий діалог без звернень до OLX")
    parser.add_argument("--once", action="store_true", help="Виконати одну перевірку")
    parser.add_argument("--advert-id", type=int, help="Перевіряти лише одне оголошення OLX")
    parser.add_argument("--poll-interval", type=int, default=30, help="Звичайна перевірка списку, секунди (мінімум 30)")
    parser.add_argument("--active-interval", type=int, default=2, help="Швидка перевірка активного чату, секунди (мінімум 2)")
    parser.add_argument("--active-window", type=int, default=120, help="Тривалість швидкого режиму після активності, секунди")
    parser.add_argument("--max-active-chats", type=int, default=8, help="Максимум одночасних швидких чатів (1–8)")
    args = parser.parse_args()
    if (args.poll_interval < 30 or args.active_interval < 2 or
            args.active_interval > args.poll_interval or args.active_window < args.active_interval or
            not 1 <= args.max_active_chats <= 8 or (args.advert_id is not None and args.advert_id <= 0)):
        parser.error("Звичайний інтервал — від 30 с, швидкий — від 2 с, вікно — не менше швидкого інтервалу, швидких чатів — 1–8, номер оголошення — додатний.")
    print("Repliq · пробний автовідповідач")
    print("Відповідь: «%s». Надсилання — тільки після введення «так»." % REPLY)
    print("Реагую на будь-яке нове повідомлення покупця після початкової перевірки. При кожному запуску наявні повідомлення пропускаються.")
    print("Enter / «ні» — пропустити; «вихід» або Ctrl+C — зупинити.", flush=True)
    try:
        if args.demo:
            print("ДЕМОРЕЖИМ: використовується тестовий діалог, звернень до OLX немає.")
            with tempfile.TemporaryDirectory(prefix="repliq-demo-") as temporary:
                run(args, Path(temporary) / "state.json", DemoClient())
        else:
            app = Repliq()
            if not read_json(app.tokens_path).get("access_token"):
                print("\nСпочатку підключи свій обліковий запис:")
                print("1. Запусти «Публічний запуск Repliq.command».")
                print("2. Збережи поточний HTTPS URI у налаштуваннях додатка на порталі OLX.")
                print("3. У Repliq внеси Client ID та Client Secret, натисни «Підключити OLX» і підтвердь доступ.")
                print("4. Запусти цей автовідповідач ще раз. Для перевірки без ключів відкрий «Демо автовідповідача.command».")
                return 1
            run(args, app.data_dir / "responder-state.json", OlxClient(app))
    except (KeyboardInterrupt, EOFError, StopRequested):
        print("\nRepliq зупинено.")
    except BlockingIOError:
        print("Автовідповідач уже працює в іншому вікні. Другий запуск зупинено.")
        return 1
    except AppError as error:
        print("\n" + str(error))
        return 1
    except (OSError, ValueError, TypeError):
        print("Не вдалося прочитати або зберегти дані Repliq. Перевір файли в private та перезапусти програму.")
        return 1
    return 0


def main():
    from sales_service import main as sales_main
    return sales_main()


if __name__ == "__main__":
    raise SystemExit(main())
