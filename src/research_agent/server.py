from __future__ import annotations

import argparse
import asyncio
import sys

import uvicorn


def main() -> None:
    """Start the API with a psycopg-compatible event loop on Windows."""
    parser = argparse.ArgumentParser(description="Run the research-agent API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    if sys.platform != "win32":
        uvicorn.run(
            "research_agent.api:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
        )
        return

    if args.reload:
        parser.error("--reload is unavailable with the Windows PostgreSQL launcher")
    config = uvicorn.Config(
        "research_agent.api:app",
        host=args.host,
        port=args.port,
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(server.serve())


if __name__ == "__main__":
    main()
