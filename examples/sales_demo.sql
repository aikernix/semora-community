-- Synthetic data for the Semora local quickstart.
BEGIN;

CREATE SCHEMA IF NOT EXISTS semora_sales_demo;

CREATE TABLE IF NOT EXISTS semora_sales_demo.orders (
    order_id integer PRIMARY KEY,
    channel text NOT NULL,
    order_date date NOT NULL,
    status text NOT NULL,
    paid_amount numeric(12, 2) NOT NULL
);

COMMENT ON TABLE semora_sales_demo.orders IS 'Synthetic sales orders for the Semora quickstart';
COMMENT ON COLUMN semora_sales_demo.orders.channel IS 'Sales channel';
COMMENT ON COLUMN semora_sales_demo.orders.paid_amount IS 'Amount paid in CNY';

INSERT INTO semora_sales_demo.orders (order_id, channel, order_date, status, paid_amount) VALUES
    (1, 'Online',  DATE '2026-10-03', 'completed', 129.00),
    (2, 'Online',  DATE '2026-10-13', 'completed', 89.00),
    (3, 'Retail',  DATE '2026-10-15', 'completed', 149.00),
    (4, 'Partner', DATE '2026-10-29', 'completed', 75.00),
    (5, 'Retail',  DATE '2026-10-16', 'cancelled', 160.00),
    (6, 'Online',  DATE '2026-11-01', 'completed', 100.00)
ON CONFLICT (order_id) DO NOTHING;

GRANT USAGE ON SCHEMA semora_sales_demo TO semora_reader;
GRANT SELECT ON TABLE semora_sales_demo.orders TO semora_reader;

COMMIT;
