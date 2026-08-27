"""Configuration loading, environment interpolation, and validation.

Design goals:
  * One human-editable YAML file drives the entire engine.
  * Secrets live in `.env` and are referenced as ``${VAR}``.
  * The config is validated *before* any scraping starts; invalid values
    produce a clear, human-readable error naming the offending key, the
    bad value, the allowed range, and a recommendation for the target VPS.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from .utils.normalize import extract_domain  # noqa: F401  (re-export convenience)

ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(ValueError):
    """Raised when the configuration is invalid. Message is user-facing."""


class _EnvResolver:
    def __init__(self) -> None:
        self._missing: list[str] = []

    def resolve(self, value):
        if isinstance(value, str):
            def _sub(m):
                name = m.group(1)
                val = os.environ.get(name)
                if val is None:
                    self._missing.append(name)
                    return m.group(0)
                return val
            return ENV_VAR_RE.sub(_sub, value)
        if isinstance(value, list):
            return [self.resolve(v) for v in value]
        if isinstance(value, dict):
            return {k: self.resolve(v) for k, v in value.items()}
        return value


def _load_dotenv_if_present() -> None:
    """Load .env into os.environ if python-dotenv is installed and file exists."""
    if Path(".env").exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(".env")
        except ImportError:
            pass  # dotenv optional; .env can be sourced by the shell too


def load_config(path: str | Path = "config.yaml") -> dict:
    """Load and validate config; returns a fully-resolved dict.

    Raises ConfigError with a human-readable message on any problem.
    """
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}. "
                          f"Copy config.yaml from the template and edit it.")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from e
    if raw is None:
        raise ConfigError(f"{path} is empty. Provide at least `job` and `queries` sections.")

    _load_dotenv_if_present()
    resolver = _EnvResolver()
    cfg = resolver.resolve(raw)
    if resolver._missing:
        listed = ", ".join(sorted(set(resolver._missing)))
        raise ConfigError(
            f"Missing environment variable(s) referenced in config: {listed}. "
            f"Define them in `.env` (see .env.example)."
        )

    validate_config(cfg)
    _apply_derived(cfg)
    return cfg


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _int_in(cfg: dict, key: str, lo: int, hi: int, recommend: str) -> None:
    val = cfg.get(key)
    if not isinstance(val, int) or isinstance(val, bool):
        raise ConfigError(f"ERROR: {key} must be an integer between {lo} and {hi}.\n"
                          f"Current value: {val!r}\nRecommended for 12 GB VPS: {recommend}")
    if val < lo or val > hi:
        raise ConfigError(f"ERROR: {key} must be between {lo} and {hi}.\n"
                          f"Current value: {val}\nRecommended for 12 GB VPS: {recommend}")


def _bool(cfg: dict, key: str) -> None:
    val = cfg.get(key)
    if not isinstance(val, bool):
        raise ConfigError(f"ERROR: {key} must be `true` or `false`.\nCurrent value: {val!r}")


def _float_in(cfg: dict, key: str, lo: float, hi: float) -> None:
    val = cfg.get(key)
    if not isinstance(val, (int, float)) or isinstance(val, bool):
        raise ConfigError(f"ERROR: {key} must be a number between {lo} and {hi}.\n"
                          f"Current value: {val!r}")
    if val < lo or val > hi:
        raise ConfigError(f"ERROR: {key} must be between {lo} and {hi}.\n"
                          f"Current value: {val}")


def _list_of_str(cfg: dict, key: str, allow_empty: bool = False) -> None:
    val = cfg.get(key)
    if not isinstance(val, list):
        raise ConfigError(f"ERROR: {key} must be a YAML list of strings.\nCurrent value: {val!r}")
    if not allow_empty and len(val) == 0:
        raise ConfigError(f"ERROR: {key} must contain at least one entry.")
    for item in val:
        if not isinstance(item, str):
            raise ConfigError(f"ERROR: {key} must contain only strings.\nOffending item: {item!r}")


def validate_config(cfg: dict) -> None:
    errors: list[str] = []

    def check(fn, *args):
        try:
            fn(*args)
        except ConfigError as e:
            errors.append(str(e))

    # --- job ---
    if "job" not in cfg or not isinstance(cfg["job"], dict):
        errors.append("ERROR: missing required `job:` section.")
    # --- queries ---
    if "queries" not in cfg:
        errors.append("ERROR: missing required `queries:` section. "
                      "List at least one Google-Maps-style search string.")
    else:
        _list_of_str(cfg, "queries")

    job = cfg.get("job", {})
    maps = cfg.get("maps", {})
    website = cfg.get("website", {})
    email = cfg.get("email", {})
    smtp = cfg.get("smtp", {})
    concurrency = cfg.get("concurrency", {})
    signals = cfg.get("signals", {})
    filters = cfg.get("filters", {})
    limits = cfg.get("limits", {})

    check(_int_in, job, "max_results_per_query", 0, 100_000, "200-1000")
    check(_int_in, job, "max_total_results", 0, 1_000_000, "2000-20000")

    check(_bool, maps, "include_permanently_closed")
    for key in ("browser_restart_after_queries", "scroll_delay_min_ms", "scroll_delay_max_ms"):
        check(_int_in, maps, key, 0, 1_000_000, "3 / 800-1600")
    # hl/gl are two-letter language/region codes.
    for key in ("hl", "gl"):
        v = maps.get(key)
        if v is not None and (not isinstance(v, str) or len(v.strip()) != 2):
            errors.append(f"ERROR: maps.{key} must be a 2-letter code (e.g. 'en', 'us').\n"
                          f"Current value: {v!r}")

    check(_int_in, website, "max_pages_per_site", 1, 50, "5-10")
    check(_int_in, website, "overall_site_timeout_seconds", 5, 600, "90-120")
    check(_bool, website, "require_website")
    check(_bool, website, "enable_playwright_fallback")
    check(_bool, website, "enable_sitemap")
    for k in ("http_connect_timeout_seconds", "http_read_timeout_seconds",
              "page_navigation_timeout_seconds"):
        check(_float_in, website, k, 1.0, 300.0)

    check(_bool, email, "enabled")
    check(_int_in, email, "max_email_length", 10, 300, "120")
    check(_bool, email, "enable_mx_check")
    check(_bool, email, "enable_ocr")

    check(_bool, smtp, "enabled")
    check(_int_in, smtp, "workers", 1, 8, "3")
    check(_int_in, smtp, "retries", 0, 10, "1")
    check(_int_in, smtp, "connection_timeout_seconds", 1, 60, "10")
    check(_int_in, smtp, "verification_timeout_seconds", 1, 120, "20")

    for key, default_range in {
        "google_maps_workers": (1, 4),
        "website_workers": (1, 8),
        "playwright_workers": (1, 4),
    }.items():
        check(_int_in, concurrency, key, default_range[0], default_range[1],
              {"google_maps_workers": "2", "website_workers": "4", "playwright_workers": "2"}[key])

    # delay
    delay = cfg.get("delays", {})
    check(_float_in, delay, "maps_min_seconds", 0.0, 60.0)
    check(_float_in, delay, "maps_max_seconds", 0.0, 120.0)
    check(_float_in, delay, "site_min_seconds", 0.0, 60.0)
    check(_float_in, delay, "site_max_seconds", 0.0, 60.0)
    check(_float_in, delay, "cooldown_seconds", 0.0, 3600.0)

    # --- filters ---
    if isinstance(filters, dict):
        def _valid_cond_list(name):
            conds = filters.get(name)
            if conds is None:
                return
            if isinstance(conds, dict):
                conds = [conds]
            if not isinstance(conds, list):
                raise ConfigError(f"ERROR: filters.{name} must be a list of conditions.\n"
                                  f"Current value: {conds!r}")
            for c in conds:
                # A condition is a map of field -> value, or has a `field` key.
                if not isinstance(c, dict):
                    raise ConfigError(
                        f"ERROR: each filters.{name} entry must be a mapping.\n"
                        f"Offending: {c!r}")
                if "field" in c and "value" not in c:
                    # explicit form must also carry the value somewhere
                    vals = {k for k in c if k not in ("field", "op", "negate")}
                    if not vals:
                        raise ConfigError(
                            f"ERROR: filters.{name} condition {c!r} has no value.")
        for name in ("include_all", "include_any", "exclude_all", "exclude_any"):
            _valid_cond_list(name)
    else:
        errors.append("ERROR: `filters` must be a mapping (it looks empty or malformed).")

    # --- signals ---
    if isinstance(signals, dict):
        for name, spec in signals.items():
            if not isinstance(spec, dict):
                errors.append(f"ERROR: signal `{name}` must be a mapping with `enabled` and rules.")
                continue
            has_rule = any(k in spec for k in ("keywords", "regex", "tags"))
            if not has_rule:
                errors.append(f"ERROR: signal `{name}` needs at least one of "
                              f"`keywords`, `regex`, or `tags`.")
    else:
        errors.append("ERROR: `signals` must be a mapping.")

    if errors:
        raise ConfigError("\n\n".join(errors))


def _apply_derived(cfg: dict) -> None:
    """Populate convenience derived fields used throughout the engine."""
    job = cfg.setdefault("job", {})
    # Output directory rooted at `output/` with the client name as a subfolder.
    client = job.get("client_name", "default")
    job["client_name"] = client
    base = job.get("output_filename", client)
    job["output_filename"] = base
    job["output_dir"] = str(Path("output") / client)
    cfg["resolved_output_dir"] = job["output_dir"]

    # Normalize missing-value placeholder to "N/A" if unset.
    cfg.setdefault("missing_value", "N/A")
