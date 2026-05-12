"""
tp_query.py — TP Query Main Entry Point
==========================================
Sidebar Dashboard: left sidebar with all controls, main area for results.

Pros:  All controls visible at once, good for power users.
Cons:  Takes horizontal space, may feel cluttered on small screens.
"""

import os
import sys


def _configure_bundled_tcl_tk():
    if not getattr(sys, "frozen", False) or not hasattr(sys, "_MEIPASS"):
        return

    tcl_data = os.path.join(sys._MEIPASS, "_tcl_data")
    tk_data = os.path.join(sys._MEIPASS, "_tk_data")
    tcl_root = os.path.join(sys._MEIPASS, "tcl")
    tcl_library = tcl_data if os.path.exists(os.path.join(tcl_data, "init.tcl")) else os.path.join(tcl_root, "tcl8.6")
    tk_library = tk_data if os.path.exists(os.path.join(tk_data, "tk.tcl")) else os.path.join(tcl_root, "tk8.6")

    if os.path.exists(os.path.join(tcl_library, "init.tcl")):
        os.environ["TCL_LIBRARY"] = tcl_library
    if os.path.exists(os.path.join(tk_library, "tk.tcl")):
        os.environ["TK_LIBRARY"] = tk_library


_configure_bundled_tcl_tk()

import customtkinter as ctk
from base_app import BaseApp
from config import PROJECT_NAME

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class App(BaseApp):
    def setup_ui(self):
        self.geometry("1100x830")
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # ── Sidebar ────────────────────────────────────────────────
        sidebar = ctk.CTkFrame(self, width=280, corner_radius=0)
        sidebar.grid(row=0, column=0, sticky="nsew")

        ctk.CTkLabel(
            sidebar,
            text="Support Call\nSummariser",
            font=ctk.CTkFont(size=20, weight="bold")
        ).pack(pady=(20, 10), padx=20)

        ctk.CTkLabel(
            sidebar,
            text=f"Project: {PROJECT_NAME}",
            font=ctk.CTkFont(size=12),
            text_color="gray"
        ).pack(pady=(0, 20), padx=20)

        # Request IDs
        ids_frame = ctk.CTkFrame(sidebar)
        ids_frame.pack(fill="x", padx=15, pady=5)

        ctk.CTkLabel(ids_frame, text="Request IDs", font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=10, pady=(10, 5))

        self.ids_entry = ctk.CTkEntry(ids_frame, placeholder_text="e.g., 123, 456, 789")
        self.ids_entry.pack(fill="x", padx=10, pady=5)

        self.update_cache_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(ids_frame, text="Refresh cached data", variable=self.update_cache_var).pack(anchor="w", padx=10, pady=5)

        # Actions
        actions_frame = ctk.CTkFrame(sidebar)
        actions_frame.pack(fill="x", padx=15, pady=5)

        ctk.CTkLabel(actions_frame, text="Actions", font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=10, pady=(10, 5))

        self.run_button = ctk.CTkButton(actions_frame, text="Summarize", command=self.run_analysis, height=32)
        self.run_button.pack(fill="x", padx=10, pady=3)

        ctk.CTkButton(actions_frame, text="Settings", command=self.open_settings, height=32).pack(fill="x", padx=10, pady=3)

        self.update_button = ctk.CTkButton(actions_frame, text="Update Cache", command=self.update_cache, height=32)
        self.update_button.pack(fill="x", padx=10, pady=3)

        ctk.CTkButton(actions_frame, text="View Cached Comments", command=self.view_cached_comments, height=32).pack(fill="x", padx=10, pady=3)
        ctk.CTkButton(actions_frame, text="View Saved Summaries", command=self.view_summaries, height=32).pack(fill="x", padx=10, pady=3)
        ctk.CTkButton(actions_frame, text="Search Cache", command=self.search_cache, height=32).pack(fill="x", padx=10, pady=3)

        # ── Chain Builder (new) ────────────────────────────────────
        chain_frame = ctk.CTkFrame(sidebar)
        chain_frame.pack(fill="x", padx=15, pady=5)

        ctk.CTkLabel(chain_frame, text="Workflows", font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=10, pady=(10, 5))

        ctk.CTkButton(
            chain_frame,
            text="⛓  Chain Builder",
            command=self.open_chain_builder,
            height=32,
        ).pack(fill="x", padx=10, pady=(0, 10))

        # Cache Range
        range_frame = ctk.CTkFrame(sidebar)
        range_frame.pack(fill="x", padx=15, pady=5)

        ctk.CTkLabel(range_frame, text="Cache Range", font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=10, pady=(10, 5))

        self.range_entry = ctk.CTkEntry(range_frame, placeholder_text="start-end")
        self.range_entry.pack(fill="x", padx=10, pady=5)

        btn_row = ctk.CTkFrame(range_frame)
        btn_row.pack(fill="x", padx=10, pady=5)

        self.cache_range_button = ctk.CTkButton(btn_row, text="Cache Range", command=self.cache_range, height=28)
        self.cache_range_button.pack(side="left", fill="x", expand=True, padx=(0, 2))

        self.stop_cache_button = ctk.CTkButton(btn_row, text="Stop", command=self.stop_cache, height=28, state="disabled")
        self.stop_cache_button.pack(side="left", fill="x", expand=True, padx=(2, 0))

        self.cache_progress = ctk.CTkProgressBar(range_frame)
        self.cache_progress.pack(fill="x", padx=10, pady=(5, 0))
        self.cache_progress.set(0)

        self.cache_progress_label = ctk.CTkLabel(range_frame, text="0%", text_color="gray", font=ctk.CTkFont(size=12))
        self.cache_progress_label.pack(anchor="w", padx=10, pady=(0, 5))

        # ── Main content area ──────────────────────────────────────
        main_frame = ctk.CTkFrame(self, corner_radius=0)
        main_frame.grid(row=0, column=1, sticky="nsew")
        main_frame.grid_columnconfigure(0, weight=1)
        main_frame.grid_rowconfigure(1, weight=1)

        self.progress_label = ctk.CTkLabel(main_frame, text="Ready", text_color="gray")
        self.progress_label.grid(row=0, column=0, padx=20, pady=(15, 5), sticky="w")

        self.results_text = ctk.CTkTextbox(main_frame, wrap="word")
        self.results_text.grid(row=1, column=0, sticky="nsew", padx=15, pady=(5, 15))


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
