import customtkinter as ctk
from tkinter import messagebox

import config
from config import set_ollama_model, set_llm_provider_type, set_cloud_config, set_local_provider, LLM_PROVIDER_TYPE, CLOUD_CONFIG
from database import (
    get_all_prompts, save_prompt, init_default_prompts,
    DEFAULT_PROMPTS,
    get_cache_counts, delete_all_summaries, get_max_min_request_id,
    check_database_health, optimize_database, analyze_indexes
)


class SettingsWindow(ctk.CTkToplevel):
    def __init__(self, parent):
        super().__init__(parent)
        
        self.title("Settings")
        self.geometry("800x850")
        self.resizable(True, True)
        
        init_default_prompts()
        
        self.setup_ui()
        
        self.attributes('-topmost', True)
        self.lift()
        self.focus_force()
        self.after(100, lambda: self.attributes('-topmost', False))
        
        self.protocol("WM_DELETE_WINDOW", self.close)

    def setup_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        
        header = ctk.CTkLabel(
            self,
            text="Settings",
            font=ctk.CTkFont(size=20, weight="bold")
        )
        header.grid(row=0, column=0, padx=20, pady=(15, 10), sticky="w")
        
        scroll = ctk.CTkScrollableFrame(self, label_text="")
        scroll.grid(row=1, column=0, sticky="nsew", padx=15, pady=(0, 15))
        scroll.grid_columnconfigure(0, weight=1)
        
        self.llm_section(scroll)
        self.prompt_section(scroll)
        self.cache_section(scroll)
        
        self.load_prompts()
        
        button_frame = ctk.CTkFrame(self)
        button_frame.grid(row=2, column=0, sticky="ew", padx=15, pady=(0, 15))

        ctk.CTkButton(button_frame, text="Save All Settings", command=self.save_all_settings, width=140).pack(side="left", padx=5)
        ctk.CTkButton(button_frame, text="Close", command=self.close, width=100).pack(side="right", padx=5)

    def llm_section(self, parent):
        card = ctk.CTkFrame(parent, corner_radius=12)
        card.pack(fill="x", pady=(0, 15))
        card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            card,
            text="LLM Settings",
            font=ctk.CTkFont(size=16, weight="bold")
        ).grid(row=0, column=0, padx=15, pady=(15, 10), sticky="w")

        provider_row = ctk.CTkFrame(card)
        provider_row.grid(row=1, column=0, padx=15, pady=(0, 10), sticky="w")

        ctk.CTkLabel(provider_row, text="Provider:").pack(side="left", padx=(0, 10))

        self.provider_var = ctk.StringVar(value=LLM_PROVIDER_TYPE)
        ctk.CTkRadioButton(provider_row, text="Local", variable=self.provider_var, value="local", command=self.on_provider_changed).pack(side="left", padx=5)
        ctk.CTkRadioButton(provider_row, text="Cloud", variable=self.provider_var, value="cloud", command=self.on_provider_changed).pack(side="left", padx=5)

        # Provider selection dropdown
        self.provider_select_row = ctk.CTkFrame(card)
        self.provider_select_row.grid(row=2, column=0, padx=15, pady=(0, 10), sticky="w")

        ctk.CTkLabel(self.provider_select_row, text="Provider:").pack(side="left", padx=(0, 10))

        self.provider_dropdown = ctk.CTkComboBox(
            self.provider_select_row,
            values=[],
            width=200,
            state="readonly"
        )
        self.provider_dropdown.pack(side="left", padx=5)
        self.provider_dropdown.bind("<<ComboboxSelected>>", self.on_provider_selected)

        self.model_var = ctk.StringVar(value=config.OLLAMA_MODEL)
        self.model_entry = ctk.CTkEntry(card, textvariable=self.model_var, width=250)
        self.model_entry.grid(row=3, column=0, padx=15, pady=(0, 10), sticky="w")

        self.save_model_btn = ctk.CTkButton(card, text="Save", command=self.save_model, width=80)
        self.save_model_btn.grid(row=3, column=0, padx=(265, 5), pady=(0, 10), sticky="w")

        self.test_model_btn = ctk.CTkButton(card, text="Test Connection", command=self.test_model, width=120)
        self.test_model_btn.grid(row=3, column=0, padx=(355, 5), pady=(0, 10), sticky="w")

        # Refresh models button
        self.refresh_models_btn = ctk.CTkButton(
            card,
            text="Refresh Models",
            command=self.refresh_models,
            width=120
        )
        self.refresh_models_btn.grid(row=3, column=0, padx=(480, 5), pady=(0, 10), sticky="w")

        # Model dropdown for local providers
        self.model_dropdown = ctk.CTkComboBox(
            card,
            values=[],
            width=200,
            state="readonly"
        )
        self.model_dropdown.grid(row=4, column=0, padx=15, pady=(0, 10), sticky="w")
        self.model_dropdown.bind("<<ComboboxSelected>>", self.on_model_selected)

        cloud_row = ctk.CTkFrame(card)
        cloud_row.grid(row=5, column=0, padx=15, pady=(0, 10), sticky="w")
        cloud_row.grid_columnconfigure((0, 1, 2), weight=1)
        cloud_row.grid_remove()

        ctk.CTkLabel(cloud_row, text="API Key:").grid(row=0, column=0, padx=(0, 5), sticky="w")
        self.cloud_api_key_var = ctk.StringVar(value=CLOUD_CONFIG.get("api_key", ""))
        ctk.CTkEntry(cloud_row, textvariable=self.cloud_api_key_var, width=250, show="*").grid(row=0, column=1, padx=5, sticky="w")
        ctk.CTkLabel(cloud_row, text="Model:").grid(row=0, column=2, padx=(10, 5), sticky="w")
        self.cloud_model_var = ctk.StringVar(value=CLOUD_CONFIG.get("model", "gpt-4"))
        ctk.CTkEntry(cloud_row, textvariable=self.cloud_model_var, width=100).grid(row=0, column=3, padx=5, sticky="w")

        self.model_status = ctk.CTkLabel(card, text="", text_color="gray", font=ctk.CTkFont(size=11))
        self.model_status.grid(row=6, column=0, padx=15, pady=(0, 10), sticky="w")

        # Provider status display
        self.status_frame = ctk.CTkFrame(card, fg_color="transparent")
        self.status_frame.grid(row=7, column=0, padx=15, pady=(5, 10), sticky="ew")

        self.provider_label = ctk.CTkLabel(
            self.status_frame,
            text="Provider: Not Connected",
            font=ctk.CTkFont(size=12, weight="bold"),
            anchor="w"
        )
        self.provider_label.pack(fill="x", pady=(0, 2))

        self.connection_details_label = ctk.CTkLabel(
            self.status_frame,
            text="",
            font=ctk.CTkFont(size=11),
            text_color="gray",
            anchor="w"
        )
        self.connection_details_label.pack(fill="x", pady=(0, 2))

        self.model_display_label = ctk.CTkLabel(
            self.status_frame,
            text="",
            font=ctk.CTkFont(size=11),
            anchor="w"
        )
        self.model_display_label.pack(fill="x")

        self.cloud_row = cloud_row
        self.update_provider_ui()
        self.update_provider_status()

    def on_provider_changed(self):
        self.update_provider_ui()
        self.update_provider_status()

    def update_provider_ui(self):
        if self.provider_var.get() == "cloud":
            self.cloud_row.grid()
            self.model_entry.delete(0, "end")
            self.model_entry.insert(0, config._config.get("llm_cloud_model", "gpt-4"))
        else:
            self.cloud_row.grid_remove()
            self.model_entry.delete(0, "end")
            self.model_entry.insert(0, config._config.get("ollama_model") or "llama3.2")
        self.update_provider_dropdown()

    def update_provider_dropdown(self):
        """Update provider dropdown based on selected mode (local/cloud)."""
        from llm_providers import LOCAL_PROVIDERS, CLOUD_PROVIDERS

        if self.provider_var.get() == "local":
            providers = list(LOCAL_PROVIDERS.keys())
            current = config._config.get("llm_local_provider", "Ollama")
        else:
            providers = list(CLOUD_PROVIDERS.keys())
            current = config._config.get("llm_cloud_provider", "openai")

        self.provider_dropdown.configure(values=providers)

        if current in providers:
            self.provider_dropdown.set(current)
        elif providers:
            self.provider_dropdown.set(providers[0])

    def on_provider_selected(self, event=None):
        """Handle provider selection change."""
        selected = self.provider_dropdown.get()
        if self.provider_var.get() == "local":
            set_local_provider(selected)
            # Refresh models when local provider changes
            self.refresh_models()
        else:
            # Update cloud provider in config
            from config import _config, save_user_config
            _config["llm_cloud_provider"] = selected
            save_user_config(_config)

        self.update_provider_status()

    def update_provider_status(self):
        """Update the provider status labels based on current connection."""
        try:
            from llm_providers import LLMClient

            provider_info = LLMClient.get_provider_info()

            if provider_info is None:
                self.provider_label.configure(text="Provider: Not Connected", text_color="red")
                self.connection_details_label.configure(text="")
                self.model_display_label.configure(text="")
                return

            if provider_info["type"] == "local":
                backend = provider_info.get("backend", "Unknown")
                self.provider_label.configure(
                    text=f"Provider: {backend} (Local)",
                    text_color="green"
                )
                self.connection_details_label.configure(
                    text=f"Endpoint: {provider_info['base_url']}"
                )
                self.model_display_label.configure(
                    text=f"Model: {provider_info['model']}"
                )
            else:
                self.provider_label.configure(
                    text=f"Provider: {provider_info['backend'].title()} (Cloud)",
                    text_color="blue"
                )
                self.connection_details_label.configure(
                    text=f"Endpoint: {provider_info['endpoint']}"
                )
                self.model_display_label.configure(
                    text=f"Model: {provider_info['model']}"
                )

        except Exception as e:
            self.provider_label.configure(text="Provider: Error", text_color="red")
            self.connection_details_label.configure(text=str(e)[:50])

    def refresh_models(self):
        """Fetch available models from the selected local provider."""
        provider_type = self.provider_var.get()
        if provider_type != "local":
            self.model_status.configure(text="Model refresh only for local providers", text_color="orange")
            return

        try:
            from llm_providers import LocalLLMProvider, LLMProviderError, LOCAL_PROVIDERS
            local_provider = self.provider_dropdown.get()
            port = LOCAL_PROVIDERS.get(local_provider, {}).get("port", 11434)

            config_obj = {
                "host": "localhost",
                "port": port,
                "model": "",
                "provider_name": local_provider,
            }
            provider = LocalLLMProvider(config_obj)
            models = provider.get_available_models()

            if models:
                self.model_dropdown.configure(values=models)
                current_model = self.model_var.get()
                if current_model in models:
                    self.model_dropdown.set(current_model)
                else:
                    self.model_dropdown.set(models[0])
                    self.model_var.set(models[0])
                self.model_status.configure(text=f"Found {len(models)} models", text_color="green")
            else:
                self.model_status.configure(text="No models found", text_color="orange")
        except LLMProviderError as e:
            self.model_status.configure(text=str(e)[:50], text_color="red")
        except Exception as e:
            self.model_status.configure(text=f"Error: {str(e)[:50]}", text_color="red")

    def on_model_selected(self, event=None):
        """Handle model selection from dropdown."""
        selected = self.model_dropdown.get()
        if selected:
            self.model_var.set(selected)

    def prompt_section(self, parent):
        card = ctk.CTkFrame(parent, corner_radius=12)
        card.pack(fill="x", pady=(0, 15))
        card.grid_columnconfigure(0, weight=1)
        card.grid_columnconfigure(1, weight=2)
        
        ctk.CTkLabel(
            card,
            text="Prompt Management",
            font=ctk.CTkFont(size=16, weight="bold")
        ).grid(row=0, column=0, columnspan=2, padx=15, pady=(15, 10), sticky="w")
        
        list_frame = ctk.CTkFrame(card)
        list_frame.grid(row=1, column=0, padx=15, pady=(0, 10), sticky="nsew")
        
        self.prompt_listbox = ctk.CTkScrollableFrame(list_frame, label_text="Prompts", width=200)
        self.prompt_listbox.pack(side="left", fill="both", expand=True, padx=(5, 5), pady=5)
        
        self.prompt_buttons_frame = ctk.CTkFrame(list_frame)
        self.prompt_buttons_frame.pack(side="right", padx=(0, 5), pady=5)
        
        ctk.CTkButton(
            self.prompt_buttons_frame,
            text="Reset to Default",
            command=self.reset_prompt_to_default,
            width=100
        ).pack(pady=2)
        
        edit_frame = ctk.CTkFrame(card)
        edit_frame.grid(row=1, column=1, padx=15, pady=(0, 10), sticky="nsew")
        
        ctk.CTkLabel(edit_frame, text="Prompt Content:").pack(anchor="w", pady=(5, 5))
        
        self.prompt_text = ctk.CTkTextbox(edit_frame, wrap="word", height=200)
        self.prompt_text.pack(fill="both", expand=True, pady=(0, 5))
        
        btn_row = ctk.CTkFrame(edit_frame)
        btn_row.pack(fill="x")
        
        ctk.CTkButton(
            btn_row,
            text="Save Changes",
            command=self.save_prompt_changes
        ).pack(side="left", padx=5)
        
        ctk.CTkLabel(edit_frame, text="* Use {query}, {match_count}, {results_text} as placeholders", 
                     text_color="gray", font=ctk.CTkFont(size=10)).pack(side="left", padx=10)
        
        card.grid_rowconfigure(1, weight=1)

    def cache_section(self, parent):
        card = ctk.CTkFrame(parent, corner_radius=12)
        card.pack(fill="x", pady=(0, 15))

        ctk.CTkLabel(
            card,
            text="Cache Management",
            font=ctk.CTkFont(size=16, weight="bold")
        ).pack(anchor="w", padx=15, pady=(15, 10))

        counts = get_cache_counts()
        self.cache_count_label = ctk.CTkLabel(
            card,
            text=f"Cached: {counts['comments']} comments, {counts['summaries']} summaries, {counts['custom_fields']} custom fields",
            text_color="gray"
        )
        self.cache_count_label.pack(anchor="w", padx=15, pady=(0, 5))

        id_range = get_max_min_request_id()
        if id_range["min"] is not None:
            self.cache_id_range_label = ctk.CTkLabel(
                card,
                text=f"ID Range: {id_range['min']} - {id_range['max']}",
                text_color="gray"
            )
            self.cache_id_range_label.pack(anchor="w", padx=15, pady=(0, 5))

        button_row = ctk.CTkFrame(card)
        button_row.pack(anchor="w", padx=15, pady=(10, 5))

        ctk.CTkButton(
            button_row,
            text="Clear Summaries",
            command=self.clear_cache,
            height=36
        ).pack(side="left", padx=(0, 10))

        ctk.CTkButton(
            button_row,
            text="Check Database Health",
            command=self.check_health,
            height=36
        ).pack(side="left", padx=(0, 10))

        ctk.CTkButton(
            button_row,
            text="Optimize Database",
            command=self.optimize_db,
            height=36
        ).pack(side="left", padx=(0, 10))

        self.health_status_label = ctk.CTkLabel(
            card,
            text="",
            text_color="gray",
            font=ctk.CTkFont(size=11)
        )
        self.health_status_label.pack(anchor="w", padx=15, pady=(5, 15))

    def load_prompts(self):
        for widget in self.prompt_listbox.winfo_children():
            widget.destroy()
        
        self.prompts = get_all_prompts()
        
        for p in self.prompts:
            is_active = " (Active)" if p["is_active"] else ""
            btn = ctk.CTkButton(
                self.prompt_listbox,
                text=f"{p['name']}{is_active}",
                command=lambda name=p["name"]: self.select_prompt(name),
                height=28,
                anchor="w"
            )
            btn.pack(fill="x", padx=5, pady=2)
        
        if self.prompts:
            self.select_prompt(self.prompts[0]["name"])

    def select_prompt(self, name):
        self.selected_prompt = name
        for p in self.prompts:
            if p["name"] == name:
                self.prompt_text.delete("1.0", "end")
                self.prompt_text.insert("1.0", p["content"])
                break

    def save_model(self):
        provider_type = self.provider_var.get()
        set_llm_provider_type(provider_type)

        if provider_type == "cloud":
            api_key = self.cloud_api_key_var.get().strip()
            model = self.cloud_model_var.get().strip()
            if not api_key:
                self.model_status.configure(text="Error: API key required for cloud", text_color="red")
                return
            if not model:
                self.model_status.configure(text="Error: Model required for cloud", text_color="red")
                return
            # Save cloud provider from dropdown
            cloud_provider = self.provider_dropdown.get()
            set_cloud_config(
                cloud_provider,
                config._config.get("llm_cloud_endpoint", "https://api.openai.com/v1/chat/completions"),
                api_key,
                model
            )
            self.model_status.configure(text=f"Saved (Cloud) - Provider: {cloud_provider} | Model: {model}", text_color="green")
        else:
            model = self.model_var.get().strip()
            if not model:
                self.model_status.configure(text="Error: Model required for local", text_color="red")
                return
            # Save local provider from dropdown
            local_provider = self.provider_dropdown.get()
            set_local_provider(local_provider)
            set_ollama_model(model)
            self.model_status.configure(text=f"Saved (Local) - Provider: {local_provider} | Model: {model}", text_color="green")

        self.update_provider_ui()
        self.update_provider_status()

    def save_all_settings(self):
        """Save all settings from all sections."""
        try:
            # Save LLM settings
            self.save_model()
            # Show success message
            messagebox.showinfo("Settings Saved", "All settings have been saved successfully.")
        except Exception as e:
            messagebox.showerror("Save Failed", f"Failed to save settings: {e}")

    def test_model(self):
        provider_type = self.provider_var.get()
        self.test_model_btn.configure(state="disabled")
        self.model_status.configure(text="Testing...", text_color="gray")

        try:
            from llm_providers import LLMClient, LocalLLMProvider, CloudLLMProvider, LLMProviderError, LOCAL_PROVIDERS, CLOUD_PROVIDERS

            if provider_type == "cloud":
                # Test cloud provider with CURRENT UI values
                api_key = self.cloud_api_key_var.get().strip()
                model = self.cloud_model_var.get().strip()
                cloud_provider = self.provider_dropdown.get()

                if not api_key:
                    self.model_status.configure(text="Error: API key required for cloud", text_color="red")
                    self.update_provider_status()
                    return
                if not model:
                    self.model_status.configure(text="Error: Model required for cloud", text_color="red")
                    self.update_provider_status()
                    return

                cloud_config = {
                    "provider": cloud_provider,
                    "endpoint": CLOUD_PROVIDERS.get(cloud_provider, {}).get("endpoint", "https://api.openai.com/v1/chat/completions"),
                    "api_key": api_key,
                    "model": model,
                }
                provider = CloudLLMProvider(cloud_config)
            else:
                # Test local provider with CURRENT UI values
                model = self.model_var.get().strip()
                local_provider = self.provider_dropdown.get()

                if not model:
                    self.model_status.configure(text="Error: Model required for local", text_color="red")
                    self.update_provider_status()
                    return

                local_provider_config = LOCAL_PROVIDERS.get(local_provider, LOCAL_PROVIDERS["Ollama"])
                local_config = {
                    "host": "localhost",
                    "port": local_provider_config["port"],
                    "model": model,
                    "timeout": 120,
                }
                provider = LocalLLMProvider(local_config)

            # Test the provider directly WITHOUT setting it on LLMClient
            success, message = provider.test_connection()

            if success:
                self.model_status.configure(text=message, text_color="green")
            else:
                self.model_status.configure(text=message, text_color="red")
            self.update_provider_status()

        except LLMProviderError as e:
            self.model_status.configure(text=f"Error: {str(e)[:50]}", text_color="red")
            self.update_provider_status()
        except Exception as e:
            self.model_status.configure(text=f"Connection failed: {str(e)[:50]}", text_color="red")
            self.update_provider_status()
        finally:
            self.test_model_btn.configure(state="normal")

    def save_prompt_changes(self):
        if not hasattr(self, 'selected_prompt'):
            return
        
        content = self.prompt_text.get("1.0", "end").strip()
        if not content:
            messagebox.showwarning("Warning", "Prompt content cannot be empty")
            return
        
        save_prompt(self.selected_prompt, content)
        self.load_prompts()
        messagebox.showinfo("Saved", f"Prompt '{self.selected_prompt}' saved successfully")

    def reset_prompt_to_default(self):
        if not hasattr(self, 'selected_prompt'):
            return
        
        if messagebox.askyesno("Confirm", f"Reset '{self.selected_prompt}' to default?"):
            default_content = DEFAULT_PROMPTS.get(self.selected_prompt, "")
            if default_content:
                save_prompt(self.selected_prompt, default_content)
                self.prompt_text.delete("1.0", "end")
                self.prompt_text.insert("1.0", default_content)
                self.load_prompts()
                messagebox.showinfo("Reset", f"'{self.selected_prompt}' reset to default")

    def clear_cache(self):
        if messagebox.askyesno("Confirm", "Clear all cached summaries?"):
            try:
                delete_all_summaries()
                messagebox.showinfo("Done", "Cleared all cached summaries")
                counts = get_cache_counts()
                self.cache_count_label.configure(
                    text=f"Cached: {counts['comments']} comments, {counts['summaries']} summaries, {counts['custom_fields']} custom fields"
                )
                id_range = get_max_min_request_id()
                if id_range["min"] is not None and hasattr(self, 'cache_id_range_label'):
                    self.cache_id_range_label.configure(
                        text=f"ID Range: {id_range['min']} - {id_range['max']}"
                    )
            except Exception as e:
                messagebox.showerror("Error", f"Failed to clear cache: {e}")

    def check_health(self):
        try:
            health = check_database_health()
            status = health.get("status", "unknown")
            size_mb = health.get("db_size_mb", 0)
            row_counts = health.get("row_counts", {})
            orphan = health.get("orphan_fields", 0)
            orphan_pct = health.get("orphan_percentage", 0)
            messages = health.get("messages", [])
            
            status_color = "green" if status == "healthy" else "orange"
            if status == "warning":
                status_color = "orange"
            
            status_text = f"Status: {status.upper()} | Size: {size_mb:.1f} MB"
            self.health_status_label.configure(text=status_text, text_color=status_color)
            
            details = []
            details.append(f"Comments: {row_counts.get('comments', 0):,}")
            details.append(f"Summaries: {row_counts.get('summaries', 0):,}")
            details.append(f"Custom Fields: {row_counts.get('request_custom_fields', 0):,}")
            if orphan > 0:
                details.append(f"Orphan fields: {orphan:,} ({orphan_pct:.0f}%)")
            
            detail_text = " | ".join(details)
            if messages:
                detail_text += " | " + "; ".join(messages)
            
            self.health_status_label.configure(text=detail_text)
            
            messagebox.showinfo(
                "Database Health",
                f"Status: {status.upper()}\n\n{detail_text}\n\n" + "\n".join(messages)
            )
        except Exception as e:
            messagebox.showerror("Error", f"Failed to check health: {e}")

    def optimize_db(self):
        if not messagebox.askyesno("Confirm", "Optimize database?\n\nThis will run VACUUM, ANALYZE, and clean orphan data.\nMay take a few seconds with a large database."):
            return
        
        try:
            self.health_status_label.configure(text="Optimizing...", text_color="gray")
            self.update()
            
            result = optimize_database()
            
            counts = get_cache_counts()
            self.cache_count_label.configure(
                text=f"Cached: {counts['comments']} comments, {counts['summaries']} summaries, {counts['custom_fields']} custom fields"
            )
            
            self.health_status_label.configure(text="Optimized!", text_color="green")
            messagebox.showinfo("Done", result.get("message", "Database optimized successfully"))
        except Exception as e:
            messagebox.showerror("Error", f"Failed to optimize: {e}")

    def close(self):
        self.destroy()