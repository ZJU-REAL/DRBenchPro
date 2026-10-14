"""Configuration without embedding credentials in artifacts."""
import os
from pathlib import Path
from importlib.resources import files
import yaml

def load_config(kind, path=None):
    resource = Path(path) if path else files("drbenchpro").joinpath("resources", "configs", kind + ".yaml")
    config = yaml.safe_load(resource.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a mapping")
    return config

def resolve_model(config):
    settings = dict(config)
    for field, default_env in (("api_key", "MODEL_API_KEY"), ("base_url", "MODEL_BASE_URL"), ("model", "MODEL_NAME")):
        settings[field] = str(settings.get(field) or os.getenv(settings.get(field + "_env", default_env), "")).strip()
        if not settings[field]:
            raise ValueError(f"Set {settings.get(field + '_env', default_env)} or config {field}")
    return settings

def public_settings(value):
    if isinstance(value, dict):
        return {k: public_settings(v) for k, v in value.items() if k.lower() not in {"api_key", "authorization", "headers", "token", "password"}}
    if isinstance(value, list):
        return [public_settings(v) for v in value]
    return value
