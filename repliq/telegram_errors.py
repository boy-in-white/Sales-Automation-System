"""Safe Telegram diagnostics: no raw API descriptions, tokens or customer text."""


def error_reason(description):
    text = str(description).lower()
    for fragments, reason in (
        (("message is not modified", "topic_not_modified"), "not_modified"),
        (("message thread not found", "topic_id_invalid", "topic_deleted"), "topic_missing"),
        (("topic_closed", "topic is closed"), "topic_closed"),
        (("message to edit not found", "message can't be edited", "message can not be edited"), "message_missing"),
        (("can't parse entities", "cannot parse entities", "entity bounds", "entities are not valid"), "entities"),
        (("message is too long",), "too_long"),
        (("button_data_invalid", "reply markup is too long", "can't parse reply keyboard"), "keyboard"),
        (("chat not found",), "chat_missing"),
        (("bot was blocked",), "owner_blocked"),
        (("not enough rights", "chat_write_forbidden", "bot was kicked", "not a member"), "permissions"),
        (("terminated by other getupdates", "conflict: terminated"), "poll_conflict"),
        (("webhook is active",), "webhook"),
        (("query is too old", "query id is invalid"), "expired_callback"),
    ):
        if any(fragment in text for fragment in fragments):
            return reason
    return ""


class TelegramError(Exception):
    def __init__(self, code=0, retry_after=0, reason="", method=""):
        self.code, self.retry_after, self.reason, self.method = code, retry_after, reason, method
        detail = {
            "topic_missing": "тему клієнта видалено або Telegram її не знаходить",
            "topic_closed": "тема клієнта закрита",
            "message_missing": "повідомлення панелі видалене або його вже не можна змінити",
            "entities": "Telegram відхилив оформлення цитат",
            "too_long": "повідомлення перевищує обмеження Telegram",
            "keyboard": "Telegram відхилив кнопки панелі",
            "chat_missing": "групу не знайдено або бот не має доступу",
            "owner_blocked": "особистий чат із ботом заблокований",
            "permissions": "бракує прав у групі",
            "poll_conflict": "цей токен одночасно використовує інша копія бота",
            "webhook": "цей токен підключений до webhook",
            "expired_callback": "натиснення кнопки застаріло",
        }.get(reason, "запит відхилено" if code == 400 else "тимчасовий збій зв'язку" if not code or code >= 500 else "перевір доступ бота")
        super().__init__("Telegram%s: %s (код %s)." % (" · " + method if method else "", detail, code))

    @property
    def rejected(self):
        # A transport timeout or HTTP 5xx can happen AFTER Telegram accepted a POST.
        return 400 <= self.code < 500 and self.code != 408
