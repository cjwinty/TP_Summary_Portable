"""
windows.py
==========
Shared popup windows used by all GUI layouts.
- CommentsWindow
- SummariesWindow
- SearchCacheWindow
"""

import customtkinter as ctk
from threading import Thread
from datetime import datetime
import re
from tkinter import messagebox
from tkcalendar import Calendar

from analysis import (
    deduplicate_text,
    refine_search_query, summarize_search_results
)
from database import (
    get_cached_comments, get_all_summaries,
    get_summaries_page, get_summary_count,
    search_cached_comments, search_summaries,
    search_and_fetch_full,
    get_all_custom_field_names as get_custom_field_names,
    get_custom_field_values,
    delete_summary, get_summary
)
from CTkScrollableDropdown import CTkScrollableDropdown


def parse_dotnet_date(date_str):
    match = re.match(r'/Date\((\d+)([+-]\d{4})\)/', date_str)
    if match:
        timestamp = int(match.group(1)) / 1000
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    return date_str


class CommentsWindow(ctk.CTkToplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("Cached Comments")
        self.geometry("900x700")
        self.resizable(True, True)

        search_frame = ctk.CTkFrame(self)
        search_frame.pack(fill="x", padx=10, pady=10)

        ctk.CTkLabel(search_frame, text="Request ID:").pack(side="left", padx=5)
        self.search_var = ctk.StringVar()
        self.search_entry = ctk.CTkEntry(search_frame, textvariable=self.search_var, width=100)
        self.search_entry.pack(side="left", padx=5)
        self.search_entry.bind("<Return>", lambda _: self.on_search())

        self.sort_var = ctk.StringVar(value="desc")
        ctk.CTkRadioButton(search_frame, text="Newest first", variable=self.sort_var, value="desc", command=self.reload).pack(side="left", padx=10)
        ctk.CTkRadioButton(search_frame, text="Oldest first", variable=self.sort_var, value="asc", command=self.reload).pack(side="left")

        self.comments_text = ctk.CTkTextbox(self, wrap="word")
        self.comments_text.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.status_label = ctk.CTkLabel(self, text="Enter ID and press Enter", text_color="gray")
        self.status_label.pack(pady=(0, 10))

        self.comments = []
        self.fetched_at = None
        self.current_id = None

        self.attributes('-topmost', True)
        self.lift()
        self.focus_force()
        self.after(100, lambda: self.attributes('-topmost', False))

    def on_search(self, event=None):
        query = self.search_var.get().strip()
        if not query:
            return
        try:
            request_id = int(query)
        except ValueError:
            self.status_label.configure(text="Invalid ID - enter a number")
            return
        self.current_id = request_id
        self.show_comments(request_id)

    def reload(self):
        if self.current_id:
            self.show_comments(self.current_id)

    def show_comments(self, request_id):
        self.comments_text.delete("1.0", "end")

        comments, fetched_at = get_cached_comments(request_id)
        self.comments = comments
        self.fetched_at = fetched_at

        if not comments:
            self.status_label.configure(text=f"ID {request_id} not in cache")
            return

        sorted_comments = sorted(
            comments,
            key=lambda x: x.get("date") or "",
            reverse=(self.sort_var.get() == "desc")
        )

        self.status_label.configure(
            text=f"Request #{request_id} | {len(sorted_comments)} comments | Cached: {fetched_at}"
        )
        self.comments_text.insert(
            "1.0",
            f"Request #{request_id} | {len(sorted_comments)} comments | Cached: {fetched_at}\n{'='*60}\n\n"
        )

        for i, comment in enumerate(sorted_comments, 1):
            text = comment.get("text", "")
            date = comment.get("date", "Unknown date")
            if date and date != "Unknown date":
                date = parse_dotnet_date(date)
            # Text is already cleaned at storage time; just deduplicate repeated lines
            display_text = deduplicate_text(text)
            self.comments_text.insert("end", f"[{i}] {date}\n{display_text}\n\n")


class SummariesWindow(ctk.CTkToplevel):
    PAGE_SIZE = 50

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Saved Summaries")
        self.geometry("900x600")
        self.resizable(True, True)

        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=2)
        self.grid_rowconfigure(1, weight=1)

        search_row = ctk.CTkFrame(self)
        search_row.grid(row=0, column=0, sticky="ew", padx=(10, 5), pady=(10, 5))

        self.search_var = ctk.StringVar()
        self.search_entry = ctk.CTkEntry(search_row, textvariable=self.search_var, placeholder_text="Search by ID...")
        self.search_entry.pack(side="left", padx=5, fill="x", expand=True)
        self.search_entry.bind("<Return>", lambda _: self.search_by_id())

        ctk.CTkButton(search_row, text="Search", command=self.search_by_id, width=60).pack(side="left", padx=2)
        ctk.CTkButton(search_row, text="Clear", command=self.clear_search, width=60).pack(side="left", padx=2)

        left_frame = ctk.CTkScrollableFrame(self, label_text="Summaries", width=250)
        left_frame.grid(row=1, column=0, sticky="nsew", padx=(10, 5), pady=(5, 10))

        self.list_frame = left_frame
        self.summary_buttons = []

        self.summary_text = ctk.CTkTextbox(self, wrap="word")
        self.summary_text.grid(row=1, column=1, sticky="nsew", padx=(5, 10), pady=10)

        self.button_row = ctk.CTkFrame(self)
        self.button_row.grid(row=0, column=1, sticky="ew", padx=(5, 10), pady=(10, 5))

        ctk.CTkButton(
            self.button_row,
            text="Delete",
            command=self.delete_selected_summary,
            fg_color="#c44",
            hover_color="#a33"
        ).pack(side="right", padx=5)

        self.selected_id = None
        self.total_count = 0
        self.loaded_count = 0
        self.load_more_btn = None

        self._load_initial_page()

        self.attributes('-topmost', True)
        self.lift()
        self.focus_force()
        self.after(100, lambda: self.attributes('-topmost', False))

    def _load_initial_page(self):
        self.total_count = get_summary_count()
        self.summaries = []
        self._clear_list()
        self._append_page(0)

    def _append_page(self, offset):
        page = get_summaries_page(limit=self.PAGE_SIZE, offset=offset)
        if not page:
            self._update_load_more_btn()
            return
        self.summaries.extend(page)
        idx_start = len(self.summaries) - len(page)
        for i, s in enumerate(page):
            btn = ctk.CTkButton(
                self.list_frame,
                text=f"#{s['id']} - {s['created'][:10]}",
                command=lambda idx=idx_start + i: self.show_summary(idx),
                height=28
            )
            btn.pack(pady=2, padx=5, fill="x")
            self.summary_buttons.append(btn)
        self.loaded_count = len(self.summaries)
        self._update_load_more_btn()

    def _clear_list(self):
        for btn in self.summary_buttons:
            btn.destroy()
        self.summary_buttons.clear()
        if self.load_more_btn:
            self.load_more_btn.destroy()
            self.load_more_btn = None

    def _update_load_more_btn(self):
        if self.load_more_btn:
            self.load_more_btn.destroy()
            self.load_more_btn = None
        remaining = self.total_count - self.loaded_count
        if remaining > 0:
            self.load_more_btn = ctk.CTkButton(
                self.list_frame,
                text=f"Load More ({remaining} remaining)",
                command=self._load_more,
                height=32,
                fg_color="#2a6",
                hover_color="#184"
            )
            self.load_more_btn.pack(pady=4, padx=5, fill="x")

    def _load_more(self):
        if self.load_more_btn:
            self.load_more_btn.configure(state="disabled", text="Loading...")
        self._append_page(self.loaded_count)

    def search_by_id(self):
        search_id = self.search_var.get().strip()
        if not search_id:
            self._load_initial_page()
            return
        from database import get_all_summaries
        all_s = get_all_summaries()
        filtered = [s for s in all_s if str(s['id']) == search_id]
        self._clear_list()
        self.summaries = [{"id": s["id"], "created": s["created"]} for s in filtered]
        self.loaded_count = len(self.summaries)
        if not filtered:
            ctk.CTkLabel(self.list_frame, text="No results found", text_color="gray").pack(pady=10)
            return
        for i, s in enumerate(self.summaries):
            btn = ctk.CTkButton(
                self.list_frame,
                text=f"#{s['id']} - {s['created'][:10]}",
                command=lambda idx=i: self.show_summary(idx),
                height=28
            )
            btn.pack(pady=2, padx=5, fill="x")
            self.summary_buttons.append(btn)
        self.show_summary(0)

    def clear_search(self):
        self.search_var.set("")
        self._load_initial_page()

    def show_summary(self, idx):
        if idx >= len(self.summaries):
            return
        s = self.summaries[idx]
        self.selected_id = s['id']
        self.summary_text.delete("1.0", "end")
        self.summary_text.insert("1.0", f"Request #{s['id']}\nCreated: {s['created']}\n{'='*50}\n\nLoading...")
        self.update_idletasks()
        text, created = get_summary(s['id'])
        self.summary_text.delete("1.0", "end")
        self.summary_text.insert(
            "1.0",
            f"Request #{s['id']}\nCreated: {created or s['created']}\n{'='*50}\n\n{text or '(empty)'}"
        )

    def delete_selected_summary(self):
        if self.selected_id is None:
            messagebox.showinfo("Delete", "No summary selected.", parent=self)
            return
        if not messagebox.askyesno("Confirm Delete", f"Delete summary #{self.selected_id}?", parent=self):
            return
        del_id = self.selected_id
        delete_summary(del_id)
        self.selected_id = None
        self.summary_text.delete("1.0", "end")
        for i, s in enumerate(self.summaries):
            if s['id'] == del_id:
                self.summary_buttons[i].destroy()
                del self.summary_buttons[i]
                del self.summaries[i]
                self.total_count -= 1
                self.loaded_count -= 1
                break
        self._update_load_more_btn()


class SearchCacheWindow(ctk.CTkToplevel):
    MAX_FILTERS = 10

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Search Cache")
        self.geometry("1100x750")
        self.cancel_search_flag = False
        self.current_query = ""
        self.custom_field_names = get_custom_field_names()
        self.filter_rows = []

        self.setup_ui()

        self.attributes('-topmost', True)
        self.lift()
        self.focus_force()
        self.after(100, lambda: self.attributes('-topmost', False))

    def setup_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=2)
        self.grid_rowconfigure(4, weight=1)

        title = ctk.CTkLabel(
            self,
            text="Search Cached Comments & Summaries",
            font=ctk.CTkFont(size=18, weight="bold")
        )
        title.grid(row=0, column=0, pady=(15, 10), padx=20, sticky="w")

        # ── Controls card ──────────────────────────────────────────
        controls_card = ctk.CTkFrame(self, corner_radius=12)
        controls_card.grid(row=1, column=0, sticky="ew", padx=15, pady=(0, 10))
        controls_card.grid_columnconfigure(0, weight=1)

        # Search row
        search_row = ctk.CTkFrame(controls_card)
        search_row.pack(fill="x", padx=10, pady=10)

        ctk.CTkLabel(search_row, text="Search:").pack(side="left", padx=5)
        self.search_var = ctk.StringVar()
        self.search_entry = ctk.CTkEntry(search_row, textvariable=self.search_var, width=300)
        self.search_entry.pack(side="left", padx=5)
        self.search_entry.bind("<Return>", lambda _: self.on_search())

        self.search_button = ctk.CTkButton(search_row, text="Search", command=self.on_search, width=100)
        self.search_button.pack(side="left", padx=5)

        self.cancel_button = ctk.CTkButton(search_row, text="Cancel", command=self.cancel_search, width=100, state="disabled")
        self.cancel_button.pack(side="left", padx=5)

        # Options row
        options_row = ctk.CTkFrame(controls_card)
        options_row.pack(fill="x", padx=10, pady=(0, 10))

        self.refine_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(options_row, text="LLM Query Refinement", variable=self.refine_var).pack(side="left", padx=10)

        self.override_enabled_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(options_row, text="Append Custom Prompt", variable=self.override_enabled_var).pack(side="left", padx=10)

        self.skip_summary_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(options_row, text="Skip LLM Summary", variable=self.skip_summary_var).pack(side="left", padx=10)

        self.full_fetch_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(options_row, text="Full Record Retrieval", variable=self.full_fetch_var).pack(side="left", padx=10)

        self.override_prompt_var = ctk.StringVar()
        self.override_prompt_entry = ctk.CTkEntry(
            options_row, textvariable=self.override_prompt_var, width=300,
            placeholder_text="Custom prompt to append..."
        )
        self.override_prompt_entry.pack(side="left", padx=5)

        # Filter row
        filter_row = ctk.CTkFrame(controls_card)
        filter_row.pack(fill="x", padx=10, pady=(0, 10))

        self.filters_container = ctk.CTkFrame(filter_row)
        self.filters_container.pack(side="left", fill="x", expand=True)

        self.add_filter_row()

        self.add_filter_btn = ctk.CTkButton(filter_row, text="+ Add Filter", command=self.add_filter_row, width=100)
        self.add_filter_btn.pack(side="left", padx=(10, 5))

        ctk.CTkButton(filter_row, text="Clear All", command=self.clear_all_filters, width=80).pack(side="left", padx=5)

        # Date filter row
        date_row = ctk.CTkFrame(controls_card)
        date_row.pack(fill="x", padx=10, pady=(0, 10))

        ctk.CTkLabel(date_row, text="Date Filter:").pack(side="left", padx=(0, 10))

        self.start_date_var = ctk.StringVar()
        self.end_date_var = ctk.StringVar()

        def show_start_calendar():
            top = ctk.CTkToplevel(self)
            top.title("Select Start Date")
            top.geometry("250x250")
            top.attributes('-topmost', True)
            cal = Calendar(top, date_pattern="yyyy-mm-dd", selectmode="day")
            cal.pack(fill="both", expand=True, padx=10, pady=10)

            def on_select():
                self.start_date_var.set(cal.get_date())
                top.destroy()

            ctk.CTkButton(top, text="Select", command=on_select).pack(pady=(0, 10))

        def show_end_calendar():
            top = ctk.CTkToplevel(self)
            top.title("Select End Date")
            top.geometry("250x250")
            top.attributes('-topmost', True)
            cal = Calendar(top, date_pattern="yyyy-mm-dd", selectmode="day")
            cal.pack(fill="both", expand=True, padx=10, pady=10)

            def on_select():
                self.end_date_var.set(cal.get_date())
                top.destroy()

            ctk.CTkButton(top, text="Select", command=on_select).pack(pady=(0, 10))

        ctk.CTkLabel(date_row, text="From:").pack(side="left", padx=5)
        ctk.CTkButton(date_row, textvariable=self.start_date_var, command=show_start_calendar, width=130).pack(side="left", padx=5)

        ctk.CTkLabel(date_row, text="To:").pack(side="left", padx=5)
        ctk.CTkButton(date_row, textvariable=self.end_date_var, command=show_end_calendar, width=130).pack(side="left", padx=5)

        ctk.CTkButton(date_row, text="Clear Dates", command=self.clear_date_filter, width=100).pack(side="left", padx=10)

        # ── Progress ───────────────────────────────────────────────
        self.progress_label = ctk.CTkLabel(self, text="Ready", text_color="gray")
        self.progress_label.grid(row=2, column=0, padx=20, pady=(5, 5), sticky="w")

        # ── Results card ───────────────────────────────────────────
        results_card = ctk.CTkFrame(self, corner_radius=12)
        results_card.grid(row=3, column=0, sticky="nsew", padx=15, pady=5)
        results_card.grid_columnconfigure(0, weight=1)
        results_card.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(results_card, text="Search Results", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=10, pady=(10, 5), sticky="w"
        )
        self.results_text = ctk.CTkTextbox(results_card, wrap="word")
        self.results_text.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))

        # ── Summary card ───────────────────────────────────────────
        summary_card = ctk.CTkFrame(self, corner_radius=12)
        summary_card.grid(row=4, column=0, sticky="nsew", padx=15, pady=(5, 15))
        summary_card.grid_columnconfigure(0, weight=1)
        summary_card.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(summary_card, text="LLM Insights & Summary", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=10, pady=(10, 5), sticky="w"
        )
        self.summary_text = ctk.CTkTextbox(summary_card, wrap="word", height=150)
        self.summary_text.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))

    # ── Filter management ──────────────────────────────────────────

    def add_filter_row(self):
        if len(self.filter_rows) >= self.MAX_FILTERS:
            return

        row_frame = ctk.CTkFrame(self.filters_container)
        row_frame.pack(fill="x", pady=2)

        custom_field_var = ctk.StringVar()
        field_value_var = ctk.StringVar()
        logic_var = ctk.StringVar(value="AND")

        field_combo = ctk.CTkComboBox(
            row_frame, variable=custom_field_var,
            values=self.custom_field_names, state="readonly", width=120
        )
        field_combo.pack(side="left", padx=2)

        value_combo = ctk.CTkComboBox(row_frame, variable=field_value_var, state="readonly", width=200)
        value_combo.pack(side="left", padx=2)

        value_dropdown = CTkScrollableDropdown(value_combo, values=[], width=200, height=150, scrollbar=True)

        logic_combo = ctk.CTkComboBox(row_frame, variable=logic_var, values=["AND", "OR"], state="readonly", width=60)
        logic_combo.pack(side="left", padx=2)

        ctk.CTkButton(
            row_frame, text="×", width=30,
            command=lambda: self.remove_filter_row(row_frame)
        ).pack(side="left", padx=2)

        def on_field_changed(choice):
            if custom_field_var.get():
                values = get_custom_field_values(custom_field_var.get())
                value_combo.configure(values=values)
                value_dropdown.configure(values=values)
            else:
                value_combo.configure(values=[])
                value_dropdown.configure(values=[])
            field_value_var.set("")

        field_combo.configure(command=on_field_changed)

        self.filter_rows.append({
            "frame": row_frame,
            "field_var": custom_field_var,
            "value_var": field_value_var,
            "logic_var": logic_var,
            "value_combo": value_combo,
            "value_dropdown": value_dropdown,
        })

        if len(self.filter_rows) >= self.MAX_FILTERS:
            self.add_filter_btn.configure(state="disabled")

    def remove_filter_row(self, row_frame):
        row_frame.destroy()
        self.filter_rows = [r for r in self.filter_rows if r["frame"] != row_frame]
        if len(self.filter_rows) < self.MAX_FILTERS:
            self.add_filter_btn.configure(state="normal")
        if not self.filter_rows:
            self.add_filter_row()

    def clear_all_filters(self):
        for row in self.filter_rows:
            row["frame"].destroy()
        self.filter_rows = []
        self.add_filter_row()
        self.clear_date_filter()

    def get_custom_field_filter(self):
        filters = [
            {"field_name": r["field_var"].get(), "field_value": r["value_var"].get()}
            for r in self.filter_rows
            if r["field_var"].get() and r["value_var"].get()
        ]
        if not filters:
            return None
        if len(filters) == 1:
            return filters[0]
        # Use the logic from the last filter row that has a value set
        logic = next(
            (r["logic_var"].get() for r in reversed(self.filter_rows) if r["field_var"].get() and r["value_var"].get()),
            "AND"
        )
        return {"filters": filters, "logic": logic}

    def get_date_filter(self):
        start_date = self.start_date_var.get().strip()
        end_date = self.end_date_var.get().strip()
        
        if not start_date and not end_date:
            return None
        
        return {"start_date": start_date if start_date else None, "end_date": end_date if end_date else None}

    def clear_date_filter(self):
        self.start_date_var.set("")
        self.end_date_var.set("")

    # ── Search ─────────────────────────────────────────────────────

    def on_search(self, event=None):
        query = self.search_var.get().strip()
        if not query:
            return

        self._override_enabled = self.override_enabled_var.get()
        self._override_prompt = self.override_prompt_var.get().strip() if self._override_enabled else ""

        if self._override_enabled and not self._override_prompt:
            from tkinter import messagebox
            messagebox.showerror("Validation Error", "Custom prompt cannot be empty when 'Append Custom Prompt' is enabled.")
            return

        self.current_query = query
        self.cancel_search_flag = False
        self.search_entry.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.progress_label.configure(text="Searching database...")
        self.results_text.delete("1.0", "end")
        self.summary_text.delete("1.0", "end")

        Thread(target=self._search, args=(query, self._override_enabled, self._override_prompt), daemon=True).start()

    def cancel_search(self):
        self.cancel_search_flag = True
        self._update_progress("Cancelling...")

    def _search(self, query, override_enabled, override_prompt):
        try:
            # Check if full fetch mode is enabled
            full_fetch_mode = self.full_fetch_var.get()
            
            search_terms = [query]

            if self.refine_var.get() and not full_fetch_mode:
                try:
                    self._update_progress("Refining query with LLM...")
                    refined = refine_search_query(query)
                    if refined and len(refined) > 1:
                        search_terms = refined
                        self._update_progress(f"Using refined terms: {', '.join(search_terms[:3])}...")
                except Exception as e:
                    self._update_progress(f"Query refinement failed: {e}")

            if self.cancel_search_flag:
                self._update_progress("Search cancelled.")
                self._enable_search()
                return

            all_matches = []
            
            if full_fetch_mode:
                # Full record retrieval mode - get complete data per ID
                self._update_progress("Searching with full record retrieval...")
                custom_field_filter = self.get_custom_field_filter()
                date_filter = self.get_date_filter()
                
                for idx, term in enumerate(search_terms):
                    self._update_progress(f"Searching term {idx+1}/{len(search_terms)}: '{term}'...")
                    try:
                        matches = search_and_fetch_full(term, custom_field_filter=custom_field_filter, date_filter=date_filter)
                        all_matches.extend(matches)
                    except Exception as e:
                        self._update_progress(f"Search error: {e}")
                
                # Deduplicate by request_id (already full records, just keep unique)
                seen = set()
                unique_matches = []
                for m in all_matches:
                    if m['request_id'] not in seen:
                        seen.add(m['request_id'])
                        unique_matches.append(m)
                
                unique_matches.sort(key=lambda x: x['request_id'])
                
                # Build display text for full records
                result_text = f"Found {len(unique_matches)} matching IDs for: {', '.join(search_terms)}\n"
                result_text += "(Full Record Retrieval: All comments per ID will be included)\n\n"
                
                for i, match in enumerate(unique_matches):
                    if self.cancel_search_flag:
                        self._update_progress("Search cancelled.")
                        self._enable_search()
                        return
                    
                    rid = match['request_id']
                    client = match.get('client') or 'N/A'
                    product = match.get('product') or 'N/A'
                    site = match.get('site') or 'N/A'
                    release_version = match.get('release_version') or 'N/A'
                    score = match.get('match_score', 0)
                    comments = match.get('comments', [])
                    summary = match.get('summary')
                    
                    result_text += "=" * 60 + "\n"
                    result_text += f"Request #{rid} | Product: {product} | Score: {score}\n"
                    result_text += "-" * 60 + "\n"
                    result_text += f"CLIENT: {client}\n"
                    result_text += f"PRODUCT: {product}\n"
                    result_text += f"RELEASE: {release_version}\n"
                    result_text += f"SITE: {site}\n"
                    result_text += "-" * 60 + "\n"
                    
                    # Add all comments
                    if comments:
                        for j, c in enumerate(comments, 1):
                            date = c.get('date', 'Unknown date')
                            if date and date.startswith('/Date('):
                                date = parse_dotnet_date(date)
                            text = c.get('text', '')
                            result_text += f"--- COMMENT {j} ({date}) ---\n"
                            result_text += text + "\n\n"
                    else:
                        result_text += "--- No comments ---\n\n"
                    
                    # Add existing summary
                    if summary:
                        result_text += f"EXISTING SUMMARY:\n{summary}\n"
                    
                    result_text += "=" * 60 + "\n\n"
                    
                    if i % 50 == 0 and i > 0:
                        self._update_progress(f"Processing... {i}/{len(unique_matches)} IDs")
                
                self._update_results(result_text)
                
                # Transform for LLM processing - create text from full records
                llm_matches = []
                for match in unique_matches:
                    # Combine all comments into full text
                    full_text_parts = []
                    for c in match.get('comments', []):
                        full_text_parts.append(c.get('text', ''))
                    full_text = "\n\n".join(full_text_parts)
                    
                    llm_matches.append({
                        'request_id': match['request_id'],
                        'text': full_text,
                        'client': match.get('client'),
                        'product': match.get('product'),
                        'source': 'comments'
                    })
                
                # LLM summary (if enabled)
                if llm_matches and not self.skip_summary_var.get():
                    if self.cancel_search_flag:
                        self._update_progress("Search cancelled.")
                        self._enable_search()
                        return
                    self._update_progress("Generating LLM summary...")
                    summary = summarize_search_results(llm_matches, self.current_query, override_prompt)
                    self._update_summary(summary)
                    self._update_progress(f"Search complete. Found {len(unique_matches)} IDs with full records.")
                elif self.skip_summary_var.get():
                    self._update_progress(f"Search complete. Found {len(unique_matches)} IDs. (LLM summary skipped)")
                else:
                    self._update_progress("No matches found.")
                
                self._enable_search()
                return

            # Original snippet-level search mode
            for idx, term in enumerate(search_terms):
                self._update_progress(f"Searching term {idx+1}/{len(search_terms)}: '{term}'...")
                custom_field_filter = self.get_custom_field_filter()
                date_filter = self.get_date_filter()
                try:
                    all_matches.extend(search_cached_comments(term, custom_field_filter=custom_field_filter, date_filter=date_filter))
                    all_matches.extend(search_summaries(term, custom_field_filter=custom_field_filter, date_filter=date_filter))
                except Exception:
                    pass

            # Deduplicate
            seen = set()
            unique_matches = []
            for m in all_matches:
                key = (m['request_id'], m['text'][:100])
                if key not in seen:
                    seen.add(key)
                    unique_matches.append(m)

            unique_matches.sort(key=lambda x: x['request_id'])

            result_text = f"Found {len(unique_matches)} matches for: {', '.join(search_terms)}\n\n"
            for i, match in enumerate(unique_matches):
                if self.cancel_search_flag:
                    self._update_progress("Search cancelled.")
                    self._enable_search()
                    return
                result_text += f"[Request #{match['request_id']}] ({match['source']})\n"
                result_text += self._highlight_text(match['text'], self.current_query)
                result_text += "\n" + "=" * 60 + "\n\n"
                if i % 100 == 0 and i > 0:
                    self._update_progress(f"Processing... {i}/{len(unique_matches)} matches")

            self._update_results(result_text)

            if unique_matches and not self.skip_summary_var.get():
                if self.cancel_search_flag:
                    self._update_progress("Search cancelled.")
                    self._enable_search()
                    return
                self._update_progress("Generating LLM summary...")
                summary = summarize_search_results(unique_matches, self.current_query, override_prompt)
                self._update_summary(summary)
                self._update_progress(f"Search complete. Found {len(unique_matches)} matches.")
            elif self.skip_summary_var.get():
                self._update_progress(f"Search complete. Found {len(unique_matches)} matches. (LLM summary skipped)")
            else:
                self._update_progress("No matches found.")

            self._enable_search()

        except Exception as e:
            self._update_progress(f"Search error: {e}")

    def _highlight_text(self, text, query):
        query_lower = query.lower()
        lines = text.split('\n')
        highlighted = []
        for line in lines:
            prefix = "  >>> " if query_lower in line.lower() else "  "
            highlighted.append(prefix + line)
        return '\n'.join(highlighted[:20])

    # ── Thread-safe UI helpers ─────────────────────────────────────

    def _update_progress(self, text):
        try:
            if self.winfo_exists():
                self.after(0, lambda: self.progress_label.configure(text=text))
        except Exception:
            pass

    def _update_results(self, text):
        try:
            if self.winfo_exists():
                self.after(0, lambda: self.results_text.insert("end", text))
        except Exception:
            pass

    def _update_summary(self, text):
        try:
            if self.winfo_exists():
                self.after(0, lambda: self.summary_text.insert("1.0", text))
        except Exception:
            pass

    def _enable_search(self):
        try:
            if self.winfo_exists():
                self.after(0, lambda: (
                    self.search_entry.configure(state="normal"),
                    self.cancel_button.configure(state="disabled")
                ))
        except Exception:
            pass
