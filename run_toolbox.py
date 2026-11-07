import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication, QMainWindow

from app.services.async_bridge import init_dispatcher
from app.ui.toolbox_view import ToolboxView


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Python Toolbox")
    init_dispatcher()

    window = QMainWindow()
    window.setWindowTitle("工具箱")
    window.resize(1200, 850)
    window.setCentralWidget(ToolboxView())
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
