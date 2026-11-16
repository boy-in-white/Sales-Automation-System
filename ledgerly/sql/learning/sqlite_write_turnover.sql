-- Local SQLite example for the separate analytics program.
-- Money is stored in kopiykas: 125000.50 UAH = 12500050.
INSERT INTO monthly_turnover (
    month,
    turnover_cents,
    currency,
    orders_count,
    calculated_at,
    source,
    note,
    updated_at
) VALUES (
    '2026-11',
    12500050,
    'UAH',
    87,
    '2026-12-01T08:00:00+00:00',
    'analytics-service',
    '',
    '2026-12-01T08:00:00+00:00'
)
ON CONFLICT(month) DO UPDATE SET
    turnover_cents = excluded.turnover_cents,
    currency = excluded.currency,
    orders_count = excluded.orders_count,
    calculated_at = excluded.calculated_at,
    source = excluded.source,
    note = excluded.note,
    updated_at = excluded.updated_at;

