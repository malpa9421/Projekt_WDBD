from __future__ import annotations

"""
Tworzenie widoków analitycznych dla bazy opensky_airports.

Uruchomienie:
    python create_opensky_views.py

Użycie jako moduł:
    from create_opensky_views import create_views

    create_views()
"""

from typing import Iterable

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import URL

from config import DB_DATABASE, DB_HOST, DB_PASSWORD, DB_PORT, DB_USERNAME


REQUIRED_TABLES = {
    "airport",
    "event_type",
    "import_log",
    "aircraft",
    "flight",
    "airport_event",
}


VIEW_DEFINITIONS: dict[str, str] = {
    "vw_daily_airport_traffic": """
        CREATE OR REPLACE VIEW vw_daily_airport_traffic AS
        SELECT
            a.airport_id,
            a.icao_code AS airport_code,
            a.airport_name,
            (
                ae.event_time_utc
                AT TIME ZONE 'Europe/Warsaw'
            )::DATE AS traffic_date,
            COUNT(*) FILTER (
                WHERE et.event_type_code = 'ARRIVAL'
            ) AS arrival_count,
            COUNT(*) FILTER (
                WHERE et.event_type_code = 'DEPARTURE'
            ) AS departure_count,
            COUNT(*) AS total_operations,
            COUNT(DISTINCT f.aircraft_id) AS unique_aircraft
        FROM airport_event ae
        JOIN airport a
            ON a.airport_id = ae.airport_id
        JOIN event_type et
            ON et.event_type_id = ae.event_type_id
        JOIN flight f
            ON f.flight_id = ae.flight_id
        GROUP BY
            a.airport_id,
            a.icao_code,
            a.airport_name,
            (
                ae.event_time_utc
                AT TIME ZONE 'Europe/Warsaw'
            )::DATE;
    """,

    "vw_hourly_airport_traffic": """
        CREATE OR REPLACE VIEW vw_hourly_airport_traffic AS
        SELECT
            a.airport_id,
            a.icao_code AS airport_code,
            a.airport_name,
            (
                ae.event_time_utc
                AT TIME ZONE 'Europe/Warsaw'
            )::DATE AS traffic_date,
            EXTRACT(
                HOUR FROM (
                    ae.event_time_utc
                    AT TIME ZONE 'Europe/Warsaw'
                )
            )::INTEGER AS traffic_hour,
            COUNT(*) FILTER (
                WHERE et.event_type_code = 'ARRIVAL'
            ) AS arrival_count,
            COUNT(*) FILTER (
                WHERE et.event_type_code = 'DEPARTURE'
            ) AS departure_count,
            COUNT(*) AS total_operations,
            COUNT(DISTINCT f.aircraft_id) AS unique_aircraft
        FROM airport_event ae
        JOIN airport a
            ON a.airport_id = ae.airport_id
        JOIN event_type et
            ON et.event_type_id = ae.event_type_id
        JOIN flight f
            ON f.flight_id = ae.flight_id
        GROUP BY
            a.airport_id,
            a.icao_code,
            a.airport_name,
            (
                ae.event_time_utc
                AT TIME ZONE 'Europe/Warsaw'
            )::DATE,
            EXTRACT(
                HOUR FROM (
                    ae.event_time_utc
                    AT TIME ZONE 'Europe/Warsaw'
                )
            );
    """,

    "vw_route_popularity": """
        CREATE OR REPLACE VIEW vw_route_popularity AS
        SELECT
            departure.airport_id AS departure_airport_id,
            departure.icao_code AS departure_airport_code,
            departure.airport_name AS departure_airport_name,
            arrival.airport_id AS arrival_airport_id,
            arrival.icao_code AS arrival_airport_code,
            arrival.airport_name AS arrival_airport_name,
            COUNT(*) AS flight_count,
            COUNT(DISTINCT f.aircraft_id) AS unique_aircraft,
            ROUND(
                AVG(
                    EXTRACT(
                        EPOCH FROM (
                            f.last_seen_utc - f.first_seen_utc
                        )
                    ) / 60.0
                )::NUMERIC,
                1
            ) AS average_duration_minutes,
            MIN(f.first_seen_utc) AS first_observed_flight,
            MAX(f.first_seen_utc) AS last_observed_flight
        FROM flight f
        JOIN airport departure
            ON departure.airport_id =
               f.estimated_departure_airport_id
        JOIN airport arrival
            ON arrival.airport_id =
               f.estimated_arrival_airport_id
        GROUP BY
            departure.airport_id,
            departure.icao_code,
            departure.airport_name,
            arrival.airport_id,
            arrival.icao_code,
            arrival.airport_name;
    """,

    "vw_flight_details": """
        CREATE OR REPLACE VIEW vw_flight_details AS
        SELECT
            f.flight_id,
            ac.aircraft_id,
            ac.icao24,
            f.callsign,
            f.first_seen_utc,
            f.last_seen_utc,
            ROUND(
                (
                    EXTRACT(
                        EPOCH FROM (
                            f.last_seen_utc - f.first_seen_utc
                        )
                    ) / 60.0
                )::NUMERIC,
                1
            ) AS duration_minutes,
            departure.airport_id AS departure_airport_id,
            departure.icao_code AS departure_airport_code,
            departure.airport_name AS departure_airport_name,
            arrival.airport_id AS arrival_airport_id,
            arrival.icao_code AS arrival_airport_code,
            arrival.airport_name AS arrival_airport_name,
            monitored.airport_id AS monitored_airport_id,
            monitored.icao_code AS monitored_airport_code,
            monitored.airport_name AS monitored_airport_name,
            et.event_type_id,
            et.event_type_code,
            et.event_type_name,
            ae.airport_event_id,
            ae.event_time_utc,
            (
                ae.event_time_utc
                AT TIME ZONE 'Europe/Warsaw'
            ) AS event_time_local,
            (
                ae.event_time_utc
                AT TIME ZONE 'Europe/Warsaw'
            )::DATE AS event_date_local,
            EXTRACT(
                HOUR FROM (
                    ae.event_time_utc
                    AT TIME ZONE 'Europe/Warsaw'
                )
            )::INTEGER AS event_hour_local,
            EXTRACT(
                ISODOW FROM (
                    ae.event_time_utc
                    AT TIME ZONE 'Europe/Warsaw'
                )
            )::INTEGER AS weekday_number,
            CASE
                WHEN EXTRACT(
                    ISODOW FROM (
                        ae.event_time_utc
                        AT TIME ZONE 'Europe/Warsaw'
                    )
                ) IN (6, 7)
                THEN TRUE
                ELSE FALSE
            END AS is_weekend,
            f.departure_candidates_count,
            f.arrival_candidates_count,
            CASE
                WHEN f.callsign IS NOT NULL
                 AND f.estimated_departure_airport_id IS NOT NULL
                 AND f.estimated_arrival_airport_id IS NOT NULL
                THEN TRUE
                ELSE FALSE
            END AS is_complete,
            ae.import_id
        FROM airport_event ae
        JOIN flight f
            ON f.flight_id = ae.flight_id
        JOIN aircraft ac
            ON ac.aircraft_id = f.aircraft_id
        JOIN airport monitored
            ON monitored.airport_id = ae.airport_id
        JOIN event_type et
            ON et.event_type_id = ae.event_type_id
        LEFT JOIN airport departure
            ON departure.airport_id =
               f.estimated_departure_airport_id
        LEFT JOIN airport arrival
            ON arrival.airport_id =
               f.estimated_arrival_airport_id;
    """,

    "vw_import_summary": """
        CREATE OR REPLACE VIEW vw_import_summary AS
        SELECT
            il.import_id,
            a.icao_code AS airport_code,
            a.airport_name,
            et.event_type_code,
            et.event_type_name,
            il.endpoint,
            il.period_begin_utc,
            il.period_end_utc,
            il.started_at,
            il.finished_at,
            CASE
                WHEN il.finished_at IS NULL
                THEN NULL
                ELSE ROUND(
                    EXTRACT(
                        EPOCH FROM (
                            il.finished_at - il.started_at
                        )
                    )::NUMERIC,
                    2
                )
            END AS duration_seconds,
            il.http_status,
            il.records_received,
            il.records_inserted,
            il.records_skipped,
            il.credits_remaining,
            il.retry_count,
            il.status,
            il.error_message
        FROM import_log il
        JOIN airport a
            ON a.airport_id = il.airport_id
        JOIN event_type et
            ON et.event_type_id = il.event_type_id;
    """,
}


def create_database_url() -> URL:
    return URL.create(
        drivername="postgresql+psycopg2",
        username=DB_USERNAME,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
        database=DB_DATABASE,
    )


def create_database_engine() -> Engine:
    return create_engine(
        create_database_url(),
        pool_pre_ping=True,
        echo=False,
    )


def validate_required_tables(engine: Engine) -> None:
    existing_tables = set(inspect(engine).get_table_names())
    missing_tables = sorted(REQUIRED_TABLES - existing_tables)

    if missing_tables:
        raise RuntimeError(
            "Brakuje wymaganych tabel: " + ", ".join(missing_tables) + ". Najpierw uruchom init_opensky_database.py.")


def create_views(engine: Engine | None = None, *, view_names: Iterable[str] | None = None,) -> list[str]:
    """
    Tworzy lub aktualizuje widoki.

    Brak view_names oznacza utworzenie wszystkich widoków.
    """
    owns_engine = engine is None
    selected_engine = engine or create_database_engine()

    try:
        validate_required_tables(selected_engine)

        selected_names = (
            list(view_names)
            if view_names is not None
            else list(VIEW_DEFINITIONS.keys())
        )

        unknown_views = [
            name
            for name in selected_names
            if name not in VIEW_DEFINITIONS
        ]

        if unknown_views:
            raise ValueError(
                "Nieznane widoki: " + ", ".join(unknown_views)
            )

        created_views: list[str] = []

        with selected_engine.begin() as connection:
            for view_name in selected_names:
                connection.execute(
                    text(VIEW_DEFINITIONS[view_name])
                )
                created_views.append(view_name)

        return created_views

    finally:
        if owns_engine:
            selected_engine.dispose()


def list_existing_views(engine: Engine | None = None,) -> list[str]:
    owns_engine = engine is None
    selected_engine = engine or create_database_engine()

    try:
        with selected_engine.connect() as connection:
            return list(
                connection.execute(
                    text(
                        """
                        SELECT table_name
                        FROM information_schema.views
                        WHERE table_schema = 'public'
                        ORDER BY table_name
                        """
                    )
                ).scalars()
            )

    finally:
        if owns_engine:
            selected_engine.dispose()


def main() -> int:
    print("Tworzenie widoków analitycznych...")

    try:
        created_views = create_views()

        print()
        for view_name in created_views:
            print(f"[OK] {view_name}")

        print()
        print("=" * 60)
        print("Widoki zostały przygotowane.")
        print(f"Baza: {DB_DATABASE}")
        print(f"Serwer: {DB_HOST}:{DB_PORT}")
        print("Widoki w schemacie public:")

        for view_name in list_existing_views():
            print(f"  - {view_name}")

        print("=" * 60)
        return 0

    except Exception as error:
        print()
        print("[BŁĄD] Nie udało się utworzyć widoków.")
        print(f"Typ: {type(error).__name__}")
        print(f"Treść: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
