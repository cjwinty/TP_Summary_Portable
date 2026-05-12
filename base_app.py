"""
base_app.py
===========
BaseApp — shared logic for all GUI layouts.

    The entry point tp_query.py subclasses BaseApp and
implements only setup_ui(), which arranges the widgets.

BaseApp expects its subclass to create these attributes in setup_ui():
    self.ids_entry          — CTkEntry  (request IDs, comma-separated)
    self.range_entry        — CTkEntry  (cache range, "start-end")
    self.update_cache_var   — BooleanVar
    self.run_button         — CTkButton  (Summarize)
    self.update_button      — CTkButton  (Update Cache)
    self.cache_range_button — CTkButton
    self.stop_cache_button  — CTkButton
    self.progress_label     — CTkLabel
    self.results_text       — CTkTextbox
"""

import sys
import customtkinter as ctk
from threading import Thread

from config import PROJECT_NAME, validate_env
from api import get_comments
from analysis import summarize_batch, deduplicate_comment_dicts
from database import (
    init_db, delete_comments,
    save_summary, get_summary_with_cache_time,
    get_all_cached_ids
)
from settings_window import SettingsWindow
from windows import CommentsWindow, SummariesWindow, SearchCacheWindow

MAX_REQUEST_IDS = 500


class BaseApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(f"TP Query - {PROJECT_NAME or 'Not Configured'}")
        self.resizable(True, True)

        self.running = False
        self.cache_running = False

        init_db()

        env_valid, env_errors = validate_env()
        if not env_valid:
            self.after(200, lambda: self._show_env_warning(env_errors))

        self.setup_ui()  # implemented by each layout subclass

    def _show_env_warning(self, errors):
        try:
            import tkinter.messagebox
            tkinter.messagebox.showwarning(
                "Configuration Required",
                "The application is not fully configured.\n\n"
                + "\n".join(f"• {e}" for e in errors)
                + "\n\nCopy .env.example to .env and fill in your credentials."
            )
        except Exception:
            pass

    # ── Must be implemented by subclass ───────────────────────────

    def setup_ui(self):
        raise NotImplementedError("Subclasses must implement setup_ui()")

    # ── Actions (shared by all layouts) ───────────────────────────

    def open_settings(self):
        SettingsWindow(self)

    def view_cached_comments(self):
        CommentsWindow(self)

    def view_summaries(self):
        SummariesWindow(self)

    def search_cache(self):
        SearchCacheWindow(self)

    def open_chain_builder(self):
        """Open the Prompt Chain Builder window."""
        from chain_window import ChainWindow
        ChainWindow(self)

    # ── Summarize ──────────────────────────────────────────────────

    def run_analysis(self):
        if self.running:
            return
        self.running = True
        self.run_button.configure(state="disabled")
        self.progress_label.configure(text="Starting...")
        self.results_text.delete("1.0", "end")
        Thread(target=self._analyze, daemon=True).start()

    def _analyze(self):
        try:
            use_cache = not self.update_cache_var.get()
            ids_text = self.ids_entry.get().strip()

            if not ids_text:
                self._append_results("Please enter request IDs.\n")
                return

            try:
                request_ids = [int(x.strip()) for x in ids_text.split(",")]
            except ValueError:
                self._append_results("Invalid ID format. Use comma-separated integers.\n")
                return

            if len(request_ids) > MAX_REQUEST_IDS:
                self._append_results(f"Too many IDs. Maximum is {MAX_REQUEST_IDS}.\n")
                return

            self._set_progress(f"Summarizing {len(request_ids)} requests...")

            all_comments = []
            request_ids_to_summarize = []
            cached_count = fetched_count = 0
            error_ids = []
            empty_ids = []

            for i, request_id in enumerate(request_ids):
                if i % 10 == 0:
                    self._set_progress(f"Fetching comments... {i}/{len(request_ids)}")

                comments, fetched_at, fresh = get_comments(request_id, use_cache=use_cache)

                if fresh:
                    fetched_count += 1
                else:
                    cached_count += 1

                if comments is None:
                    error_ids.append(request_id)
                    continue

                if not comments:
                    empty_ids.append(request_id)
                    continue

                source = f" (cached from {fetched_at[:10]})" if fetched_at else " (fresh)"
                summary_data = get_summary_with_cache_time(request_id)

                # Check if cached summary is valid (not an error)
                cached_summary = summary_data.get("summary", "") if summary_data else ""
                has_error = cached_summary.startswith("Error") or "API error" in cached_summary

                if summary_data and not fresh and summary_data.get("fetched_at") == fetched_at and not has_error:
                    self._append_results(
                        f"[Request #{request_id}]{source} (cached summary)\n{cached_summary}\n\n"
                    )
                else:
                    if has_error:
                        # Log and re-fetch since cached summary has an error
                        import logging
                        logging.getLogger(__name__).info(f"Cached summary for #{request_id} contains error, re-fetching")
                    unique_lines = deduplicate_comment_dicts(comments)
                    if unique_lines:
                        all_comments.append(f"[Request #{request_id}]{source}:\n" + "\n".join(unique_lines))
                        request_ids_to_summarize.append(request_id)

            if not all_comments:
                if error_ids:
                    msg = (
                        "No comments could be fetched from the API.\n"
                        "Possible causes:\n"
                        "  - Network / proxy connectivity issue\n"
                        "  - SSL certificate verification failure\n"
                        "  - Invalid credentials in .env\n"
                        "  - Targetprocess API is unreachable"
                    )
                    msg += "\n\nCheck tp_query_error.log alongside this application for details.\n"
                    self._append_results(msg)
                if empty_ids:
                    id_list = ", ".join(str(i) for i in empty_ids)
                    self._append_results(f"No comments found for request ID(s): {id_list}\n")
                return

            self._append_results(f"Using {cached_count} cached, {fetched_count} fresh\n\n")
            self._set_progress(f"Summarizing {len(all_comments)} requests...")

            summaries = summarize_batch(all_comments)

            self._append_results("=" * 60 + "\nSUPPORT CALL SUMMARY\n" + "=" * 60 + "\n\n")

            for i, summary in enumerate(summaries):
                self._append_results(summary + "\n\n")
                save_summary(request_ids_to_summarize[i], summary)

            self._set_progress("Done! Summaries saved to database.")

        except Exception as e:
            self._append_results(f"Error: {e}\nCheck tp_query_error.log for details.\n")
        finally:
            self.running = False
            self.after(0, lambda: self.run_button.configure(state="normal"))

    # ── Cache: update specific IDs ─────────────────────────────────

    def update_cache(self):
        from tkinter import messagebox
        ids_text = self.ids_entry.get().strip()
        if not ids_text:
            messagebox.showwarning("Warning", "Please enter request IDs first.")
            return
        if self.cache_running:
            return
        self.cache_running = True
        self.update_button.configure(state="disabled")
        self._set_progress(f"Updating cache for {ids_text}...")
        Thread(target=self._update_cache, args=(ids_text,), daemon=True).start()

    def _update_cache(self, ids_text):
        try:
            request_ids = [int(x.strip()) for x in ids_text.split(",")]
            for i, rid in enumerate(request_ids):
                if i % 10 == 0:
                    self._set_progress(f"Updating... {i}/{len(request_ids)}")
                delete_comments(rid)
                get_comments(rid, use_cache=False)
            self._set_progress(f"Cache updated for {len(request_ids)} requests")
        except ValueError:
            self._set_progress("Invalid ID format. Use comma-separated integers.")
        except Exception as e:
            self._set_progress(f"Cache error: {e}")
        finally:
            self.cache_running = False
            self.after(0, lambda: self.update_button.configure(state="normal"))

    # ── Cache: range ───────────────────────────────────────────────

    def cache_range(self):
        from tkinter import messagebox
        try:
            text = self.range_entry.get().strip()
            start, end = map(int, text.split("-"))
        except Exception:
            messagebox.showerror("Error", "Enter range as start-end (e.g. 1000-2000)")
            return

        self.cache_range_button.configure(state="disabled")
        self.stop_cache_button.configure(state="normal")
        self._set_progress(f"Caching range {start}-{end}...")
        self.cache_running = True
        Thread(target=self._do_cache_range, args=(start, end), daemon=True).start()

    def _do_cache_range(self, start, end):
        total = end - start + 1
        count = 0
        for rid in range(start, end + 1):
            if not self.cache_running:
                break
            get_comments(rid, use_cache=False)
            count += 1
            percent = int((count / total) * 100)
            self._update_cache_progress(percent, count, total)
        self._set_progress(f"Cached {count} requests")
        self.after(0, lambda: (
            self.cache_range_button.configure(state="normal"),
            self.stop_cache_button.configure(state="disabled"),
            self.cache_progress.set(0),
            self.cache_progress_label.configure(text="0%")
        ))

    def _update_cache_progress(self, percent, count, total):
        def _update():
            self.cache_progress.set(percent / 100.0)
            self.cache_progress_label.configure(text=f"{percent}% ({count}/{total})")
        try:
            self.after(0, _update)
        except Exception:
            pass

    def stop_cache(self):
        self.cache_running = False
        self.cache_range_button.configure(state="normal")
        self.stop_cache_button.configure(state="disabled")

    # ── Thread-safe UI helpers ─────────────────────────────────────

    def _set_progress(self, text):
        self.after(0, lambda: self.progress_label.configure(text=text))

    def _append_results(self, text):
        self.after(0, lambda: self.results_text.insert("end", text))
