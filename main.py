from PySide6.QtWidgets import QApplication, QMainWindow, QWidget, QPushButton, QStackedWidget, QLabel, QVBoxLayout, QHBoxLayout, QMenu
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtCore import QUrl
from PySide6.QtWebEngineCore import QWebEngineSettings
from qt_material import apply_stylesheet
import sys
from pathlib import Path


class MainWindow(QMainWindow):
    def __init__(self):
        apply_stylesheet(app, theme='dark_teal.xml')
        super().__init__()
        self.setWindowTitle("Flight Tracker")

        central = QWidget()
        main_layout = QVBoxLayout(central)

        #górne menu
        topbar= QHBoxLayout()
        btn_map = QPushButton("Mapa")
        btn_history = QPushButton("Historia lotów")
        btn_settings = QPushButton("Ustawienia")
        topbar.addWidget(btn_map)
        topbar.addWidget(btn_history)
        topbar.addWidget(btn_settings)
        

        #mapa + filtry
        map_page = QWidget()
        map_page_layout = QHBoxLayout(map_page)
        map_page_layout.setContentsMargins(10, 10, 10, 10)
        

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


        

        # strony
        self.pages = QStackedWidget()
        self.pages.addWidget(map_page)       # index 0
        self.pages.addWidget(QLabel("Tutaj będzie historia"))   # index 1
        self.pages.addWidget(QLabel("Tutaj będą ustawienia"))   # index 2

        btn_map.clicked.connect(lambda: self.pages.setCurrentIndex(0))
        btn_history.clicked.connect(lambda: self.pages.setCurrentIndex(1))
        btn_settings.clicked.connect(lambda: self.pages.setCurrentIndex(2))

        main_layout.addLayout(topbar, stretch=1)
        main_layout.addWidget(self.pages, stretch=8)
        

        self.setCentralWidget(central)
        
        
    def refresh_data(self):
        print("Pobieram dane z OpenSky...")


app = QApplication(sys.argv)
window = MainWindow()
window.showMaximized()
sys.exit(app.exec())