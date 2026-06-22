from __future__ import annotations

"""
Import historycznych i live lotów OpenSky do bazy opensky_airports.

Użycie z wiersza poleceń:
  python import_data_all.py --start-date 2026-06-01 --end-date 2026-06-07

Jako moduł:
  from import_data_all import import_flights
  summary = import_flights(start_date="2026-06-01", end_date="2026-06-07")
"""

import argparse
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import requests
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import URL

from config import DB_DATABASE, DB_HOST, DB_PASSWORD, DB_PORT, DB_USERNAME


# KONFIGURACJA

API_BASE_URL = "https://opensky-network.org/api"
TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/"
    "opensky-network/protocol/openid-connect/token"
)

FLIGHTS_ALL_ENDPOINT = "/flights/all"

MAX_WINDOW_HOURS = 2
WINDOW_SECONDS = MAX_WINDOW_HOURS * 3600

TOKEN_REFRESH_MARGIN_SECONDS = 30
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_REQUEST_DELAY_SECONDS = 0.5
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_RATE_LIMIT_WAIT_SECONDS = 600
DEFAULT_CREDIT_RESERVE = 0

ESTIMATED_CREDITS_PER_WINDOW = 30  

DEFAULT_CREDENTIALS_PATH = (
    Path(__file__).resolve().with_name("credentials.json")
)

SUPPORTED_OPERATIONS = ("ARRIVAL", "DEPARTURE")


# TYPY I WYJĄTKI

DateLike = date | datetime | str
ProgressCallback = Callable[[dict[str, Any]], None]


class ConfigurationError(RuntimeError):
    pass


class DatabaseSchemaError(RuntimeError):
    pass


class OpenSkyRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        credits_remaining: int | None = None,
        retry_count: int = 0,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.credits_remaining = credits_remaining
        self.retry_count = retry_count


@dataclass(slots=True)
class ApiResult:
    records: list[dict[str, Any]]
    status_code: int
    credits_remaining: int | None
    retry_count: int


@dataclass(slots=True)
class ImportSummary:
    start_date: str
    end_date: str
    planned_calls: int = 0
    api_calls: int = 0
    skipped_completed_calls: int = 0
    successful_calls: int = 0
    no_data_calls: int = 0
    failed_calls: int = 0
    records_received: int = 0
    records_inserted: int = 0
    records_skipped: int = 0
    estimated_credits: int = 0
    credits_remaining: int | None = None
    stopped_for_credit_reserve: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class NormalizedFlight:
    icao24: str
    callsign: str | None
    first_seen_utc: datetime
    last_seen_utc: datetime
    departure_airport_code: str | None
    arrival_airport_code: str | None
    departure_candidates_count: int | None
    arrival_candidates_count: int | None


@dataclass(slots=True)
class TimeWindow:
    """Jedno 2-godzinne okno czasowe do odpytania /flights/all."""
    begin_utc: datetime
    end_utc: datetime
    begin_timestamp: int
    end_timestamp: int

    @property
    def label(self) -> str:
        return (
            f"{self.begin_utc.strftime('%Y-%m-%d %H:%M')}"
            f"–{self.end_utc.strftime('%H:%M')} UTC"
        )


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

REQUIRED_TABLES = {
    "airport",
    "event_type",
    "import_log",
    "aircraft",
    "flight",
    "airport_event",
}

SQL_INSERT_IMPORT_LOG = text(
    """
    INSERT INTO import_log (
        airport_id,
        event_type_id,
        endpoint,
        period_begin_utc,
        period_end_utc,
        status
    )
    VALUES (
        :airport_id,
        :event_type_id,
        :endpoint,
        :period_begin_utc,
        :period_end_utc,
        'RUNNING'
    )
    RETURNING import_id
    """
)

SQL_FINISH_IMPORT_LOG = text(
    """
    UPDATE import_log
    SET
        finished_at    = CURRENT_TIMESTAMP,
        http_status    = :http_status,
        records_received = :records_received,
        records_inserted = :records_inserted,
        records_skipped  = :records_skipped,
        credits_remaining = :credits_remaining,
        retry_count    = :retry_count,
        status         = :status,
        error_message  = :error_message
    WHERE import_id = :import_id
    """
)


SQL_CHECK_WINDOW_COMPLETED = text(
    """
    SELECT COUNT(*) FROM import_log
    WHERE airport_id     = :airport_id
      AND event_type_id  = :event_type_id
      AND period_begin_utc = :period_begin_utc
      AND period_end_utc   = :period_end_utc
      AND status IN ('SUCCESS', 'NO_DATA')
    """
)

SQL_UPSERT_ROUTE_AIRPORT = text(
    """
    INSERT INTO airport (icao_code, airport_name, city, country_code, is_monitored)
    VALUES (:icao_code, NULL, NULL, NULL, FALSE)
    ON CONFLICT (icao_code) DO UPDATE SET icao_code = EXCLUDED.icao_code
    RETURNING airport_id
    """
)

SQL_UPSERT_AIRCRAFT = text(
    """
    INSERT INTO aircraft (icao24, first_observed_at, last_observed_at)
    VALUES (:icao24, :first_observed_at, :last_observed_at)
    ON CONFLICT (icao24) DO UPDATE SET
        first_observed_at = CASE
            WHEN aircraft.first_observed_at IS NULL THEN EXCLUDED.first_observed_at
            ELSE LEAST(aircraft.first_observed_at, EXCLUDED.first_observed_at)
        END,
        last_observed_at = CASE
            WHEN aircraft.last_observed_at IS NULL THEN EXCLUDED.last_observed_at
            ELSE GREATEST(aircraft.last_observed_at, EXCLUDED.last_observed_at)
        END,
        updated_at = CURRENT_TIMESTAMP
    RETURNING aircraft_id
    """
)

SQL_UPSERT_FLIGHT = text(
    """
    INSERT INTO flight (
        aircraft_id,
        callsign,
        first_seen_utc,
        last_seen_utc,
        estimated_departure_airport_id,
        estimated_arrival_airport_id,
        departure_candidates_count,
        arrival_candidates_count,
        first_import_id
    )
    VALUES (
        :aircraft_id,
        :callsign,
        :first_seen_utc,
        :last_seen_utc,
        :estimated_departure_airport_id,
        :estimated_arrival_airport_id,
        :departure_candidates_count,
        :arrival_candidates_count,
        :first_import_id
    )
    ON CONFLICT ON CONSTRAINT uq_flight_aircraft_time
    DO UPDATE SET
        callsign = COALESCE(EXCLUDED.callsign, flight.callsign),
        estimated_departure_airport_id = COALESCE(
            EXCLUDED.estimated_departure_airport_id,
            flight.estimated_departure_airport_id
        ),
        estimated_arrival_airport_id = COALESCE(
            EXCLUDED.estimated_arrival_airport_id,
            flight.estimated_arrival_airport_id
        ),
        departure_candidates_count = COALESCE(
            EXCLUDED.departure_candidates_count,
            flight.departure_candidates_count
        ),
        arrival_candidates_count = COALESCE(
            EXCLUDED.arrival_candidates_count,
            flight.arrival_candidates_count
        ),
        updated_at = CURRENT_TIMESTAMP
    RETURNING flight_id
    """
)

SQL_INSERT_AIRPORT_EVENT = text(
    """
    INSERT INTO airport_event (
        flight_id, airport_id, event_type_id, import_id, event_time_utc
    )
    VALUES (
        :flight_id, :airport_id, :event_type_id, :import_id, :event_time_utc
    )
    ON CONFLICT ON CONSTRAINT uq_airport_event DO NOTHING
    RETURNING airport_event_id
    """
)


# AUTH


def _read_credentials_file(credentials_path: str | os.PathLike[str]) -> tuple[str, str]:
    path = Path(credentials_path)
    if not path.exists():
        raise ConfigurationError(f"Nie znaleziono pliku credentials: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"Nie można odczytać '{path}': {error}") from error

    client_id = data.get("clientId") or data.get("client_id") or data.get("CLIENT_ID")
    client_secret = data.get("clientSecret") or data.get("client_secret") or data.get("CLIENT_SECRET")

    if not client_id or not client_secret:
        raise ConfigurationError("Brak clientId/clientSecret w pliku credentials.")

    return str(client_id), str(client_secret)


def resolve_opensky_credentials(
    credentials_path: str | os.PathLike[str] | None = None,
) -> tuple[str, str]:
    selected_path = credentials_path or os.getenv("OPENSKY_CREDENTIALS")
    if selected_path:
        return _read_credentials_file(selected_path)

    client_id = os.getenv("OPENSKY_CLIENT_ID")
    client_secret = os.getenv("OPENSKY_CLIENT_SECRET")
    if client_id and client_secret:
        return client_id, client_secret

    if DEFAULT_CREDENTIALS_PATH.exists():
        return _read_credentials_file(DEFAULT_CREDENTIALS_PATH)

    raise ConfigurationError(
        "Brak danych uwierzytelniających OpenSky. "
        "Umieść credentials.json obok skryptu lub ustaw zmienne środowiskowe."
    )


class OAuthTokenManager:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        session: requests.Session,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = session
        self.timeout_seconds = timeout_seconds
        self._token: str | None = None
        self._expires_at: datetime | None = None

    def invalidate(self) -> None:
        self._token = None
        self._expires_at = None

    def get_token(self) -> str:
        now = datetime.now(timezone.utc)
        if self._token and self._expires_at and now < self._expires_at:
            return self._token

        response = self.session.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=self.timeout_seconds,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as error:
            raise ConfigurationError(
                f"Nie udało się pobrać tokenu OpenSky. HTTP {response.status_code}: "
                f"{response.text[:300]}"
            ) from error

        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise ConfigurationError("Serwer nie zwrócił access_token.")

        expires_in = int(payload.get("expires_in", 1800))
        self._token = str(token)
        self._expires_at = now + timedelta(seconds=max(1, expires_in - TOKEN_REFRESH_MARGIN_SECONDS))
        return self._token

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.get_token()}", "Accept": "application/json"}


class OpenSkyClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_rate_limit_wait_seconds: int = DEFAULT_MAX_RATE_LIMIT_WAIT_SECONDS,
        session: requests.Session | None = None,
    ) -> None:
        self.session = session or requests.Session()
        self._owns_session = session is None
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.max_rate_limit_wait_seconds = max_rate_limit_wait_seconds
        self.tokens = OAuthTokenManager(client_id, client_secret, session=self.session)

    @classmethod
    def from_environment(
        cls,
        *,
        credentials_path: str | os.PathLike[str] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_rate_limit_wait_seconds: int = DEFAULT_MAX_RATE_LIMIT_WAIT_SECONDS,
    ) -> "OpenSkyClient":
        client_id, client_secret = resolve_opensky_credentials(credentials_path)
        return cls(
            client_id, client_secret,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            max_rate_limit_wait_seconds=max_rate_limit_wait_seconds,
        )

    def close(self) -> None:
        if self._owns_session:
            self.session.close()

    def __enter__(self) -> "OpenSkyClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def get_all_flights(
        self,
        *,
        begin_timestamp: int,
        end_timestamp: int,
    ) -> ApiResult:
        """
        Wywołuje /flights/all dla podanego okna (max 2h).
        Zwraca wszystkie loty na świecie w tym oknie.
        """
        url = f"{API_BASE_URL}{FLIGHTS_ALL_ENDPOINT}"
        retry_count = 0
        refreshed_after_401 = False

        while True:
            try:
                response = self.session.get(
                    url,
                    headers=self.tokens.headers(),
                    params={"begin": begin_timestamp, "end": end_timestamp},
                    timeout=self.timeout_seconds,
                )
            except requests.RequestException as error:
                if retry_count >= self.max_retries:
                    raise OpenSkyRequestError(
                        f"Błąd sieci: {error}", retry_count=retry_count
                    ) from error
                retry_count += 1
                time.sleep(min(2 ** retry_count, 30))
                continue

            credits_remaining = _parse_optional_int(
                response.headers.get("X-Rate-Limit-Remaining")
            )

            if response.status_code == 401 and not refreshed_after_401:
                self.tokens.invalidate()
                refreshed_after_401 = True
                retry_count += 1
                continue

            if response.status_code == 404:
                return ApiResult(
                    records=[], status_code=404,
                    credits_remaining=credits_remaining, retry_count=retry_count,
                )

            if response.status_code == 429:
                retry_after = _parse_optional_int(
                    response.headers.get("X-Rate-Limit-Retry-After-Seconds")
                ) or 60

                if retry_count >= self.max_retries or retry_after > self.max_rate_limit_wait_seconds:
                    raise OpenSkyRequestError(
                        f"Wyczerpano limit OpenSky. Czas oczekiwania: {retry_after}s.",
                        status_code=429,
                        credits_remaining=credits_remaining,
                        retry_count=retry_count,
                    )

                retry_count += 1
                time.sleep(retry_after)
                continue

            if response.status_code >= 500:
                if retry_count >= self.max_retries:
                    raise OpenSkyRequestError(
                        f"OpenSky HTTP {response.status_code}.",
                        status_code=response.status_code,
                        credits_remaining=credits_remaining,
                        retry_count=retry_count,
                    )
                retry_count += 1
                time.sleep(min(2 ** retry_count, 30))
                continue

            try:
                response.raise_for_status()
            except requests.HTTPError as error:
                raise OpenSkyRequestError(
                    f"OpenSky HTTP {response.status_code}: {response.text.strip()[:500]}",
                    status_code=response.status_code,
                    credits_remaining=credits_remaining,
                    retry_count=retry_count,
                ) from error

            try:
                payload = response.json()
            except requests.JSONDecodeError as error:
                raise OpenSkyRequestError(
                    "OpenSky zwrócił nieprawidłowy JSON.",
                    status_code=response.status_code,
                    credits_remaining=credits_remaining,
                    retry_count=retry_count,
                ) from error

            if not isinstance(payload, list):
                raise OpenSkyRequestError(
                    "Odpowiedź OpenSky nie jest listą.",
                    status_code=response.status_code,
                    credits_remaining=credits_remaining,
                    retry_count=retry_count,
                )

            records = [dict(r) for r in payload if isinstance(r, Mapping)]
            return ApiResult(
                records=records,
                status_code=response.status_code,
                credits_remaining=credits_remaining,
                retry_count=retry_count,
            )


# POMOCNICZE FUNKCJE


def _parse_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_date(value: DateLike, field_name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as error:
        raise ValueError(f"{field_name} musi mieć format YYYY-MM-DD.") from error


def iter_2h_windows(start_date: date, end_date: date) -> Iterable[TimeWindow]:
    """
    Generuje 2-godzinne okna czasowe dla podanego zakresu dat.
    Jeden dzień = 12 okien. Tydzień = 84 okna.
    """
    begin = datetime.combine(start_date, datetime_time.min, tzinfo=timezone.utc)
    end = datetime.combine(end_date, datetime_time.min, tzinfo=timezone.utc) + timedelta(days=1)

    current = begin
    while current < end:
        window_end = min(current + timedelta(hours=MAX_WINDOW_HOURS), end)
        # OpenSky wymaga end > begin — ostatnia sekunda jest wyłączna
        actual_end = window_end - timedelta(seconds=1)
        yield TimeWindow(
            begin_utc=current,
            end_utc=actual_end,
            begin_timestamp=int(current.timestamp()),
            end_timestamp=int(actual_end.timestamp()),
        )
        current = window_end


def _normalize_airport_code(value: Any) -> str | None:
    if value is None:
        return None
    code = str(value).strip().upper()
    return code if re.fullmatch(r"[A-Z0-9]{4}", code) else None


def _normalize_nonnegative_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    number = int(value)
    if number < 0:
        raise ValueError("Liczba kandydatów nie może być ujemna.")
    return number


def normalize_flight_record(record: Mapping[str, Any]) -> NormalizedFlight:
    icao24 = str(record.get("icao24") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{6}", icao24):
        raise ValueError("Brak poprawnego icao24.")

    try:
        first_seen_ts = int(record["firstSeen"])
        last_seen_ts = int(record["lastSeen"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Brak poprawnego firstSeen lub lastSeen.") from error

    if last_seen_ts < first_seen_ts:
        raise ValueError("lastSeen jest wcześniejsze niż firstSeen.")

    raw_callsign = record.get("callsign")
    callsign = str(raw_callsign).strip() if raw_callsign else None
    callsign = callsign[:16] if callsign else None

    return NormalizedFlight(
        icao24=icao24,
        callsign=callsign,
        first_seen_utc=datetime.fromtimestamp(first_seen_ts, tz=timezone.utc),
        last_seen_utc=datetime.fromtimestamp(last_seen_ts, tz=timezone.utc),
        departure_airport_code=_normalize_airport_code(record.get("estDepartureAirport")),
        arrival_airport_code=_normalize_airport_code(record.get("estArrivalAirport")),
        departure_candidates_count=_normalize_nonnegative_int(
            record.get("departureAirportCandidatesCount")
        ),
        arrival_candidates_count=_normalize_nonnegative_int(
            record.get("arrivalAirportCandidatesCount")
        ),
    )


def filter_flights_for_airports(
    flights: Sequence[NormalizedFlight],
    monitored_codes: frozenset[str],
) -> list[NormalizedFlight]:
    """
    Zwraca tylko loty gdzie departure LUB arrival należy do monitorowanych lotnisk.
    """
    return [
        f for f in flights
        if (f.departure_airport_code in monitored_codes)
        or (f.arrival_airport_code in monitored_codes)
    ]


# BAZA DANYCH


def create_database_engine() -> Engine:
    url = URL.create(
        drivername="postgresql+psycopg2",
        username=DB_USERNAME,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
        database=DB_DATABASE,
    )
    return create_engine(url, pool_pre_ping=True, echo=False)


def validate_schema(engine: Engine) -> None:
    existing = set(inspect(engine).get_table_names())
    missing = sorted(REQUIRED_TABLES - existing)
    if missing:
        raise DatabaseSchemaError(
            "Brakuje tabel: " + ", ".join(missing) +
            ". Najpierw uruchom create_database.py."
        )


def load_monitored_airports(engine: Engine) -> list[dict[str, Any]]:
    """Wczytuje wszystkie lotniska z is_monitored = TRUE."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT airport_id, icao_code, airport_name "
                "FROM airport WHERE is_monitored = TRUE ORDER BY icao_code"
            )
        ).mappings().all()

    if not rows:
        raise DatabaseSchemaError("W bazie nie ma żadnych monitorowanych lotnisk.")

    return [dict(row) for row in rows]


def load_event_types(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT event_type_id, event_type_code FROM event_type")
        ).mappings().all()

    result = {str(row["event_type_code"]).upper(): int(row["event_type_id"]) for row in rows}

    for op in SUPPORTED_OPERATIONS:
        if op not in result:
            raise DatabaseSchemaError(f"Brakuje event_type_code = '{op}' w tabeli event_type.")

    return result


def is_window_completed(
    engine: Engine,
    *,
    airport_id: int,
    event_type_id: int,
    period_begin_utc: datetime,
    period_end_utc: datetime,
) -> bool:
    with engine.connect() as conn:
        count = conn.scalar(
            SQL_CHECK_WINDOW_COMPLETED,
            {
                "airport_id": airport_id,
                "event_type_id": event_type_id,
                "period_begin_utc": period_begin_utc,
                "period_end_utc": period_end_utc,
            },
        )
    return int(count or 0) > 0


def start_import_log(
    engine: Engine,
    *,
    airport_id: int,
    event_type_id: int,
    period_begin_utc: datetime,
    period_end_utc: datetime,
) -> int:
    with engine.begin() as conn:
        result = conn.execute(
            SQL_INSERT_IMPORT_LOG,
            {
                "airport_id": airport_id,
                "event_type_id": event_type_id,
                "endpoint": FLIGHTS_ALL_ENDPOINT,
                "period_begin_utc": period_begin_utc,
                "period_end_utc": period_end_utc,
            },
        )
        return int(result.scalar_one())


def finish_import_log(
    engine: Engine,
    *,
    import_id: int,
    status: str,
    http_status: int | None,
    records_received: int,
    records_inserted: int,
    records_skipped: int,
    credits_remaining: int | None,
    retry_count: int,
    error_message: str | None = None,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            SQL_FINISH_IMPORT_LOG,
            {
                "import_id": import_id,
                "status": status,
                "http_status": http_status,
                "records_received": records_received,
                "records_inserted": records_inserted,
                "records_skipped": records_skipped,
                "credits_remaining": credits_remaining,
                "retry_count": retry_count,
                "error_message": (error_message or "")[:4000] or None,
            },
        )


def _upsert_route_airport(
    connection: Any,
    airport_code: str | None,
    cache: dict[str, int],
) -> int | None:
    if airport_code is None:
        return None
    if airport_code in cache:
        return cache[airport_code]
    result = connection.execute(SQL_UPSERT_ROUTE_AIRPORT, {"icao_code": airport_code})
    airport_id = int(result.scalar_one())
    cache[airport_code] = airport_id
    return airport_id


def save_flights_for_airport(
    engine: Engine,
    *,
    import_id: int,
    airport_id: int,
    airport_code: str,
    event_type_id: int,
    operation: str,
    flights: Sequence[NormalizedFlight],
) -> tuple[int, int]:
    """
    Zapisuje loty dotyczące konkretnego lotniska i operacji.
    Zwraca (inserted_events, skipped_events).
    """
    # Filtruj loty dla tej konkretnej kombinacji lotnisko+operacja
    if operation == "DEPARTURE":
        relevant = [f for f in flights if f.departure_airport_code == airport_code]
        get_event_time = lambda f: f.first_seen_utc
    else:  # ARRIVAL
        relevant = [f for f in flights if f.arrival_airport_code == airport_code]
        get_event_time = lambda f: f.last_seen_utc

    inserted = 0
    skipped = 0
    airport_id_cache: dict[str, int] = {}

    with engine.begin() as conn:
        for flight in relevant:
            dep_airport_id = _upsert_route_airport(conn, flight.departure_airport_code, airport_id_cache)
            arr_airport_id = _upsert_route_airport(conn, flight.arrival_airport_code, airport_id_cache)

            aircraft_result = conn.execute(
                SQL_UPSERT_AIRCRAFT,
                {
                    "icao24": flight.icao24,
                    "first_observed_at": flight.first_seen_utc,
                    "last_observed_at": flight.last_seen_utc,
                },
            )
            aircraft_id = int(aircraft_result.scalar_one())

            flight_result = conn.execute(
                SQL_UPSERT_FLIGHT,
                {
                    "aircraft_id": aircraft_id,
                    "callsign": flight.callsign,
                    "first_seen_utc": flight.first_seen_utc,
                    "last_seen_utc": flight.last_seen_utc,
                    "estimated_departure_airport_id": dep_airport_id,
                    "estimated_arrival_airport_id": arr_airport_id,
                    "departure_candidates_count": flight.departure_candidates_count,
                    "arrival_candidates_count": flight.arrival_candidates_count,
                    "first_import_id": import_id,
                },
            )
            flight_id = int(flight_result.scalar_one())

            event_result = conn.execute(
                SQL_INSERT_AIRPORT_EVENT,
                {
                    "flight_id": flight_id,
                    "airport_id": airport_id,
                    "event_type_id": event_type_id,
                    "import_id": import_id,
                    "event_time_utc": get_event_time(flight),
                },
            )

            if event_result.scalar_one_or_none() is not None:
                inserted += 1
            else:
                skipped += 1

    return inserted, skipped


# ---------------------------------------------------------------------------
# GŁÓWNA FUNKCJA IMPORTU
# ---------------------------------------------------------------------------

def _emit(callback: ProgressCallback | None, **kwargs: Any) -> None:
    if callback:
        try:
            callback(kwargs)
        except Exception:
            pass


def import_flights(
    *,
    start_date: DateLike,
    end_date: DateLike,
    credentials_path: str | os.PathLike[str] | None = None,
    force: bool = False,
    request_delay_seconds: float = DEFAULT_REQUEST_DELAY_SECONDS,
    continue_on_error: bool = True,
    max_retries: int = DEFAULT_MAX_RETRIES,
    max_rate_limit_wait_seconds: int = DEFAULT_MAX_RATE_LIMIT_WAIT_SECONDS,
    credit_reserve: int = DEFAULT_CREDIT_RESERVE,
    progress_callback: ProgressCallback | None = None,
    engine: Engine | None = None,
    client: OpenSkyClient | None = None,
    is_live_mode: bool = False,
) -> ImportSummary:
    start = parse_date(start_date, "start_date")
    end = parse_date(end_date, "end_date")

    if end < start:
        raise ValueError("end_date nie może być wcześniejszy niż start_date.")

    owns_engine = engine is None
    owns_client = client is None

    selected_engine = engine or create_database_engine()
    selected_client = client or OpenSkyClient.from_environment(
        credentials_path=credentials_path,
        max_retries=max_retries,
        max_rate_limit_wait_seconds=max_rate_limit_wait_seconds,
    )

    try:
        validate_schema(selected_engine)
        airports = load_monitored_airports(selected_engine)
        event_types = load_event_types(selected_engine)

        # Słownik: kod ICAO → airport_id
        airport_map: dict[str, int] = {
            str(a["icao_code"]): int(a["airport_id"]) for a in airports
        }
        monitored_codes = frozenset(airport_map.keys())

        
        
        windows = list(iter_2h_windows(start, end))

        estimated = len(windows) * ESTIMATED_CREDITS_PER_WINDOW

        summary = ImportSummary(
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            planned_calls=len(windows),
            estimated_credits=estimated,
        )

        _emit(
            progress_callback,
            event="import_started",
            summary=summary.to_dict(),
            windows=len(windows),
            airports=len(airports),
        )

        last_credits_remaining: int | None = None

        for window in windows:
            # Sprawdź rezerwę kredytów
            if (
                credit_reserve > 0
                and last_credits_remaining is not None
                and last_credits_remaining <= credit_reserve
            ):
                summary.stopped_for_credit_reserve = True
                _emit(
                    progress_callback,
                    event="credit_reserve_reached",
                    credits_remaining=last_credits_remaining,
                    credit_reserve=credit_reserve,
                )
                break

            _emit(
                progress_callback,
                event="window_started",
                window=window.label,
                begin=window.begin_utc.isoformat(),
                end=window.end_utc.isoformat(),
            )

            summary.api_calls += 1

            try:
                result = selected_client.get_all_flights(
                    begin_timestamp=window.begin_timestamp,
                    end_timestamp=window.end_timestamp,
                )

                if result.credits_remaining is not None:
                    last_credits_remaining = result.credits_remaining
                    summary.credits_remaining = result.credits_remaining
                _emit(
                    progress_callback,
                    event="api_response",
                    window=window.label,
                    credits_remaining=result.credits_remaining,
                    records_count=len(result.records),
                )
                # Parsuj i filtruj loty
                raw_flights: list[NormalizedFlight] = []
                for record in result.records:
                    try:
                        raw_flights.append(normalize_flight_record(record))
                    except (ValueError, KeyError):
                        continue

                # Zostaw tylko loty dotyczące polskich lotnisk
                relevant_flights = filter_flights_for_airports(raw_flights, monitored_codes)
                summary.records_received += len(relevant_flights)

                if not relevant_flights:
                    summary.no_data_calls += 1
                    _emit(
                        progress_callback,
                        event="window_finished",
                        window=window.label,
                        status="NO_DATA",
                        records_received=0,
                        records_inserted=0,
                        records_skipped=0,
                        credits_remaining=result.credits_remaining,
                    )
                else:
                    summary.successful_calls += 1
                    total_inserted = 0
                    total_skipped = 0

                    # Dla każdego lotniska i operacji zapisz odpowiednie loty
                    for airport_code, airport_id in airport_map.items():
                        for operation, event_type_id in event_types.items():
                            
                            if not force and is_window_completed(
                                selected_engine,
                                airport_id=airport_id,
                                event_type_id=event_type_id,
                                period_begin_utc=window.begin_utc,
                                period_end_utc=window.end_utc,
                            ):
                                summary.skipped_completed_calls += 1
                                continue

                            import_id = start_import_log(
                                selected_engine,
                                airport_id=airport_id,
                                event_type_id=event_type_id,
                                period_begin_utc=window.begin_utc,
                                period_end_utc=window.end_utc,
                            )

                            inserted, skipped = save_flights_for_airport(
                                selected_engine,
                                import_id=import_id,
                                airport_id=airport_id,
                                airport_code=airport_code,
                                event_type_id=event_type_id,
                                operation=operation,
                                flights=relevant_flights,
                            )

                            status = "SUCCESS" if inserted > 0 else "NO_DATA"
                            finish_import_log(
                                selected_engine,
                                import_id=import_id,
                                status=status,
                                http_status=result.status_code,
                                records_received=len(relevant_flights),
                                records_inserted=inserted,
                                records_skipped=skipped,
                                credits_remaining=result.credits_remaining,
                                retry_count=result.retry_count,
                            )

                            total_inserted += inserted
                            total_skipped += skipped

                    summary.records_inserted += total_inserted
                    summary.records_skipped += total_skipped

                    _emit(
                        progress_callback,
                        event="window_finished",
                        window=window.label,
                        status="SUCCESS",
                        records_received=len(relevant_flights),
                        records_inserted=total_inserted,
                        records_skipped=total_skipped,
                        credits_remaining=result.credits_remaining,
                    )

            except OpenSkyRequestError as error:
                status_code = error.status_code
                credits_remaining = error.credits_remaining

                if credits_remaining is not None:
                    last_credits_remaining = credits_remaining
                    summary.credits_remaining = credits_remaining

                summary.failed_calls += 1

                _emit(
                    progress_callback,
                    event="window_failed",
                    window=window.label,
                    error=f"{type(error).__name__}: {error}",
                    credits_remaining=credits_remaining,
                )

                if status_code == 429:
                    summary.stopped_for_credit_reserve = True
                    break

                if not continue_on_error:
                    raise

            finally:
                if request_delay_seconds > 0:
                    time.sleep(request_delay_seconds)

        _emit(progress_callback, event="import_finished", summary=summary.to_dict())
        return summary

    finally:
        if owns_client:
            selected_client.close()
        if owns_engine:
            selected_engine.dispose()


# CLI


def console_progress(event: dict[str, Any]) -> None:
    name = event.get("event")

    if name == "import_started":
        s = event["summary"]
        print(
            f"Rozpoczynam import: {s['start_date']} - {s['end_date']}, "
            f"{s['planned_calls']} okien, "
            f"szacunkowo {s['estimated_credits']} kredytów, "
            f"{event['airports']} lotnisk."
        )

    elif name == "window_started":
        print(f"[START] {event['window']}")
    elif name == "api_response":
        credits_remaining = event.get("credits_remaining")
        if credits_remaining is not None:
            print(f"[API] {event['window']} | Rekordy: {event['records_count']} | Kredyty: {credits_remaining}")
        else:
            print(f"[API] {event['window']} | Rekordy: {event['records_count']} | Kredyty: ???")
    elif name == "window_finished":
        credits_remaining = event.get("credits_remaining")

        if credits_remaining is not None:
            credits_info = f"| Pozostało: {credits_remaining} kr"
        else:
            credits_info = "| Brak danych o kredytach"

        print(
            f"[OK] {event['window']} "
            f"| Rekordów: {event['records_received']} "
            f"| Dodano: {event['records_inserted']} "
            f"| Pominięto: {event['records_skipped']} "
            f"{credits_info}"
        )

    elif name == "window_failed":
        print(f"[FAILED] {event['window']} | {event['error']}")

    elif name == "credit_reserve_reached":
        print(
            f"[STOP KREDYTY] Pozostało {event['credits_remaining']} kredytów. "
            f"Rezerwa: {event['credit_reserve']}."
        )

    elif name == "import_finished":
        s = event["summary"]
        print()
        print("=" * 68)
        print("IMPORT ZAKOŃCZONY")
        print(f"Okna API: {s['api_calls']} / {s['planned_calls']}")
        print(f"Pominięte (już zaimportowane): {s['skipped_completed_calls']}")
        print(f"SUCCESS: {s['successful_calls']}")
        print(f"NO_DATA: {s['no_data_calls']}")
        print(f"FAILED:  {s['failed_calls']}")
        print(f"Pobrane rekordy (PL): {s['records_received']}")
        print(f"Nowe zdarzenia: {s['records_inserted']}")
        print(f"Pominięte duplikaty: {s['records_skipped']}")
        print(f"Szacowany koszt: {s['estimated_credits']} kredytów")
        print(f"Pozostałe kredyty: {s['credits_remaining']}")
        print("=" * 68)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import lotów OpenSky (/flights/all) do PostgreSQL."
    )
    parser.add_argument("--start-date", required=True, help="Format YYYY-MM-DD.")
    parser.add_argument("--end-date", required=True, help="Format YYYY-MM-DD (włącznie).")
    parser.add_argument("--credentials", help="Ścieżka do pliku credentials.json.")
    parser.add_argument(
        "--force", action="store_true",
        help="Ponownie pobiera okresy już zaimportowane.",
    )
    parser.add_argument(
        "--delay", type=float, default=DEFAULT_REQUEST_DELAY_SECONDS,
        help=f"Przerwa między wywołaniami w sekundach. Domyślnie {DEFAULT_REQUEST_DELAY_SECONDS}.",
    )
    parser.add_argument(
        "--max-retries", type=int, default=DEFAULT_MAX_RETRIES,
        help=f"Maksymalna liczba ponowień. Domyślnie {DEFAULT_MAX_RETRIES}.",
    )
    parser.add_argument(
        "--max-rate-limit-wait", type=int, default=DEFAULT_MAX_RATE_LIMIT_WAIT_SECONDS,
        help=f"Max czas oczekiwania po HTTP 429. Domyślnie {DEFAULT_MAX_RATE_LIMIT_WAIT_SECONDS}s.",
    )
    parser.add_argument(
        "--credit-reserve", type=int, default=DEFAULT_CREDIT_RESERVE,
        help=f"Rezerwa kredytów. Domyślnie {DEFAULT_CREDIT_RESERVE}.",
    )
    parser.add_argument(
        "--stop-on-error", action="store_true",
        help="Kończy import po pierwszym błędzie.",
    )
    
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    try:
        summary = import_flights(
            start_date=args.start_date,
            end_date=args.end_date,
            credentials_path=args.credentials,
            force=args.force,
            request_delay_seconds=args.delay,
            continue_on_error=not args.stop_on_error,
            max_retries=args.max_retries,
            max_rate_limit_wait_seconds=args.max_rate_limit_wait,
            credit_reserve=args.credit_reserve,
            progress_callback=console_progress,
        )
    except Exception as error:
        print(f"[BŁĄD KRYTYCZNY] {type(error).__name__}: {error}")
        return 1

    return 1 if summary.failed_calls > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
