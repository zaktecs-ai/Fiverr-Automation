"""B2B Lead Scraper Engine — entrypoint.

Usage:
    python main.py            # load config.yaml, validate, resume-or-start
    python main.py --config myconfig.yaml

The run flow:
  1. validate configuration (abort with a clear message on error)
  2. set up logging
  3. load/seed the checkpoint (resume if present)
  4. build collectors + pipeline and run
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from scraper.config import ConfigError, load_config
from scraper.utils.logging_utils import setup_logging


def _parse_args(argv):
    p = argparse.ArgumentParser(description="B2B Lead Scraper Engine")
    p.add_argument("--config", default="config.yaml",
                   help="Path to the YAML config file (default: config.yaml)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2

    # Logging dir roots under the job output directory.
    output_dir = Path(cfg["resolved_output_dir"])
    setup_logging(output_dir, level=logging.INFO)
    log = logging.getLogger("main")

    log.info("config validated OK — client: %s, output: %s",
             cfg["job"].get("client_name"), output_dir)

    # Build Playwright-backed collectors lazily (so HTTP-only tests don't need it).
    from scraper.browser import BrowserManager, ProxyManager
    from scraper.maps import MapsCollector

    proxy_cfg = ProxyManager().config.from_dict(cfg.get("proxy"))
    proxy_manager = ProxyManager(proxy_cfg)

    maps_cfg = cfg.get("maps", {})
    browser_manager = BrowserManager(
        restart_after_queries=maps_cfg.get("browser_restart_after_queries", 0),
        headless=maps_cfg.get("headless", True),
        proxy=proxy_manager.playwright_proxy(),
        nav_timeout_ms=int(maps_cfg.get("page_navigation_timeout_ms", 30_000)),
        display=cfg.get("vnc", {}).get("display") if not maps_cfg.get("headless", True) else None,
    )

    limits = cfg.get("job", {})
    maps_collector = MapsCollector(
        browser_manager,
        max_results_per_query=limits.get("max_results_per_query", 0),
        max_total_results=limits.get("max_total_results", 0),
        include_permanently_closed=maps_cfg.get("include_permanently_closed", False),
        scroll_delay=(maps_cfg.get("scroll_delay_min_ms", 800),
                      maps_cfg.get("scroll_delay_max_ms", 1600)),
        cooldown_seconds=cfg.get("delays", {}).get("cooldown_seconds", 0.0),
        hl=maps_cfg.get("hl", "en"),
        gl=maps_cfg.get("gl", "us"),
        nav_timeout_ms=int(maps_cfg.get("page_navigation_timeout_ms", 30_000)),
        maps_delay=(float(cfg.get("delays", {}).get("maps_min_seconds", 0.0)),
                    float(cfg.get("delays", {}).get("maps_max_seconds", 0.0))),
    )

    from scraper.pipeline import Pipeline
    pipeline = Pipeline(cfg, maps_collector=maps_collector, browser_manager=browser_manager)

    try:
        pipeline.run()
    except KeyboardInterrupt:
        log.warning("interrupted — checkpoint state is durable; rerun to resume.")
        pipeline.csv.close()
        pipeline.store.write_json_mirror()
        pipeline.store.close()
        return 130
    except Exception as e:  # noqa: BLE001
        log.exception("fatal error: %s", e)
        try:
            pipeline.csv.close()
            pipeline.store.write_json_mirror()
            pipeline.store.close()
        except Exception:
            pass
        return 1
    finally:
        try:
            browser_manager.close()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
