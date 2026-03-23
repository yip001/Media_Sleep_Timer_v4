import os
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta
import sys
import pyaudio
import vosk
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
        self.message = QtWidgets.QLabel(
            "Are you still here? If you want to sleep the system, please click yes."
        )
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
    start_exit_countdown_signal = QtCore.pyqtSignal()
    extend_timer_signal = QtCore.pyqtSignal(int)  # extra seconds to add
    # Thread-safe signal for voice commands
    voice_command_signal = QtCore.pyqtSignal(str)
    # Thread-safe signal for voice status messages
    voice_status_signal = QtCore.pyqtSignal(str)

    def __init__(self):
        super(MyQtApp, self).__init__()
        self.setupUi(self)
        self.setWindowTitle("Sleep Timer")

        # ---------- Voice control init ----------
        self.vosk_model = None
        self._voice_listening = False
        self._voice_lock = threading.Lock()
        self.init_vosk()

        # Voice status label (shows partial / recognized text)
        self.voice_status_label = QtWidgets.QLabel("")
        self.voice_status_label.setStyleSheet(
            "color: #87A752; font-size: 10px;"
        )
        self.voice_status_label.setAlignment(QtCore.Qt.AlignCenter)
        self.voice_status_label.setWordWrap(True)

        # Voice toggle button (manual override)
        self.voice_button = QtWidgets.QPushButton("🎤 Voice: Off")
        self.voice_button.setAutoDefault(True)

        # Insert voice widgets into layout after the last time-button
        layout = self.verticalLayout
        index = layout.indexOf(self.thirty_min_button) + 1
        layout.insertWidget(index, self.voice_status_label)
        layout.insertWidget(index + 1, self.voice_button)
        self.voice_button.clicked.connect(self.toggle_voice)

        # ---- Large exit-countdown number display (hidden by default) ----
        self.exit_countdown_display = QtWidgets.QLabel("")
        self.exit_countdown_display.setAlignment(QtCore.Qt.AlignCenter)
        self.exit_countdown_display.setMinimumHeight(80)
        self.exit_countdown_display.setStyleSheet(
            "font-size: 64px; font-weight: bold; color: #FF4444;"
        )
        self.exit_countdown_display.setVisible(False)
        layout.insertWidget(index + 2, self.exit_countdown_display)

        # Connect voice signals (thread-safe Qt signals → main-thread slots)
        self.voice_command_signal.connect(self._on_voice_command)
        self.voice_status_signal.connect(self._on_voice_status)

        # Timer for auto-clearing the voice status label
        self._voice_status_timer = QtCore.QTimer(self)
        self._voice_status_timer.setSingleShot(True)
        self._voice_status_timer.timeout.connect(
            lambda: self.voice_status_label.setText(
                "🎤 Listening..." if self._voice_listening else ""
            )
        )

        # ---------- Countdown timer ----------
        self.timer = None
        self.countdown_end_time = None
        self.countdown_qtimer = QtCore.QTimer(self)
        self.countdown_qtimer.timeout.connect(self.update_countdown_label)

        # Exit countdown (10-second auto-close after timer finishes)
        self.exit_countdown_seconds = 0
        self.exit_countdown_qtimer = QtCore.QTimer(self)
        self.exit_countdown_qtimer.timeout.connect(self._exit_countdown_tick)

        # Video delay status message (shown temporarily when delay is applied)
        self.video_delay_message = ""
        self._video_delay_msg_timer = QtCore.QTimer(self)
        self._video_delay_msg_timer.setSingleShot(True)
        self._video_delay_msg_timer.timeout.connect(
            self._clear_video_delay_message
        )

        # Status for video pause detection and delay application
        self.total_extended_seconds = 0
        self.extend_trigger_count = 0

        # Dark mode & StyleSheet
        self.dark_mode = True
        self.load_config()
        self.stylesheet()
        self.action_Dark_Mode.triggered.connect(self.set_dark_mode)
        self.action_Light_Mode.triggered.connect(self.set_light_mode)

        # Signal from worker to show confirmation dialog
        self.show_confirm.connect(self._on_show_confirm)
        self.start_exit_countdown_signal.connect(self._on_start_exit_countdown)
        self.extend_timer_signal.connect(self._on_extend_timer)

        # Connecting button signals and slots
        self.cancel_button.clicked.connect(self.cancel_timer)
        self.exit_button.clicked.connect(self.cancel_timer)
        self.two_hours_button.clicked.connect(
            lambda: self.start_timer(2 * 60 * 60)
        )
        self.one_hour_button.clicked.connect(
            lambda: self.start_timer(1 * 60 * 60)
        )
        self.thirty_min_button.clicked.connect(
            lambda: self.start_timer(30 * 60)
        )

    # ------------------------------------------------------------------
    #  Timer control
    # ------------------------------------------------------------------

    def start_timer(self, duration):
        # If a timer is already running, cancel it first.
        if self.timer:
            self.timer.cancel()
        # Set end time and start QTimer for UI updates
        self.countdown_end_time = datetime.now() + timedelta(seconds=duration)
        self.countdown_qtimer.start(1000)
        self.video_delay_message = ""
        self.total_extended_seconds = 0
        self.extend_trigger_count = 0
        # Timer starten (logic only)
        self.timer = CountdownTimer(duration, self)
        self.timer.start()
        # ▶ Auto-start continuous voice recognition
        self.start_continuous_voice()

    def cancel_timer(self):
        # Reset the timer
        if self.timer:
            self.timer.cancel()
            self.timer = None
        self.countdown_end_time = None
        self.countdown_qtimer.stop()
        self.video_delay_message = ""
        self.total_extended_seconds = 0
        self.extend_trigger_count = 0
        self.time_label.setText("Please select a new time.")
        # ■ Stop continuous voice recognition
        self.stop_continuous_voice()

    # ------------------------------------------------------------------
    #  Confirm dialog / extend timer / exit countdown (unchanged logic)
    # ------------------------------------------------------------------

    def _on_show_confirm(self, event):
        dlg = ConfirmDialog(self, seconds=60)
        dlg.exec_()
        if getattr(dlg, 'cancelled', False):
            self.cancel_timer()
        try:
            event.set()
        except Exception:
            pass

    def _on_extend_timer(self, extra_seconds):
        if self.countdown_end_time:
            self.countdown_end_time += timedelta(seconds=extra_seconds)
            self.total_extended_seconds += extra_seconds
            self.extend_trigger_count += 1
            total_mins = self.total_extended_seconds // 60
            self.video_delay_message = (
                f" ( Video Stopped — Cumulative Extension "
                f"{total_mins} Minutes)"
            )
            if self.extend_trigger_count >= 2:
                self._video_delay_msg_timer.stop()
            else:
                self._video_delay_msg_timer.start(60000)

    def _clear_video_delay_message(self):
        self.video_delay_message = ""

    def update_countdown_label(self):
        if not self.countdown_end_time:
            self.time_label.setText("Please select a new time.")
            self.countdown_qtimer.stop()
            return
        remaining = (self.countdown_end_time - datetime.now()).total_seconds()
        if remaining <= 0:
            self.time_label.setText("Shutting down now...")
            self.countdown_qtimer.stop()
            return
        hours, remainder = divmod(int(remaining), 3600)
        minutes, seconds = divmod(remainder, 60)
        time_str = "{:02d}:{:02d}:{:02d}".format(hours, minutes, seconds)
        shutdown_at = self.countdown_end_time.strftime("%H:%M:%S")
        msg = (
            f"The system will shut down in: {time_str} at {shutdown_at}."
        )
        if self.video_delay_message:
            msg += self.video_delay_message
        self.time_label.setText(msg)

    def _on_start_exit_countdown(self):
        self.countdown_qtimer.stop()
        self.countdown_end_time = None
        # Hide all time buttons, cancel button, and voice widgets
        for widget in [
            self.two_hours_button, self.one_hour_button,
            self.thirty_min_button, self.cancel_button,
            self.voice_button, self.voice_status_label,
        ]:
            widget.setVisible(False)
        # Show the large exit countdown number display
        self.exit_countdown_display.setVisible(True)
        # Re-wire exit button to close the window immediately
        try:
            self.exit_button.clicked.disconnect()
        except Exception:
            pass
        self.exit_button.clicked.connect(self.close)
        self.exit_button.setText("Exit Now")
        # Start 10-second exit countdown
        self.exit_countdown_seconds = 10
        self._update_exit_label()
        self.exit_countdown_qtimer.start(1000)
        # Stop voice when timer finishes
        self.stop_continuous_voice()

    def _update_exit_label(self):
        self.time_label.setText(
            f"Sleep timer finished. Media has been paused.\n"
            f"This page will automatically close "
            f"in {self.exit_countdown_seconds} seconds."
        )
        self.exit_countdown_display.setText(
            str(self.exit_countdown_seconds)
        )

    def _exit_countdown_tick(self):
        self.exit_countdown_seconds -= 1
        if self.exit_countdown_seconds <= 0:
            self.exit_countdown_qtimer.stop()
            self.close()
            return
        self._update_exit_label()

    # ------------------------------------------------------------------
    #  Dark / Light mode
    # ------------------------------------------------------------------

    def stylesheet(self):
        for button in self.findChildren(QPushButton):
            button.setStyleSheet(
                "QPushButton:hover { background-color: rgba(135, 167, 82, 100%); "
                "border: 1px solid #00FF00; }"
            )
        for qmenu in self.findChildren(QMenu):
            qmenu.setStyleSheet(
                "QMenu::item:selected { background-color: rgba(135, 167, 82, 100%); "
                "border: 1px solid #00FF00; color: #fff; }"
            )
        for qmenubar in self.findChildren(QMenuBar):
            qmenubar.setStyleSheet(
                "QMenuBar::item:selected { background-color: rgba(135, 167, 82, 100%); "
                "border: 1px solid #00FF00; color: #fff; }"
            )

    def set_dark_mode(self):
        self.dark_mode = True
        self.setStyleSheet(
            "background-color: #222222; color: #ffffff;"
        )
        self.save_config()

    def set_light_mode(self):
        self.dark_mode = False
        self.setStyleSheet(
            "background-color: #ffffff; color: #000000;"
        )
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
        self.stop_continuous_voice()
        self.cancel_timer()
        self.save_config()
        event.accept()

    # ==================================================================
    #  VOICE RECOGNITION — Continuous, hands-free, microphone-only
    # ==================================================================

    def init_vosk(self):
        """Load Vosk English model (called once at startup)."""
        model_path = "vosk-model-en"
        try:
            if not os.path.exists(model_path):
                import glob
                folders = glob.glob("vosk-model-en*")
                if folders:
                    model_path = folders[0]
                    print(f"Found model folder: {model_path}")
                else:
                    raise FileNotFoundError(
                        f"Cannot find Vosk model directory: {model_path}"
                    )
            self.vosk_model = vosk.Model(model_path)
            print("✅ Vosk English model loaded successfully")
        except Exception as e:
            print(f"❌ Vosk init failed: {e}")
            self.vosk_model = None

    # ---------- Start / Stop / Toggle ----------

    def start_continuous_voice(self):
        """Start continuous voice recognition in a background thread.

        Called automatically when a timer starts.  Does nothing if
        already listening or if the Vosk model failed to load.

        NOTE — PyAudio's ``input=True`` opens the system's default
        *input* device (the physical microphone).  Audio played through
        *output* devices (speakers / headphones — e.g. video soundtracks)
        is NOT captured, so movie dialogue will not trigger commands.
        """
        if not self.vosk_model:
            print("❌ Cannot start voice: Vosk model not loaded")
            return
        with self._voice_lock:
            if self._voice_listening:
                return  # already listening — no-op
            self._voice_listening = True

        self.voice_button.setText("🎤 Voice: Active")
        self.voice_button.setStyleSheet(
            "QPushButton { color: #00FF00; font-weight: bold; } "
            "QPushButton:hover { background-color: rgba(135, 167, 82, 100%); "
            "border: 1px solid #00FF00; }"
        )
        self.voice_status_label.setText("🎤 Listening for commands...")
        self._voice_status_timer.start(3000)

        threading.Thread(
            target=self._continuous_listen_loop, daemon=True
        ).start()

    def stop_continuous_voice(self):
        """Signal the voice thread to stop (non-blocking)."""
        with self._voice_lock:
            if not self._voice_listening:
                return
            self._voice_listening = False

        self.voice_button.setText("🎤 Voice: Off")
        self.voice_button.setStyleSheet(
            "QPushButton:hover { background-color: rgba(135, 167, 82, 100%); "
            "border: 1px solid #00FF00; }"
        )
        self.voice_status_label.setText("")

    def toggle_voice(self):
        """Manual toggle (connected to the voice button click)."""
        if self._voice_listening:
            self.stop_continuous_voice()
        else:
            self.start_continuous_voice()

    # ---------- Background listening thread ----------

    def _continuous_listen_loop(self):
        """Runs on a daemon thread.  Opens the microphone, feeds audio
        chunks to a Vosk recogniser, and emits ``voice_command_signal``
        whenever a complete utterance is detected.

        The loop keeps running until ``_voice_listening`` is set to
        ``False`` (by ``stop_continuous_voice``).
        """
        p = pyaudio.PyAudio()
        stream = None
        # Create a *fresh* KaldiRecognizer for each session to avoid
        # stale state from a previous session.
        recognizer = vosk.KaldiRecognizer(self.vosk_model, 16000)

        try:
            # --- Select the default microphone input device ---
            # PyAudio's ``input=True`` automatically uses the system's
            # default input device (microphone).  System audio from
            # videos / media players goes through output devices and
            # is **not** captured here.
            stream = p.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=16000,
                input=True,
                frames_per_buffer=4000,
            )
            stream.start_stream()
            print("🎤 Continuous voice recognition started "
                  "(microphone-only)")

            while self._voice_listening:
                # Read a chunk of audio from the microphone
                try:
                    data = stream.read(4000, exception_on_overflow=False)
                except OSError:
                    # Occasionally the stream can hiccup — retry
                    time.sleep(0.1)
                    continue

                if recognizer.AcceptWaveform(data):
                    # ---------- Complete utterance ----------
                    result = json.loads(recognizer.Result())
                    text = result.get("text", "").strip()
                    if text:
                        print(f"🎤 Recognized: '{text}'")
                        # Emit signal → processed on main/GUI thread
                        self.voice_command_signal.emit(text)
                else:
                    # ---------- Partial result (live feedback) ----------
                    partial = json.loads(recognizer.PartialResult())
                    partial_text = partial.get("partial", "").strip()
                    if partial_text:
                        self.voice_status_signal.emit(
                            f"🎤 ...{partial_text}"
                        )

        except Exception as e:
            print(f"❌ Voice recognition error: {e}")
            self.voice_status_signal.emit(f"❌ Mic error: {e}")
        finally:
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            p.terminate()
            print("🎤 Continuous voice recognition stopped")
            # Make sure UI reflects the stopped state
            QtCore.QTimer.singleShot(0, self._on_voice_thread_exited)

    def _on_voice_thread_exited(self):
        """Called on the main thread after the voice thread finishes."""
        with self._voice_lock:
            self._voice_listening = False
        self.voice_button.setText("🎤 Voice: Off")
        self.voice_button.setStyleSheet(
            "QPushButton:hover { background-color: rgba(135, 167, 82, 100%); "
            "border: 1px solid #00FF00; }"
        )

    # ---------- Signal slots (main thread) ----------

    @QtCore.pyqtSlot(str)
    def _on_voice_status(self, msg):
        """Show partial recognition text in the status label."""
        self.voice_status_label.setText(msg)
        # Do NOT start the clear-timer here — partials update rapidly

    @QtCore.pyqtSlot(str)
    def _on_voice_command(self, text):
        """Process a fully-recognised utterance."""
        self.handle_voice_command(text)

    # ---------- Command processing ----------

    def handle_voice_command(self, text):
        """Parse the recognised text and execute the corresponding action."""
        print(f"📢 Processing voice command: '{text}'")

        # Show recognised text briefly
        self.voice_status_label.setText(f'🎤 "{text}"')
        self._voice_status_timer.start(5000)

        command = self.parse_voice_command(text.lower())

        if command is None:
            print(f"❌ Unrecognized command: '{text}'")
            self.voice_status_label.setText(
                f'🎤 "{text}" — not a known command'
            )
            self._voice_status_timer.start(5000)
            return

        cmd_type, value = command
        print(f"➡️ Command: {cmd_type}, value: {value}")

        if cmd_type == "time":
            # start_timer already handles cancelling any running timer.
            # It also calls start_continuous_voice(), which is a no-op
            # if we are already listening.
            self.start_timer(value)

            if value >= 3600:
                h = value // 3600
                label = f"{h} hour{'s' if h > 1 else ''}"
            else:
                m = max(1, value // 60)
                label = f"{m} minute{'s' if m > 1 else ''}"
            self.voice_status_label.setText(f"🎤 Timer set to {label}")
            self._voice_status_timer.start(5000)
            print(f"✅ Timer started via voice: {label}")

        elif cmd_type == "reset":
            # cancel_timer stops voice, so we restart it afterwards
            # so the user can immediately set a new time by voice.
            self.cancel_timer()
            self.time_label.setText("🎤 Timer reset by voice.")
            self.voice_status_label.setText("🎤 Timer reset")
            self._voice_status_timer.start(5000)
            # Re-enable voice so the user can pick a new time hands-free
            self.start_continuous_voice()

        elif cmd_type == "exit":
            self.stop_continuous_voice()
            self.close()

    # ---------- Voice command parser ----------

    def parse_voice_command(self, text):
        """Parse an English voice command.

        Returns ``(cmd_type, value)`` or ``None``.

        Supported formats
        -----------------
        Time:
          - digits + unit:   "2 hours", "30 minutes", "10 seconds"
          - words + unit:    "two hours", "thirty minutes"
          - compound words:  "twenty five minutes"
          - phrases:         "half an hour", "an hour", "a minute"
        Reset:  "reset", "cancel", "stop", "abort"
        Exit:   "exit", "quit", "close"
        """

        num_words = {
            "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
            "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
            "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
            "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
            "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
            "ninety": 90,
        }

        unit_map = {
            "hour": 3600, "hours": 3600,
            "minute": 60, "minutes": 60, "min": 60, "mins": 60,
            "second": 1, "seconds": 1, "sec": 1, "secs": 1,
        }

        # ── Special phrases ──
        if re.search(r'\bhalf\s+(an?\s+)?hour', text):
            return ("time", 30 * 60)

        # ── Digit + unit  (e.g. "2 hours", "30 minutes") ──
        digit_patterns = [
            (r'(\d+)\s*(hours?)', 3600),
            (r'(\d+)\s*(minutes?|mins?)', 60),
            (r'(\d+)\s*(seconds?|secs?)', 1),
        ]
        for pattern, multiplier in digit_patterns:
            match = re.search(pattern, text)
            if match:
                num = int(match.group(1))
                if num > 0:
                    return ("time", num * multiplier)

        # ── Word numbers + unit  (incl. compound like "twenty five") ──
        words = text.split()
        for i, word in enumerate(words):
            if word not in num_words:
                continue
            num = num_words[word]
            next_idx = i + 1
            # Compound number: "twenty" + "five" → 25
            if (next_idx < len(words)
                    and words[next_idx] in num_words
                    and num >= 20
                    and num_words[words[next_idx]] < 10):
                num += num_words[words[next_idx]]
                next_idx += 1
            # Check for a time-unit word after the number
            if next_idx < len(words) and words[next_idx] in unit_map:
                return ("time", num * unit_map[words[next_idx]])

        # ── "an hour" / "a minute" ──
        if re.search(r'\ban?\s+hour', text):
            return ("time", 3600)
        if re.search(r'\ba\s+minute', text):
            return ("time", 60)

        # ── Reset commands ──
        if re.search(r'\b(reset|cancel|stop|abort)\b', text):
            return ("reset", None)

        # ── Exit commands ──
        if re.search(r'\b(exit|quit|close)\b', text):
            return ("exit", None)

        return None

    def show_voice_error(self, msg):
        """Show a transient voice-related message in the status label."""
        self.voice_status_label.setText(f"🎤 {msg}")
        self._voice_status_timer.start(5000)


# ------------------------------
#  Video playback detection
# ------------------------------

def check_browser_video_status():
    """Check if main (visible, large) videos in Chrome or Safari are paused.

    Returns
    -------
    str
        'playing' - at least one main video is currently playing
        'paused'  - at least one main video exists and all are paused
        'none'    - no main video found in any browser
    """
    _js = r"""
        (function() {
            var videos = document.querySelectorAll('video');
            for (var i = 0; i < videos.length; i++) {
                var v = videos[i];
                var rect = v.getBoundingClientRect();
                var style = window.getComputedStyle(v);
                if (rect.width > 200 && rect.height > 150
                    && style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && style.opacity !== '0') {
                    if (!v.paused) return 'playing';
                    if (v.paused && v.readyState > 0) return 'paused';
                }
            }
            return 'none';
        })();
    """

    chrome_script = f'''
if application "Google Chrome" is running then
    set foundPaused to false
    tell application "Google Chrome"
        repeat with w in windows
            repeat with t in tabs of w
                try
                    set jsResult to execute t javascript "{_js}"
                    if jsResult is "playing" then return "playing"
                    if jsResult is "paused" then set foundPaused to true
                end try
            end repeat
        end repeat
    end tell
    if foundPaused then return "paused"
end if
return "none"
'''

    safari_script = f'''
if application "Safari" is running then
    set foundPaused to false
    tell application "Safari"
        repeat with w in windows
            repeat with t in tabs of w
                try
                    set jsResult to do JavaScript "{_js}" in t
                    if jsResult is "playing" then return "playing"
                    if jsResult is "paused" then set foundPaused to true
                end try
            end repeat
        end repeat
    end tell
    if foundPaused then return "paused"
end if
return "none"
'''

    found_paused = False
    for script in [chrome_script, safari_script]:
        try:
            result = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True, text=True, timeout=5
            )
            output = result.stdout.strip()
            if output == 'playing':
                return 'playing'
            elif output == 'paused':
                found_paused = True
        except Exception:
            continue

    return 'paused' if found_paused else 'none'


class VideoMonitor:
    """Background daemon thread that polls browser video status every second."""

    def __init__(self):
        self.status = 'none'  # 'playing', 'paused', or 'none'
        self._running = False
        self._thread = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            try:
                self.status = check_browser_video_status()
            except Exception:
                self.status = 'none'
            time.sleep(1)


# ---------------------------
#  Media stop helper
# ---------------------------

def stop_media_and_disconnect():

    # VLC
    subprocess.run([
        'osascript', '-e',
        'if application "VLC" is running then tell application "VLC" to quit'
    ])

    # QuickTime
    subprocess.run([
        'osascript', '-e',
        'if application "QuickTime Player" is running then '
        'tell application "QuickTime Player" to quit'
    ])

    # IINA
    subprocess.run([
        'osascript', '-e',
        'if application "IINA" is running then '
        'tell application "IINA" to quit'
    ])

    # Safari
    subprocess.run([
        'osascript', '-e',
        '''if application "Safari" is running then
            tell application "Safari"
                repeat with w in windows
                    set tabCount to count of tabs of w
                    repeat with i from tabCount to 1 by -1
                        set t to tab i of w
                        set tabURL to URL of t

                        if tabURL contains "youtube.com/watch" or tabURL contains "netflix.com" or tabURL contains "bilibili.com" or tabURL contains "vimeo.com" then
                            close t
                        else
                            try
                                set hasVideo to (do JavaScript "
                                    var v = document.querySelector('video');
                                    if(v){v.pause();}
                                    (v!==null).toString();
                                " in t)
                                if hasVideo is "true" then
                                    close t
                                end if
                            end try
                        end if
                    end repeat
                end repeat
            end tell
        end if'''
    ])
    subprocess.run([
        'osascript', '-e',
        '''if application "Google Chrome" is running then
            tell application "Google Chrome"
                repeat with w in windows
                    set tabCount to count of tabs of w
                    repeat with i from tabCount to 1 by -1
                        set t to tab i of w
                        set tabURL to URL of t

                        if tabURL contains "youtube.com/watch" or tabURL contains "netflix.com" or tabURL contains "bilibili.com" or tabURL contains "vimeo.com" then
                            close t
                        else
                            try
                                set hasVideo to (execute t javascript "
                                    var v = document.querySelector('video');
                                    if(v){v.pause();}
                                    (v!==null).toString();
                                ")
                                if hasVideo is "true" then
                                    close t
                                end if
                            end try
                        end if
                    end repeat
                end repeat
            end tell
        end if'''
    ])


# ------------------------
#  Countdown timer
# ------------------------

class CountdownTimer:
    DELAY_SECONDS = 15 * 60  # 15-minute delay when video pause is detected

    def __init__(self, duration, ui):
        self.duration = duration
        self.initial_duration = duration
        self.ui = ui
        self.end_time = datetime.now() + timedelta(seconds=duration)
        self.timer = None
        self.cancelled = False
        self.pause_event = threading.Event()
        self.pause_event.set()

        self.video_monitor = VideoMonitor()
        self.delay_count = 0
        self.max_delays = 4
        self.delay_cooldown = 5 * 60
        self.last_delay_time = None

    def start(self):
        self.video_monitor.start()
        self.timer = threading.Thread(target=self.run)
        self.timer.start()

    def run(self):
        try:
            while self.duration and not self.cancelled:
                # Video pause detection and delay logic
                if (self.delay_count < self.max_delays
                        and self.video_monitor.status == 'paused'):
                    now = datetime.now()
                    cooldown_ok = (
                        self.last_delay_time is None
                        or (now - self.last_delay_time).total_seconds()
                        >= self.delay_cooldown
                    )
                    if cooldown_ok:
                        self.delay_count += 1
                        self.last_delay_time = now
                        self.duration += self.DELAY_SECONDS
                        self.end_time += timedelta(
                            seconds=self.DELAY_SECONDS
                        )
                        try:
                            self.ui.extend_timer_signal.emit(
                                self.DELAY_SECONDS
                            )
                        except Exception:
                            pass

                # 30-minute interval confirmation dialog
                if (self.duration != self.initial_duration
                        and self.duration % 1800 == 0):
                    self.pause_event.clear()
                    try:
                        self.ui.show_confirm.emit(self.pause_event)
                    except Exception:
                        self.pause_event.set()
                    self.pause_event.wait(timeout=60)

                time.sleep(1)
                self.duration -= 1

            if not self.cancelled:
                stop_media_and_disconnect()
                try:
                    self.ui.start_exit_countdown_signal.emit()
                except Exception:
                    pass
        except KeyboardInterrupt:
            print("\nReset the timer")
        finally:
            self.video_monitor.stop()

    def cancel(self):
        self.cancelled = True
        self.video_monitor.stop()
        try:
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
        QtCore.QTimer.singleShot(
            100, lambda: qt_app.start_timer(start_duration)
        )

    sys.exit(app.exec_())
