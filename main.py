from PySide6.QtWidgets import QApplication, QMainWindow, QWidget, QPushButton, QStackedWidget, QLabel, QVBoxLayout, QHBoxLayout, QMenu, QTableView, QComboBox, QHeaderView, QSizePolicy
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtCore import QUrl
from PySide6.QtWebEngineCore import QWebEngineSettings
from qt_material import apply_stylesheet
from Qt_df_model import *
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
import pandas as pd
import sys
from pathlib import Path
import json


from analytical_queries import create_engine_for_database, traffic_by_airport, arrival_by_airport, departure_by_airport, list_monitored_airports, popular_routes


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

AIRPORT_COORDINATES: dict[str, tuple[float, float]] = {
    "EPBA": (49.805, 19.001), "EPPK": (52.421, 16.826),
    "EPLR": (51.240, 22.717), "EPKM": (50.238, 19.035),
    "EPGI": (53.524, 18.847), "EPOM": (51.577, 17.835),
    "EPIN": (52.781, 18.249), "EPLS": (51.840, 16.529),
    "EPZR": (49.766, 19.246), "EPJG": (50.899, 15.785),
    "EPRG": (50.016, 18.636), "EPML": (50.322, 21.462),
    "EPLU": (51.418, 16.202), "EPSW": (51.235, 22.715),
    "EPNL": (49.750, 20.632), "EPZP": (51.976, 15.594),
    "EPGL": (50.239, 18.668), "EPJS": (50.804, 15.785),
    "EPKR": (49.683, 21.770), "EPPT": (51.721, 19.699),
    "EPKA": (50.900, 20.700), "EPKP": (50.079, 20.245),
    "EPPL": (52.421, 19.309), "EPWK": (52.807, 19.005),
    "EPST": (50.570, 22.055), "EPBK": (53.104, 23.170),
    "EPOD": (53.777, 20.408), "EPRP": (51.389, 21.213),
    "EPNT": (49.462, 20.050), "EPOP": (50.625, 17.781),
    "EPLL": (51.721, 19.398), "EPSD": (53.389, 14.633),
    "EPWA": (52.165, 20.967), "EPPO": (52.421, 16.826),
    "EPTO": (53.116, 18.010), "EPGD": (54.377, 18.466),
    "EPKE": (54.077, 21.375), "EPEL": (54.167, 19.450),
    "EPSU": (54.269, 22.893), "EPRZ": (50.110, 22.019),
    "EPSC": (53.584, 14.902), "EPZA": (50.706, 23.207),
    "EPSK": (54.479, 17.107), "EPRJ": (50.048, 22.019),
    "EPKT": (50.474, 19.080), "EPBY": (53.096, 17.977),
    "EPKK": (50.077, 19.784), "EPWR": (51.103, 16.886),
    "EPZG": (52.139, 15.798), "EPSY": (53.481, 20.937),
    "EPMO": (52.451, 20.651), "EPKG": (54.129, 15.285),
    "EPBC": (52.269, 20.911), "EPLB": (51.240, 22.714),
    "EPRA": (51.390, 21.214), "EPKW": (49.855, 19.059),
    "EPCD": (51.198, 23.303), "EPRU": (50.884, 19.193),
    "EPSA": (49.560, 22.207), "EPZE": (51.841, 16.519),
    "EPKH": (54.207, 16.265), "EPPB": (52.491, 16.948),
    "EPPG": (51.792, 16.784), "EPMR": (50.984, 16.887),
    "EPBH": (53.096, 17.978), "EPKX": (50.077, 19.784),
}


class MainWindow(QMainWindow):
    def setup_table_columns(table: QTableView) -> None:
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        last_column = table.model().columnCount() - 1
        header.setSectionResizeMode(last_column, QHeaderView.ResizeMode.Stretch)
    
    
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
        topbar.addWidget(btn_map)
        topbar.addWidget(btn_history)
        topbar.addWidget(btn_settings)
        

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
        self.web_view.loadFinished.connect(lambda ok: self.load_airports_to_map())

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
        
        

        # strony
        self.pages = QStackedWidget()
        self.pages.addWidget(map_page)       # index 0
        self.pages.addWidget(hist)   # index 1
        self.pages.addWidget(analytics_page)   # index 2

        btn_map.clicked.connect(lambda: self.pages.setCurrentIndex(0))
        btn_history.clicked.connect(lambda: self.pages.setCurrentIndex(1))
        btn_settings.clicked.connect(lambda: self.pages.setCurrentIndex(2))

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
    
    def load_airports_to_map(self):
        df = list_monitored_airports(self.engine)

        data = df.to_dict(orient="records")
        js = f"loadAirports({json.dumps(data)});"

        self.web_view.page().runJavaScript(js)

        routes_df = popular_routes(self.engine, limit=800)

        # Budujemy prostą listę list: [lat1, lng1, lat2, lng2, flight_count]
        routes_simple_list = []
        for _, row in routes_df.iterrows():
            dep = row['departure_airport_code']
            arr = row['arrival_airport_code']
            
            if dep in AIRPORT_COORDINATES and arr in AIRPORT_COORDINATES:
                routes_simple_list.append([
                    AIRPORT_COORDINATES[dep][0],
                    AIRPORT_COORDINATES[dep][1],
                    AIRPORT_COORDINATES[arr][0],
                    AIRPORT_COORDINATES[arr][1], 
                    int(row['flight_count'])
                ])

        js_routes = f"loadRoutes({json.dumps(routes_simple_list)});"
        self.web_view.page().runJavaScript(js_routes)
    
app = QApplication(sys.argv)
window = MainWindow()
window.showMaximized()
sys.exit(app.exec())