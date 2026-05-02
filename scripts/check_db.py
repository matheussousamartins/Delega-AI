import psycopg

from whatsapp_task_agent.settings import settings


def main() -> None:
    if not settings.database_url:
        raise SystemExit("DATABASE_URL is not set")

    with psycopg.connect(settings.database_url) as conn:
        tables = conn.execute(
            """
            select table_name
            from information_schema.tables
            where table_schema = 'public'
            order by table_name
            """
        ).fetchall()
        counts = conn.execute(
            """
            select
                (select count(*) from companies) as companies,
                (select count(*) from users) as users,
                (select count(*) from company_members) as company_members
            """
        ).fetchone()

    print("tables:", ", ".join(row[0] for row in tables))
    print("counts companies/users/members:", counts)


if __name__ == "__main__":
    main()
