# SPDX-License-Identifier: GPL-3.0-or-later
from PySide6.QtGui import QColor, QPalette

ACCENT = "#ff3b30"

QSS = """
QMainWindow, QDialog { background: #15171b; }
QWidget { color: #e6e8eb; font-size: 10pt; }
QDockWidget { titlebar-close-icon: none; font-weight: 600; }
QDockWidget::title { background: #1d2025; padding: 6px 8px; border-bottom: 1px solid #2a2e35; }
QGroupBox { border: 1px solid #2a2e35; border-radius: 8px; margin-top: 14px; padding: 8px 6px 6px 6px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #aab1ba; font-weight: 600; }
QPushButton { background: #262a31; border: 1px solid #343a43; border-radius: 6px; padding: 5px 10px; }
QPushButton:hover { background: #2f343c; border-color: #454c57; }
QPushButton:pressed { background: #22262c; }
QPushButton:disabled { color: #6b727c; background: #1e2126; }
QPushButton#primary { background: %(a)s; border-color: %(a)s; color: white; font-weight: 600; }
QPushButton#primary:hover { background: #ff5a51; }
QPushButton:checked { background: #3a2a2c; border-color: %(a)s; }
QToolButton { background: transparent; border: 1px solid transparent; border-radius: 6px; padding: 4px; }
QToolButton:hover { background: #2a2e35; border-color: #343a43; }
QToolButton:checked { background: #3a2a2c; border-color: %(a)s; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox { background: #1b1e23; border: 1px solid #343a43;
    border-radius: 5px; padding: 3px 6px; selection-background-color: %(a)s; }
QComboBox QAbstractItemView { background: #1b1e23; selection-background-color: %(a)s; }
QListWidget, QTreeWidget, QTableWidget { background: #181b1f; border: 1px solid #2a2e35; border-radius: 6px;
    alternate-background-color: #1c1f24; }
QListWidget::item:selected, QTreeWidget::item:selected, QTableWidget::item:selected { background: #4a2326; }
QHeaderView::section { background: #1d2025; border: none; border-bottom: 1px solid #2a2e35; padding: 4px; color: #aab1ba; }
QTabWidget::pane { border: none; }
QTabBar::tab { background: #1b1e23; padding: 6px 12px; border-top-left-radius: 6px; border-top-right-radius: 6px; color: #aab1ba; }
QTabBar::tab:selected { background: #262a31; color: white; }
QSlider::groove:horizontal { height: 4px; background: #2a2e35; border-radius: 2px; }
QSlider::handle:horizontal { background: %(a)s; width: 14px; margin: -6px 0; border-radius: 7px; }
QProgressBar { background: #1b1e23; border: 1px solid #343a43; border-radius: 5px; text-align: center; height: 16px; }
QProgressBar::chunk { background: %(a)s; border-radius: 4px; }
QStatusBar { background: #111316; color: #aab1ba; }
QMenuBar { background: #111316; }
QMenuBar::item:selected { background: #2a2e35; }
QMenu { background: #1d2025; border: 1px solid #2a2e35; }
QMenu::item:selected { background: #4a2326; }
QLabel#muted { color: #8b939d; }
QLabel#badge { background: #3a2a2c; color: #ff8a80; border-radius: 4px; padding: 1px 6px; font-size: 8pt; font-weight: 600; }
QLabel#good { color: #30d158; }
QLabel#warn { color: #ffd60a; }
QLabel#bad { color: #ff453a; }
QScrollArea { border: none; }
""" % {"a": ACCENT}


def apply_dark(app):
    app.setStyle("Fusion")
    pal = QPalette()
    base = QColor("#15171b")
    pal.setColor(QPalette.ColorRole.Window, base)
    pal.setColor(QPalette.ColorRole.WindowText, QColor("#e6e8eb"))
    pal.setColor(QPalette.ColorRole.Base, QColor("#1b1e23"))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor("#1f2227"))
    pal.setColor(QPalette.ColorRole.Text, QColor("#e6e8eb"))
    pal.setColor(QPalette.ColorRole.Button, QColor("#262a31"))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor("#e6e8eb"))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor("#262a31"))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor("#e6e8eb"))
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor("#6b727c"))
    app.setPalette(pal)
    app.setStyleSheet(QSS)
