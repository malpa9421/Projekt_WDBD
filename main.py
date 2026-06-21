from PySide6.QtWidgets import QApplication, QMainWindow, QWidget, QPushButton, QStackedWidget, QLabel, QVBoxLayout, QHBoxLayout, QMenu, QTableView, QComboBox, QHeaderView, QSizePolicy, QLineEdit, QDateEdit, QTimeEdit, QFormLayout, QSpinBox
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtCore import QUrl, QDate, QTime
from PySide6.QtWebEngineCore import QWebEngineSettings
from qt_material import apply_stylesheet
from Qt_df_model import *
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
import pandas as pd
import sys
from pathlib import Path

from analytical_queries import create_engine_for_database, traffic_by_airport, arrival_by_airport, departure_by_airport, list_monitored_airports, search_flights


class TrafficChart(FigureCanvasQTAgg):
    def __init__(self, engine):
        self.engine = engine

        fig = Figure(figsize=(8, 5))
        self.ax = fig.add_subplot(111)

        super().__init__(fig)

        self.ax.set_title("Ruch lotniczy")
        self.ax.set_xlabel("Lotnisko")
        self.ax.set_ylabel("Operacje")

        self.load_data()
    
    def load_data(self):
        df = traffic_by_airport(self.engine)

        self.ax.clear()

        x = df["airport_code"]
        arrivals = df["arrivals"]
        departures = df["departures"]

        self.ax.bar(x, arrivals, label="Przyloty")

        self.ax.bar(x, departures, bottom=arrivals, label="Odloty")

        self.ax.set_title("Ruch lotniczy wg lotniska")
        self.ax.set_xlabel("Lotnisko")
        self.ax.set_ylabel("Liczba operacji")

        totals = df["total_operations"]
        for i, total in enumerate(totals):
            self.ax.text(
                i,
                total,
                str(total),
                ha="center",
                va="bottom"
            )

        self.ax.legend()

        self.figure.tight_layout()
        self.draw()

def create_flight_table(title: str) -> tuple[QVBoxLayout, QTableView]:
    column_layout = QVBoxLayout()
    
    label = QLabel(title)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setStyleSheet("font-weight: bold; font-size: 14px;")
    column_layout.addWidget(label)
    
    
    table = QTableView()
    table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    header = table.horizontalHeader()
    
    
    header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    
    header.setStretchLastSection(True)
    
    column_layout.addWidget(table)
    return column_layout, table

class MainWindow(QMainWindow):
    def setup_table_columns(table: QTableView) -> None:
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        last_column = table.model().columnCount() - 1
        header.setSectionResizeMode(last_column, QHeaderView.ResizeMode.Stretch)
    
    def on_search_clicked(self) -> None:
        monitored_airport = None
        monitored_text = self.monitored_airport_input.currentText()
        if monitored_text != "Wszystkie":
            monitored_airport = self.airports_lookup.get(monitored_text)

        event_type_map = {"Wszystkie": None, "Przylot": "ARRIVAL", "Odlot": "DEPARTURE"}
        event_type = event_type_map.get(self.event_type_input.currentText())

        search_results = search_flights(
            self.engine,
            departure_airport=self.departure_airport_input.text().strip() or None,
            arrival_airport=self.arrival_airport_input.text().strip() or None,
            callsign=self.flight_number_input.text().strip() or None,
            monitored_airport=monitored_airport,
            event_type=event_type,
            start_date=self.departure_date_from.date().toString("yyyy-MM-dd"),
            end_date=self.departure_date_to.date().toString("yyyy-MM-dd"),
            start_time=self.departure_time_from.time().toString("HH:mm:ss"),
            end_time=self.departure_time_to.time().toString("HH:mm:ss"),
        )

        self.search_results.setModel(PandasModel(search_results))
    
    def __init__(self):
        apply_stylesheet(app, theme='dark_teal.xml')
        app.setStyleSheet(app.styleSheet() + """
        QHeaderView::section {
            text-transform: none;
            }
        """)
        super().__init__()

        #engine dla całego gui, usuwany przy zamknięciu
        self.engine = create_engine_for_database()

        self.setWindowTitle("Flight Tracker")

        central = QWidget()
        main_layout = QVBoxLayout(central)

        #górne menu
        topbar= QHBoxLayout()
        btn_map = QPushButton("Mapa")
        btn_history = QPushButton("Historia lotów")
        btn_settings = QPushButton("Wykres")
        btn_search = QPushButton("Wyszukiwarka")
        topbar.addWidget(btn_map)
        topbar.addWidget(btn_history)
        topbar.addWidget(btn_settings)
        topbar.addWidget(btn_search)
        

        #mapa + filtry
        map_page = QWidget()
        map_page_layout = QHBoxLayout(map_page)
        map_page_layout.setContentsMargins(10, 10, 10, 10)
        
        #wykres
        analytics_page = QWidget()
        analytics_layout = QVBoxLayout(analytics_page)
        analytics_layout.addWidget(TrafficChart(self.engine))

        sidebar = QWidget()
        sidebar_layout = QVBoxLayout(sidebar)
        action_group = QActionGroup(self)
        sidebar_layout.addStretch()

        self.web_view = QWebEngineView()
        html_path = Path(__file__).parent / "map/map.html"
        
        settings = self.web_view.settings()
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True
        )
        self.web_view.load(QUrl.fromLocalFile(str(html_path)))
        

        map_page_layout.addWidget(sidebar, stretch=1)
        map_page_layout.addWidget(self.web_view, stretch=4)


        
        hist = QWidget()
        hist_layout = QVBoxLayout(hist)
        
        # 1. Lista rozwijana z lotniskami
        self.airport_selector = QComboBox()
        airports_df = list_monitored_airports(self.engine)
        
        
        self.airports_lookup = dict(
            zip(airports_df["monitored_airport_name"], airports_df["monitored_airport_code"])
        )
        self.airport_selector.addItems(airports_df["monitored_airport_name"].tolist())
        self.airport_selector.currentTextChanged.connect(self.on_airport_changed)
        hist_layout.addWidget(self.airport_selector)
        
        
        tables_layout = QHBoxLayout()
        
        self.airport = (
            airports_df["monitored_airport_code"].iloc[0]
            if not airports_df.empty
            else "EPWA"
        )
        
        arrivals_layout, self.arrivals = create_flight_table("Przyloty")
        departures_layout, self.departures = create_flight_table("Odloty")

        # Ustawienie danych startowych
        self.arrivals.setModel(PandasModel(arrival_by_airport(self.engine, self.airport)))
        self.departures.setModel(PandasModel(departure_by_airport(self.engine, self.airport)))

        # Dodanie do głównego układu
        tables_layout.addLayout(arrivals_layout, 1)
        tables_layout.addLayout(departures_layout, 1)
        hist_layout.addLayout(tables_layout)
        
        # strona wyszukiwarki
        search_page = QWidget()
        search_layout = QHBoxLayout(search_page)
        search_layout.setContentsMargins(10, 10, 10, 10)

        filter_panel = QWidget()
        filter_panel.setMaximumWidth(340)
        filter_layout = QVBoxLayout(filter_panel)
        filter_layout.setSpacing(10)

        filter_layout.addWidget(QLabel("Filtry wyszukiwania"))
        form_layout = QFormLayout()
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.departure_airport_input = QLineEdit()
        self.arrival_airport_input = QLineEdit()
        self.flight_number_input = QLineEdit()
        self.monitored_airport_input = QComboBox()
        self.monitored_airport_input.addItem("Wszystkie")
        self.monitored_airport_input.addItems(airports_df["monitored_airport_name"].tolist())

        self.event_type_input = QComboBox()
        self.event_type_input.addItems(["Wszystkie", "Przylot", "Odlot"])

        self.departure_date_from = QDateEdit()
        self.departure_date_from.setCalendarPopup(True)
        self.departure_date_to = QDateEdit()
        self.departure_date_to.setCalendarPopup(True)
        self.departure_date_to.setDate(QDate.currentDate())
        self.departure_time_from = QTimeEdit()
        self.departure_time_to = QTimeEdit()
        self.departure_time_to.setTime(QTime(23, 59))

        self.arrival_date_from = QDateEdit()
        self.arrival_date_from.setCalendarPopup(True)
        self.arrival_date_to = QDateEdit()
        self.arrival_date_to.setCalendarPopup(True)
        self.arrival_date_to.setDate(QDate.currentDate())
        self.arrival_time_from = QTimeEdit()
        self.arrival_time_to = QTimeEdit()
        self.arrival_time_to.setTime(QTime(23, 59))

        self.min_duration_spin = QSpinBox()
        self.min_duration_spin.setRange(0, 2000)
        self.max_duration_spin = QSpinBox()
        self.max_duration_spin.setRange(0, 2000)
        self.max_duration_spin.setValue(2000)

        form_layout.addRow("Lotnisko wylotu [ICAO]:", self.departure_airport_input)
        form_layout.addRow("Lotnisko przylotu [ICAO]:", self.arrival_airport_input)
        form_layout.addRow("Numer lotu:", self.flight_number_input)
        form_layout.addRow("Lotnisko:", self.monitored_airport_input)
        form_layout.addRow("Typ operacji:", self.event_type_input)
        form_layout.addRow("Data wylotu od:", self.departure_date_from)
        form_layout.addRow("Data wylotu do:", self.departure_date_to)
        form_layout.addRow("Godzina wylotu od:", self.departure_time_from)
        form_layout.addRow("Godzina wylotu do:", self.departure_time_to)
        form_layout.addRow("Data przylotu od:", self.arrival_date_from)
        form_layout.addRow("Data przylotu do:", self.arrival_date_to)
        form_layout.addRow("Godzina przylotu od:", self.arrival_time_from)
        form_layout.addRow("Godzina przylotu do:", self.arrival_time_to)
        form_layout.addRow("Min. długość [min]:", self.min_duration_spin)
        form_layout.addRow("Max. długość [min]:", self.max_duration_spin)

        filter_layout.addLayout(form_layout)

        btn_search_run = QPushButton("Szukaj")
        btn_search_run.clicked.connect(self.on_search_clicked)
        filter_layout.addWidget(btn_search_run)
        filter_layout.addStretch()

        self.search_results = QTableView()
        self.search_results.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        header = self.search_results.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)

        search_layout.addWidget(filter_panel, stretch=1)
        search_layout.addWidget(self.search_results, stretch=3)
        

        # strony
        self.pages = QStackedWidget()
        self.pages.addWidget(map_page)       # index 0
        self.pages.addWidget(hist)   # index 1
        self.pages.addWidget(analytics_page)   # index 2
        self.pages.addWidget(search_page)

        btn_map.clicked.connect(lambda: self.pages.setCurrentIndex(0))
        btn_history.clicked.connect(lambda: self.pages.setCurrentIndex(1))
        btn_settings.clicked.connect(lambda: self.pages.setCurrentIndex(2))
        btn_search.clicked.connect(lambda: self.pages.setCurrentIndex(3))

        main_layout.addLayout(topbar, stretch=1)
        main_layout.addWidget(self.pages, stretch=8)
        

        self.setCentralWidget(central)
        
        
    def refresh_data(self):
        print("Pobieram dane z OpenSky...")

    def closeEvent(self, event):
        self.engine.dispose()
        event.accept()
    def on_airport_changed(self, airport_name: str) -> None:
        self.airport = self.airports_lookup.get(airport_name, self.airport)
    
        self.arrivals.setModel(PandasModel(arrival_by_airport(self.engine, self.airport)))
        
    
        self.departures.setModel(PandasModel(departure_by_airport(self.engine, self.airport)))
        
    
app = QApplication(sys.argv)
window = MainWindow()
window.showMaximized()
sys.exit(app.exec())