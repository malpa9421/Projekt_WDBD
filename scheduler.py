import time
from datetime import datetime, timedelta
from import_data_all import import_flights

while True:
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    
    try:
        import_flights(start_date=yesterday, end_date=yesterday)
        print(f"[{datetime.now()}] Import zakończony sukcesem. Następne uruchomienie za 24h.")
    except Exception as e:
        print(f"[{datetime.now()}] Błąd podczas automatycznego importu: {e}")

    time.sleep(24 * 60 * 60)