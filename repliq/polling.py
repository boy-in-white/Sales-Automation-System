"""Bounded per-conversation polling; timers use a monotonic clock."""

import time


class AdaptivePolling:
    def __init__(self, idle_interval=30, active_interval=2, active_window=120,
                 max_active_chats=8, clock=time.monotonic):
        if (idle_interval < 5 or active_interval < 2 or
                active_interval > idle_interval or active_window < active_interval or
                not 1 <= max_active_chats <= 8):
            raise ValueError("Invalid adaptive polling settings")
        self.idle_interval = idle_interval
        self.active_interval = active_interval
        self.active_window = active_window
        self.max_active_chats = max_active_chats
        self.clock = clock
        self.next_inbox_at = clock()
        self.active = {}

    def expire(self):
        now = self.clock()
        for thread_id in list(self.active):
            if self.active[thread_id]["until"] <= now:
                del self.active[thread_id]

    def activate(self, thread_id):
        self.expire()
        now = self.clock()
        self.active[str(thread_id)] = {
            "until": now + self.active_window,
            "next_at": now + self.active_interval,
            "last_activity": now,
        }
        if len(self.active) > self.max_active_chats:
            oldest = min(self.active, key=lambda key: self.active[key]["last_activity"])
            del self.active[oldest]

    def deactivate(self, thread_id):
        self.active.pop(str(thread_id), None)

    def due(self):
        self.expire()
        now = self.clock()
        active_due = sorted(
            (key for key, item in self.active.items() if item["next_at"] <= now),
            key=lambda key: self.active[key]["next_at"],
        )
        return now >= self.next_inbox_at, active_due

    def inbox_checked(self):
        self.next_inbox_at = self.clock() + self.idle_interval

    def active_checked(self, thread_ids=None):
        self.expire()
        next_at = self.clock() + self.active_interval
        for thread_id in (list(self.active) if thread_ids is None else thread_ids):
            if thread_id in self.active:
                self.active[thread_id]["next_at"] = next_at

    def delay(self):
        self.expire()
        deadlines = [self.next_inbox_at]
        for item in self.active.values():
            deadlines.extend((item["next_at"], item["until"]))
        return max(0.0, min(deadlines) - self.clock())

    @property
    def active_count(self):
        self.expire()
        return len(self.active)
