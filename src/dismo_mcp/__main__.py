"""Command-line entry point."""

from __future__ import annotations

import argparse
import os

from .server import create_http_server, create_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the dismo MCP server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "http", "streamable-http", "sse"),
        default=os.getenv("DISMO_MCP_TRANSPORT", "stdio"),
    )
    parser.add_argument("--host", default=os.getenv("DISMO_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("DISMO_MCP_PORT", "8000")))
    args = parser.parse_args()
    if args.transport != "stdio":
        try:
            server = create_http_server(host=args.host, port=args.port)
        except Exception as exc:
            parser.error(str(exc))
    else:
        server = create_server()
    kwargs = {} if args.transport == "stdio" else {"host": args.host, "port": args.port}
    server.run(transport=args.transport, show_banner=args.transport != "stdio", **kwargs)


if __name__ == "__main__":
    main()
