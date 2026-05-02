from pathlib import Path

import psycopg

from whatsapp_task_agent.settings import settings


def main() -> None:
    if not settings.database_url:
        raise SystemExit("DATABASE_URL is not set")

    migrations_dir = Path(__file__).resolve().parents[1] / "sql" / "migrations"
    migration_paths = sorted(migrations_dir.glob("*.sql"))

    with psycopg.connect(settings.database_url) as conn:
        for path in migration_paths:
            conn.execute(path.read_text(encoding="utf-8"))
            print(f"applied {path.name}")
        conn.commit()


if __name__ == "__main__":
    main()
