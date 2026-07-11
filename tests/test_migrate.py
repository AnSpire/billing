"""DoD фазы 0: миграции воспроизводимы (повторный прогон — no-op, порядок по имени файла)."""

from __future__ import annotations

from pathlib import Path

from billing.infrastructure.db import migrate


def test_discover_migrations_orders_by_filename(tmp_path: Path) -> None:
    (tmp_path / "0002_second.sql").write_text("SELECT 1;")
    (tmp_path / "0001_first.sql").write_text("SELECT 1;")

    names = [m.name for m in migrate.discover_migrations(tmp_path)]

    assert names == ["0001_first", "0002_second"]


def test_apply_migrations_is_idempotent(tmp_path: Path, db_connection) -> None:
    (tmp_path / "0001_create_probe_table.sql").write_text(
        "CREATE TABLE phase0_probe (id serial primary key);"
    )

    first_run = migrate.apply_migrations(db_connection, tmp_path)
    assert first_run == ["0001_create_probe_table"]

    second_run = migrate.apply_migrations(db_connection, tmp_path)
    assert second_run == [], "повторный прогон не должен переигрывать применённую миграцию"

    with db_connection.cursor() as cur:
        cur.execute("SELECT to_regclass('phase0_probe')")
        assert cur.fetchone()[0] == "phase0_probe"
