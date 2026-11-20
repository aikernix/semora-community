"""Summarize synthetic completed orders using an in-memory SQLite database."""

import sqlite3
from contextlib import closing

ORDERS = [
    (1, "Online", 12900, "completed"),
    (2, "Online", 8900, "completed"),
    (3, "Retail", 14900, "completed"),
    (4, "Partner", 7500, "completed"),
    (5, "Retail", 16000, "cancelled"),
]

QUERY = """
SELECT channel, SUM(amount_cents) AS revenue_cents, COUNT(*) AS order_count
FROM orders
WHERE status = ?
GROUP BY channel
ORDER BY revenue_cents DESC, channel
"""


def main():
    with closing(sqlite3.connect(":memory:")) as database:
        database.execute(
            """CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                channel TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                status TEXT NOT NULL
            )"""
        )
        database.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", ORDERS)
        print(f"{'Channel':<12} {'Revenue':>10} {'Orders':>8}")
        for channel, cents, count in database.execute(QUERY, ("completed",)):
            print(f"{channel:<12} {cents // 100:>7}.{cents % 100:02d} {count:>8}")


if __name__ == "__main__":
    main()
