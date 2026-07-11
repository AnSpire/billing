"""Реализация порта ``ReferenceParameterRepository`` (domain) поверх обычного
SQL (psycopg3) — единственный сейчас адаптер к единственной СУБД проекта.

Никакой ORM (CLAUDE.md §7). Транзакцию открывает и коммитит вызывающий код
(см. паттерн ``new_connection`` / ``db_connection`` в тестах) — репозиторий
только выполняет запросы на переданном ``Connection``.
"""

from __future__ import annotations

from datetime import datetime

import psycopg.errors
from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg.types.range import Range

from billing.domain.reference_parameter import (
    OverlappingValidTimeError,
    ParameterValue,
    ParameterValueVersion,
    Provenance,
    ReferenceParameter,
    ReferenceParameterNotFoundError,
    ReferenceParameterRepository,
    TemporalValidity,
)

_SELECT_COLUMNS = """
    version_id, key, jurisdiction, value, valid_range, tx_range,
    provenance_regulation_ref, provenance_document_id, provenance_effective_date
"""


class PostgresReferenceParameterRepository(ReferenceParameterRepository):
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def register_value(
        self,
        key: str,
        jurisdiction: str,
        value: ParameterValue,
        validity: TemporalValidity,
        provenance: Provenance,
        *,
        now: datetime,
    ) -> ParameterValueVersion:
        aggregate = ReferenceParameter(key=key, jurisdiction=jurisdiction)
        version, _event = aggregate.register_value(value, validity, provenance, now=now)
        self._insert(version)
        return version

    def correct(
        self,
        key: str,
        jurisdiction: str,
        value: ParameterValue,
        validity: TemporalValidity,
        provenance: Provenance,
        *,
        now: datetime,
    ) -> ParameterValueVersion:
        aggregate = ReferenceParameter(key=key, jurisdiction=jurisdiction)
        superseded = self._find_actual_overlapping(key, jurisdiction, validity)
        version, _event = aggregate.correct(
            value, validity, provenance, now=now, superseded=superseded
        )
        self._close(superseded, tx_to=now)
        self._insert(version)
        return version

    def repeal(
        self,
        key: str,
        jurisdiction: str,
        repeal_from: datetime,
        provenance: Provenance,
        *,
        now: datetime,
    ) -> ParameterValueVersion:
        aggregate = ReferenceParameter(key=key, jurisdiction=jurisdiction)
        target = self._find_actual_covering(key, jurisdiction, repeal_from)
        if target is None:
            raise ReferenceParameterNotFoundError(
                f"no actual version of ({key!r}, {jurisdiction!r}) covers {repeal_from!r}"
            )
        version, _event = aggregate.repeal(repeal_from, provenance, now=now, target=target)
        self._close([target], tx_to=now)
        self._insert(version)
        return version

    def resolve(
        self, key: str, jurisdiction: str, valid_on: datetime, as_of_tx: datetime
    ) -> ParameterValueVersion | None:
        """``resolve(key, jurisdiction, valid_on, as_of_tx) -> версия``
        (billing_aggregates.md, «Резолвинг референсных параметров»): что
        система считала правдой в момент ``as_of_tx`` о моменте ``valid_on``.
        """
        row = self._conn.execute(
            f"""
            SELECT {_SELECT_COLUMNS}
            FROM reference_parameter_version
            WHERE key = %s AND jurisdiction = %s
              AND valid_range @> %s
              AND tx_range @> %s
            """,
            (key, jurisdiction, valid_on, as_of_tx),
        ).fetchone()
        return self._row_to_version(row) if row else None

    def _find_actual_overlapping(
        self, key: str, jurisdiction: str, validity: TemporalValidity
    ) -> list[ParameterValueVersion]:
        new_range = Range(validity.valid_from, validity.valid_to, bounds="[)")
        rows = self._conn.execute(
            f"""
            SELECT {_SELECT_COLUMNS}
            FROM reference_parameter_version
            WHERE key = %s AND jurisdiction = %s
              AND upper(tx_range) IS NULL
              AND valid_range && %s
            """,
            (key, jurisdiction, new_range),
        ).fetchall()
        return [self._row_to_version(row) for row in rows]

    def _find_actual_covering(
        self, key: str, jurisdiction: str, point: datetime
    ) -> ParameterValueVersion | None:
        row = self._conn.execute(
            f"""
            SELECT {_SELECT_COLUMNS}
            FROM reference_parameter_version
            WHERE key = %s AND jurisdiction = %s
              AND upper(tx_range) IS NULL
              AND valid_range @> %s
            """,
            (key, jurisdiction, point),
        ).fetchone()
        return self._row_to_version(row) if row else None

    def _insert(self, version: ParameterValueVersion) -> None:
        try:
            self._conn.execute(
                """
                INSERT INTO reference_parameter_version (
                    version_id, key, jurisdiction, value, valid_range, tx_range,
                    provenance_regulation_ref, provenance_document_id,
                    provenance_effective_date
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    version.version_id,
                    version.key,
                    version.jurisdiction,
                    Jsonb({"kind": version.value.kind, "payload": dict(version.value.payload)}),
                    Range(version.validity.valid_from, version.validity.valid_to, bounds="[)"),
                    Range(version.tx_from, version.tx_to, bounds="[)"),
                    version.provenance.regulation_ref,
                    version.provenance.document_id,
                    version.provenance.effective_date,
                ),
            )
        except psycopg.errors.ExclusionViolation as exc:
            raise OverlappingValidTimeError(
                f"({version.key!r}, {version.jurisdiction!r}) has an actual version "
                "whose valid-time overlaps the one being written"
            ) from exc

    def _close(self, versions: list[ParameterValueVersion], *, tx_to: datetime) -> None:
        for version in versions:
            self._conn.execute(
                """
                UPDATE reference_parameter_version
                SET tx_range = tstzrange(lower(tx_range), %s, '[)')
                WHERE version_id = %s
                """,
                (tx_to, version.version_id),
            )

    @staticmethod
    def _row_to_version(row: tuple) -> ParameterValueVersion:
        (
            version_id,
            key,
            jurisdiction,
            value_json,
            valid_range,
            tx_range,
            regulation_ref,
            document_id,
            effective_date,
        ) = row
        return ParameterValueVersion(
            version_id=version_id,
            key=key,
            jurisdiction=jurisdiction,
            value=ParameterValue(kind=value_json["kind"], payload=value_json["payload"]),
            validity=TemporalValidity(valid_from=valid_range.lower, valid_to=valid_range.upper),
            tx_from=tx_range.lower,
            tx_to=tx_range.upper,
            provenance=Provenance(
                regulation_ref=regulation_ref,
                document_id=document_id,
                effective_date=effective_date,
            ),
        )
