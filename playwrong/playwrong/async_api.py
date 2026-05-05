from __future__ import annotations

from .sync_api import sync_playwright


class _AsyncPlaywrongContextManager:
    def __init__(self, **kwargs):
        self._sync_cm = sync_playwright(**kwargs)
        self._instance = None

    async def __aenter__(self):
        self._instance = self._sync_cm.start()
        return self._instance

    async def __aexit__(self, exc_type, exc, traceback):
        self._sync_cm.stop()
        self._instance = None

    async def start(self):
        return await self.__aenter__()

    async def stop(self):
        await self.__aexit__(None, None, None)


def async_playwright(**kwargs):
    return _AsyncPlaywrongContextManager(**kwargs)


__all__ = [
    "async_playwright",
]
