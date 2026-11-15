"""Sensor daemon: python -m sensor.main"""

from __future__ import annotations

import logging
import signal
import threading

from common.catalog import get_catalog

from .config import SensorConfig
from .emitter import Emitter
from .mcp_watcher import McpConfigWatcher
from .netattr import NetAttributor
from .paths import report
from .process_watcher import ProcessWatcher


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = SensorConfig.load()
    print(report())
    print(f"\nbackend: {cfg.backend_url}   stdio probing: {'ON' if cfg.probe_stdio else 'off (needs consent)'}\n")

    emitter = Emitter(cfg.backend_url, echo=cfg.echo)
    mcp = McpConfigWatcher(cfg, emitter)
    net = NetAttributor(get_catalog(cfg.catalog_path))
    procs = ProcessWatcher(emitter, net, mcp.all_servers, cfg.process_poll_s)

    mcp.start()
    procs.start()
    print(f"watching {len(mcp.locations)} config locations; polling processes every {cfg.process_poll_s}s", flush=True)

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    stop.wait()
    mcp.stop()
    procs.stop()
    emitter.stop()


if __name__ == "__main__":
    main()
