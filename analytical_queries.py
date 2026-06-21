from __future__ import annotations
from datetime import date, time
import argparse
from datetime import date
from typing import Sequence

import pandas as pd
from sqlalchemy import Engine, bindparam, create_engine, inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError

from config import DB_DATABASE, DB_HOST, DB_PASSWORD, DB_PORT, DB_USERNAME

REQUIRED_VIEWS = {
    "vw_daily_airport_traffic",
    "vw_hourly_airport_traffic",
    "vw_route_popularity",
    "vw_flight_details",
}


def create_engine_for_database() -> Engine:
    url = URL.create(
        drivername="postgresql+psycopg2",
        username=DB_USERNAME,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
        database=DB_DATABASE,
    )
    return create_engine(url, pool_pre_ping=True)


def validate_views(engine: Engine) -> None:
    existing = set(inspect(engine).get_view_names())
    missing = sorted(REQUIRED_VIEWS - existing)
    if missing:
        raise RuntimeError(
            "Brakuje widoków: "
            + ", ".join(missing)
            + ". Uruchom wcześniej create_opensky_views.py."
        )


def normalize_date(value: str | date | None, name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise ValueError(f"{name} musi mieć format YYYY-MM-DD.") from error


def normalize_airports(
    airports: Sequence[str] | None,
) -> list[str] | None:
    if not airports:
        return None

    result: list[str] = []
    for airport in airports:
        code = airport.strip().upper()
        if len(code) != 4 or not code.isalnum():
            raise ValueError(f"Nieprawidłowy kod ICAO: {airport}")
        if code not in result:
            result.append(code)
    return result


def build_filters(
    date_column: str,
    airport_column: str,
    start_date: str | date | None,
    end_date: str | date | None,
    airports: Sequence[str] | None,
) -> tuple[str, dict]:
    start = normalize_date(start_date, "start_date")
    end = normalize_date(end_date, "end_date")
    selected_airports = normalize_airports(airports)

    conditions: list[str] = []
    params: dict = {}

    if start:
        conditions.append(f"{date_column} >= :start_date")
        params["start_date"] = start

    if end:
        conditions.append(f"{date_column} <= :end_date")
        params["end_date"] = end

    if selected_airports:
        conditions.append(f"{airport_column} IN :airport_codes")
        params["airport_codes"] = selected_airports

    where_sql = "WHERE " + " AND ".join(conditions) if conditions else ""
    return where_sql, params


def read_dataframe(
    engine: Engine,
    sql: str,
    params: dict,
) -> pd.DataFrame:
    statement = text(sql)
    if "airport_codes" in params:
        statement = statement.bindparams(
            bindparam("airport_codes", expanding=True)
        )

    with engine.connect() as connection:
        return pd.read_sql_query(statement, connection, params=params)


def traffic_by_airport(
    engine: Engine,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    airports: Sequence[str] | None = None,
) -> pd.DataFrame:
    where_sql, params = build_filters(
        "traffic_date",
        "airport_code",
        start_date,
        end_date,
        airports,
    )

    sql = f"""
        SELECT
            airport_code,
            MAX(airport_name) AS airport_name,
            SUM(arrival_count) AS arrivals,
            SUM(departure_count) AS departures,
            SUM(total_operations) AS total_operations
        FROM vw_daily_airport_traffic
        {where_sql}
        GROUP BY airport_code
        ORDER BY total_operations DESC, airport_code
    """
    return read_dataframe(engine, sql, params)


def busiest_hours(
    engine: Engine,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    airports: Sequence[str] | None = None,
) -> pd.DataFrame:
    where_sql, params = build_filters(
        "traffic_date",
        "airport_code",
        start_date,
        end_date,
        airports,
    )

    sql = f"""
        SELECT
            airport_code,
            MAX(airport_name) AS airport_name,
            traffic_hour,
            SUM(arrival_count) AS arrivals,
            SUM(departure_count) AS departures,
            SUM(total_operations) AS total_operations
        FROM vw_hourly_airport_traffic
        {where_sql}
        GROUP BY airport_code, traffic_hour
        ORDER BY total_operations DESC, airport_code, traffic_hour
    """
    return read_dataframe(engine, sql, params)


def popular_routes(
    engine: Engine,
    airports: Sequence[str] | None = None,
    limit: int = 20,
) -> pd.DataFrame:
    if limit <= 0:
        raise ValueError("limit musi być większy od zera.")

    selected_airports = normalize_airports(airports)
    params: dict = {"limit": limit}
    where_sql = ""

    if selected_airports:
        where_sql = """
            WHERE departure_airport_code IN :airport_codes
               OR arrival_airport_code IN :airport_codes
        """
        params["airport_codes"] = selected_airports

    sql = f"""
        SELECT
            departure_airport_code,
            departure_airport_name,
            arrival_airport_code,
            arrival_airport_name,
            flight_count,
            unique_aircraft,
            average_duration_minutes
        FROM vw_route_popularity
        {where_sql}
        ORDER BY flight_count DESC
        LIMIT :limit
    """
    return read_dataframe(engine, sql, params)


def weekend_vs_weekday(
    engine: Engine,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    airports: Sequence[str] | None = None,
) -> pd.DataFrame:
    where_sql, params = build_filters(
        "event_date_local",
        "monitored_airport_code",
        start_date,
        end_date,
        airports,
    )

    sql = f"""
        SELECT
            monitored_airport_code AS airport_code,
            MAX(monitored_airport_name) AS airport_name,
            CASE
                WHEN is_weekend THEN 'Weekend'
                ELSE 'Dzień roboczy'
            END AS day_type,
            COUNT(*) AS operation_count,
            COUNT(*) FILTER (
                WHERE event_type_code = 'ARRIVAL'
            ) AS arrivals,
            COUNT(*) FILTER (
                WHERE event_type_code = 'DEPARTURE'
            ) AS departures,
            COUNT(DISTINCT aircraft_id) AS unique_aircraft
        FROM vw_flight_details
        {where_sql}
        GROUP BY monitored_airport_code, is_weekend
        ORDER BY monitored_airport_code, is_weekend
    """
    return read_dataframe(engine, sql, params)


def data_quality(
    engine: Engine,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    airports: Sequence[str] | None = None,
) -> pd.DataFrame:
    where_sql, params = build_filters(
        "event_date_local",
        "monitored_airport_code",
        start_date,
        end_date,
        airports,
    )

    sql = f"""
        SELECT
            monitored_airport_code AS airport_code,
            MAX(monitored_airport_name) AS airport_name,
            COUNT(*) AS all_records,
            COUNT(*) FILTER (
                WHERE callsign IS NULL
            ) AS missing_callsign,
            COUNT(*) FILTER (
                WHERE departure_airport_code IS NULL
            ) AS missing_departure_airport,
            COUNT(*) FILTER (
                WHERE arrival_airport_code IS NULL
            ) AS missing_arrival_airport,
            COUNT(*) FILTER (
                WHERE is_complete = FALSE
            ) AS incomplete_records,
            ROUND(
                (
                    100.0 * COUNT(*) FILTER (
                        WHERE is_complete = FALSE
                    )
                    / NULLIF(COUNT(*), 0)
                )::NUMERIC,
                2
            ) AS incomplete_percentage
        FROM vw_flight_details
        {where_sql}
        GROUP BY monitored_airport_code
        ORDER BY incomplete_percentage DESC, monitored_airport_code
    """
    return read_dataframe(engine, sql, params)


def display_dataframe(title: str, dataframe: pd.DataFrame) -> None:
    print()
    print("=" * 90)
    print(title)
    print("=" * 90)

    if dataframe.empty:
        print("Brak wyników.")
        return

    try:
        from IPython.display import display
        display(dataframe)
    except ImportError:
        print(dataframe.to_string(index=False))


def run_all_queries(
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    airports: Sequence[str] | None = None,
    limit: int = 20,
    display: bool = True,
    engine: Engine | None = None,
) -> dict[str, pd.DataFrame]:
    owns_engine = engine is None
    selected_engine = engine or create_engine_for_database()

    try:
        validate_views(selected_engine)

        results = {
            "traffic_by_airport": traffic_by_airport(
                selected_engine, start_date, end_date, airports
            ),
            "busiest_hours": busiest_hours(
                selected_engine, start_date, end_date, airports
            ),
            "popular_routes": popular_routes(
                selected_engine, airports, limit
            ),
            "weekend_vs_weekday": weekend_vs_weekday(
                selected_engine, start_date, end_date, airports
            ),
            "data_quality": data_quality(
                selected_engine, start_date, end_date, airports
            ),
        }

        if display:
            titles = {
                "traffic_by_airport": "1. Ruch według lotniska",
                "busiest_hours": "2. Najbardziej ruchliwe godziny",
                "popular_routes": "3. Najpopularniejsze trasy",
                "weekend_vs_weekday": "4. Weekendy i dni robocze",
                "data_quality": "5. Jakość danych",
            }
            for name, dataframe in results.items():
                display_dataframe(titles[name], dataframe)

        return results

    finally:
        if owns_engine:
            selected_engine.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Uruchamia pięć kwerend analitycznych OpenSky."
    )
    parser.add_argument("--start-date", help="Data YYYY-MM-DD.")
    parser.add_argument("--end-date", help="Data YYYY-MM-DD.")
    parser.add_argument(
        "--airports",
        nargs="+",
        help="Kody ICAO, np. EPWA EPKK EPGD.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Limit rankingu tras.",
    )
    return parser

def arrival_by_airport(
    engine: Engine,
    airport: str,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
) -> pd.DataFrame:
    where_sql, params = build_filters(
        "event_date_local",
        "monitored_airport_code",
        start_date,
        end_date,
        [airport],
    )
    domestic_filter = "AND departure_airport_code LIKE 'EP%'"
    query = f"""
        SELECT
            callsign AS "Numer lotu",
            event_time_utc AS "Godzina przylotu",
            departure_airport_name as "Z lotniska: "
        FROM vw_flight_details
        {where_sql}
        AND event_type_code = 'ARRIVAL'
        {domestic_filter}
        ORDER BY event_time_utc DESC
    """
    dataframe = read_dataframe(engine, query, params)
    dataframe["Z lotniska: "] = dataframe["Z lotniska: "].fillna("Nieznane lotnisko")

    dataframe["Godzina przylotu"] = pd.to_datetime(dataframe["Godzina przylotu"])
    dataframe.insert(
        1,
        "Data przylotu",
        dataframe["Godzina przylotu"].dt.strftime("%Y-%m-%d"),
    )
    dataframe["Godzina przylotu"] = dataframe["Godzina przylotu"].dt.strftime(
        "%H:%M:%S"
    )

    return dataframe

def departure_by_airport(
    engine: Engine,
    airport: str,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
) -> pd.DataFrame:
    where_sql, params = build_filters(
        "event_date_local",
        "monitored_airport_code",
        start_date,
        end_date,
        [airport],
    )

    domestic_filter = "AND arrival_airport_code LIKE 'EP%'"

    query = f"""
        SELECT
            callsign AS "Numer lotu",
            event_time_utc AS "Godzina wylotu",
            arrival_airport_name as "Do lotniska: "
        FROM vw_flight_details
        {where_sql}
        AND event_type_code = 'DEPARTURE'
        {domestic_filter}
        ORDER BY event_time_utc DESC
    """
    
    dataframe = read_dataframe(engine, query, params)
    dataframe["Do lotniska: "] = dataframe["Do lotniska: "].fillna("Nieznane lotnisko")

    dataframe["Godzina wylotu"] = pd.to_datetime(dataframe["Godzina wylotu"])
    dataframe.insert(
        1,
        "Data wylotu",
        dataframe["Godzina wylotu"].dt.strftime("%Y-%m-%d"),
    )
    dataframe["Godzina wylotu"] = dataframe["Godzina wylotu"].dt.strftime(
        "%H:%M:%S"
    )

    return dataframe


def list_monitored_airports(engine: Engine) -> pd.DataFrame:
    
    query = """
        SELECT DISTINCT
            monitored_airport_code,
            monitored_airport_name
        FROM vw_flight_details
        ORDER BY monitored_airport_name
    """
    return read_dataframe(engine, query, {})

def normalize_time(value: str | None, name: str) -> str | None:
    if not value:
        return None
    try:
        return time.fromisoformat(value).isoformat()
    except ValueError as error:
        raise ValueError(f"{name} musi mieć format HH:MM lub HH:MM:SS.") from error
    
def build_flight_search_filters(
    departure_airport: str | None = None,
    arrival_airport: str | None = None,
    callsign: str | None = None,
    monitored_airport: str | None = None,
    event_type: str | None = None,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
) -> tuple[str, dict]:
    conditions: list[str] = []
    params: dict = {}

    if departure_airport:
        conditions.append("departure_airport_code ILIKE :departure_airport")
        params["departure_airport"] = f"%{departure_airport.strip()}%"

    if arrival_airport:
        conditions.append("arrival_airport_code ILIKE :arrival_airport")
        params["arrival_airport"] = f"%{arrival_airport.strip()}%"

    if callsign:
        conditions.append("callsign ILIKE :callsign")
        params["callsign"] = f"%{callsign.strip()}%"

    if monitored_airport:
        conditions.append("monitored_airport_code = :monitored_airport")
        params["monitored_airport"] = monitored_airport.strip().upper()

    if event_type:
        conditions.append("event_type_code = :event_type")
        params["event_type"] = event_type

    start_date = normalize_date(start_date, "start_date")
    end_date = normalize_date(end_date, "end_date")
    if start_date:
        conditions.append("event_date_local >= :start_date")
        params["start_date"] = start_date
    if end_date:
        conditions.append("event_date_local <= :end_date")
        params["end_date"] = end_date

    start_time = normalize_time(start_time, "start_time")
    end_time = normalize_time(end_time, "end_time")
    if start_time:
        conditions.append("event_time_utc::time >= :start_time")
        params["start_time"] = start_time
    if end_time:
        conditions.append("event_time_utc::time <= :end_time")
        params["end_time"] = end_time

    where_sql = "WHERE " + " AND ".join(conditions) if conditions else ""
    return where_sql, params

def search_flights(
    engine: Engine,
    departure_airport: str | None = None,
    arrival_airport: str | None = None,
    callsign: str | None = None,
    monitored_airport: str | None = None,
    event_type: str | None = None,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    limit: int = 300,
) -> pd.DataFrame:
    where_sql, params = build_flight_search_filters(
        departure_airport=departure_airport,
        arrival_airport=arrival_airport,
        callsign=callsign,
        monitored_airport=monitored_airport,
        event_type=event_type,
        start_date=start_date,
        end_date=end_date,
        start_time=start_time,
        end_time=end_time,
    )

    params["limit"] = limit

    sql = f"""
        SELECT
            callsign AS "Numer lotu",
            event_type_code AS "Typ operacji",
            event_time_utc AS "Czas UTC",
            monitored_airport_name AS "Lotnisko monitorowane",
            departure_airport_code AS "Kod lotniska wylotu",
            departure_airport_name AS "Lotnisko wylotu",
            arrival_airport_code AS "Kod lotniska przylotu",
            arrival_airport_name AS "Lotnisko przylotu",
            aircraft_id AS "Aircraft ID",
            is_complete AS "Kompletny"
        FROM vw_flight_details
        {where_sql}
        ORDER BY event_time_utc DESC
        LIMIT :limit
    """
    dataframe = read_dataframe(engine, sql, params)

    dataframe["Czas UTC"] = pd.to_datetime(dataframe["Czas UTC"])
    dataframe.insert(1, "Data", dataframe["Czas UTC"].dt.strftime("%Y-%m-%d"))
    dataframe["Czas UTC"] = dataframe["Czas UTC"].dt.strftime("%H:%M:%S")

    return dataframe


def main() -> int:
    args = build_parser().parse_args()

    try:
        run_all_queries(
            start_date=args.start_date,
            end_date=args.end_date,
            airports=args.airports,
            limit=args.limit,
            display=True,
        )
        return 0
    except (SQLAlchemyError, RuntimeError, ValueError) as error:
        print()
        print("[BŁĄD] Nie udało się wykonać kwerend.")
        print(f"{type(error).__name__}: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
