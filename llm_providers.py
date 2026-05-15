import logging
import requests
import threading
import urllib3
from datetime import datetime
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, Literal
import config

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ProviderType = Literal["local", "cloud"]

# Provider registries
LOCAL_PROVIDERS = {
    "Ollama": {"port": 11434, "endpoint_suffix": "/api/generate"},
    "LM Studio": {"port": 1234, "endpoint_suffix": "/v1/chat/completions"},
}

CLOUD_PROVIDERS = {
    "OpenAI": {"endpoint": "https://api.openai.com/v1/chat/completions"},
    "Azure OpenAI": {"endpoint": ""},
    "Anthropic": {"endpoint": "https://api.anthropic.com/v1/messages"},
}

logger = logging.getLogger(__name__)


class LLMProviderError(Exception):
    def __init__(self, message: str, provider: str = "unknown"):
        super().__init__(message)
        self.provider = provider


class BaseLLMProvider(ABC):
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self._model = config.get("model", "")
        self._timeout = config.get("timeout", 120)

    @abstractmethod
    def generate(self, prompt: str, **kwargs) -> str:
        pass

    @abstractmethod
    def test_connection(self) -> tuple[bool, str]:
        pass

    @property
    @abstractmethod
    def provider_type(self) -> ProviderType:
        pass

    @property
    def model(self) -> str:
        return self._model

    @abstractmethod
    def get_provider_info(self) -> dict:
        pass


class LocalLLMProvider(BaseLLMProvider):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._host = config.get("host", "localhost")
        self._port = config.get("port", 11434)
        self._base_url = f"http://{self._host}:{self._port}"
        # Get endpoint suffix from provider registry
        provider_name = config.get("provider_name", "Ollama")
        from llm_providers import LOCAL_PROVIDERS
        provider_config = LOCAL_PROVIDERS.get(provider_name, LOCAL_PROVIDERS["Ollama"])
        self._endpoint_suffix = provider_config.get("endpoint_suffix", "/api/generate")
        self._is_openai_compat = self._endpoint_suffix != "/api/generate"

    def _detect_backend(self) -> str:
        """Detect the local LLM backend based on port or API response."""
        port_backends = {
            11434: "Ollama",
            1234: "LM Studio",
        }

        if self._port in port_backends:
            return port_backends[self._port]

        try:
            response = requests.get(f"{self._base_url}/", timeout=2)
            content = str(response.content).lower()
            if "ollama" in content:
                return "Ollama"
        except:
            pass

        return "Unknown Local LLM"

    def get_provider_info(self) -> dict:
        """Return provider details for UI display."""
        return {
            "type": "local",
            "backend": self._detect_backend(),
            "base_url": self._base_url,
            "host": self._host,
            "port": self._port,
            "model": self._model,
        }

    def get_available_models(self) -> list[str]:
        """Fetch available models from the local LLM provider."""
        try:
            if self._endpoint_suffix == "/api/generate":
                # Ollama format - use /api/tags
                response = requests.get(f"{self._base_url}/api/tags", timeout=5)
                response.raise_for_status()
                data = response.json()
                # Extract model names from "name" field
                models = [m.get("name", "") for m in data.get("models", [])]
                return [m for m in models if m]
            else:
                # OpenAI-compatible format (LM Studio, LocalAI, vLLM) - use /v1/models
                response = requests.get(f"{self._base_url}/v1/models", timeout=5)
                response.raise_for_status()
                data = response.json()
                # Extract model ids from "id" field
                models = [m.get("id", "") for m in data.get("data", [])]
                return [m for m in models if m]
        except Exception as e:
            raise LLMProviderError(f"Failed to fetch models: {e}", "local")

    @property
    def provider_type(self) -> ProviderType:
        return "local"

    def generate(self, prompt: str, temperature: float = 0.3, max_tokens: Optional[int] = None, **kwargs) -> str:
        if self._is_openai_compat:
            # OpenAI-compatible format (LM Studio, LocalAI, vLLM)
            messages = [{"role": "user", "content": prompt}]
            payload = {
                "model": self._model,
                "messages": messages,
                "temperature": temperature,
            }
            if max_tokens:
                payload["max_tokens"] = max_tokens
        else:
            # Ollama format
            options = {"temperature": temperature}
            if max_tokens:
                options["num_predict"] = max_tokens
            payload = {
                "model": self._model,
                "prompt": prompt,
                "stream": False,
                "options": options,
            }

        try:
            response = requests.post(
                f"{self._base_url}{self._endpoint_suffix}",
                json=payload,
                timeout=self._timeout
            )
            response.raise_for_status()

            # Parse response based on format
            if self._is_openai_compat:
                data = response.json()
                if "choices" in data and len(data["choices"]) > 0:
                    return data["choices"][0]["message"]["content"].strip()
                return ""
            else:
                # Ollama format
                return response.json().get("response", "").strip()

        except requests.exceptions.ConnectionError:
            raise LLMProviderError(f"Cannot connect to {self._base_url}. Is the server running?", "local")
        except requests.exceptions.HTTPError as e:
            raise LLMProviderError(f"HTTP error: {e.response.status_code}", "local")
        except Exception as e:
            raise LLMProviderError(f"Error: {str(e)}", "local")

    def test_connection(self) -> tuple[bool, str]:
        try:
            if self._is_openai_compat:
                # OpenAI-compatible format (LM Studio, LocalAI, vLLM)
                response = requests.post(
                    f"{self._base_url}{self._endpoint_suffix}",
                    json={"model": self._model, "messages": [{"role": "user", "content": "Say OK"}], "max_tokens": 10},
                    timeout=30
                )
            else:
                # Ollama format
                response = requests.post(
                    f"{self._base_url}/api/generate",
                    json={"model": self._model, "prompt": "Say 'OK'", "stream": False},
                    timeout=30
                )
            response.raise_for_status()
            return True, f"Connected to {self._base_url}"
        except requests.exceptions.ConnectionError:
            return False, f"Cannot connect to {self._base_url}"
        except Exception as e:
            return False, str(e)


class CloudLLMProvider(BaseLLMProvider):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._provider_name = config.get("provider", "generic")
        self._api_key = config.get("api_key", "")
        self._endpoint = config.get("endpoint", "")
        self._verify = config.get("verify", True)

    @property
    def provider_type(self) -> ProviderType:
        return "cloud"

    def get_provider_info(self) -> dict:
        """Return provider details for UI display."""
        return {
            "type": "cloud",
            "backend": self._provider_name,
            "endpoint": self._endpoint,
            "model": self._model,
        }

    def _get_headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._provider_name == "openai":
            headers["Authorization"] = f"Bearer {self._api_key}"
        elif self._provider_name == "anthropic":
            headers["x-api-key"] = self._api_key
            headers["anthropic-version"] = "2023-06-01"
        return headers

    def generate(self, prompt: str, temperature: float = 0.3, max_tokens: Optional[int] = None, **kwargs) -> str:
        if not self._api_key:
            raise LLMProviderError("API key not configured", "cloud")
        if not self._endpoint:
            raise LLMProviderError("API endpoint not configured", "cloud")

        if self._provider_name == "openai":
            return self._generate_openai(prompt, temperature, max_tokens)
        else:
            raise LLMProviderError(f"Provider {self._provider_name} not supported", "cloud")

    def _generate_openai(self, prompt: str, temperature: float, max_tokens: Optional[int]) -> str:
        messages = [{"role": "user", "content": prompt}]
        payload = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens

        try:
            response = requests.post(
                self._endpoint,
                headers=self._get_headers(),
                json=payload,
                timeout=self._timeout,
                verify=self._verify
            )
            response.raise_for_status()
            data = response.json()
            if "choices" in data and len(data["choices"]) > 0:
                return data["choices"][0]["message"]["content"].strip()
            return ""
        except requests.exceptions.HTTPError as e:
            detail = ""
            try:
                body = e.response.json()
                detail = body.get("error", {}).get("message", "")
            except Exception:
                detail = e.response.text[:200] if e.response.text else ""
            msg = f"API error: {e.response.status_code}"
            if detail:
                msg += f" - {detail}"
            raise LLMProviderError(msg, "openai")
        except Exception as e:
            raise LLMProviderError(f"Error: {str(e)}", "cloud")

    def test_connection(self) -> tuple[bool, str]:
        if not self._api_key:
            return False, "API key not configured"
        try:
            result = self.generate("Say 'OK'", max_tokens=10)
            return True, f"Connected to {self._provider_name}"
        except Exception as e:
            return False, str(e)


class LLMClient:
    _provider: Optional[BaseLLMProvider] = None
    _lock = threading.Lock()

    @classmethod
    def set_provider(cls, provider: BaseLLMProvider):
        with cls._lock:
            cls._provider = provider
            logger.info(f"LLM provider set to: {provider.provider_type}")

    @classmethod
    def get_provider(cls) -> Optional[BaseLLMProvider]:
        with cls._lock:
            return cls._provider

    @classmethod
    def get_provider_info(cls) -> Optional[dict]:
        """Get provider info for UI display. Returns None if no provider configured."""
        with cls._lock:
            if cls._provider is None:
                return None
            return cls._provider.get_provider_info()

    @classmethod
    def generate(cls, prompt: str, **kwargs) -> str:
        with cls._lock:
            if cls._provider is None:
                raise LLMProviderError("No provider configured", "unknown")

            # Safety check: verify provider matches config
            from config import _config
            expected_type = _config.get("llm_provider_type", "local")
            if cls._provider.provider_type != expected_type:
                raise LLMProviderError(
                    f"Provider type mismatch: active is '{cls._provider.provider_type}', "
                    f"config says '{expected_type}'",
                    cls._provider.provider_type
                )

            logger.info(f"Generating with provider type: {cls._provider.provider_type}, model: {cls._provider.model}")

            if config.PROMPT_LOGGING_ENABLED:
                try:
                    with open(config.PROMPT_LOG_FILE, "a", encoding="utf-8") as f:
                        f.write(f"\n{'='*60}\n")
                        f.write(f"[{datetime.now().isoformat()}] PROMPT SENT TO LLM\n")
                        f.write(f"{'='*60}\n")
                        f.write(prompt)
                        f.write(f"\n{'='*60}\n\n")
                except Exception:
                    logger.exception("Failed to write prompt log")

            return cls._provider.generate(prompt, **kwargs)

    @classmethod
    def test_connection(cls) -> tuple[bool, str]:
        with cls._lock:
            if cls._provider is None:
                return False, "No provider configured"
            return cls._provider.test_connection()