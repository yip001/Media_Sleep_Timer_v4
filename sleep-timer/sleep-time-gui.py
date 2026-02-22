import os
import threading
import time
from datetime import datetime, timedelta
import sys
import json
from PyQt5.QtWidgets import QApplication, QWidget, QPushButton, QMenu, QMenuBar
from PyQt5 import QtCore, QtGui, QtWidgets

from ui import main


class ConfirmDialog(QtWidgets.QDialog):
    def __init__(self, parent=None, seconds=60):
        super().__init__(parent)
        self.setWindowTitle("Sleep confirmation")
        self.setModal(True)
        self.cancelled = False

        self.seconds = seconds

        layout = QtWidgets.QVBoxLayout(self)
        self.message = QtWidgets.QLabel("Are you still here? If you want to sleep the system, please click yes.")
        layout.addWidget(self.message)

        self.count_label = QtWidgets.QLabel("")
        layout.addWidget(self.count_label)

        btn_layout = QtWidgets.QHBoxLayout()
        self.yes_btn = QtWidgets.QPushButton("Yes")
        self.cancel_btn = QtWidgets.QPushButton("Cancel the timer")
        btn_layout.addWidget(self.yes_btn)
        btn_layout.addWidget(self.cancel_btn)
        layout.addLayout(btn_layout)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self._update_label()
        self.timer.start(1000)

        self.yes_btn.clicked.connect(self._yes)
        self.cancel_btn.clicked.connect(self._cancel)

    def _update_label(self):
        self.count_label.setText(f"Auto-continue in: {self.seconds} s")

    def _tick(self):
        self.seconds -= 1
        if self.seconds <= 0:
            self.timer.stop()
            self.accept()
            return
        self._update_label()

    def _yes(self):
        self.timer.stop()
        self.accept()

    def _cancel(self):
        self.timer.stop()
        self.cancelled = True
        self.reject()


class MyQtApp(main.Ui_MainWindow, QtWidgets.QMainWindow):
    show_confirm = QtCore.pyqtSignal(object)

    def __init__(self):
        super(MyQtApp, self).__init__()
        self.setupUi(self)
        self.setWindowTitle("Sleep Timer")

        # Timer starten
        self.timer = None

        # Dark mode & StyleSheet
        self.dark_mode = True
        self.load_config()
        self.stylesheet()
        self.action_Dark_Mode.triggered.connect(self.set_dark_mode)
        self.action_Light_Mode.triggered.connect(self.set_light_mode)

        # signal from worker to show confirmation dialog
        self.show_confirm.connect(self._on_show_confirm)

        # Connecting signals and slots
        self.cancel_button.clicked.connect(self.cancel_timer)
        self.exit_button.clicked.connect(self.cancel_timer)
        self.two_hours_button.clicked.connect(lambda: self.start_timer(2 * 60 * 60))
        self.one_hour_button.clicked.connect(lambda: self.start_timer(1 * 60 * 60))
        self.thirty_min_button.clicked.connect(lambda: self.start_timer(30 * 60))

    def start_timer(self, duration):
        # If a timer is already running, cancel it first.
        if self.timer:
            self.timer.cancel()

        # Timer starten
        self.timer = CountdownTimer(duration, self)
        self.timer.start()

    def cancel_timer(self):
        # Reset the timer
        if self.timer:
            self.timer.cancel()
            self.timer = None
            self.time_label.setText("Please select a new time.")

    def _on_show_confirm(self, event):
        # event is a threading.Event passed by the timer thread
        dlg = ConfirmDialog(self, seconds=60)
        result = dlg.exec_()
        # if user chose to cancel the timer, stop it
        if getattr(dlg, 'cancelled', False):
            self.cancel_timer()
        # signal the worker thread to continue (or timeout)
        try:
            event.set()
        except Exception:
            pass

    def set_time_label(self, time_str):
        # Guard against timer being None
        if getattr(self, 'timer', None):
            shutdown_at = (datetime.now() + timedelta(seconds=self.timer.duration)).strftime("%H:%M:%S")
            self.time_label.setText("The system will shut down in: {} at {}.".format(time_str, shutdown_at))
        else:
            self.time_label.setText("The system will shut down in: {}".format(time_str))

    def stylesheet(self):
        # StyleSheet
        for button in self.findChildren(QPushButton):
            button.setStyleSheet("QPushButton:hover { background-color: rgba(135, 167, 82, 100%); border: 1px solid #00FF00; }")
        for qmenu in self.findChildren(QMenu):
            qmenu.setStyleSheet("QMenu::item:selected { background-color: rgba(135, 167, 82, 100%); border: 1px solid #00FF00; color: #fff; }")
        for qmenubar in self.findChildren(QMenuBar):
            qmenubar.setStyleSheet("QMenuBar::item:selected { background-color: rgba(135, 167, 82, 100%); border: 1px solid #00FF00; color: #fff; }")

    def set_dark_mode(self):
        # Activate dark mode
        self.dark_mode = True
        self.setStyleSheet("background-color: #222222; color: #ffffff;")
        self.save_config()

    def set_light_mode(self):
        # Activate light mode
        self.dark_mode = False
        self.setStyleSheet("background-color: #ffffff; color: #000000;")
        self.save_config()

    def load_config(self):
        try:
            with open("config.json", "r") as f:
                config = json.load(f)
                self.dark_mode = config["dark_mode"]
                if self.dark_mode:
                    self.set_dark_mode()
                else:
                    self.set_light_mode()
        except FileNotFoundError:
            pass

    def save_config(self):
        config = {"dark_mode": self.dark_mode}
        with open("config.json", "w") as f:
            json.dump(config, f)

    def closeEvent(self, event):
        self.cancel_timer()
        self.save_config()
        event.accept()

class CountdownTimer:
    def __init__(self, duration, ui):
        self.duration = duration
        self.initial_duration = duration
        self.ui = ui
        self.end_time = datetime.now() + timedelta(seconds=duration)
        self.timer = None
        self.cancelled = False
        # event used to pause the worker when showing confirmation dialog
        self.pause_event = threading.Event()
        self.pause_event.set()

    def start(self):
        self.timer = threading.Thread(target=self.run)
        self.timer.start()

    def run(self):
        try:
            while self.duration and not self.cancelled:
                hours, remainder = divmod(self.duration, 3600)
                minutes, seconds = divmod(remainder, 60)
                time_str = "{:02d}:{:02d}:{:02d}".format(hours, minutes, seconds)
                # update UI
                self.ui.set_time_label(time_str)

                # Check for 30-minute interval (every 1800s) excluding initial moment
                if self.duration != self.initial_duration and self.duration % 1800 == 0:
                    # pause worker and request UI to show confirmation dialog
                    self.pause_event.clear()
                    try:
                        self.ui.show_confirm.emit(self.pause_event)
                    except Exception:
                        # if signal fails, just continue
                        self.pause_event.set()
                    # wait up to 60 seconds for user response
                    self.pause_event.wait(timeout=60)
                    # continue loop without decrementing during pause

                time.sleep(1)
                self.duration -= 1

            if not self.cancelled:
                os.system("shutdown -h now")
        except KeyboardInterrupt:
            print("\nReset the timer")

    def cancel(self):
        self.cancelled = True
        try:
            # wake any wait on the pause event so thread can exit promptly
            self.pause_event.set()
        except Exception:
            pass

if __name__ == "__main__":
    def _parse_start_arg(argv):
        """Parse a start-duration argument from command line.

        Supported formats:
        - integers = seconds (e.g. 1800)
        - suffix 'm' for minutes (e.g. 30m)
        - suffix 'h' for hours (e.g. 1h)
        - presets: '30m', '1h', '2h'
        Returns seconds (int) or None.
        """
        if len(argv) < 2:
            return None
        s = str(argv[1]).lower()
        try:
            if s.endswith('m'):
                return int(s[:-1]) * 60
            if s.endswith('h'):
                return int(s[:-1]) * 3600
            return int(s)
        except Exception:
            return None

    start_duration = _parse_start_arg(sys.argv)

    app = QApplication(sys.argv)
    qt_app = MyQtApp()
    qt_app.show()

    # If a start duration was provided, schedule the timer to start
    # once the event loop is running to ensure UI is ready.
    if start_duration:
        QtCore.QTimer.singleShot(100, lambda: qt_app.start_timer(start_duration))

    sys.exit(app.exec_())
