"""Configuration loading: JSON file + environment-variable overrides.

Providers: "ollama" and "openai_compatible" (OpenAI, GapGPT, LM Studio,
vLLM, OpenRouter, ... anything exposing /chat/completions).
"""
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SUPPORTED_PROVIDERS = ("ollama", "openai_compatible")
DEFAULT_BASE_URLS = {
    "ollama": "http://localhost:11434",
    "openai_compatible": "https://api.openai.com/v1",
}


@dataclass
class Config:
    provider: str
    model: str
    base_url: str
    api_key: str = field(default="", repr=False)         # never shown in repr()/tracebacks
    temperature: float = 0.2
    timeout: float = 120.0
    max_attempts: int = 3
    retry_delay: float = 2.0
    json_mode: bool = False
    sessions_dir: str = "sessions"
    prompts_dir: str = str(PROJECT_ROOT / "prompts")
    extra_headers: dict = field(default_factory=dict)
    seed_rows: int = 8                                   # Stage 6B: sample rows per main table
    validation: dict = field(default_factory=dict)       # Stage 6B: optional real-server settings per DBMS id
    api_key_env: str = ""                                # name of the env var the key came from (for messages only)

    def validate(self):
        if self.provider not in SUPPORTED_PROVIDERS:
            raise ConfigError(
                f"Unsupported provider '{self.provider}'. "
                f"Use one of: {', '.join(SUPPORTED_PROVIDERS)}."
            )
        if not self.model:
            raise ConfigError("Config is missing 'model'.")
        if not self.base_url:
            raise ConfigError("Config is missing 'base_url'.")
        if self.provider == "openai_compatible" and not self.api_key:
            raise ConfigError(
                "Provider 'openai_compatible' needs an API key: set 'api_key_env' "
                "in the config (name of an environment variable holding the key) "
                "or set DBA_AGENT_API_KEY."
            )
        if self.seed_rows < 1:
            raise ConfigError("'seed_rows' must be >= 1.")
        if not isinstance(self.validation, dict):
            raise ConfigError("'validation' must be an object keyed by DBMS id.")
        if self.max_attempts < 1:
            raise ConfigError("'max_attempts' must be >= 1.")


def find_config_path(explicit=None):
    candidate = explicit or os.environ.get("DBA_AGENT_CONFIG") or "config.json"
    path = Path(candidate)
    if not path.is_file():
        raise ConfigError(
            f"Config file not found: {path}. Copy config.example.json to "
            "config.json and edit it (or pass --config / set DBA_AGENT_CONFIG)."
        )
    return path


def load_config(path=None, env=None):
    """Load config from JSON file, apply env overrides, validate."""
    cfg_path = find_config_path(path)
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"Config file {cfg_path} is not valid JSON: {e}")
    except OSError as e:
        raise ConfigError(f"Cannot read config file {cfg_path}: {e}")
    if not isinstance(raw, dict):
        raise ConfigError("Config file must contain a JSON object.")
    return config_from_dict(raw, env)


_TRUE, _FALSE = {"true", "yes", "on", "1"}, {"false", "no", "off", "0"}


def _bool(raw, key, default):
    v = raw.get(key, default)
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in _TRUE | _FALSE:
        return v.strip().lower() in _TRUE
    raise ConfigError(f"'{key}' must be true or false (a JSON boolean), got {v!r}.")


def _num(raw, key, default, kind, lo, hi, lo_open=False):
    v = raw.get(key, default)
    if isinstance(v, bool) or v is None:
        raise ConfigError(f"'{key}' must be a number, got {v!r}.")
    try:
        if kind is int:
            if isinstance(v, float) and not v.is_integer():
                raise ValueError
            n = int(v)
        else:
            n = float(v)
    except (TypeError, ValueError, OverflowError):
        raise ConfigError(f"'{key}' must be {'an integer' if kind is int else 'a number'}, got {v!r}.")
    if n != n or n in (float("inf"), float("-inf")) or n < lo or n > hi or (lo_open and n == lo):
        rng = f"greater than {lo}" if lo_open else f"between {lo} and {hi}"
        raise ConfigError(f"'{key}' must be {rng} (and at most {hi}), got {v!r}.")
    return n


def config_from_dict(raw, env=None):
    env = os.environ if env is None else env
    provider = env.get("DBA_AGENT_PROVIDER") or raw.get("provider")
    if not provider:
        raise ConfigError("Config is missing 'provider'.")
    model = env.get("DBA_AGENT_MODEL") or raw.get("model") or ""
    base_url = (
        env.get("DBA_AGENT_BASE_URL")
        or raw.get("base_url")
        or DEFAULT_BASE_URLS.get(provider, "")
    )
    for key in ("api_key", "api_key_env"):                  # type-check before any string handling
        if raw.get(key) is not None and not isinstance(raw.get(key), str):
            raise ConfigError(f"'{key}' must be a string, got {type(raw[key]).__name__}: {raw[key]!r}.")
    api_key = (env.get("DBA_AGENT_API_KEY") or "").strip() or (raw.get("api_key") or "").strip()
    key_env = raw.get("api_key_env")
    if not api_key and key_env and isinstance(key_env, str):
        api_key = (env.get(key_env) or "").strip()
        if not api_key and provider == "openai_compatible":
            state = "is empty" if key_env in env else "is not set"
            raise ConfigError(
                f"API key missing: environment variable {key_env} {state} (needed for provider "
                f"'{provider}', model '{model or '?'}'). Set it in this terminal, e.g. export {key_env}=... "
                "(PowerShell: $env:" + key_env + "=\"...\"), or set DBA_AGENT_API_KEY.")
    if not isinstance(provider, str):
        raise ConfigError("'provider' must be a string.")
    for key in ("model", "base_url", "api_key", "api_key_env", "sessions_dir", "prompts_dir"):
        if raw.get(key) is not None and not isinstance(raw.get(key), str):
            raise ConfigError(f"'{key}' must be a string, got {type(raw[key]).__name__}: {raw[key]!r}.")
    if not isinstance(base_url, str):
        raise ConfigError(f"'base_url' must be a string, got {base_url!r}.")
    headers = {} if raw.get("extra_headers") is None else raw["extra_headers"]
    if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
        raise ConfigError("'extra_headers' must be an object mapping header names to string values, "
                          'e.g. {"X-Org": "abc"}.')
    validation = {} if raw.get("validation") is None else raw["validation"]
    if not isinstance(validation, dict):
        raise ConfigError("'validation' must be an object keyed by DBMS id.")
    cfg = Config(
        provider=provider,
        model=model,
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        temperature=_num(raw, "temperature", 0.2, float, 0, 2),
        timeout=_num(raw, "timeout", 120, float, 0, 86400, lo_open=True),
        max_attempts=_num(raw, "max_attempts", 3, int, 1, 20),
        retry_delay=_num(raw, "retry_delay", 2.0, float, 0, 3600),
        json_mode=_bool(raw, "json_mode", False),
        sessions_dir=raw.get("sessions_dir") or "sessions",
        prompts_dir=raw.get("prompts_dir") or str(PROJECT_ROOT / "prompts"),
        extra_headers=headers,
        seed_rows=_num(raw, "seed_rows", 8, int, 1, 1000),
        validation=validation,
        api_key_env=key_env if isinstance(key_env, str) else "",
    )
    cfg.validate()
    return cfg
