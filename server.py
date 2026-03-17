"""
Kripto Drop Mini App — Basit statik dosya sunucusu
Railway'de bot.py ile birlikte asyncio task olarak çalışır.
"""

import os
import asyncio
from aiohttp import web

PORT = int(os.getenv("PORT", 8080))

async def serve_index(request):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    index    = os.path.join(base_dir, "index.html")
    with open(index, "r", encoding="utf-8") as f:
        content = f.read()
    return web.Response(text=content, content_type="text/html", charset="utf-8")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/",         serve_index)
    app.router.add_get("/miniapp",  serve_index)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"✅ Mini App sunucu başladı: http://0.0.0.0:{PORT}")
