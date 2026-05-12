"""
chain_window.py
===============
Prompt Chain Manager — a self-contained CTkToplevel window.

Opens from any layout via BaseApp.open_chain_builder().
Touches zero existing code; imports only from prompt_chain_db
and prompt_chain_executor (the two new backend files).

Layout (single window, three panes):
  ┌─────────────────────────────────────────────────────┐
  │  Header                                             │
  ├──────────────┬──────────────────────────────────────┤
  │  Chain list  │  Right panel (tabs):                 │
  │  [+ New]     │    Edit  |  Run  |  History          │
  │  [chain 1]   │                                      │
  │  [chain 2]   │                                      │
  └──────────────┴──────────────────────────────────────┘

Thread safety: all LLM calls run in daemon threads; UI updates
use self.after(0, ...) throughout, matching the rest of the project.
"""

import customtkinter as ctk
from tkinter import messagebox
from threading import Thread
import re
import datetime

from prompt_chain_db import (
    init_chain_db,
    save_chain, get_chain, list_chains, update_chain, delete_chain,
    list_runs, get_run,
)
from prompt_chain_executor import execute_chain


# ── Helpers ────────────────────────────────────────────────────────────────

def _focus_window(win: ctk.CTkToplevel) -> None:
    """Bring a CTkToplevel to the front (project-standard pattern)."""
    win.attributes("-topmost", True)
    win.lift()
    win.focus_force()
    win.after(100, lambda: win.attributes("-topmost", False))


# ── Main window ────────────────────────────────────────────────────────────

class ChainWindow(ctk.CTkToplevel):
    """Top-level Prompt Chain Manager window."""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Prompt Chain Builder")
        self.geometry("1200x780")
        self.resizable(True, True)

        # Initialise DB tables (idempotent)
        init_chain_db()

        self._selected_chain_id: int | None = None
        self._chain_buttons: dict[int, ctk.CTkButton] = {}
        self._cancel_run = False

        self._build_ui()
        self._load_chain_list()

        _focus_window(self)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    # ── UI construction ────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # Header
        header = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=15, pady=(12, 6))

        ctk.CTkLabel(
            header,
            text="Prompt Chain Builder",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).pack(side="left")

        ctk.CTkLabel(
            header,
            text="Build multi-step LLM workflows",
            font=ctk.CTkFont(size=12),
            text_color="gray",
        ).pack(side="left", padx=15)

        # ── Left: chain list ───────────────────────────────────────
        left = ctk.CTkFrame(self, width=220, corner_radius=8)
        left.grid(row=1, column=0, sticky="nsew", padx=(15, 6), pady=(0, 15))
        left.grid_rowconfigure(1, weight=1)
        left.grid_propagate(False)

        ctk.CTkButton(
            left, text="＋  New Chain",
            command=self._new_chain, height=34,
            font=ctk.CTkFont(weight="bold"),
        ).grid(row=0, column=0, padx=10, pady=10, sticky="ew")

        self._list_frame = ctk.CTkScrollableFrame(left, label_text="Saved Chains")
        self._list_frame.grid(row=1, column=0, sticky="nsew", padx=5, pady=(0, 10))
        self._list_frame.grid_columnconfigure(0, weight=1)
        left.grid_columnconfigure(0, weight=1)

        # ── Right: tabview ─────────────────────────────────────────
        self._tabs = ctk.CTkTabview(self, corner_radius=8)
        self._tabs.grid(row=1, column=1, sticky="nsew", padx=(6, 15), pady=(0, 15))

        self._tab_edit    = self._tabs.add("✏️  Edit")
        self._tab_run     = self._tabs.add("▶  Run")
        self._tab_history = self._tabs.add("📋  History")

        self._build_edit_tab(self._tab_edit)
        self._build_run_tab(self._tab_run)
        self._build_history_tab(self._tab_history)

    # ── Edit tab ───────────────────────────────────────────────────

    def _build_edit_tab(self, tab: ctk.CTkFrame) -> None:
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(2, weight=1)

        # Metadata card
        meta = ctk.CTkFrame(tab, corner_radius=10)
        meta.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        meta.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(meta, text="Chain Name:", anchor="w").grid(
            row=0, column=0, padx=(12, 6), pady=(12, 4), sticky="w")
        self._name_var = ctk.StringVar()
        ctk.CTkEntry(meta, textvariable=self._name_var, placeholder_text="e.g. Triage & Summarise").grid(
            row=0, column=1, padx=(0, 12), pady=(12, 4), sticky="ew")

        ctk.CTkLabel(meta, text="Description:", anchor="w").grid(
            row=1, column=0, padx=(12, 6), pady=(4, 12), sticky="w")
        self._desc_var = ctk.StringVar()
        ctk.CTkEntry(meta, textvariable=self._desc_var, placeholder_text="Optional description").grid(
            row=1, column=1, padx=(0, 12), pady=(4, 12), sticky="ew")

        # Steps card
        steps_header = ctk.CTkFrame(tab, corner_radius=0, fg_color="transparent")
        steps_header.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 4))

        ctk.CTkLabel(
            steps_header, text="Steps",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).pack(side="left")

        ctk.CTkButton(
            steps_header, text="＋ Add Step",
            command=self._add_step_card, width=100, height=28,
        ).pack(side="right")

        self._steps_scroll = ctk.CTkScrollableFrame(tab, corner_radius=8)
        self._steps_scroll.grid(row=2, column=0, sticky="nsew", padx=10, pady=(0, 6))
        self._steps_scroll.grid_columnconfigure(0, weight=1)

        self._step_cards: list[_StepCard] = []

        # Bottom action bar
        bar = ctk.CTkFrame(tab, corner_radius=0, fg_color="transparent")
        bar.grid(row=3, column=0, sticky="ew", padx=10, pady=(4, 10))

        ctk.CTkButton(bar, text="💾  Save Chain", command=self._save_chain,
                      height=34, font=ctk.CTkFont(weight="bold")).pack(side="left", padx=(0, 6))
        ctk.CTkButton(bar, text="🗑  Delete Chain", command=self._delete_chain,
                      height=34, fg_color="#8B2020", hover_color="#6B1A1A").pack(side="left")

        self._edit_status = ctk.CTkLabel(bar, text="", text_color="gray",
                                         font=ctk.CTkFont(size=11))
        self._edit_status.pack(side="right")

    # ── Run tab ────────────────────────────────────────────────────

    def _build_run_tab(self, tab: ctk.CTkFrame) -> None:
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(2, weight=1)

        # Input card
        input_card = ctk.CTkFrame(tab, corner_radius=10)
        input_card.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        input_card.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(input_card, text="TP ID",
                     font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, padx=12, pady=(12, 6), sticky="w")

        self._run_input = ctk.CTkEntry(
            input_card, placeholder_text="Enter TP request ID (e.g., 67600)",
            height=36, font=ctk.CTkFont(size=13)
        )
        self._run_input.grid(row=0, column=1, sticky="ew", padx=12, pady=(12, 6))

        # Controls
        ctrl = ctk.CTkFrame(tab, corner_radius=0, fg_color="transparent")
        ctrl.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 6))

        self._fetch_btn = ctk.CTkButton(
            ctrl, text="🔍  Fetch & Preview", command=self._fetch_preview,
            height=34, fg_color="#2D5A27", hover_color="#1E3D1A")
        self._fetch_btn.pack(side="left", padx=(0, 6))

        self._run_btn = ctk.CTkButton(
            ctrl, text="▶  Run Chain", command=self._run_chain,
            height=34, font=ctk.CTkFont(weight="bold"))
        self._run_btn.pack(side="left", padx=(0, 6))

        self._cancel_btn = ctk.CTkButton(
            ctrl, text="⏹  Cancel", command=self._cancel_chain,
            height=34, state="disabled",
            fg_color="#8B2020", hover_color="#6B1A1A")
        self._cancel_btn.pack(side="left")

        self._run_status = ctk.CTkLabel(ctrl, text="Select a chain from the list.",
                                        text_color="gray", font=ctk.CTkFont(size=11))
        self._run_status.pack(side="right")

        # Output
        self._run_output = ctk.CTkTextbox(tab, wrap="word")
        self._run_output.grid(row=2, column=0, sticky="nsew", padx=10, pady=(0, 10))

    # ── History tab ────────────────────────────────────────────────

    def _build_history_tab(self, tab: ctk.CTkFrame) -> None:
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_columnconfigure(1, weight=2)
        tab.grid_rowconfigure(1, weight=1)

        header_row = ctk.CTkFrame(tab, corner_radius=0, fg_color="transparent")
        header_row.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(10, 4))

        ctk.CTkLabel(header_row, text="Run History",
                     font=ctk.CTkFont(size=15, weight="bold")).pack(side="left")
        ctk.CTkButton(header_row, text="↻ Refresh",
                      command=self._load_history, width=80, height=28).pack(side="right")

        self._history_list = ctk.CTkScrollableFrame(tab, label_text="Runs", width=260)
        self._history_list.grid(row=1, column=0, sticky="nsew", padx=(10, 5), pady=(0, 10))
        self._history_list.grid_columnconfigure(0, weight=1)

        self._history_detail = ctk.CTkTextbox(tab, wrap="word")
        self._history_detail.grid(row=1, column=1, sticky="nsew", padx=(5, 10), pady=(0, 10))

    # ── Chain list (left panel) ────────────────────────────────────

    def _load_chain_list(self) -> None:
        for w in self._list_frame.winfo_children():
            w.destroy()
        self._chain_buttons.clear()

        chains = list_chains()
        if not chains:
            ctk.CTkLabel(self._list_frame, text="No chains yet.",
                         text_color="gray", font=ctk.CTkFont(size=11)).pack(pady=10)
            return

        for c in chains:
            btn = ctk.CTkButton(
                self._list_frame,
                text=f"{c['name']}\n{c['step_count']} step{'s' if c['step_count'] != 1 else ''}",
                command=lambda cid=c["id"]: self._select_chain(cid),
                height=42, anchor="w", font=ctk.CTkFont(size=12),
            )
            btn.pack(fill="x", padx=4, pady=3)
            self._chain_buttons[c["id"]] = btn

    def _select_chain(self, chain_id: int) -> None:
        self._selected_chain_id = chain_id
        # Highlight selected button
        for cid, btn in self._chain_buttons.items():
            btn.configure(fg_color=("gray75", "gray30") if cid != chain_id else "transparent")

        chain = get_chain(chain_id)
        if not chain:
            return
        self._populate_edit_tab(chain)
        self._run_status.configure(text=f"Ready to run: {chain['name']}")

    # ── Edit tab population ────────────────────────────────────────

    def _populate_edit_tab(self, chain: dict) -> None:
        self._name_var.set(chain.get("name", ""))
        self._desc_var.set(chain.get("description", ""))

        # Clear existing step cards
        for card in self._step_cards:
            card.destroy()
        self._step_cards.clear()

        for step in chain.get("steps", []):
            card = _StepCard(self._steps_scroll, step_number=step["step_order"],
                             on_remove=self._remove_step_card)
            card.pack(fill="x", padx=4, pady=4)
            card.populate(step)
            self._step_cards.append(card)

    def _new_chain(self) -> None:
        self._selected_chain_id = None
        self._name_var.set("")
        self._desc_var.set("")
        for card in self._step_cards:
            card.destroy()
        self._step_cards.clear()
        self._add_step_card()
        self._edit_status.configure(text="New chain — fill in details and save.")
        self._tabs.set("✏️  Edit")

    def _add_step_card(self) -> None:
        number = len(self._step_cards) + 1
        card = _StepCard(self._steps_scroll, step_number=number,
                         on_remove=self._remove_step_card)
        card.pack(fill="x", padx=4, pady=4)
        self._step_cards.append(card)

    def _remove_step_card(self, card: "_StepCard") -> None:
        card.destroy()
        self._step_cards = [c for c in self._step_cards if c.winfo_exists()]
        # Renumber remaining steps
        for i, c in enumerate(self._step_cards, start=1):
            c.set_number(i)

    def _save_chain(self) -> None:
        name = self._name_var.get().strip()
        if not name:
            messagebox.showerror("Validation", "Chain name is required.", parent=self)
            return
        if not self._step_cards:
            messagebox.showerror("Validation", "Add at least one step.", parent=self)
            return

        steps = []
        for i, card in enumerate(self._step_cards, start=1):
            data = card.get_data()
            if not data.get("prompt_template", "").strip():
                messagebox.showerror("Validation",
                                     f"Step {i}: prompt template cannot be empty.",
                                     parent=self)
                return
            data["step_order"] = i
            steps.append(data)

        desc = self._desc_var.get().strip()

        if self._selected_chain_id is None:
            new_id = save_chain(name=name, description=desc, steps=steps)
            self._selected_chain_id = new_id
            self._edit_status.configure(text=f"✓ Saved new chain (ID {new_id})", text_color="green")
        else:
            update_chain(self._selected_chain_id, name=name, description=desc, steps=steps)
            self._edit_status.configure(text="✓ Chain updated", text_color="green")

        self._load_chain_list()

    def _delete_chain(self) -> None:
        if self._selected_chain_id is None:
            messagebox.showinfo("Delete", "No chain selected.", parent=self)
            return
        if not messagebox.askyesno("Confirm Delete",
                                   "Delete this chain and all its run history?",
                                   parent=self):
            return
        delete_chain(self._selected_chain_id)
        self._selected_chain_id = None
        for card in self._step_cards:
            card.destroy()
        self._step_cards.clear()
        self._name_var.set("")
        self._desc_var.set("")
        self._edit_status.configure(text="Chain deleted.", text_color="gray")
        self._load_chain_list()

    # ── Run tab logic ──────────────────────────────────────────────

    def _format_date(self, date_val):
        """Convert .NET JSON date format to human readable."""
        if not date_val:
            return "Unknown"
        if isinstance(date_val, str) and date_val.startswith("/Date("):
            match = re.search(r"/Date\((\d+)", date_val)
            if match:
                try:
                    ts = int(match.group(1)) / 1000
                    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
                except (ValueError, OSError):
                    pass
        return str(date_val)

    def _fetch_preview(self) -> None:
        """Fetch and preview cached data for the entered TP ID."""
        tp_id = self._run_input.get().strip()
        if not tp_id:
            messagebox.showerror("Fetch & Preview", "TP ID cannot be empty.", parent=self)
            return

        # Validate numeric
        try:
            request_id = int(tp_id)
        except ValueError:
            messagebox.showerror("Fetch & Preview", "TP ID must be a number.", parent=self)
            return

        self._run_status.configure(text="Fetching cached data...", text_color="gray")
        self._run_output.delete("1.0", "end")

        # Fetch data from database
        from database import get_cached_comments, get_custom_fields, get_summary
        try:
            comments, _ = get_cached_comments(request_id)
            fields, _ = get_custom_fields(request_id)
            summary, _ = get_summary(request_id)
        except Exception as e:
            self._run_status.configure(text="Error fetching data", text_color="red")
            messagebox.showerror("Fetch & Preview", f"Error: {e}", parent=self)
            return

        # Build preview output
        preview = f"═══ TP ID: {request_id} ═══\n\n"

        # Custom fields
        if fields:
            preview += "--- CUSTOM FIELDS ---\n"
            preview += f"Client: {fields.get('Client', 'N/A')}\n"
            preview += f"Product: {fields.get('Product', 'N/A')}\n"
            preview += f"Release Version: {fields.get('Release Version', 'N/A')}\n"
            preview += f"Site: {fields.get('Site', 'N/A')}\n\n"
        else:
            preview += "--- CUSTOM FIELDS ---\nNo custom fields found.\n\n"

        # Summary
        if summary:
            preview += "--- EXISTING SUMMARY ---\n"
            preview += summary[:500]
            if len(summary) > 500:
                preview += "... (truncated)"
            preview += "\n\n"
        else:
            preview += "--- EXISTING SUMMARY ---\nNo summary found.\n\n"

        # Comments
        if comments:
            preview += f"--- COMMENTS ({len(comments)} found) ---\n"
            for i, c in enumerate(comments[:20], 1):
                date = self._format_date(c.get("date"))
                text = c.get("text", "")
                preview += f"\n[{date}] COMMENT {i}:\n{text[:300]}"
                if len(text) > 300:
                    preview += "... (truncated)"
                preview += "\n"
            if len(comments) > 20:
                preview += f"\n... and {len(comments) - 20} more comments."
        else:
            preview += "--- COMMENTS ---\nNo comments found."

        if not comments and not fields:
            self._run_status.configure(
                text=f"No cached data found for request {request_id}. Please download it first.",
                text_color="orange"
            )
        else:
            self._run_status.configure(text="Cached data loaded.", text_color="green")

        self._run_output.insert("1.0", preview)

    def _run_chain(self) -> None:
        if self._selected_chain_id is None:
            messagebox.showinfo("Run Chain", "Select a chain from the list first.", parent=self)
            return

        tp_id = self._run_input.get().strip()
        if not tp_id:
            messagebox.showerror("Run Chain", "TP ID cannot be empty.", parent=self)
            return

        # Validate numeric
        try:
            request_id = int(tp_id)
        except ValueError:
            messagebox.showerror("Run Chain", "TP ID must be a number.", parent=self)
            return

        self._cancel_run = False
        self._run_btn.configure(state="disabled")
        self._cancel_btn.configure(state="normal")
        self._run_output.delete("1.0", "end")
        self._run_status.configure(text="Running…", text_color="gray")

        Thread(
            target=self._execute_thread,
            args=(self._selected_chain_id, str(request_id)),
            daemon=True,
        ).start()

    def _execute_thread(self, chain_id: int, initial_input: str) -> None:
        def _progress(msg: str) -> None:
            if self.winfo_exists():
                self.after(0, lambda: self._run_status.configure(text=msg))

        def _on_step(order: int, output: str, context: dict) -> None:
            if self._cancel_run:
                return
            sep = "─" * 55
            header = f"\n{'─'*10} Step {order} output {'─'*10}\n"
            if self.winfo_exists():
                self.after(0, lambda h=header, o=output: (
                    self._run_output.insert("end", h + o + "\n")
                ))

        try:
            result = execute_chain(
                chain_id=chain_id,
                initial_input=initial_input,
                progress_callback=_progress,
                on_step_complete=_on_step,
            )

            if self.winfo_exists():
                self.after(0, lambda r=result: self._finish_run(r))
        except Exception as exc:
            if self.winfo_exists():
                self.after(0, lambda e=exc: (
                    self._run_output.insert("end", f"\nUnexpected error: {e}\n"),
                    self._reset_run_buttons(),
                ))

    def _finish_run(self, result: dict) -> None:
        sep = "═" * 55
        if result["status"] == "completed":
            self._run_output.insert("end", f"\n{sep}\nFINAL OUTPUT\n{sep}\n")
            self._run_output.insert("end", result.get("final_output", "") + "\n")
            self._run_status.configure(text="✓ Completed", text_color="green")
        else:
            self._run_output.insert("end", f"\n{sep}\nFAILED\n{sep}\n")
            self._run_output.insert("end", result.get("error", "Unknown error") + "\n")
            self._run_status.configure(text="✗ Failed", text_color="red")

        self._reset_run_buttons()
        self._load_history()   # refresh history tab after each run

    def _cancel_chain(self) -> None:
        self._cancel_run = True
        self._run_status.configure(text="Cancelling…", text_color="gray")
        self._reset_run_buttons()

    def _reset_run_buttons(self) -> None:
        self._run_btn.configure(state="normal")
        self._cancel_btn.configure(state="disabled")

    # ── History tab logic ──────────────────────────────────────────

    def _load_history(self) -> None:
        for w in self._history_list.winfo_children():
            w.destroy()

        if self._selected_chain_id is None:
            ctk.CTkLabel(self._history_list, text="Select a chain to view history.",
                         text_color="gray", font=ctk.CTkFont(size=11)).pack(pady=10)
            return

        runs = list_runs(self._selected_chain_id, limit=50)
        if not runs:
            ctk.CTkLabel(self._history_list, text="No runs yet.",
                         text_color="gray", font=ctk.CTkFont(size=11)).pack(pady=10)
            return

        for run in runs:
            icon = "✓" if run["status"] == "completed" else "✗"
            color = "green" if run["status"] == "completed" else "#cc4444"
            label = (f"{icon}  Run #{run['id']}\n"
                     f"{run['started_at'][:16] if run['started_at'] else '?'}")
            ctk.CTkButton(
                self._history_list,
                text=label,
                command=lambda rid=run["id"]: self._show_run_detail(rid),
                height=44, anchor="w",
                font=ctk.CTkFont(size=11),
                text_color=color,
            ).pack(fill="x", padx=4, pady=2)

    def _show_run_detail(self, run_id: int) -> None:
        run = get_run(run_id)
        if not run:
            return

        self._history_detail.delete("1.0", "end")
        sep = "═" * 55
        status_icon = "✓" if run["status"] == "completed" else "✗"

        self._history_detail.insert("end",
            f"Run #{run['id']}  {status_icon} {run['status'].upper()}\n"
            f"Started:  {run.get('started_at', '?')}\n"
            f"Finished: {run.get('finished_at', '?')}\n\n"
            f"INITIAL INPUT\n{'─'*40}\n{run['initial_input']}\n\n"
        )

        for step in run.get("steps", []):
            self._history_detail.insert("end",
                f"{sep}\nSTEP {step['step_order']}  {step['status'].upper()}"
                f"  ({step.get('duration_ms', '?')} ms)\n{'─'*40}\n"
                f"PROMPT SENT:\n{step['input_sent']}\n\n"
                f"OUTPUT:\n{step.get('output_received', '(none)')}\n\n"
            )

        if run.get("final_output"):
            self._history_detail.insert("end",
                f"{sep}\nFINAL OUTPUT\n{'─'*40}\n{run['final_output']}\n")
        if run.get("error"):
            self._history_detail.insert("end",
                f"\nERROR\n{'─'*40}\n{run['error']}\n")


# ── Step card widget ───────────────────────────────────────────────────────

class _StepCard(ctk.CTkFrame):
    """One collapsible card representing a single chain step in the editor."""

    def __init__(self, parent, step_number: int,
                 on_remove: callable, **kwargs):
        super().__init__(parent, corner_radius=10, **kwargs)
        self._on_remove = on_remove
        self._number = step_number
        self._build(step_number)

    def _build(self, number: int) -> None:
        self.grid_columnconfigure(0, weight=1)

        # ── Header row ─────────────────────────────────────────────
        header = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        header.grid_columnconfigure(1, weight=1)

        self._number_label = ctk.CTkLabel(
            header,
            text=f"Step {number}",
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self._number_label.grid(row=0, column=0, sticky="w")

        self._name_var = ctk.StringVar()
        ctk.CTkEntry(header, textvariable=self._name_var,
                     placeholder_text="Step name (optional)").grid(
            row=0, column=1, sticky="ew", padx=(10, 0))

        ctk.CTkButton(
            header, text="✕", width=28, height=28,
            command=lambda: self._on_remove(self),
            fg_color="transparent", hover_color=("gray80", "gray30"),
        ).grid(row=0, column=2, padx=(6, 0))

        # ── Prompt template ────────────────────────────────────────
        ctk.CTkLabel(self, text="Prompt Template  (use {{input}}, {{cached_comments}}, {{cached_client}}, etc.)",
                     text_color="gray", font=ctk.CTkFont(size=11)).grid(
            row=1, column=0, sticky="w", padx=12, pady=(4, 2))

        self._template_box = ctk.CTkTextbox(self, height=90, wrap="word")
        self._template_box.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 6))

        # ── Step type row ──────────────────────────────────────────
        type_row = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        type_row.grid(row=3, column=0, sticky="ew", padx=8, pady=(0, 4))
        type_row.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(type_row, text="Step type:",
                     font=ctk.CTkFont(size=11)).grid(row=0, column=0, sticky="w")
        self._step_type_var = ctk.StringVar(value="llm")
        self._step_type_menu = ctk.CTkOptionMenu(
            type_row, variable=self._step_type_var,
            values=["llm", "db_query"],
            width=120,
        )
        self._step_type_menu.grid(row=0, column=1, padx=(6, 0), sticky="w")

        # ── Variable row ───────────────────────────────────────────
        var_row = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        var_row.grid(row=4, column=0, sticky="ew", padx=8, pady=(0, 10))
        var_row.grid_columnconfigure(1, weight=1)
        var_row.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(var_row, text="Input variable:",
                     font=ctk.CTkFont(size=11)).grid(row=0, column=0, sticky="w")
        self._input_var = ctk.StringVar(value="input")
        ctk.CTkEntry(var_row, textvariable=self._input_var, width=120).grid(
            row=0, column=1, padx=(6, 20), sticky="w")

        ctk.CTkLabel(var_row, text="Output variable:",
                     font=ctk.CTkFont(size=11)).grid(row=0, column=2, sticky="w")
        self._output_var = ctk.StringVar(value=f"output_{self._number}")
        ctk.CTkEntry(var_row, textvariable=self._output_var, width=120).grid(
            row=0, column=3, padx=(6, 0), sticky="w")

    def set_number(self, n: int) -> None:
        self._number = n
        self._number_label.configure(text=f"Step {n}")

    def populate(self, step: dict) -> None:
        self._name_var.set(step.get("name", ""))
        self._step_type_var.set(step.get("step_type", "llm"))
        self._template_box.delete("1.0", "end")
        self._template_box.insert("1.0", step.get("prompt_template", ""))
        self._input_var.set(step.get("input_variable", "input"))
        self._output_var.set(step.get("output_variable", f"output_{self._number}"))

    def get_data(self) -> dict:
        return {
            "name":             self._name_var.get().strip(),
            "step_type":        self._step_type_var.get(),
            "prompt_template":  self._template_box.get("1.0", "end").strip(),
            "input_variable":   self._input_var.get().strip() or "input",
            "output_variable":  self._output_var.get().strip() or f"output_{self._number}",
            "variables":        {},
        }
