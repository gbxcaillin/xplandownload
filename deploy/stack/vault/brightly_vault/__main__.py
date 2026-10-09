"""Run both apps: public on 8080 (Caddy -> /v/*), staff on 8081 (oauth2-proxy -> /vault/)."""

import asyncio
import logging
import sys

import uvicorn

from .config import ConfigError, load
from .store import Store
from .web import build_admin, build_public


async def snapshots(store: Store) -> None:
    while True:
        try:
            store.snapshot()
        except Exception as exc:
            logging.getLogger("vault").error("snapshot failed: %s", exc)
        await asyncio.sleep(3600)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        s = load()
    except ConfigError as exc:
        sys.exit(f"vault: {exc}")
    store = Store(s.data_dir, s.key)
    if s.require_scan and not s.clamd_host:
        logging.warning("VAULT_REQUIRE_SCAN is on but CLAMD_HOST isn't set: uploads are refused")
    servers = [
        uvicorn.Server(uvicorn.Config(build_public(s, store), host="0.0.0.0", port=8080,
                                      proxy_headers=False, server_header=False,
                                      log_level="info")),
        uvicorn.Server(uvicorn.Config(build_admin(s, store), host="0.0.0.0", port=8081,
                                      proxy_headers=False, server_header=False,
                                      log_level="info")),
    ]
    await asyncio.gather(snapshots(store), *(srv.serve() for srv in servers))


if __name__ == "__main__":
    asyncio.run(main())
