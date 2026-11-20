from __future__ import annotations

import asyncio
import os
import re

import psycopg
from psycopg import sql
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver


async def migrate() -> None:
    dsn = os.environ["LANGGRAPH_DATABASE_URL"]
    api_role = os.environ["METADATA_API_ROLE"]
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_role):
        raise ValueError("METADATA_API_ROLE must be a PostgreSQL identifier")

    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
        await saver.setup()

    tables = (
        "checkpoint_migrations",
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
    )
    with psycopg.connect(dsn, autocommit=True) as connection, connection.cursor() as cursor:
        for table in tables:
            cursor.execute(
                sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(
                    sql.Identifier(table), sql.Identifier(api_role)
                )
            )


if __name__ == "__main__":
    asyncio.run(migrate())
