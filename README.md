# Flight Tracker — System Monitorowania Ruchu Lotniczego
 
Aplikacja do śledzenia lotów w Polsce. Pobiera dane historyczne i bieżące z API OpenSky Network, przechowuje je w bazie PostgreSQL i wyświetla na interaktywnej mapie.
 
---
 
## Stos technologiczny
 
| Warstwa | Technologia |
|---|---|
| Interfejs i wizualizacja | PySide6 (Qt), Matplotlib |
| Baza danych | PostgreSQL |
| Komunikacja z bazą | SQLAlchemy, pandas |
| Zarządzanie środowiskiem | uv |
| Źródło danych | OpenSky Network API |
 
---
 
## Wymagania
 
- Python 3.14+
- PostgreSQL 14+
- [uv](https://docs.astral.sh/uv/)
- Konto w serwisie [OpenSky Network](https://opensky-network.org)
 - Git
---
 
## Instalacja
### 1. Pobranie repozytorium
```bash
git clone https://github.com/malpa9421/Projekt_WDBD.git
cd Projekt_WDBD
```
### 2. Przygotowanie środowiska

```bash
uv sync
```

## Konfiguracja
 
Utwórz plik `.env` w katalogu głównym projektu:
 
```dotenv
DB_HOST=localhost
DB_PORT=5432
DB_USERNAME=UsernameSerweraPostgres
DB_PASSWORD=TwojeHaslo
DB_DATABASE=NazwaBazy
DB_ADMIN_DATABASE=postgres
```
 
Następnie pobierz poświadczenia API ze strony [opensky-network.org](https://opensky-network.org) i umieść plik `credentials.json` w katalogu głównym projektu.
 
### Inicjalizacja bazy danych
 
```bash
uv run create_database.py
uv run create_views.py
```
 
### Wstępny import danych
 
```bash
uv run import_data_all.py --start-date YYYY-MM-DD --end-date YYYY-MM-DD
```
 
> **Uwaga:** Import jednego dnia kosztuje ~360 kredytów API. Dzienny limit dla konta standardowego wynosi 4 000 kredytów, co pozwala na import maksymalnie 11 dni wstecz w ciągu jednej doby.
 
## Uruchomienie aplikacji
 
```bash
uv run main.py
```
 
### Automatyczna synchronizacja danych
  
```bash
uv run scheduler.py
```
 
Skrypt odświeża bazę danych co 24 godziny. Musi pozostać uruchomiony przez cały czas, w którym ma odbywać się automatyczna aktualizacja.
