"""python -m mcp_server [--stdio] [--host H] [--port P]"""
import argparse
import logging
import os

import uvicorn

from .server import build_http_app, build_server

p = argparse.ArgumentParser(prog="mcp_server")
p.add_argument("--stdio", action="store_true", help="serve over stdio (Claude Desktop / Codex local)")
p.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8790")))
a = p.parse_args()
logging.basicConfig(level=logging.INFO)  # stderr: stdout is the protocol channel in stdio mode
if a.stdio:
    build_server().run("stdio")
else:
    uvicorn.run(build_http_app(), host=a.host, port=a.port, log_level="info")
