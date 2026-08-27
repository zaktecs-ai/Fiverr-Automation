"""Tests for config loading and validation."""
import pytest

from scraper.config import ConfigError, load_config, validate_config


def _minimal_cfg(**overrides):
    cfg = {
        "job": {"client_name": "test", "output_filename": "test",
                "max_results_per_query": 0, "max_total_results": 0},
        "queries": ["dentists in Dallas, TX"],
        "maps": {"include_permanently_closed": False,
                 "browser_restart_after_queries": 5,
                 "scroll_delay_min_ms": 800, "scroll_delay_max_ms": 1600},
        "website": {"require_website": False, "enable_playwright_fallback": True,
                    "enable_sitemap": True, "max_pages_per_site": 8,
                    "overall_site_timeout_seconds": 120,
                    "http_connect_timeout_seconds": 10.0,
                    "http_read_timeout_seconds": 20.0,
                    "page_navigation_timeout_seconds": 30.0},
        "email": {"enabled": True, "max_email_length": 120,
                  "enable_mx_check": False, "enable_ocr": False},
        "smtp": {"enabled": False, "workers": 3, "retries": 1,
                 "connection_timeout_seconds": 10, "verification_timeout_seconds": 20},
        "concurrency": {"google_maps_workers": 2, "website_workers": 4,
                        "playwright_workers": 2},
        "delays": {"maps_min_seconds": 2.0, "maps_max_seconds": 5.0,
                   "site_min_seconds": 0.5, "site_max_seconds": 1.5,
                   "cooldown_seconds": 60.0},
        "signals": {},
        "filters": {},
    }
    cfg.update(overrides)
    return cfg


class TestValidation:
    def test_valid_config_passes(self):
        validate_config(_minimal_cfg())  # should not raise

    def test_missing_queries_fails(self):
        cfg = _minimal_cfg()
        del cfg["queries"]
        with pytest.raises(ConfigError) as e:
            validate_config(cfg)
        assert "queries" in str(e.value)

    def test_out_of_range_workers(self):
        cfg = _minimal_cfg()
        cfg["concurrency"]["website_workers"] = 20
        with pytest.raises(ConfigError) as e:
            validate_config(cfg)
        msg = str(e.value)
        assert "website_workers" in msg
        assert "1 and 8" in msg

    def test_bool_must_be_bool(self):
        cfg = _minimal_cfg()
        cfg["maps"]["include_permanently_closed"] = "false"  # string, not bool
        with pytest.raises(ConfigError):
            validate_config(cfg)

    def test_error_message_is_human_readable(self):
        cfg = _minimal_cfg()
        cfg["concurrency"]["website_workers"] = 20
        with pytest.raises(ConfigError) as e:
            validate_config(cfg)
        assert "Recommended" in str(e.value) or "recommended" in str(e.value)


class TestLoadConfig:
    def test_loads_yaml_and_resolves_output_dir(self, tmp_path):
        cfg = _minimal_cfg()
        import yaml
        p = tmp_path / "config.yaml"
        p.write_text(yaml.safe_dump(cfg))
        loaded = load_config(str(p))
        assert loaded["resolved_output_dir"] == "output/test"

    def test_missing_file_raises(self):
        with pytest.raises(ConfigError):
            load_config("/nonexistent/config.yaml")

    def test_invalid_yaml_raises(self, tmp_path):
        p = tmp_path / "bad.yaml"
        p.write_text("job: [unclosed")
        with pytest.raises(ConfigError):
            load_config(str(p))
