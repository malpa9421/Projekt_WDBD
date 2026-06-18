from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine, URL
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from config import (
    DB_ADMIN_DATABASE,
    DB_DATABASE as TARGET_DATABASE,
    DB_HOST,
    DB_PASSWORD,
    DB_PORT,
    DB_USERNAME,
)


# Istniejąca baza administracyjna używana wyłącznie do CREATE DATABASE.
ADMIN_DATABASE = DB_ADMIN_DATABASE

# LISTA POLSKICH LOTNISK
POLISH_AIRPORTS: list[tuple[str, str, str]] = [
    ("EPBA", "Aleksandrowice k. Bielska-Białej", "Bielsko-Biała"),
    ("EPPK", "Poznań/Kobylnica", "Kobylnica"),
    ("EPLR", "Radawiec k. Lublina", "Radawiec"),
    ("EPKM", "Katowice Muchowiec", "Katowice"),
    ("EPGI", "Lisie Kąty k. Grudziądza", "Lisie Kąty"),
    ("EPOM", "Michałków k. Ostrowa Wielkopolskiego", "Ostrów Wielkopolski"),
    ("EPIN", "Inowrocław", "Inowrocław"),
    ("EPLS", "Leszno", "Leszno"),
    ("EPZR", "Żar k. Żywca", "Żar"),
    ("EPJG", "Jelenia Góra", "Jelenia Góra"),
    ("EPRG", "Gotartowice k. Rybnika", "Rybnik"),
    ("EPML", "Mielec", "Mielec"),
    ("EPLU", "Lubin", "Lubin"),
    ("EPSW", "Świdnik", "Świdnik"),
    ("EPNL", "Łososina Dolna k. Nowego Sącza", "Łososina Dolna"),
    ("EPZP", "Przylep k. Zielonej Góry", "Przylep"),
    ("EPGL", "Gliwice", "Gliwice"),
    ("EPJS", "Jeżów Sudecki k. Jeleniej Góry", "Jeżów Sudecki"),
    ("EPKR", "Krosno", "Krosno"),
    ("EPPT", "Piotrków Trybunalski", "Piotrków Trybunalski"),
    ("EPKA", "Masłów k. Kielc", "Masłów"),
    ("EPKP", "Pobiednik k. Krakowa", "Pobiednik Wielki"),
    ("EPPL", "Płock", "Płock"),
    ("EPWK", "Kruszyn k. Włocławka", "Kruszyn"),
    ("EPST", "Turbia k. Stalowej Woli", "Turbia"),
    ("EPBK", "Białystok Krywlany", "Białystok"),
    ("EPOD", "Dajtki k. Olsztyna", "Olsztyn"),
    ("EPRP", "Piastów k. Radomia", "Piastów"),
    ("EPNT", "Nowy Targ", "Nowy Targ"),
    ("EPOP", "Polska Nowa Wieś k. Opola", "Polska Nowa Wieś"),
    ("EPLL", "Łódź", "Łódź"),
    ("EPSD", "Szczecin Dąbie", "Szczecin"),
    ("EPWA", "Lotnisko Chopina w Warszawie", "Warszawa"),
    ("EPPO", "Poznań-Ławica", "Poznań"),
    ("EPTO", "Toruń", "Toruń"),
    ("EPGD", "Gdańsk im. Lecha Wałęsy", "Gdańsk"),
    ("EPKE", "Kętrzyn", "Kętrzyn"),
    ("EPEL", "Elbląg", "Elbląg"),
    ("EPSU", "Suwałki", "Suwałki"),
    ("EPRZ", "Rzeszów-Jasionka", "Rzeszów"),
    ("EPSC", "Szczecin-Goleniów", "Goleniów"),
    ("EPZA", "Zamość", "Zamość"),
    ("EPSK", "Krępa k. Słupska", "Krępa Słupska"),
    ("EPRJ", "Rzeszów", "Rzeszów"),
    ("EPKT", "Katowice-Pyrzowice", "Pyrzowice"),
    ("EPBY", "Bydgoszcz", "Bydgoszcz"),
    ("EPKK", "Kraków-Balice", "Kraków"),
    ("EPWR", "Wrocław-Strachowice", "Wrocław"),
    ("EPZG", "Zielona Góra-Babimost", "Babimost"),
    ("EPSY", "Olsztyn-Mazury", "Szymany"),
    ("EPMO", "Warszawa/Modlin", "Nowy Dwór Mazowiecki"),
    ("EPKG", "Bagicz k. Kołobrzegu", "Bagicz"),
    ("EPBC", "Warszawa-Babice", "Warszawa"),
    ("EPLB", "Lublin", "Świdnik"),
    ("EPRA", "Warszawa-Radom", "Radom"),
    ("EPKW", "Kaniów", "Kaniów"),
    ("EPCD", "Depułtycze Królewskie", "Chełm"),
    ("EPRU", "Rudniki k. Częstochowy", "Rudniki"),
    ("EPSA", "Sanok - Baza LPR", "Sanok"),
    ("EPZE", "Żerniki", "Żerniki"),
    ("EPKH", "Koszalin Baza LPR", "Koszalin"),
    ("EPPB", "Poznań-Bednary", "Bednary"),
    ("EPPG", "Kąkolewo", "Kąkolewo"),
    ("EPMR", "Mirosławice", "Mirosławice"),
    ("EPBH", "Bydgoszcz Baza LPR", "Bydgoszcz"),
    ("EPKX", "Kraków Baza LPR", "Kraków"),]


class Base(DeclarativeBase):
    pass


class Airport(Base):
    __tablename__ = "airport"

    airport_id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(),
        primary_key=True,
    )
    icao_code: Mapped[str] = mapped_column(
        String(4),
        nullable=False,
        unique=True,
    )
    airport_name: Mapped[str | None] = mapped_column(String(150))
    city: Mapped[str | None] = mapped_column(String(100))
    country_code: Mapped[str | None] = mapped_column(String(2))
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    is_monitored: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("FALSE"),)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(),)

    __table_args__ = (
        CheckConstraint(
            "icao_code ~ '^[A-Z0-9]{4}$'",
            name="ck_airport_icao_code",
        ),
        CheckConstraint(
            "country_code IS NULL OR country_code ~ '^[A-Z]{2}$'",
            name="ck_airport_country_code",
        ),
        CheckConstraint(
            "latitude IS NULL OR latitude BETWEEN -90 AND 90",
            name="ck_airport_latitude",
        ),
        CheckConstraint(
            "longitude IS NULL OR longitude BETWEEN -180 AND 180",
            name="ck_airport_longitude",
        ),
        Index("idx_airport_is_monitored", "is_monitored"),
    )


class EventType(Base):
    __tablename__ = "event_type"

    event_type_id: Mapped[int] = mapped_column(
        SmallInteger,
        Identity(),
        primary_key=True,
    )
    event_type_code: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        unique=True,
    )
    event_type_name: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "event_type_code IN ('ARRIVAL', 'DEPARTURE')",
            name="ck_event_type_code",
        ),
    )


class ImportLog(Base):
    __tablename__ = "import_log"

    import_id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(),
        primary_key=True,
    )
    airport_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "airport.airport_id",
            onupdate="CASCADE",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    event_type_id: Mapped[int] = mapped_column(
        SmallInteger,
        ForeignKey(
            "event_type.event_type_id",
            onupdate="CASCADE",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    endpoint: Mapped[str] = mapped_column(String(200), nullable=False)
    period_begin_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    period_end_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    http_status: Mapped[int | None] = mapped_column(SmallInteger)
    records_received: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    records_inserted: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    records_skipped: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    credits_remaining: Mapped[int | None] = mapped_column(Integer)
    retry_count: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="RUNNING",
        server_default=text("'RUNNING'"),
    )
    error_message: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "period_end_utc > period_begin_utc",
            name="ck_import_log_period",
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_import_log_finished_at",
        ),
        CheckConstraint(
            "http_status IS NULL OR http_status BETWEEN 100 AND 599",
            name="ck_import_log_http_status",
        ),
        CheckConstraint(
            "records_received >= 0",
            name="ck_import_log_records_received",
        ),
        CheckConstraint(
            "records_inserted >= 0",
            name="ck_import_log_records_inserted",
        ),
        CheckConstraint(
            "records_skipped >= 0",
            name="ck_import_log_records_skipped",
        ),
        CheckConstraint(
            "credits_remaining IS NULL OR credits_remaining >= 0",
            name="ck_import_log_credits_remaining",
        ),
        CheckConstraint(
            "retry_count >= 0",
            name="ck_import_log_retry_count",
        ),
        CheckConstraint(
            """
            status IN (
                'RUNNING',
                'SUCCESS',
                'PARTIAL',
                'FAILED',
                'NO_DATA'
            )
            """,
            name="ck_import_log_status",
        ),
        Index("idx_import_log_started_at", "started_at"),
        Index("idx_import_log_status", "status"),
        Index(
            "idx_import_log_airport_period",
            "airport_id",
            "period_begin_utc",
            "period_end_utc",
        ),
    )


class Aircraft(Base):
    __tablename__ = "aircraft"

    aircraft_id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(),
        primary_key=True,
    )
    icao24: Mapped[str] = mapped_column(
        String(6),
        nullable=False,
        unique=True,
    )
    first_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "icao24 ~ '^[0-9a-f]{6}$'",
            name="ck_aircraft_icao24",
        ),
        CheckConstraint(
            """
            first_observed_at IS NULL
            OR last_observed_at IS NULL
            OR last_observed_at >= first_observed_at
            """,
            name="ck_aircraft_observation_time",
        ),
        Index("idx_aircraft_last_observed", "last_observed_at"),
    )


class Flight(Base):
    __tablename__ = "flight"

    flight_id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(),
        primary_key=True,
    )
    aircraft_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "aircraft.aircraft_id",
            onupdate="CASCADE",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    callsign: Mapped[str | None] = mapped_column(String(16))
    first_seen_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    last_seen_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    estimated_departure_airport_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "airport.airport_id",
            onupdate="CASCADE",
            ondelete="RESTRICT",
        ),
    )
    estimated_arrival_airport_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "airport.airport_id",
            onupdate="CASCADE",
            ondelete="RESTRICT",
        ),
    )
    departure_candidates_count: Mapped[int | None] = mapped_column(Integer)
    arrival_candidates_count: Mapped[int | None] = mapped_column(Integer)
    first_import_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "import_log.import_id",
            onupdate="CASCADE",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "aircraft_id",
            "first_seen_utc",
            "last_seen_utc",
            name="uq_flight_aircraft_time",
        ),
        CheckConstraint(
            "last_seen_utc >= first_seen_utc",
            name="ck_flight_time",
        ),
        CheckConstraint(
            """
            departure_candidates_count IS NULL
            OR departure_candidates_count >= 0
            """,
            name="ck_flight_departure_candidates",
        ),
        CheckConstraint(
            """
            arrival_candidates_count IS NULL
            OR arrival_candidates_count >= 0
            """,
            name="ck_flight_arrival_candidates",
        ),
        Index("idx_flight_first_seen", "first_seen_utc"),
        Index("idx_flight_last_seen", "last_seen_utc"),
        Index("idx_flight_callsign", "callsign"),
        Index(
            "idx_flight_departure_airport",
            "estimated_departure_airport_id",
        ),
        Index(
            "idx_flight_arrival_airport",
            "estimated_arrival_airport_id",
        ),
        Index(
            "idx_flight_route",
            "estimated_departure_airport_id",
            "estimated_arrival_airport_id",
        ),
    )


class AirportEvent(Base):
    __tablename__ = "airport_event"

    airport_event_id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(),
        primary_key=True,
    )
    flight_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "flight.flight_id",
            onupdate="CASCADE",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    airport_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "airport.airport_id",
            onupdate="CASCADE",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    event_type_id: Mapped[int] = mapped_column(
        SmallInteger,
        ForeignKey(
            "event_type.event_type_id",
            onupdate="CASCADE",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    import_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "import_log.import_id",
            onupdate="CASCADE",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    event_time_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "flight_id",
            "airport_id",
            "event_type_id",
            name="uq_airport_event",
        ),
        Index("idx_airport_event_time", "event_time_utc"),
        Index(
            "idx_airport_event_airport_time",
            "airport_id",
            "event_time_utc",
        ),
        Index(
            "idx_airport_event_type_time",
            "event_type_id",
            "event_time_utc",
        ),
    )


def make_url(database_name: str) -> URL:
    """Tworzy URL SQLAlchemy i poprawnie obsługuje znak @ w haśle."""
    return URL.create(
        drivername="postgresql+psycopg2",
        username=DB_USERNAME,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
        database=database_name,
    )


def validate_database_name(database_name: str) -> None:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", database_name):
        raise ValueError(
            "Nazwa bazy może zawierać tylko litery, cyfry i znak _. "
            "Nie może zaczynać się od cyfry."
        )


def create_database_if_missing() -> None:
    validate_database_name(TARGET_DATABASE)

    admin_engine = create_engine(
        make_url(ADMIN_DATABASE),
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=True,
    )

    try:
        with admin_engine.connect() as connection:
            exists = connection.scalar(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM pg_database
                        WHERE datname = :database_name
                    )
                    """
                ),
                {"database_name": TARGET_DATABASE},
            )

            if exists:
                print(f"[OK] Baza '{TARGET_DATABASE}' już istnieje.")
                return

            connection.exec_driver_sql(
                f'CREATE DATABASE "{TARGET_DATABASE}" '
                "WITH ENCODING 'UTF8' TEMPLATE template0"
            )
            print(f"[OK] Utworzono bazę '{TARGET_DATABASE}'.")

    finally:
        admin_engine.dispose()


def seed_event_types(engine: Engine) -> None:
    values = [
        {
            "event_type_code": "ARRIVAL",
            "event_type_name": "Przylot",
        },
        {
            "event_type_code": "DEPARTURE",
            "event_type_name": "Odlot",
        },
    ]

    statement = insert(EventType).values(values)
    statement = statement.on_conflict_do_nothing(
        index_elements=[EventType.event_type_code]
    )

    with engine.begin() as connection:
        connection.execute(statement)

    print("[OK] Sprawdzono podstawowe typy operacji.")


def seed_airports(engine: Engine) -> None:
    values = [
        {
            "icao_code": icao_code,
            "airport_name": airport_name,
            "city": city,
            "country_code": "PL",
            "is_monitored": True,
        }
        for icao_code, airport_name, city in POLISH_AIRPORTS
    ]

    statement = insert(Airport).values(values)
    statement = statement.on_conflict_do_nothing(
        index_elements=[Airport.icao_code]
    )

    with engine.begin() as connection:
        connection.execute(statement)

    print(
        f"[OK] Sprawdzono dane {len(POLISH_AIRPORTS)} polskich lotnisk."
    )


def print_summary(engine: Engine) -> None:
    with engine.connect() as connection:
        table_names = connection.execute(
            text(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_type = 'BASE TABLE'
                ORDER BY table_name
                """
            )
        ).scalars().all()

        airport_count = connection.scalar(
            text("SELECT COUNT(*) FROM airport")
        )
        event_type_count = connection.scalar(
            text("SELECT COUNT(*) FROM event_type")
        )

    print()
    print("=" * 60)
    print("Baza została przygotowana.")
    print(f"Baza: {TARGET_DATABASE}")
    print(f"Serwer: {DB_HOST}:{DB_PORT}")
    print(f"Tabele: {', '.join(table_names)}")
    print(f"Liczba lotnisk: {airport_count}")
    print(f"Liczba typów operacji: {event_type_count}")
    print("=" * 60)


def main() -> None:
    print("Przygotowywanie bazy OpenSky...")

    create_database_if_missing()

    target_engine = create_engine(
        make_url(TARGET_DATABASE),
        pool_pre_ping=True,
        echo=False,
    )

    try:
        # create_all tworzy wyłącznie brakujące tabele i indeksy.
        Base.metadata.create_all(target_engine)
        print("[OK] Sprawdzono tabele i indeksy.")

        # Dane są dodawane tylko wtedy, gdy brak rekordu o danym kodzie.
        seed_event_types(target_engine)
        seed_airports(target_engine)

        print_summary(target_engine)

    except Exception as error:
        print()
        print("[BŁĄD] Nie udało się przygotować bazy.")
        print(f"Typ: {type(error).__name__}")
        print(f"Treść: {error}")
        raise

    finally:
        target_engine.dispose()


if __name__ == "__main__":
    main()
