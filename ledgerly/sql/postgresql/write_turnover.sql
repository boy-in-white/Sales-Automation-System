-- Example for the separate analytics program.
-- Money is stored in kopiykas: 125000.50 UAH = 12500050.
INSERT INTO analytics.monthly_turnover (
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
    NOW(),
    'analytics-service',
    '',
    NOW()
)
ON CONFLICT (month) DO UPDATE SET
    turnover_cents = EXCLUDED.turnover_cents,
    currency = EXCLUDED.currency,
    orders_count = EXCLUDED.orders_count,
    calculated_at = EXCLUDED.calculated_at,
    source = EXCLUDED.source,
    note = EXCLUDED.note,
    updated_at = NOW();

