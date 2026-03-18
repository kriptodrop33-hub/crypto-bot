import os
from aiohttp import web
import aiohttp

PORT = int(os.getenv("PORT", 8080))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# HTML serve
async def serve_index(request):
    with open(os.path.join(BASE_DIR, "index.html"), "r", encoding="utf-8") as f:
        return web.Response(text=f.read(), content_type="text/html")

# PROXY (CORS fix)
async def handle_proxy(request):
    url = request.query.get("url")
    if not url:
        return web.Response(text="Missing url", status=400)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.read()
                return web.Response(
                    body=data,
                    status=resp.status,
                    headers={
                        "Access-Control-Allow-Origin": "*",
                        "Content-Type": resp.headers.get("Content-Type", "application/json")
                    }
                )
    except Exception as e:
        return web.Response(text=str(e), status=500)

async def start():
    app = web.Application()
    app.router.add_get("/", serve_index)
    app.router.add_get("/miniapp", serve_index)
    app.router.add_get("/api/proxy", handle_proxy)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    print(f"Server running on {PORT}")

if __name__ == "__main__":
    import asyncio
    asyncio.run(start())
