# Repliq–Ledgerly bridge

Ця папка призначена для локальної SQLite-черги, через яку Repliq передає
підготовлені відправлення до Ledgerly.

Файли `shipments.sqlite3`, `shipments.sqlite3-wal` і `shipments.sqlite3-shm`
містять робочий стан, створюються локально та виключені з Git. Шлях до основної
бази задається змінною `REPLIQ_BRIDGE_DB` у локальному `.env` Ledgerly.

Опис процесу: [`../ledgerly/REPLIQ.md`](../ledgerly/REPLIQ.md) і
[`../repliq/SHIPPING.md`](../repliq/SHIPPING.md).
