import os
import asyncio
import aiohttp
from aiohttp import web

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = os.getenv("BOT_TOKEN")
PORT  = int(os.getenv("PORT", 8080))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = f"https://{os.getenv('RAILWAY_STATIC_URL')}"
    keyboard = [[InlineKeyboardButton("🚀 MiniApp Aç", web_app={"url": url})]]

    await update.message.reply_text(
        "KriptoDrop MiniApp'e hoşgeldin 🚀",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def serve_index(request):
    with open(os.path.join(BASE_DIR, "index.html"), "r", encoding="utf-8") as f:
        return web.Response(text=f.read(), content_type="text/html")

async def handle_proxy(request):
    url = request.query.get("url")
    if not url:
        return web.Response(text="Missing URL", status=400)

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

async def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))

    web_app = web.Application()
    web_app.router.add_get("/", serve_index)
    web_app.router.add_get("/miniapp", serve_index)
    web_app.router.add_get("/api/proxy", handle_proxy)

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    print(f"Server running on {PORT}")

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
