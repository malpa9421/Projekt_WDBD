from __future__ import annotations

"""
Import historycznych przylotów i odlotów OpenSky do bazy opensky_airports.

Plik może być używany na dwa sposoby:

1. Z wiersza poleceń:
   python import_data_optimized.py \
       --start-date 2026-06-01 \
       --end-date 2026-06-07

2. Jako moduł w innym skrypcie:
   from import_data_optimized import import_flights

   summary = import_flights(
       start_date="2026-06-01",
       end_date="2026-06-07",
       airports=["EPWA", "EPKK"],
   )
   print(summary.to_dict())

Zakres dat jest domknięty: start_date i end_date są importowane.
Domyślnie importowane są wszystkie lotniska oznaczone w bazie jako
is_monitored = TRUE oraz operacje ARRIVAL i DEPARTURE.

Optymalizacja kredytów:
- importer łączy dwa kolejne dni w jedno żądanie API,
- OpenSky dopuszcza maksymalnie dwa dni dla endpointów lotniskowych,
- historyczne żądanie obejmujące 1 albo 2 partycje dzienne kosztuje
  tyle samo, więc okno dwudniowe zwykle daje około 2 razy więcej danych
  za tę samą liczbę kredytów,
- dni już poprawnie zaimportowane nie są pobierane ponownie,
- opcjonalna rezerwa kredytów zatrzymuje import przed odpowiedzią 429.
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


# KONFIGURACJA OPENSKY
API_BASE_URL = "https://opensky-network.org/api"
TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/"
    "opensky-network/protocol/openid-connect/token"
)

TOKEN_REFRESH_MARGIN_SECONDS = 30
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_REQUEST_DELAY_SECONDS = 0.25
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_RATE_LIMIT_WAIT_SECONDS = 600

DEFAULT_WINDOW_DAYS = 2
MAX_WINDOW_DAYS = 2
ESTIMATED_HISTORICAL_REQUEST_CREDITS = 30
DEFAULT_CREDIT_RESERVE = 0

DEFAULT_CREDENTIALS_PATH = (
    Path(__file__).resolve().with_name("credentials.json")
)

SUPPORTED_OPERATIONS = ("ARRIVAL", "DEPARTURE")
OPERATION_ENDPOINTS = {
    "ARRIVAL": "/flights/arrival",
    "DEPARTURE": "/flights/departure",
}

# TYPY I WYJĄTKI
DateLike = date | datetime | str
ProgressCallback = Callable[[dict[str, Any]], None]


class ConfigurationError(RuntimeError):
    """Błąd konfiguracji połączenia lub danych uwierzytelniających."""


class DatabaseSchemaError(RuntimeError):
    """Brak wymaganych tabel albo danych słownikowych."""


class OpenSkyRequestError(RuntimeError):
    """Błąd wywołania API OpenSky."""

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
    airports: list[str]
    operations: list[str]
    planned_calls: int = 0
    planned_days: int = 0
    api_calls: int = 0
    skipped_completed_calls: int = 0
    skipped_completed_days: int = 0
    days_requested: int = 0
    successful_calls: int = 0
    no_data_calls: int = 0
    partial_calls: int = 0
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
class ImportTask:
    airport_id: int
    airport_code: str
    operation: str
    event_type_id: int
    start_date: date
    end_date: date
    period_begin_utc: datetime
    period_end_utc: datetime

    @property
    def days_count(self) -> int:
        return (self.end_date - self.start_date).days + 1

    @property
    def date_label(self) -> str:
        if self.start_date == self.end_date:
            return self.start_date.isoformat()
        return (
            f"{self.start_date.isoformat()}.."
            f"{self.end_date.isoformat()}"
        )


# SQL
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
        finished_at = CURRENT_TIMESTAMP,
        http_status = :http_status,
        records_received = :records_received,
        records_inserted = :records_inserted,
        records_skipped = :records_skipped,
        credits_remaining = :credits_remaining,
        retry_count = :retry_count,
        status = :status,
        error_message = :error_message
    WHERE import_id = :import_id
    """
)

SQL_LOAD_COMPLETED_PERIODS = text(
    """
    SELECT
        period_begin_utc,
        period_end_utc
    FROM import_log
    WHERE airport_id = :airport_id
      AND event_type_id = :event_type_id
      AND status IN ('SUCCESS', 'NO_DATA')
      AND period_end_utc >= :range_begin_utc
      AND period_begin_utc <= :range_end_utc
    ORDER BY period_begin_utc
    """
)

SQL_UPSERT_ROUTE_AIRPORT = text(
    """
    INSERT INTO airport (
        icao_code,
        airport_name,
        city,
        country_code,
        is_monitored
    )
    VALUES (
        :icao_code,
        NULL,
        NULL,
        NULL,
        FALSE
    )
    ON CONFLICT (icao_code)
    DO UPDATE SET
        icao_code = EXCLUDED.icao_code
    RETURNING airport_id
    """
)

SQL_UPSERT_AIRCRAFT = text(
    """
    INSERT INTO aircraft (
        icao24,
        first_observed_at,
        last_observed_at
    )
    VALUES (
        :icao24,
        :first_observed_at,
        :last_observed_at
    )
    ON CONFLICT (icao24)
    DO UPDATE SET
        first_observed_at = CASE
            WHEN aircraft.first_observed_at IS NULL
                THEN EXCLUDED.first_observed_at
            ELSE LEAST(
                aircraft.first_observed_at,
                EXCLUDED.first_observed_at
            )
        END,
        last_observed_at = CASE
            WHEN aircraft.last_observed_at IS NULL
                THEN EXCLUDED.last_observed_at
            ELSE GREATEST(
                aircraft.last_observed_at,
                EXCLUDED.last_observed_at
            )
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
        callsign = COALESCE(
            EXCLUDED.callsign,
            flight.callsign
        ),
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
        flight_id,
        airport_id,
        event_type_id,
        import_id,
        event_time_utc
    )
    VALUES (
        :flight_id,
        :airport_id,
        :event_type_id,
        :import_id,
        :event_time_utc
    )
    ON CONFLICT ON CONSTRAINT uq_airport_event
    DO NOTHING
    RETURNING airport_event_id
    """
)


# TOKEN OAUTH2 I KLIENT OPENSKY
def _read_credentials_file(credentials_path: str | os.PathLike[str], ) -> tuple[str, str]:
    path = Path(credentials_path)

    if not path.exists():
        raise ConfigurationError(
            f"Nie znaleziono pliku danych OpenSky: {path}"
        )

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(
            f"Nie można odczytać pliku OpenSky '{path}': {error}"
        ) from error

    client_id = (
        data.get("clientId")
        or data.get("client_id")
        or data.get("CLIENT_ID")
    )
    client_secret = (
        data.get("clientSecret")
        or data.get("client_secret")
        or data.get("CLIENT_SECRET")
    )

    if not client_id or not client_secret:
        raise ConfigurationError(
            "Plik credentials.json nie zawiera clientId/clientSecret."
        )

    return str(client_id), str(client_secret)


def resolve_opensky_credentials(credentials_path: str | os.PathLike[str] | None = None,) -> tuple[str, str]:
    """
    Kolejność wyszukiwania:
    1. parametr credentials_path,
    2. OPENSKY_CREDENTIALS,
    3. OPENSKY_CLIENT_ID i OPENSKY_CLIENT_SECRET,
    4. plik credentials.json w bieżącym katalogu.
    """

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
        "Umieść credentials.json obok tego skryptu, podaj "
        "credentials_path, ustaw OPENSKY_CREDENTIALS albo ustaw "
        "OPENSKY_CLIENT_ID i OPENSKY_CLIENT_SECRET."
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

        if (
            self._token is not None
            and self._expires_at is not None
            and now < self._expires_at
        ):
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
                "Nie udało się pobrać tokenu OpenSky. "
                f"HTTP {response.status_code}: {response.text[:300]}"
            ) from error

        payload = response.json()
        token = payload.get("access_token")

        if not token:
            raise ConfigurationError(
                "Serwer uwierzytelniania OpenSky nie zwrócił access_token."
            )

        expires_in = int(payload.get("expires_in", 1800))
        valid_for = max(
            1,
            expires_in - TOKEN_REFRESH_MARGIN_SECONDS,
        )

        self._token = str(token)
        self._expires_at = now + timedelta(seconds=valid_for)

        return self._token

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.get_token()}",
            "Accept": "application/json",
        }


class OpenSkyClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_rate_limit_wait_seconds: int = (
            DEFAULT_MAX_RATE_LIMIT_WAIT_SECONDS
        ),
        session: requests.Session | None = None,
    ) -> None:
        self.session = session or requests.Session()
        self._owns_session = session is None
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.max_rate_limit_wait_seconds = max_rate_limit_wait_seconds
        self.tokens = OAuthTokenManager(
            client_id,
            client_secret,
            session=self.session,
            timeout_seconds=timeout_seconds,
        )

    @classmethod
    def from_environment(
        cls,
        *,
        credentials_path: str | os.PathLike[str] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_rate_limit_wait_seconds: int = (
            DEFAULT_MAX_RATE_LIMIT_WAIT_SECONDS
        ),
    ) -> "OpenSkyClient":
        client_id, client_secret = resolve_opensky_credentials(
            credentials_path
        )
        return cls(
            client_id,
            client_secret,
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

    def get_airport_flights(
        self,
        *,
        airport: str,
        operation: str,
        begin_timestamp: int,
        end_timestamp: int,
    ) -> ApiResult:
        operation = normalize_operation(operation)
        endpoint = OPERATION_ENDPOINTS[operation]
        url = f"{API_BASE_URL}{endpoint}"

        retry_count = 0
        refreshed_after_401 = False

        while True:
            try:
                response = self.session.get(
                    url,
                    headers=self.tokens.headers(),
                    params={
                        "airport": airport,
                        "begin": begin_timestamp,
                        "end": end_timestamp,
                    },
                    timeout=self.timeout_seconds,
                )
            except requests.RequestException as error:
                if retry_count >= self.max_retries:
                    raise OpenSkyRequestError(
                        f"Błąd sieci podczas pobierania {airport}: {error}",
                        retry_count=retry_count,
                    ) from error

                retry_count += 1
                time.sleep(min(2**retry_count, 30))
                continue

            credits_remaining = parse_optional_int(
                response.headers.get("X-Rate-Limit-Remaining")
            )

            if response.status_code == 401 and not refreshed_after_401:
                self.tokens.invalidate()
                refreshed_after_401 = True
                retry_count += 1
                continue

            if response.status_code == 404:
                return ApiResult(
                    records=[],
                    status_code=404,
                    credits_remaining=credits_remaining,
                    retry_count=retry_count,
                )

            if response.status_code == 429:
                retry_after = parse_optional_int(
                    response.headers.get(
                        "X-Rate-Limit-Retry-After-Seconds"
                    )
                ) or 60

                if (
                    retry_count >= self.max_retries
                    or retry_after > self.max_rate_limit_wait_seconds
                ):
                    raise OpenSkyRequestError(
                        "Wyczerpano limit OpenSky. "
                        f"Zalecany czas oczekiwania: {retry_after} s.",
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
                        f"OpenSky zwrócił HTTP {response.status_code}.",
                        status_code=response.status_code,
                        credits_remaining=credits_remaining,
                        retry_count=retry_count,
                    )

                retry_count += 1
                time.sleep(min(2**retry_count, 30))
                continue

            try:
                response.raise_for_status()
            except requests.HTTPError as error:
                message = response.text.strip()[:500]
                raise OpenSkyRequestError(
                    f"OpenSky zwrócił HTTP {response.status_code}: "
                    f"{message or 'brak treści odpowiedzi'}",
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
                    "Odpowiedź OpenSky nie jest listą lotów.",
                    status_code=response.status_code,
                    credits_remaining=credits_remaining,
                    retry_count=retry_count,
                )

            records = [
                dict(record)
                for record in payload
                if isinstance(record, Mapping)
            ]

            return ApiResult(
                records=records,
                status_code=response.status_code,
                credits_remaining=credits_remaining,
                retry_count=retry_count,
            )


# POMOCNICZE FUNKCJE DAT I WALIDACJI
def parse_date(value: DateLike, field_name: str) -> date:
    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    try:
        return date.fromisoformat(str(value))
    except ValueError as error:
        raise ValueError(
            f"{field_name} musi mieć format YYYY-MM-DD."
        ) from error


def iter_dates(start_date: date, end_date: date) -> Iterable[date]:
    current = start_date

    while current <= end_date:
        yield current
        current += timedelta(days=1)


def utc_day_bounds(selected_date: date) -> tuple[datetime, datetime]:
    begin = datetime.combine(
        selected_date,
        datetime_time.min,
        tzinfo=timezone.utc,
    )
    end = begin + timedelta(days=1) - timedelta(seconds=1)
    return begin, end



def utc_window_bounds(
    start_date: date,
    end_date: date,
) -> tuple[datetime, datetime]:
    if end_date < start_date:
        raise ValueError("Koniec okna nie może poprzedzać początku.")

    begin = datetime.combine(
        start_date,
        datetime_time.min,
        tzinfo=timezone.utc,
    )
    end = datetime.combine(
        end_date,
        datetime_time.min,
        tzinfo=timezone.utc,
    ) + timedelta(days=1) - timedelta(seconds=1)

    return begin, end


def group_consecutive_dates(
    selected_dates: Sequence[date],
    *,
    max_days: int = DEFAULT_WINDOW_DAYS,
) -> list[tuple[date, date]]:
    """Łączy kolejne daty w okna o długości najwyżej max_days."""
    if not 1 <= max_days <= MAX_WINDOW_DAYS:
        raise ValueError(
            f"window_days musi być w zakresie 1..{MAX_WINDOW_DAYS}."
        )

    if not selected_dates:
        return []

    sorted_dates = sorted(set(selected_dates))
    windows: list[tuple[date, date]] = []

    window_start = sorted_dates[0]
    window_end = sorted_dates[0]

    for current in sorted_dates[1:]:
        is_consecutive = current == window_end + timedelta(days=1)
        current_size = (window_end - window_start).days + 1

        if is_consecutive and current_size < max_days:
            window_end = current
            continue

        windows.append((window_start, window_end))
        window_start = current
        window_end = current

    windows.append((window_start, window_end))
    return windows


def normalize_operation(operation: str) -> str:
    value = str(operation).strip().upper()

    aliases = {
        "ARRIVAL": "ARRIVAL",
        "ARRIVALS": "ARRIVAL",
        "PRZYLOT": "ARRIVAL",
        "PRZYLOTY": "ARRIVAL",
        "DEPARTURE": "DEPARTURE",
        "DEPARTURES": "DEPARTURE",
        "ODLOT": "DEPARTURE",
        "ODLOTY": "DEPARTURE",
    }

    normalized = aliases.get(value)

    if normalized is None:
        raise ValueError(
            f"Nieobsługiwana operacja '{operation}'. "
            "Dozwolone: ARRIVAL, DEPARTURE."
        )

    return normalized


def normalize_operations(
    operations: Sequence[str] | None,
) -> list[str]:
    selected = operations or list(SUPPORTED_OPERATIONS)
    result: list[str] = []

    for operation in selected:
        normalized = normalize_operation(operation)
        if normalized not in result:
            result.append(normalized)

    if not result:
        raise ValueError("Wybierz co najmniej jeden typ operacji.")

    return result


def normalize_airport_codes(
    airports: Sequence[str] | None,
) -> list[str] | None:
    if airports is None:
        return None

    result: list[str] = []

    for airport in airports:
        code = str(airport).strip().upper()

        if not re.fullmatch(r"[A-Z0-9]{4}", code):
            raise ValueError(
                f"Nieprawidłowy kod ICAO lotniska: '{airport}'."
            )

        if code not in result:
            result.append(code)

    if not result:
        return None

    return result


def parse_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_airport_code(value: Any) -> str | None:
    if value is None:
        return None

    code = str(value).strip().upper()

    if re.fullmatch(r"[A-Z0-9]{4}", code):
        return code

    return None


def _normalize_nonnegative_int(value: Any) -> int | None:
    if value is None or value == "":
        return None

    number = int(value)

    if number < 0:
        raise ValueError("Liczba kandydatów nie może być ujemna.")

    return number


def normalize_flight_record(
    record: Mapping[str, Any],
) -> NormalizedFlight:
    icao24 = str(record.get("icao24") or "").strip().lower()

    if not re.fullmatch(r"[0-9a-f]{6}", icao24):
        raise ValueError("Brak poprawnego icao24.")

    try:
        first_seen_timestamp = int(record["firstSeen"])
        last_seen_timestamp = int(record["lastSeen"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Brak poprawnego firstSeen lub lastSeen.") from error

    if last_seen_timestamp < first_seen_timestamp:
        raise ValueError("lastSeen jest wcześniejsze niż firstSeen.")

    first_seen_utc = datetime.fromtimestamp(
        first_seen_timestamp,
        tz=timezone.utc,
    )
    last_seen_utc = datetime.fromtimestamp(
        last_seen_timestamp,
        tz=timezone.utc,
    )

    raw_callsign = record.get("callsign")
    callsign = str(raw_callsign).strip() if raw_callsign else None
    callsign = callsign[:16] if callsign else None

    return NormalizedFlight(
        icao24=icao24,
        callsign=callsign,
        first_seen_utc=first_seen_utc,
        last_seen_utc=last_seen_utc,
        departure_airport_code=_normalize_airport_code(
            record.get("estDepartureAirport")
        ),
        arrival_airport_code=_normalize_airport_code(
            record.get("estArrivalAirport")
        ),
        departure_candidates_count=_normalize_nonnegative_int(
            record.get("departureAirportCandidatesCount")
        ),
        arrival_candidates_count=_normalize_nonnegative_int(
            record.get("arrivalAirportCandidatesCount")
        ),
    )


# BAZA DANYCH
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


def validate_schema(engine: Engine) -> None:
    existing_tables = set(inspect(engine).get_table_names())
    missing = sorted(REQUIRED_TABLES - existing_tables)

    if missing:
        raise DatabaseSchemaError(
            "Brakuje tabel: "
            + ", ".join(missing)
            + ". Najpierw uruchom init_opensky_database.py."
        )


def load_airports(
    engine: Engine,
    requested_codes: Sequence[str] | None,
) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT airport_id, icao_code, airport_name
                FROM airport
                WHERE is_monitored = TRUE
                ORDER BY icao_code
                """
            )
        ).mappings().all()

    available = {
        str(row["icao_code"]).upper(): dict(row)
        for row in rows
    }

    if requested_codes is None:
        selected = list(available.values())
    else:
        missing = [
            code
            for code in requested_codes
            if code not in available
        ]

        if missing:
            raise ValueError(
                "Podane lotniska nie są oznaczone w bazie jako "
                f"monitorowane: {', '.join(missing)}"
            )

        selected = [available[code] for code in requested_codes]

    if not selected:
        raise DatabaseSchemaError(
            "W bazie nie ma żadnych monitorowanych lotnisk."
        )

    return selected


def load_event_types(
    engine: Engine,
    operations: Sequence[str],
) -> dict[str, int]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT event_type_id, event_type_code
                FROM event_type
                """
            )
        ).mappings().all()

    available = {
        str(row["event_type_code"]).upper(): int(row["event_type_id"])
        for row in rows
    }

    missing = [
        operation
        for operation in operations
        if operation not in available
    ]

    if missing:
        raise DatabaseSchemaError(
            "Brakuje danych w event_type: " + ", ".join(missing)
        )

    return {
        operation: available[operation]
        for operation in operations
    }


def load_completed_periods(
    engine: Engine,
    *,
    airport_id: int,
    event_type_id: int,
    range_begin_utc: datetime,
    range_end_utc: datetime,
) -> list[tuple[datetime, datetime]]:
    with engine.connect() as connection:
        rows = connection.execute(
            SQL_LOAD_COMPLETED_PERIODS,
            {
                "airport_id": airport_id,
                "event_type_id": event_type_id,
                "range_begin_utc": range_begin_utc,
                "range_end_utc": range_end_utc,
            },
        ).all()

    return [
        (row.period_begin_utc, row.period_end_utc)
        for row in rows
    ]


def is_day_covered(
    selected_date: date,
    completed_periods: Sequence[tuple[datetime, datetime]],
) -> bool:
    day_begin, day_end = utc_day_bounds(selected_date)

    return any(
        period_begin <= day_begin and period_end >= day_end
        for period_begin, period_end in completed_periods
    )


def build_import_tasks(
    engine: Engine,
    *,
    selected_dates: Sequence[date],
    selected_airports: Sequence[Mapping[str, Any]],
    selected_operations: Sequence[str],
    event_type_ids: Mapping[str, int],
    force: bool,
    window_days: int,
) -> tuple[list[ImportTask], int]:
    """
    Buduje zadania API. Kolejne niezakończone dni są łączone
    w maksymalnie dwudniowe okna.
    """
    if not 1 <= window_days <= MAX_WINDOW_DAYS:
        raise ValueError(
            f"window_days musi być w zakresie 1..{MAX_WINDOW_DAYS}."
        )

    if not selected_dates:
        return [], 0

    full_range_begin, _ = utc_day_bounds(selected_dates[0])
    _, full_range_end = utc_day_bounds(selected_dates[-1])

    tasks: list[ImportTask] = []
    skipped_days = 0

    for airport in selected_airports:
        airport_id = int(airport["airport_id"])
        airport_code = str(airport["icao_code"])

        for operation in selected_operations:
            event_type_id = int(event_type_ids[operation])

            if force:
                pending_dates = list(selected_dates)
            else:
                completed_periods = load_completed_periods(
                    engine,
                    airport_id=airport_id,
                    event_type_id=event_type_id,
                    range_begin_utc=full_range_begin,
                    range_end_utc=full_range_end,
                )

                pending_dates = [
                    selected_date
                    for selected_date in selected_dates
                    if not is_day_covered(
                        selected_date,
                        completed_periods,
                    )
                ]
                skipped_days += (
                    len(selected_dates) - len(pending_dates)
                )

            for window_start, window_end in group_consecutive_dates(
                pending_dates,
                max_days=window_days,
            ):
                period_begin_utc, period_end_utc = utc_window_bounds(
                    window_start,
                    window_end,
                )

                tasks.append(
                    ImportTask(
                        airport_id=airport_id,
                        airport_code=airport_code,
                        operation=operation,
                        event_type_id=event_type_id,
                        start_date=window_start,
                        end_date=window_end,
                        period_begin_utc=period_begin_utc,
                        period_end_utc=period_end_utc,
                    )
                )

    return tasks, skipped_days


def start_import_log(
    engine: Engine,
    *,
    airport_id: int,
    event_type_id: int,
    endpoint: str,
    period_begin_utc: datetime,
    period_end_utc: datetime,
) -> int:
    with engine.begin() as connection:
        result = connection.execute(
            SQL_INSERT_IMPORT_LOG,
            {
                "airport_id": airport_id,
                "event_type_id": event_type_id,
                "endpoint": endpoint,
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
    if error_message:
        error_message = error_message[:4000]

    with engine.begin() as connection:
        connection.execute(
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
                "error_message": error_message,
            },
        )


def _upsert_route_airport(
    connection: Any,
    airport_code: str | None,
    cache: dict[str, int],
) -> int | None:
    if airport_code is None:
        return None

    cached = cache.get(airport_code)
    if cached is not None:
        return cached

    result = connection.execute(
        SQL_UPSERT_ROUTE_AIRPORT,
        {"icao_code": airport_code},
    )
    airport_id = int(result.scalar_one())
    cache[airport_code] = airport_id
    return airport_id


def save_flights(
    engine: Engine,
    *,
    import_id: int,
    monitored_airport_id: int,
    event_type_id: int,
    operation: str,
    flights: Sequence[NormalizedFlight],
) -> tuple[int, int]:
    """
    Zwraca:
    - liczbę nowych airport_event,
    - liczbę zdarzeń pominiętych jako duplikaty.
    """

    inserted_events = 0
    duplicate_events = 0
    airport_id_cache: dict[str, int] = {}

    with engine.begin() as connection:
        for flight in flights:
            departure_airport_id = _upsert_route_airport(
                connection,
                flight.departure_airport_code,
                airport_id_cache,
            )
            arrival_airport_id = _upsert_route_airport(
                connection,
                flight.arrival_airport_code,
                airport_id_cache,
            )

            aircraft_result = connection.execute(
                SQL_UPSERT_AIRCRAFT,
                {
                    "icao24": flight.icao24,
                    "first_observed_at": flight.first_seen_utc,
                    "last_observed_at": flight.last_seen_utc,
                },
            )
            aircraft_id = int(aircraft_result.scalar_one())

            flight_result = connection.execute(
                SQL_UPSERT_FLIGHT,
                {
                    "aircraft_id": aircraft_id,
                    "callsign": flight.callsign,
                    "first_seen_utc": flight.first_seen_utc,
                    "last_seen_utc": flight.last_seen_utc,
                    "estimated_departure_airport_id":
                        departure_airport_id,
                    "estimated_arrival_airport_id":
                        arrival_airport_id,
                    "departure_candidates_count":
                        flight.departure_candidates_count,
                    "arrival_candidates_count":
                        flight.arrival_candidates_count,
                    "first_import_id": import_id,
                },
            )
            flight_id = int(flight_result.scalar_one())

            event_time_utc = (
                flight.last_seen_utc
                if operation == "ARRIVAL"
                else flight.first_seen_utc
            )

            airport_event_id = connection.scalar(
                SQL_INSERT_AIRPORT_EVENT,
                {
                    "flight_id": flight_id,
                    "airport_id": monitored_airport_id,
                    "event_type_id": event_type_id,
                    "import_id": import_id,
                    "event_time_utc": event_time_utc,
                },
            )

            if airport_event_id is None:
                duplicate_events += 1
            else:
                inserted_events += 1

    return inserted_events, duplicate_events


# IMPORT
def _emit(
    callback: ProgressCallback | None,
    **event: Any,
) -> None:
    if callback is not None:
        callback(event)


def import_flights(
    start_date: DateLike,
    end_date: DateLike,
    *,
    airports: Sequence[str] | None = None,
    operations: Sequence[str] | None = None,
    engine: Engine | None = None,
    client: OpenSkyClient | None = None,
    credentials_path: str | os.PathLike[str] | None = None,
    force: bool = False,
    request_delay_seconds: float = DEFAULT_REQUEST_DELAY_SECONDS,
    continue_on_error: bool = True,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    max_rate_limit_wait_seconds: int = (
        DEFAULT_MAX_RATE_LIMIT_WAIT_SECONDS
    ),
    window_days: int = DEFAULT_WINDOW_DAYS,
    credit_reserve: int = DEFAULT_CREDIT_RESERVE,
    progress_callback: ProgressCallback | None = None,
) -> ImportSummary:
    """
    Importuje dane z OpenSky do PostgreSQL.

    Parametry:
        start_date, end_date:
            Daty w formacie YYYY-MM-DD albo obiekty datetime.date.
            Zakres jest domknięty.

        airports:
            Lista kodów ICAO, np. ["EPWA", "EPKK"].
            None oznacza wszystkie monitorowane lotniska z bazy.

        operations:
            ARRIVAL i/lub DEPARTURE.
            None oznacza obie operacje.

        engine:
            Opcjonalny istniejący SQLAlchemy Engine.

        client:
            Opcjonalny istniejący OpenSkyClient.

        credentials_path:
            Ścieżka do credentials.json, gdy klient nie jest przekazany.

        force:
            True ponawia także importy ze statusem SUCCESS lub NO_DATA.

        window_days:
            Liczba kolejnych dni w jednym żądaniu. Domyślnie 2,
            co zwykle podwaja liczbę danych za tę samą liczbę kredytów.

        credit_reserve:
            Minimalna liczba kredytów, którą importer ma pozostawić.
            Wartość 0 wykorzystuje pulę możliwie maksymalnie.

        progress_callback:
            Funkcja przyjmująca słownik ze stanem bieżącego zadania.

    Zwraca:
        ImportSummary.
    """

    selected_start_date = parse_date(start_date, "start_date")
    selected_end_date = parse_date(end_date, "end_date")

    if selected_end_date < selected_start_date:
        raise ValueError(
            "end_date nie może być wcześniejsze niż start_date."
        )

    if not 1 <= window_days <= MAX_WINDOW_DAYS:
        raise ValueError(
            f"window_days musi być w zakresie 1..{MAX_WINDOW_DAYS}."
        )

    if credit_reserve < 0:
        raise ValueError("credit_reserve nie może być ujemne.")

    latest_supported_date = (
        datetime.now(timezone.utc).date() - timedelta(days=1)
    )

    if selected_end_date > latest_supported_date:
        raise ValueError(
            "OpenSky udostępnia przyloty i odloty po nocnym "
            "przetworzeniu. end_date nie może być późniejsze niż "
            f"{latest_supported_date.isoformat()}."
        )

    selected_operations = normalize_operations(operations)
    requested_airports = normalize_airport_codes(airports)

    owns_engine = engine is None
    owns_client = client is None

    selected_engine = engine or create_database_engine()
    selected_client = client or OpenSkyClient.from_environment(
        credentials_path=credentials_path,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        max_rate_limit_wait_seconds=max_rate_limit_wait_seconds,
    )

    try:
        validate_schema(selected_engine)

        selected_airports = load_airports(
            selected_engine,
            requested_airports,
        )
        event_type_ids = load_event_types(
            selected_engine,
            selected_operations,
        )

        selected_dates = list(
            iter_dates(
                selected_start_date,
                selected_end_date,
            )
        )

        tasks, skipped_completed_days = build_import_tasks(
            selected_engine,
            selected_dates=selected_dates,
            selected_airports=selected_airports,
            selected_operations=selected_operations,
            event_type_ids=event_type_ids,
            force=force,
            window_days=window_days,
        )

        summary = ImportSummary(
            start_date=selected_start_date.isoformat(),
            end_date=selected_end_date.isoformat(),
            airports=[
                str(airport["icao_code"])
                for airport in selected_airports
            ],
            operations=selected_operations,
            planned_calls=len(tasks),
            planned_days=(
                len(selected_dates)
                * len(selected_airports)
                * len(selected_operations)
            ),
            skipped_completed_days=skipped_completed_days,
            estimated_credits=(
                len(tasks)
                * ESTIMATED_HISTORICAL_REQUEST_CREDITS
            ),
        )

        _emit(
            progress_callback,
            event="import_started",
            window_days=window_days,
            credit_reserve=credit_reserve,
            summary=summary.to_dict(),
        )

        last_credits_remaining: int | None = None

        for task in tasks:
            minimum_to_start = (
                ESTIMATED_HISTORICAL_REQUEST_CREDITS
                + credit_reserve
            )

            if (
                last_credits_remaining is not None
                and last_credits_remaining < minimum_to_start
            ):
                summary.stopped_for_credit_reserve = True
                summary.credits_remaining = last_credits_remaining

                _emit(
                    progress_callback,
                    event="credit_reserve_reached",
                    credits_remaining=last_credits_remaining,
                    credit_reserve=credit_reserve,
                    estimated_next_request=(
                        ESTIMATED_HISTORICAL_REQUEST_CREDITS
                    ),
                )
                break

            endpoint = OPERATION_ENDPOINTS[task.operation]
            progress_base = {
                "date": task.date_label,
                "start_date": task.start_date.isoformat(),
                "end_date": task.end_date.isoformat(),
                "days": task.days_count,
                "airport": task.airport_code,
                "operation": task.operation,
            }

            import_id = start_import_log(
                selected_engine,
                airport_id=task.airport_id,
                event_type_id=task.event_type_id,
                endpoint=endpoint,
                period_begin_utc=task.period_begin_utc,
                period_end_utc=task.period_end_utc,
            )

            summary.api_calls += 1
            summary.days_requested += task.days_count

            _emit(
                progress_callback,
                event="call_started",
                import_id=import_id,
                **progress_base,
            )

            try:
                api_result = selected_client.get_airport_flights(
                    airport=task.airport_code,
                    operation=task.operation,
                    begin_timestamp=int(
                        task.period_begin_utc.timestamp()
                    ),
                    end_timestamp=int(
                        task.period_end_utc.timestamp()
                    ),
                )

                last_credits_remaining = (
                    api_result.credits_remaining
                )
                summary.credits_remaining = last_credits_remaining
                summary.records_received += len(api_result.records)

                if not api_result.records:
                    finish_import_log(
                        selected_engine,
                        import_id=import_id,
                        status="NO_DATA",
                        http_status=api_result.status_code,
                        records_received=0,
                        records_inserted=0,
                        records_skipped=0,
                        credits_remaining=(
                            api_result.credits_remaining
                        ),
                        retry_count=api_result.retry_count,
                    )

                    summary.no_data_calls += 1
                    _emit(
                        progress_callback,
                        event="call_finished",
                        import_id=import_id,
                        status="NO_DATA",
                        records_received=0,
                        records_inserted=0,
                        records_skipped=0,
                        credits_remaining=(
                            api_result.credits_remaining
                        ),
                        **progress_base,
                    )
                else:
                    normalized_flights: list[
                        NormalizedFlight
                    ] = []
                    validation_errors: list[str] = []

                    for index, record in enumerate(
                        api_result.records,
                        start=1,
                    ):
                        try:
                            normalized_flights.append(
                                normalize_flight_record(record)
                            )
                        except (
                            TypeError,
                            ValueError,
                            OverflowError,
                            OSError,
                        ) as error:
                            validation_errors.append(
                                f"rekord {index}: {error}"
                            )

                    inserted, duplicates = save_flights(
                        selected_engine,
                        import_id=import_id,
                        monitored_airport_id=task.airport_id,
                        event_type_id=task.event_type_id,
                        operation=task.operation,
                        flights=normalized_flights,
                    )

                    invalid_count = len(validation_errors)
                    skipped_count = duplicates + invalid_count
                    status = (
                        "PARTIAL"
                        if invalid_count > 0
                        else "SUCCESS"
                    )
                    error_message = (
                        "; ".join(validation_errors[:10])
                        if validation_errors
                        else None
                    )

                    finish_import_log(
                        selected_engine,
                        import_id=import_id,
                        status=status,
                        http_status=api_result.status_code,
                        records_received=len(api_result.records),
                        records_inserted=inserted,
                        records_skipped=skipped_count,
                        credits_remaining=(
                            api_result.credits_remaining
                        ),
                        retry_count=api_result.retry_count,
                        error_message=error_message,
                    )

                    summary.records_inserted += inserted
                    summary.records_skipped += skipped_count

                    if status == "PARTIAL":
                        summary.partial_calls += 1
                    else:
                        summary.successful_calls += 1

                    _emit(
                        progress_callback,
                        event="call_finished",
                        import_id=import_id,
                        status=status,
                        records_received=len(api_result.records),
                        records_inserted=inserted,
                        records_skipped=skipped_count,
                        credits_remaining=(
                            api_result.credits_remaining
                        ),
                        **progress_base,
                    )

            except Exception as error:
                status_code = getattr(
                    error,
                    "status_code",
                    None,
                )
                credits_remaining = getattr(
                    error,
                    "credits_remaining",
                    None,
                )
                retry_count = int(
                    getattr(error, "retry_count", 0)
                )

                if credits_remaining is not None:
                    last_credits_remaining = credits_remaining
                    summary.credits_remaining = credits_remaining

                finish_import_log(
                    selected_engine,
                    import_id=import_id,
                    status="FAILED",
                    http_status=status_code,
                    records_received=0,
                    records_inserted=0,
                    records_skipped=0,
                    credits_remaining=credits_remaining,
                    retry_count=retry_count,
                    error_message=(
                        f"{type(error).__name__}: {error}"
                    ),
                )

                summary.failed_calls += 1

                _emit(
                    progress_callback,
                    event="call_failed",
                    import_id=import_id,
                    status="FAILED",
                    error=f"{type(error).__name__}: {error}",
                    **progress_base,
                )

                # Po 429 kolejne żądania nie mają sensu do czasu
                # odnowienia puli kredytów.
                if status_code == 429:
                    summary.stopped_for_credit_reserve = True
                    break

                if not continue_on_error:
                    raise

            finally:
                if request_delay_seconds > 0:
                    time.sleep(request_delay_seconds)

        _emit(
            progress_callback,
            event="import_finished",
            summary=summary.to_dict(),
        )

        return summary

    finally:
        if owns_client:
            selected_client.close()

        if owns_engine:
            selected_engine.dispose()


# CLI
def console_progress(event: dict[str, Any]) -> None:
    event_name = event.get("event")

    if event_name == "import_started":
        summary = event["summary"]
        print(
            "Rozpoczynam import: "
            f"{summary['start_date']} – {summary['end_date']}, "
            f"{summary['planned_calls']} planowanych wywołań, "
            f"szacunkowo {summary['estimated_credits']} kredytów."
        )
        return

    if event_name == "call_started":
        print(
            f"[START] {event['date']} | {event['airport']} | "
            f"{event['operation']}"
        )
        return

    if event_name == "call_skipped":
        print(
            f"[POMINIĘTO] {event['date']} | {event['airport']} | "
            f"{event['operation']} | import już wykonany"
        )
        return

    if event_name == "call_finished":
        print(
            f"[{event['status']}] {event['date']} | "
            f"{event['airport']} | {event['operation']} | "
            f"pobrano={event['records_received']} | "
            f"dodano={event['records_inserted']} | "
            f"pominięto={event['records_skipped']} | "
            f"kredyty={event['credits_remaining']}"
        )
        return

    if event_name == "call_failed":
        print(
            f"[FAILED] {event['date']} | {event['airport']} | "
            f"{event['operation']} | {event['error']}"
        )
        return

    if event_name == "credit_reserve_reached":
        print(
            "[STOP KREDYTY] Pozostało "
            f"{event['credits_remaining']} kredytów. "
            f"Rezerwa: {event['credit_reserve']}; "
            "import można wznowić po odnowieniu puli."
        )
        return

    if event_name == "import_finished":
        summary = event["summary"]
        print()
        print("=" * 68)
        print("IMPORT ZAKOŃCZONY")
        print(f"Wywołania API: {summary['api_calls']}")
        print(f"Dni objęte żądaniami: {summary['days_requested']}")
        print(
            "Pominięte, już zaimportowane dni: "
            f"{summary['skipped_completed_days']}"
        )
        print(
            "Szacowany koszt planu: "
            f"{summary['estimated_credits']} kredytów"
        )
        print(
            "Pozostałe kredyty: "
            f"{summary['credits_remaining']}"
        )
        print(f"SUCCESS: {summary['successful_calls']}")
        print(f"NO_DATA: {summary['no_data_calls']}")
        print(f"PARTIAL: {summary['partial_calls']}")
        print(f"FAILED: {summary['failed_calls']}")
        print(f"Pobrane rekordy: {summary['records_received']}")
        print(f"Nowe zdarzenia: {summary['records_inserted']}")
        print(f"Pominięte rekordy: {summary['records_skipped']}")
        print("=" * 68)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Import historycznych przylotów i odlotów OpenSky "
            "do PostgreSQL."
        )
    )

    parser.add_argument(
        "--start-date",
        required=True,
        help="Pierwsza data importu, format YYYY-MM-DD.",
    )
    parser.add_argument(
        "--end-date",
        required=True,
        help="Ostatnia data importu, format YYYY-MM-DD (włącznie).",
    )
    parser.add_argument(
        "--airports",
        nargs="+",
        help=(
            "Opcjonalna lista kodów ICAO, np. EPWA EPKK. "
            "Brak parametru oznacza wszystkie monitorowane lotniska."
        ),
    )
    parser.add_argument(
        "--operations",
        nargs="+",
        default=list(SUPPORTED_OPERATIONS),
        help=(
            "ARRIVAL i/lub DEPARTURE. "
            "Domyślnie importowane są oba typy."
        ),
    )
    parser.add_argument(
        "--credentials",
        help="Ścieżka do pliku credentials.json OpenSky.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Ponownie pobiera także okresy zakończone wcześniej "
            "statusem SUCCESS lub NO_DATA."
        ),
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_REQUEST_DELAY_SECONDS,
        help=(
            "Przerwa pomiędzy wywołaniami API w sekundach. "
            f"Domyślnie {DEFAULT_REQUEST_DELAY_SECONDS}."
        ),
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help=f"Maksymalna liczba ponowień. Domyślnie {DEFAULT_MAX_RETRIES}.",
    )
    parser.add_argument(
        "--max-rate-limit-wait",
        type=int,
        default=DEFAULT_MAX_RATE_LIMIT_WAIT_SECONDS,
        help=(
            "Maksymalny czas automatycznego oczekiwania po HTTP 429. "
            f"Domyślnie {DEFAULT_MAX_RATE_LIMIT_WAIT_SECONDS} sekund."
        ),
    )
    parser.add_argument(
        "--window-days",
        type=int,
        choices=range(1, MAX_WINDOW_DAYS + 1),
        default=DEFAULT_WINDOW_DAYS,
        metavar="{1,2}",
        help=(
            "Liczba dni w jednym żądaniu. Domyślnie 2, "
            "co zwykle daje około 2 razy więcej danych "
            "za tę samą liczbę kredytów."
        ),
    )
    parser.add_argument(
        "--credit-reserve",
        type=int,
        default=DEFAULT_CREDIT_RESERVE,
        help=(
            "Liczba kredytów pozostawiana w puli. "
            f"Domyślnie {DEFAULT_CREDIT_RESERVE}."
        ),
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Kończy cały import po pierwszym błędzie.",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    try:
        summary = import_flights(
            start_date=args.start_date,
            end_date=args.end_date,
            airports=args.airports,
            operations=args.operations,
            credentials_path=args.credentials,
            force=args.force,
            request_delay_seconds=args.delay,
            continue_on_error=not args.stop_on_error,
            max_retries=args.max_retries,
            max_rate_limit_wait_seconds=args.max_rate_limit_wait,
            window_days=args.window_days,
            credit_reserve=args.credit_reserve,
            progress_callback=console_progress,
        )
    except Exception as error:
        print(f"[BŁĄD KRYTYCZNY] {type(error).__name__}: {error}")
        return 1

    return 1 if summary.failed_calls > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
