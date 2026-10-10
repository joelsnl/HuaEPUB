# Author: joelsnl and Anthropic Claude
"""MainWindow mixin: Polish glossaries with Qwen (startup offer, menu, modal)."""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import QProgressDialog

from core.download_job import load_job
from core.settings import set_setting
from gui.dialogs import (
    ask_accept_glossary_proposals, ask_yes_no, ask_yes_not_now_dont_ask, busy_message,
    show_error, show_info, show_warning,
)
from gui.workers.glossary_worker import GlossaryQwenWorker


class GlossaryActionsMixin:
    _GLOSSARY_QWEN_PROMPT = (
        "Use the local Qwen model (same llama.cpp GGUF as Polish English) to "
        "classify names and domain terms found in your books.\n\n"
        "This dialog stays in front until the pass finishes — you will not be "
        "able to download while it runs. The polish GGUF must already be on "
        "disk (this will not start a 2–9 GB download).\n\n"
        "You will get a list to Accept all or Discard. "
        "Everyday Chinese is not added (this is not a general dictionary)."
    )

    def _library_glossary_books(self) -> list[dict]:
        books = []
        for entry in self.session.library_store.get_library():
            name = (entry.title or entry.translated_title or "").strip()
            if not name:
                continue
            books.append({
                "title": name,
                "source_url": entry.source_url or "",
                "description": getattr(entry, "description", "") or "",
            })
        return books

    def _maybe_offer_glossary_qwen(self):
        if self._worker_busy or self.session.control.is_downloading or self._serving():
            return
        if load_job(self.session.data_dir):
            return
        from core.translation.qwen_glossary import (
            has_harvested_terms,
            polish_gguf_on_disk,
            qwen_glossary_capable,
            should_offer_glossary_qwen,
        )

        if not should_offer_glossary_qwen(
            self.session.settings,
            has_library=bool(self.session.library_store.get_library()),
            has_harvested=has_harvested_terms(),
            model_ready=polish_gguf_on_disk(),
            qwen_capable=qwen_glossary_capable(),
        ):
            return
        choice = ask_yes_not_now_dont_ask(
            self,
            "Polish glossaries with Qwen?",
            self._GLOSSARY_QWEN_PROMPT,
        )
        if choice == "later":
            return
        if choice == "never":
            self.session.settings["glossary_qwen_ask"] = False
            set_setting("glossary_qwen_ask", False)
            return
        self._run_glossary_qwen_modal()

    def _menu_glossary_qwen(self):
        if self._worker_busy or self.session.control.is_downloading:
            show_warning(self, "Busy", busy_message("polish glossaries with Qwen"))
            return
        from core.translation.qwen_glossary import (
            polish_gguf_on_disk,
            qwen_glossary_capable,
        )

        if not polish_gguf_on_disk():
            show_warning(
                self,
                "Glossary · Qwen",
                "The polish GGUF is not on disk yet. Tick Polish English once "
                "so llama.cpp can download it, then run this again. "
                "Glossary Qwen will not start a 2–9 GB download by itself.",
            )
            return
        if not qwen_glossary_capable():
            show_warning(
                self,
                "Glossary · Qwen",
                "This PC is on a 3B polish profile. Glossary classification "
                "needs the 7B or 14B Qwen GGUF. Names are still romanized "
                "with pinyin during translate.",
            )
            return
        if not ask_yes_no(self, "Polish glossaries with Qwen?", self._GLOSSARY_QWEN_PROMPT):
            return
        self._run_glossary_qwen_modal()

    def _run_glossary_qwen_modal(self):
        dlg = QProgressDialog(
            "Starting local Qwen…", "Cancel", 0, 0, self
        )
        dlg.setWindowTitle("Glossary · Qwen")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        self._glossary_qwen_dlg = dlg
        worker = GlossaryQwenWorker(
            self._library_glossary_books(),
            cache=self.session.cache,
        )
        dlg.canceled.connect(worker.request_cancel)
        if not self._bind_and_run(
            worker,
            (worker.progress, self._on_glossary_qwen_progress),
            (worker.finished_ok, self._on_glossary_qwen_ok),
            (worker.finished_error, self._on_glossary_qwen_error),
        ):
            dlg.close()
            self._glossary_qwen_dlg = None
            show_warning(self, "Busy", busy_message("polish glossaries with Qwen"))
            return
        dlg.show()

    @Slot(str)
    def _on_glossary_qwen_progress(self, status: str):
        dlg = self._glossary_qwen_dlg
        if dlg is not None and status:
            dlg.setLabelText(status)

    def _close_glossary_qwen_dlg(self):
        dlg = self._glossary_qwen_dlg
        self._glossary_qwen_dlg = None
        if dlg is not None:
            dlg.close()

    @Slot(object)
    def _on_glossary_qwen_ok(self, payload):
        self._close_glossary_qwen_dlg()
        self._finish_worker_later()
        now = time.time()
        self.session.settings["glossary_qwen_last_at"] = now
        set_setting("glossary_qwen_last_at", now)
        if isinstance(payload, dict):
            message = str(payload.get("message") or "Done.")
            proposals = list(payload.get("proposals") or [])
        else:
            message = str(payload or "Done.")
            proposals = []
        if proposals:
            from core.translation.qwen_glossary import apply_glossary_proposals

            if ask_accept_glossary_proposals(self, proposals):
                added, updated = apply_glossary_proposals(proposals)
                show_info(
                    self,
                    "Glossaries updated",
                    f"Accepted {added + updated} term(s). {message}",
                )
                return
            show_info(self, "Glossary polish", "Discarded. Nothing was written.")
            return
        show_info(self, "Glossaries updated", message)

    @Slot(str)
    def _on_glossary_qwen_error(self, message: str):
        self._close_glossary_qwen_dlg()
        self._finish_worker_later()
        if "cancel" in (message or "").casefold():
            show_info(self, "Glossary polish", message)
            return
        show_error(self, "Glossary polish failed", message)
