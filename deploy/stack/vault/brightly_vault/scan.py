"""Stream a file to ClamAV (clamd INSTREAM over TCP) while it uploads."""

from __future__ import annotations

import asyncio


class ScanError(Exception):
    """The scanner couldn't give a verdict."""


class Scanner:
    def __init__(self, host: str, port: int = 3310, timeout: float = 60):
        self.host, self.port, self.timeout = host, port, timeout
        self.reader = self.writer = None

    async def open(self) -> None:
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), 10)
            self.writer.write(b"zINSTREAM\0")
            await self.writer.drain()
        except (OSError, asyncio.TimeoutError) as exc:
            raise ScanError(f"virus scanner unavailable ({exc})") from exc

    async def feed(self, data: bytes) -> None:
        try:
            for i in range(0, len(data), 256 * 1024):
                piece = data[i:i + 256 * 1024]
                self.writer.write(len(piece).to_bytes(4, "big") + piece)
            await self.writer.drain()
        except (OSError, ConnectionError) as exc:
            raise ScanError(f"virus scanner stopped ({exc})") from exc

    async def finish(self) -> tuple[bool, str]:
        """(clean, detail). Raises ScanError when there's no clear answer."""
        try:
            self.writer.write(b"\0\0\0\0")
            await self.writer.drain()
            reply = await asyncio.wait_for(self.reader.readuntil(b"\0"), self.timeout)
        except (OSError, ConnectionError, asyncio.TimeoutError,
                asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            raise ScanError(f"virus scanner gave no answer ({exc})") from exc
        finally:
            self.close()
        text = reply.rstrip(b"\0").decode(errors="replace").strip()
        if text.endswith(" OK"):
            return True, "clean"
        if text.endswith(" FOUND"):
            return False, text.split(":", 1)[-1].strip().removesuffix(" FOUND")
        raise ScanError(text)

    def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
            self.writer = None
