from __future__ import annotations

import asyncio
import sys

import pytest


@pytest.fixture(scope="session")
def _asyncio_loop_factory() -> type[asyncio.AbstractEventLoop] | None:
    """Use the event-loop implementation supported by async psycopg on Windows."""
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop
    return None
