"""Раннер миграций без фреймворка: пронумерованные .sql-файлы + таблица учёта.

Никакого Alembic/ORM-магии сверху (CLAUDE.md §7) — миграция это просто SQL,
который выполняется один раз и запоминается в ``schema_migrations``. Порядок
применения — по имени файла (``0001_...``, ``0002_...``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from psycopg import Connection

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    name TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


@dataclass(frozen=True)
class Migration:
    name: str
    sql: str


def discover_migrations(migrations_dir: Path = MIGRATIONS_DIR) -> list[Migration]:
    return [
        Migration(name=path.stem, sql=path.read_text())
        for path in sorted(migrations_dir.glob("*.sql"))
    ]


def applied_migrations(conn: Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(_BOOTSTRAP_SQL)
        cur.execute("SELECT name FROM schema_migrations")
        return {row[0] for row in cur.fetchall()}


def apply_migrations(
    conn: Connection, migrations_dir: Path = MIGRATIONS_DIR
) -> list[str]:
    """Применяет ещё не применённые миграции по порядку. Возвращает их имена.

    Повторный вызов на уже мигрированной БД — no-op (воспроизводимость,
    требуемая DoD фазы 0).
    """
    already = applied_migrations(conn)
    newly_applied: list[str] = []
    for migration in discover_migrations(migrations_dir):
        if migration.name in already:
            continue
        with conn.cursor() as cur:
            cur.execute(migration.sql)
            cur.execute(
                "INSERT INTO schema_migrations (name) VALUES (%s)",
                (migration.name,),
            )
        newly_applied.append(migration.name)
    return newly_applied


if __name__ == "__main__":
    from billing.infrastructure.db.connection import new_connection

    with new_connection() as connection:
        applied = apply_migrations(connection)
    if applied:
        print("Applied:", ", ".join(applied))
    else:
        print("Nothing to apply — already up to date.")
