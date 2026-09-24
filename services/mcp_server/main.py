"""Entry point for the MCP server."""

import logging
import os

from services.mcp_server.server import mcp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "8080"))

    logger.info("Starting MCP server on %s:%s with %s transport", host, port, transport)
    if transport == "http":
        mcp.run(transport="http", host=host, port=port)
    else:
        mcp.run()
