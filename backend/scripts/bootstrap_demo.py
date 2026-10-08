"""Initialize an empty hosted demo database and seed its sample accounts."""
import asyncio
import os
from pathlib import Path

import asyncpg


async def main():
    database_url = os.environ["DATABASE_URL"]
    conn = None
    for attempt in range(30):
        try:
            conn = await asyncpg.connect(database_url, timeout=5)
            break
        except (OSError, asyncpg.PostgresError):
            if attempt == 29:
                raise
            await asyncio.sleep(2)

    try:
        schema = await conn.fetchval("SELECT to_regclass('public.users')")
        if schema is None:
            migrations = Path(__file__).resolve().parents[1] / "migrations"
            async with conn.transaction():
                for migration in sorted(migrations.glob("*.sql")):
                    await conn.execute(migration.read_text())
            print("Initialized demo database schema.")
        else:
            print("Demo database schema already exists; keeping its data.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
