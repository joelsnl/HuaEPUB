# Author: joelsnl and Anthropic Claude
from __future__ import annotations

from PySide6.QtCore import QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QProgressDialog, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

from core.settings import get_default_books_dir


class OptionsBar(QWidget):
    """Shared download/translate options."""
    options_changed = Signal()

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        s = session.settings

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        row1 = QHBoxLayout()
        row2 = QHBoxLayout()

        self.clean_cb = QCheckBox("Remove watermarks & ads")
        self.clean_cb.setChecked(bool(s.get("clean", True)))
        self.clean_cb.setToolTip("Strip site ads and watermarks from chapter HTML.")
        self.translate_cb = QCheckBox("Translate to English")
        self.translate_cb.setChecked(bool(s.get("translate", True)))
        self.translate_cb.setToolTip(
            "Machine-translate chapter text to English while building the EPUB."
        )
        self.polish_cb = QCheckBox("Polish English")
        self.polish_cb.setChecked(bool(s.get("ollama_polish", False)))
        self.cache_cb = QCheckBox("Use chapter cache (resume)")
        self.cache_cb.setChecked(bool(s.get("use_chapter_cache", True)))
        self.cache_cb.setToolTip(
            "Reuse chapters already saved on this PC. Keep on unless you want a "
            "full re-download. Size cap and clear controls are in Help → Cache…"
        )
        self.clipboard_cb = QCheckBox("Watch clipboard for URLs")
        self.clipboard_cb.setChecked(bool(s.get("clipboard_watcher", False)))
        self.clipboard_cb.setToolTip(
            "When on, copied novel URLs are queued into Multi (and fill Single if empty)."
        )

        for cb in (self.clean_cb, self.translate_cb, self.polish_cb, self.cache_cb, self.clipboard_cb):
            row1.addWidget(cb)
        self.clean_cb.stateChanged.connect(self._emit_options)
        self.translate_cb.stateChanged.connect(self._emit_options)
        self.cache_cb.stateChanged.connect(self._emit_options)
        self.clipboard_cb.stateChanged.connect(self._emit_options)
        self.polish_cb.stateChanged.connect(self._on_polish_changed)
        row1.addStretch(1)

        row2.addWidget(QLabel("Translator:"))
        self.backend = QComboBox()
        self.backend.addItems(["Google", "LibreTranslate", "Ollama"])
        self.backend.setToolTip(
            "Google is fast and online (default). LibreTranslate uses your own "
            "server. Ollama translates fully on this PC and is much slower."
        )
        _backend = s.get("translation_backend", "google")
        self.backend.setCurrentText(
            "LibreTranslate" if _backend == "libretranslate"
            else "Ollama" if _backend == "ollama"
            else "Google"
        )
        self.backend.currentTextChanged.connect(self._on_backend_changed)
        row2.addWidget(self.backend)
        self._last_non_ollama = (
            "libretranslate" if _backend == "libretranslate" else "google"
        )

        row2.addWidget(QLabel("Workers:"))
        self.workers = QSpinBox()
        self.workers.setRange(1, 500)
        # Native spin arrows disappear under our dark QSS — use explicit +/− buttons
        self.workers.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.workers.setMinimumWidth(64)
        self.workers.setAlignment(Qt.AlignCenter)
        self.workers.setValue(int(s.get("workers", 200) or 200))
        self.workers.valueChanged.connect(self._emit_options)

        minus = QPushButton("−")
        plus = QPushButton("+")
        for b in (minus, plus):
            b.setObjectName("secondaryBtn")
            b.setFixedSize(32, 32)
            b.setStyleSheet("font-size: 16px; font-weight: bold; padding: 0;")
        minus.clicked.connect(self.workers.stepDown)
        plus.clicked.connect(self.workers.stepUp)
        row2.addWidget(minus)
        row2.addWidget(self.workers)
        row2.addWidget(plus)

        row2.addWidget(QLabel("Save to:"))
        self.folder_label = QLabel()
        self.folder_label.setMinimumWidth(200)
        row2.addWidget(self.folder_label, 1)
        browse = QPushButton("Browse…")
        browse.setObjectName("secondaryBtn")
        browse.clicked.connect(self._browse)
        reset = QPushButton("Default")
        reset.setObjectName("secondaryBtn")
        reset.clicked.connect(self._reset_folder)
        row2.addWidget(browse)
        row2.addWidget(reset)

        root.addLayout(row1)
        root.addLayout(row2)

        self.ollama_row = QHBoxLayout()
        self.ollama_row.addWidget(QLabel("Ollama model:"))
        self.ollama_model = QComboBox()
        self.ollama_model.setEditable(True)
        self.ollama_model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.ollama_model.setMinimumWidth(200)
        saved_model = s.get("ollama_model", "") or ""
        if saved_model:
            self.ollama_model.addItem(saved_model)
            self.ollama_model.setCurrentText(saved_model)
        self.ollama_model.setPlaceholderText("qwen2.5:3b or another pulled model")
        self.ollama_model.currentTextChanged.connect(self._emit_options)
        self.ollama_row.addWidget(self.ollama_model)
        self.ollama_row.addWidget(QLabel("URL:"))
        self.ollama_url = QLineEdit()
        self.ollama_url.setPlaceholderText("http://127.0.0.1:11434")
        self.ollama_url.setText(
            s.get("ollama_url", "http://127.0.0.1:11434") or "http://127.0.0.1:11434"
        )
        self.ollama_url.setMinimumWidth(180)
        self.ollama_url.setToolTip(
            "Must be this PC (localhost). Default is http://127.0.0.1:11434"
        )
        self.ollama_url.editingFinished.connect(self._refresh_ollama_models)
        self.ollama_url.textChanged.connect(self._emit_options)
        self.ollama_row.addWidget(self.ollama_url)
        self.ollama_row.addStretch(1)
        self.ollama_hint = QLabel(
            "Google still translates. This model only polishes the English afterward."
        )
        self.ollama_hint.setObjectName("hintLabel")
        self.ollama_hint.setWordWrap(True)
        ollama_box = QVBoxLayout()
        ollama_box.setContentsMargins(0, 0, 0, 0)
        ollama_box.setSpacing(4)
        ollama_box.addLayout(self.ollama_row)
        ollama_box.addWidget(self.ollama_hint)
        self.ollama_wrap = QWidget()
        self.ollama_wrap.setLayout(ollama_box)
        root.addWidget(self.ollama_wrap)
        self._sync_ollama_row()

        self._refresh_folder_label()
        self._ollama_pull_thread = None
        self._ollama_pull_worker = None
        self._ollama_pull_dlg = None
        self._ollama_pull_model = ""
        self._ollama_pull_url = ""
        self._ollama_pull_result = None
        self._ollama_pull_purpose = "translator"
        self.translate_cb.stateChanged.connect(self._sync_polish_enabled)
        self._sync_polish_enabled()

    def _on_backend_changed(self, text: str):
        if text == "Ollama":
            # Finish after the combo has settled. Nested dialogs/event loops
            # inside currentTextChanged can hard-crash Qt on Windows.
            QTimer.singleShot(0, self._finish_switch_to_ollama)
            return
        self._last_non_ollama = self._backend_value()
        if self.workers.value() <= 4:
            self.workers.setValue(200)
        self._sync_polish_enabled()
        self._sync_ollama_row()
        self._emit_options()

    @Slot()
    def _finish_switch_to_ollama(self):
        if self.backend.currentText() != "Ollama":
            return
        if self._ollama_pull_thread is not None:
            return
        from core.translator import GoogleTranslator, probe_ollama

        url = self.ollama_url.text().strip() or "http://127.0.0.1:11434"
        recommended = GoogleTranslator.DEFAULT_OLLAMA_MODEL
        installed = probe_ollama(url)
        if installed is None:
            self._warn_ollama_unavailable()
            self._revert_from_ollama()
            return
        if installed:
            self._use_installed_ollama_model(installed, recommended)
            self._apply_ollama_selected()
            return

        prev = self._backend_label(self._last_non_ollama)
        reply = QMessageBox.question(
            self.window(),
            "Download a local model?",
            "Ollama is running but has no models yet.\n\n"
            f"Download {recommended} now? About 2 GB, one-time.\n\n"
            f"If you choose No, translator stays on {prev}.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            self._revert_from_ollama()
            return
        self._ollama_pull_purpose = "translator"
        self._ollama_pull_model = recommended
        self._ollama_pull_url = url
        QTimer.singleShot(0, self._start_ollama_pull)

    def _apply_ollama_selected(self):
        if self.workers.value() >= 50:
            self.workers.setValue(2)
        self.polish_cb.blockSignals(True)
        self.polish_cb.setChecked(False)
        self.polish_cb.blockSignals(False)
        self._sync_polish_enabled()
        self._sync_ollama_row()
        self._emit_options()

    def _polish_active(self) -> bool:
        return (
            self.translate_cb.isChecked()
            and self.polish_cb.isChecked()
            and self.backend.currentText() != "Ollama"
        )

    def _sync_polish_enabled(self, *_args):
        ollama = self.backend.currentText() == "Ollama"
        can_polish = self.translate_cb.isChecked() and not ollama
        self.polish_cb.setEnabled(can_polish)
        # Remember polish when Translate is toggled off. Only clear it when
        # Ollama is already the translator (extra polish would be redundant).
        if ollama and self.polish_cb.isChecked():
            self.polish_cb.blockSignals(True)
            self.polish_cb.setChecked(False)
            self.polish_cb.blockSignals(False)
        self._sync_option_hints()
        self._sync_ollama_row()

    def _on_polish_changed(self, *_args):
        self._sync_ollama_row()
        self._emit_options()

    def _uncheck_polish(self):
        self.polish_cb.blockSignals(True)
        self.polish_cb.setChecked(False)
        self.polish_cb.blockSignals(False)
        self._sync_ollama_row()
        self._emit_options()

    def _revert_from_ollama(self):
        self.backend.setEnabled(True)
        self.backend.blockSignals(True)
        self.backend.setCurrentText(self._backend_label(self._last_non_ollama))
        self.backend.blockSignals(False)
        self._sync_ollama_row()
        self._emit_options()

    def _warn_ollama_unavailable(self, for_polish: bool = False):
        from PySide6.QtCore import QUrl
        from core.translator import ollama_is_installed

        prev = self._backend_label(self._last_non_ollama)
        stay = (
            "Polish English will stay off."
            if for_polish
            else f"Translator will stay on {prev}."
        )
        parent = self.window()
        box = QMessageBox(parent)
        box.setIcon(QMessageBox.Icon.Warning)
        if ollama_is_installed():
            box.setWindowTitle("Ollama is not running")
            box.setText("Ollama is installed but not running.")
            box.setInformativeText(
                "Start Ollama from the Start menu (or run ollama serve), "
                "then try again.\n\n"
                f"{stay}"
            )
            box.addButton("OK", QMessageBox.ButtonRole.AcceptRole)
            box.exec()
            return

        box.setWindowTitle("Ollama is not installed")
        box.setText("Ollama is not installed on this PC.")
        box.setInformativeText(
            "HuaEPUB can use a local model after you install Ollama "
            "from https://ollama.com\n\n"
            f"{stay}"
        )
        open_btn = box.addButton(
            "Open ollama.com", QMessageBox.ButtonRole.AcceptRole
        )
        box.addButton("OK", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is open_btn:
            QDesktopServices.openUrl(QUrl("https://ollama.com"))

    @Slot()
    def _start_ollama_pull(self):
        from gui.workers.ollama_worker import OllamaPullWorker

        if self._ollama_pull_thread is not None:
            return
        model = self._ollama_pull_model
        url = self._ollama_pull_url
        if not model:
            if self._ollama_pull_purpose == "polish":
                self._uncheck_polish()
            else:
                self._revert_from_ollama()
            return

        print(f"Starting Ollama pull of {model} from {url}")
        parent = self.window()
        dlg = QProgressDialog(f"Downloading {model}…", "Cancel", 0, 100, parent)
        dlg.setWindowTitle("Ollama")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setValue(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)

        thread = QThread(self)
        worker = OllamaPullWorker(model, url)
        worker.moveToThread(thread)
        self._ollama_pull_thread = thread
        self._ollama_pull_worker = worker
        self._ollama_pull_dlg = dlg
        self.backend.setEnabled(False)

        thread.started.connect(worker.run)
        worker.progress.connect(
            self._on_ollama_pull_progress, Qt.ConnectionType.QueuedConnection
        )
        worker.finished.connect(
            self._on_ollama_pull_finished, Qt.ConnectionType.QueuedConnection
        )
        dlg.canceled.connect(
            self._cancel_ollama_pull, Qt.ConnectionType.QueuedConnection
        )
        thread.start()
        dlg.show()

    @Slot()
    def _cancel_ollama_pull(self):
        # Set the flag on the GUI thread. The worker thread is blocked in
        # the HTTP pull, so a queued Slot on the worker would never run.
        worker = self._ollama_pull_worker
        if worker is not None:
            worker._cancel = True

    @Slot(int, str)
    def _on_ollama_pull_progress(self, pct: int, status: str):
        dlg = self._ollama_pull_dlg
        if dlg is None:
            return
        if pct >= 0:
            dlg.setValue(min(100, pct))
        model = self._ollama_pull_model or "model"
        if status:
            dlg.setLabelText(f"Downloading {model}…\n{status}")

    @Slot(bool, str)
    def _on_ollama_pull_finished(self, ok: bool, error: str):
        self._ollama_pull_result = {"ok": ok, "error": error}
        dlg = self._ollama_pull_dlg
        if dlg is not None:
            if ok:
                dlg.setValue(100)
            dlg.close()
        self._ollama_pull_dlg = None
        thread = self._ollama_pull_thread
        if thread is not None:
            thread.quit()
        # Do not wait() here — we are still in the worker's finished delivery.
        QTimer.singleShot(0, self._after_ollama_pull)

    @Slot()
    def _after_ollama_pull(self):
        thread = self._ollama_pull_thread
        if thread is not None and thread.isRunning():
            thread.wait(8000)
        self._ollama_pull_thread = None
        self._ollama_pull_worker = None
        self.backend.setEnabled(True)

        result = getattr(self, "_ollama_pull_result", {}) or {}
        ok = bool(result.get("ok"))
        error = result.get("error") or ""
        model = self._ollama_pull_model
        purpose = self._ollama_pull_purpose or "translator"
        if not ok:
            err = error or "Download failed"
            print(f"Ollama pull failed: {err}")
            stay = (
                "Polish English will stay off."
                if purpose == "polish"
                else (
                    "Translator will stay on "
                    f"{self._backend_label(self._last_non_ollama)}."
                )
            )
            QMessageBox.warning(
                self.window(),
                "Ollama download failed",
                f"{err}\n\n{stay}",
            )
            if purpose == "polish":
                self._uncheck_polish()
            else:
                self._revert_from_ollama()
            return
        print(f"Ollama pull finished: {model}")
        self.ollama_model.blockSignals(True)
        if self.ollama_model.findText(model) < 0:
            self.ollama_model.addItem(model)
        self.ollama_model.setCurrentText(model)
        self.ollama_model.blockSignals(False)
        if purpose == "polish":
            self.polish_cb.blockSignals(True)
            self.polish_cb.setChecked(True)
            self.polish_cb.blockSignals(False)
            self._sync_ollama_row()
            self._emit_options()
            return
        self.backend.blockSignals(True)
        self.backend.setCurrentText("Ollama")
        self.backend.blockSignals(False)
        self._apply_ollama_selected()

    def _use_installed_ollama_model(self, installed, recommended: str):
        from core.translator import resolve_ollama_model

        preferred = self.ollama_model.currentText().strip() or recommended
        chosen = resolve_ollama_model(preferred, installed)
        self.ollama_model.blockSignals(True)
        if chosen and self.ollama_model.findText(chosen) < 0:
            self.ollama_model.addItem(chosen)
        if chosen:
            self.ollama_model.setCurrentText(chosen)
        self.ollama_model.blockSignals(False)

    def _sync_option_hints(self):
        ollama = self.backend.currentText() == "Ollama"
        translate_on = self.translate_cb.isChecked()
        polish_on = self._polish_active()
        if ollama:
            self.polish_cb.setToolTip(
                "Already translating with Ollama — extra polish is not needed."
            )
            self.workers.setToolTip(
                "Ollama can only handle a few requests at once. 1–4 is typical."
            )
        elif not translate_on:
            self.polish_cb.setToolTip(
                "Turn on Translate to English first, then you can polish "
                "the English with a local model."
            )
            self.workers.setToolTip(
                "How many Google/LibreTranslate requests to run at once. "
                "200 is the default."
            )
        else:
            self.polish_cb.setToolTip(
                "After Google or LibreTranslate finishes, copy-edit awkward English "
                "on this PC. First run downloads llama.cpp + a Qwen GGUF that fits "
                "this GPU — Ollama is not required. Same EPUB (no extra copy). "
                "Progress is in File → Open log file."
            )
            if polish_on:
                self.workers.setToolTip(
                    "Workers are for Google/LibreTranslate only. Polish runs "
                    "separately and is not affected. Leave this at 200."
                )
            else:
                self.workers.setToolTip(
                    "How many Google/LibreTranslate requests to run at once. "
                    "200 is the default."
                )
        if hasattr(self, "ollama_hint"):
            if polish_on:
                self.ollama_hint.setText(
                    "Google still translates. Polish then uses llama.cpp on this PC "
                    "(vLLM/Ollama only if already running). Only awkward spans, not the whole book."
                )
                self.ollama_hint.setVisible(True)
            elif ollama:
                self.ollama_hint.setText(
                    "Ollama translates the whole book on this PC. This is much slower than Google."
                )
                self.ollama_hint.setVisible(True)
            else:
                self.ollama_hint.setVisible(False)

    def _sync_ollama_row(self):
        visible = self.backend.currentText() == "Ollama"
        self.ollama_wrap.setVisible(visible)
        self._sync_option_hints()
        if visible:
            self._refresh_ollama_models()

    def _refresh_ollama_models(self):
        """List models already on this PC. Never pulls. No-op if Ollama is down."""
        from core.translator import list_ollama_models, resolve_ollama_model

        url = self.ollama_url.text().strip() or "http://127.0.0.1:11434"
        preferred = self.ollama_model.currentText().strip()
        installed = list_ollama_models(url)
        chosen = resolve_ollama_model(preferred, installed)
        self.ollama_model.blockSignals(True)
        self.ollama_model.clear()
        if installed:
            self.ollama_model.addItems(installed)
            self.ollama_model.setCurrentText(chosen)
            self.ollama_model.setToolTip(
                "Models already on this PC. HuaEPUB uses one of these — "
                "it will not download another unless none are installed."
            )
        else:
            if chosen:
                self.ollama_model.addItem(chosen)
                self.ollama_model.setCurrentText(chosen)
            self.ollama_model.setToolTip(
                "Ollama did not report any models (not running, or none pulled). "
                "Install Ollama from ollama.com, then try again."
            )
        self.ollama_model.blockSignals(False)
        if chosen != preferred:
            self._emit_options()

    def _backend_value(self) -> str:
        t = self.backend.currentText()
        if t == "LibreTranslate":
            return "libretranslate"
        if t == "Ollama":
            return "ollama"
        return "google"

    def _backend_label(self, value: str) -> str:
        if value == "libretranslate":
            return "LibreTranslate"
        if value == "ollama":
            return "Ollama"
        return "Google"

    def _emit_options(self, *_args):
        self.options_changed.emit()

    def _refresh_folder_label(self):
        if self.session.output_dir:
            self.folder_label.setText(self.session.output_dir)
        else:
            self.folder_label.setText(str(get_default_books_dir()) + " (default)")

    def _browse(self):
        start = self.session.output_dir or str(get_default_books_dir())
        path = QFileDialog.getExistingDirectory(self, "Choose output folder", start)
        if path:
            self.session.output_dir = path
            self._refresh_folder_label()
            self.options_changed.emit()

    def _reset_folder(self):
        self.session.output_dir = ""
        self._refresh_folder_label()
        self.options_changed.emit()

    def snapshot(self) -> dict:
        return {
            "translate": self.translate_cb.isChecked(),
            "clean": self.clean_cb.isChecked(),
            "use_cache": self.cache_cb.isChecked(),
            "clipboard": self.clipboard_cb.isChecked(),
            "workers": self.workers.value(),
            "backend": self._backend_value(),
            "ollama_model": self.ollama_model.currentText().strip(),
            "ollama_url": self.ollama_url.text().strip() or "http://127.0.0.1:11434",
            "ollama_polish": self.polish_cb.isChecked(),
            "output_dir": self.session.output_dir or "",
        }

    def apply_snapshot(self, options: dict):
        if not options:
            return
        if "translate" in options:
            self.translate_cb.setChecked(bool(options["translate"]))
        if "clean" in options:
            self.clean_cb.setChecked(bool(options["clean"]))
        if "use_cache" in options:
            self.cache_cb.setChecked(bool(options.get("use_cache", True)))
        if "workers" in options:
            self.workers.setValue(int(options["workers"]))
        if "translation_backend" in options or "backend" in options:
            b = options.get("backend") or options.get("translation_backend")
            self.backend.blockSignals(True)
            self.backend.setCurrentText(self._backend_label(b))
            self.backend.blockSignals(False)
        if "ollama_model" in options and options["ollama_model"]:
            self.ollama_model.setCurrentText(str(options["ollama_model"]))
        if "ollama_url" in options and options["ollama_url"]:
            self.ollama_url.setText(str(options["ollama_url"]))
        if "ollama_polish" in options:
            self.polish_cb.blockSignals(True)
            self.polish_cb.setChecked(bool(options["ollama_polish"]))
            self.polish_cb.blockSignals(False)
        self._sync_polish_enabled()
        self._sync_ollama_row()
        if "output_dir" in options and options["output_dir"] is not None:
            self.session.output_dir = options["output_dir"] or ""
            self._refresh_folder_label()
