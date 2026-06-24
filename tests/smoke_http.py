"""End-to-end smoke test: connect to the running server over Streamable HTTP,
list tools, and call get_current_time + echo. Run while the server is up:

    python tests/smoke_http.py
"""

import asyncio

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = "http://localhost:8080/mcp"


async def main() -> None:
    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("TOOLS:", [t.name for t in tools.tools])
            r1 = await session.call_tool("get_current_time", {"city": "London"})
            print("get_current_time:", r1.content[0].text)
            r2 = await session.call_tool("echo", {"text": "hello mcp"})
            print("echo:", r2.content[0].text)


if __name__ == "__main__":
    asyncio.run(main())
