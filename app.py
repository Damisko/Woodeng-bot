# ================ AJOUT POUR RENDER 24/7 – /health endpoint ================
from flask import Flask, jsonify
import os
from threading import Thread 
import asyncio 

app = Flask(__name__)

@app.route('/')
def home():
    return "<h1>Woodeng Tracker 24/7</h1><p>Bot actif – <a href='/health'>/health</a></p>"

@app.route('/health')
def health():
    return jsonify({"status": "alive", "bot": "Woodeng Tracker", "uptime": "100%"}), 200
# ===========================================================================
import asyncio
import os
import aiohttp
from aiohttp import web
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from datetime import datetime
# ==================== CONFIGURATION ====================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
GROUP_IDS = [gid.strip() for gid in os.environ.get("TELEGRAM_GROUP_IDS", "").split(",") if gid.strip()]
ALL_CHAT_IDS = [CHAT_ID] if CHAT_ID else []
ALL_CHAT_IDS.extend(GROUP_IDS)
HELIUS_RPC_URL = os.environ.get("WOODENG_API_URL", "")  # À configurer via env pour sécurité
WOODENG_PROGRAM_ID = "8YCde6Jm1Xz8FDiYS3R4AksgNVPEmrjNvkmdMnugEzrV"
WOODENG_MINT = "83zcTaQRqL1s3PxBRdGVkee9PiGLVP6JXg3oLVF6eAR5"
PINATA_GATEWAY = "https://gateway.pinata.cloud/ipfs"
CHECK_INTERVAL = 4.2
PORT = int(os.environ.get("PORT", 5000))  # Render utilise $PORT
# ==================== GLOBAL STATE ====================
sent_txs = set()
api_error_count = 0
token_cache = {}
tracker_status = {"running": False, "last_alert": None, "total_alerts": 0}
def format_amount(amount: float) -> str:
    """Format token amount: decimals if needed, else integer."""
    if amount % 1 != 0:
        return f"{amount:.2f}"
    return str(int(amount))
def convert_ipfs_to_pinata(uri: str) -> str:
    """Convert IPFS URI to Pinata gateway URL."""
    if not uri or uri.startswith("http"):
        return uri
    if uri.startswith("ipfs://"):
        return f"{PINATA_GATEWAY}/{uri.replace('ipfs://', '')}"
    return uri
async def fetch_ipfs_json(uri: str, session: aiohttp.ClientSession) -> dict:
    """Fetch JSON metadata from IPFS."""
    if not uri:
        return {}
    try:
        url = convert_ipfs_to_pinata(uri)
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status == 200:
                return await r.json()
    except Exception as e:
        print(f"❌ IPFS fetch failed: {str(e)[:80]}")
    return {}
async def get_solscan_nft_metadata(mint: str, session: aiohttp.ClientSession) -> dict:
    """Get NFT metadata from Solscan API as fallback."""
    try:
        url = f"https://api.solscan.io/api2/nft/metadata?tokenMint={mint}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status == 200 and (data := await r.json()).get("success"):
                if uri := data.get("data", {}).get("uri"):
                    return await fetch_ipfs_json(uri, session)
    except Exception as e:
        print(f"Solscan API error: {str(e)[:40]}")
    return {}
def extract_media_from_metadata(metadata: dict) -> tuple:
    """Extract image and audio URLs from Metaplex metadata."""
    image_uri = metadata.get("image")
    audio_uri = metadata.get("animation_url")
    image = convert_ipfs_to_pinata(image_uri) if image_uri else None
    audio = convert_ipfs_to_pinata(audio_uri) if audio_uri else None
   
    if not image or not audio:
        for file in metadata.get("properties", {}).get("files", []):
            file_type = file.get("type", "").lower()
            file_uri = file.get("uri", "")
            if not image and "image" in file_type:
                image = convert_ipfs_to_pinata(file_uri)
            if not audio and "audio" in file_type:
                audio = convert_ipfs_to_pinata(file_uri)
   
    return image or None, audio or None
async def get_token_metadata(mint: str, session: aiohttp.ClientSession) -> dict:
    """Get token metadata from Helius or Solscan."""
    if not mint or mint == "Unknown":
        return {"name": "Unknown", "symbol": "?", "image": None, "audio": None}
   
    if mint in token_cache:
        return token_cache[mint]
   
    result = {"name": mint[:8], "symbol": "?", "image": None, "audio": None}
   
    # Try Helius getAsset
    try:
        payload = {"jsonrpc": "2.0", "id": "1", "method": "getAsset", "params": [mint]}
        async with session.post(HELIUS_RPC_URL, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status == 200 and (data := await r.json()).get("result"):
                asset = data["result"]
                metadata = asset.get("content", {}).get("metadata", {})
                result["name"] = metadata.get("name", mint[:8])
                result["symbol"] = metadata.get("symbol", "?")
               
                ipfs_data = None
                if uri := metadata.get("uri"):
                    print(f"🔗 Fetching URI from Helius: {uri[:50]}...")
                    ipfs_data = await fetch_ipfs_json(uri, session)
                    if ipfs_data:
                        print(f"✅ Got IPFS data: {list(ipfs_data.keys())}")
                else:
                    print(f"📡 No URI in Helius, trying Solscan...")
                    ipfs_data = await get_solscan_nft_metadata(mint, session)
               
                if ipfs_data:
                    img, aud = extract_media_from_metadata(ipfs_data)
                    print(f"🖼️ Extracted - Image: {img[:40] if img else 'None'}, Audio: {aud[:40] if aud else 'None'}")
                    result["image"], result["audio"] = img, aud
                else:
                    print(f"❌ No IPFS data found for {mint[:8]}")
               
                token_cache[mint] = result
                return result
    except Exception as e:
        print(f"Helius error: {str(e)[:80]}")
   
    token_cache[mint] = result
    return result
async def get_transaction_full(session: aiohttp.ClientSession, tx_sig: str):
    """Fetch full transaction details from Helius."""
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "getTransaction",
            "params": [tx_sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
        }
        async with session.post(HELIUS_RPC_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200 and (data := await r.json()).get("result"):
                return data["result"]
    except Exception as e:
        print(f"Transaction fetch error: {str(e)[:60]}")
    return {}
def _collect_tokens_bought(pre_map: dict, post_map: dict) -> dict:
    """Extract tokens bought (balance increase, excluding WOODENG)."""
    tokens_bought = {}
    for (owner, mint), post_amount in post_map.items():
        if mint == WOODENG_MINT:
            continue
        pre_amount = pre_map.get((owner, mint), 0)
        delta = post_amount - pre_amount
        if delta > 0:
            tokens_bought[mint] = tokens_bought.get(mint, 0) + delta
    return tokens_bought
def calculate_token_changes(tx_data: dict) -> dict:
    """Calculate WOODENG spent and tokens bought. Only count as purchase if WOODENG decreases."""
    result = {"woodeng_spent": 0, "tokens_bought": {}}
    if not tx_data:
        return result
   
    try:
        meta = tx_data.get("meta", {})
        pre_balances = meta.get("preTokenBalances", [])
        post_balances = meta.get("postTokenBalances", [])
       
        # Map balances by (owner, mint)
        pre_map = {(b.get("owner"), b.get("mint")): float(b.get("uiTokenAmount", {}).get("uiAmount", 0))
                   for b in pre_balances if b.get("mint")}
        post_map = {(b.get("owner"), b.get("mint")): float(b.get("uiTokenAmount", {}).get("uiAmount", 0))
                    for b in post_balances if b.get("mint")}
       
        # Check if WOODENG was spent (purchase indicator)
        is_purchase = False
        for (owner, mint), post_amount in post_map.items():
            pre_amount = pre_map.get((owner, mint), 0)
            if post_amount < pre_amount and mint == WOODENG_MINT:
                result["woodeng_spent"] += pre_amount - post_amount
                is_purchase = True
       
        # Only collect tokens bought if WOODENG was spent
        if is_purchase:
            result["tokens_bought"] = _collect_tokens_bought(pre_map, post_map)
       
        # Fallback: check if WOODENG was completely spent (not detected above)
        if result["woodeng_spent"] == 0:
            for (owner, mint), pre_amount in pre_map.items():
                if mint == WOODENG_MINT and pre_amount > post_map.get((owner, mint), 0):
                    result["woodeng_spent"] = pre_amount - post_map[(owner, mint)]
                    result["tokens_bought"] = _collect_tokens_bought(pre_map, post_map)
                    break
    except Exception as e:
        print(f"Calculation error: {str(e)[:40]}")
   
    return result
async def send_transaction_alert(bot: Bot, tx_sig: str, tx_data: dict, session: aiohttp.ClientSession):
    """Send Telegram alert for a transaction."""
    if tx_sig in sent_txs:
        return
    sent_txs.add(tx_sig)
    if len(sent_txs) > 500:
        sent_txs.clear()
   
    # Get full transaction details
    tx_details = await get_transaction_full(session, tx_sig)
    changes = calculate_token_changes(tx_details)
   
    # Format transaction info
    block_time = tx_data.get("blockTime", "Unknown")
    timestamp = datetime.fromtimestamp(block_time).strftime("%Y-%m-%d %H:%M:%S") if isinstance(block_time, int) else "Unknown"
    status = "✅ Success" if tx_data.get("err") is None else "❌ Failed"
   
    # Format WOODENG amount
    woodeng_text = format_amount(changes["woodeng_spent"]) if changes["woodeng_spent"] > 0 else "Unknown"
   
    # Get token info and first media
    token_lines = []
    first_image = None
    first_audio = None
   
    for mint, amount in changes["tokens_bought"].items():
        metadata = await get_token_metadata(mint, session)
        token_lines.append(f"*{metadata['name']}* ({metadata['symbol']}): {format_amount(amount)}")
       
        if not first_image and metadata.get("image"):
            first_image = metadata["image"]
        if not first_audio and metadata.get("audio"):
            first_audio = metadata["audio"]
   
    # Build message
    token_text = "\n".join(token_lines) if token_lines else "N/A"
    message = f"🚀 Sound Meme Purchase!\n\n*WOODENG Spent:* {woodeng_text}\n\n*Tokens Bought:*\n{token_text}\n*Status:* {status}\n*Time:* {timestamp}"
   
    # Build buttons
    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Solscan", url=f"https://solscan.io/tx/{tx_sig}"),
            InlineKeyboardButton("Helius", url=f"https://explorer.helius.xyz/tx/{tx_sig}")
        ],
        [
            InlineKeyboardButton("🛒 BUY Sound", url="https://woodengsol.com/sound-memes"),
            InlineKeyboardButton("💰 BUY Woodeng", url="https://raydium.io/swap/?inputMint=sol&outputMint=83zcTaQRqL1s3PxBRdGVkee9PiGLVP6JXg3oLVF6eAR5")
        ]
    ])
   
    # Send to all chats
    for chat_id in ALL_CHAT_IDS:
        try:
            if first_image:
                print(f"📸 Sending image: {first_image[:60]}...")
                try:
                    await bot.send_photo(chat_id=chat_id, photo=first_image, caption=message, parse_mode=ParseMode.MARKDOWN)
                    print(f"✅ Image sent successfully!")
                except Exception as e:
                    print(f"❌ Image send failed: {str(e)[:80]}")
                    # Fallback: send text message without image
                    await bot.send_message(chat_id=chat_id, text=message, reply_markup=buttons, parse_mode=ParseMode.MARKDOWN)
            else:
                print(f"⚠️ No image found for this token")
                await bot.send_message(chat_id=chat_id, text=message, reply_markup=buttons, parse_mode=ParseMode.MARKDOWN)
           
            if first_audio:
                print(f"🔊 Sending audio: {first_audio[:60]}...")
                try:
                    await bot.send_audio(chat_id=chat_id, audio=first_audio, title="🔊 Sound Meme", performer="Woodeng")
                    print(f"✅ Audio sent successfully!")
                except Exception as e:
                    print(f"⚠️ Audio send failed (not critical): {str(e)[:60]}")
           
            if first_image:
                await bot.send_message(chat_id=chat_id, text="👇 Actions:", reply_markup=buttons)
        except Exception as e:
            print(f"❌ Critical send error for {chat_id}: {str(e)[:80]}")
   
    tracker_status["total_alerts"] += 1
    tracker_status["last_alert"] = datetime.now().isoformat()
    print(f"✅ Alert: Spent {woodeng_text} WOODENG → {len(ALL_CHAT_IDS)} chat(s)")
async def get_recent_transactions(session: aiohttp.ClientSession) -> list:
    """Get recent transactions from Woodeng program."""
    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "getSignaturesForAddress",
        "params": [WOODENG_PROGRAM_ID, {"limit": 15}]
    }
    try:
        async with session.post(HELIUS_RPC_URL, json=payload) as r:
            if r.status == 200 and (data := await r.json()).get("result"):
                return data["result"]
    except Exception as e:
        print(f"Transaction list error: {str(e)[:60]}")
    return []
async def format_last_transactions(session: aiohttp.ClientSession, limit: int = 10) -> str:
    """Format last N transactions for display."""
    transactions = await get_recent_transactions(session)
    if not transactions:
        return "❌ No recent transactions found"
   
    response = f"📊 **Last {min(limit, len(transactions))} Sound Meme Purchases**\n\n"
   
    for idx, tx in enumerate(transactions[:limit], 1):
        sig = tx.get("signature", "Unknown")
        block_time = tx.get("blockTime", 0)
        timestamp = datetime.fromtimestamp(block_time).strftime("%H:%M:%S") if isinstance(block_time, int) else "Unknown"
        status = "✅" if tx.get("err") is None else "❌"
       
        # Get full transaction details for WOODENG amount
        tx_details = await get_transaction_full(session, sig)
        changes = calculate_token_changes(tx_details)
        woodeng_text = format_amount(changes["woodeng_spent"]) if changes["woodeng_spent"] > 0 else "?"
       
        # Get token info with name and symbol
        token_names = []
        for mint in list(changes["tokens_bought"].keys())[:1]:
            metadata = token_cache.get(mint, {})
            token_name = metadata.get("name", "?")
            token_symbol = metadata.get("symbol", "?")
            token_names.append(f"{token_name} ({token_symbol})")
       
        tokens_bought = ", ".join(token_names) if token_names else "Sound Meme"
       
        response += f"{idx}. {status} {woodeng_text} WOODENG → *{tokens_bought}* @ {timestamp}\n"
   
    return response
# ==================== HTTP SERVER ====================
async def dashboard_handler(request):
    """Serve dashboard HTML."""
    try:
        with open("dashboard.html", "r") as f:
            html = f.read()
        return web.Response(text=html, content_type="text/html")
    except Exception as e:
        return web.Response(text=f"Error loading dashboard: {e}", status=500)
async def health_handler(request):
    """Health check endpoint."""
    return web.json_response({
        "status": "healthy",
        "service": "Woodeng Tracker",
        "running": tracker_status["running"],
        "total_alerts": tracker_status["total_alerts"],
        "last_alert": tracker_status["last_alert"]
    })
async def stats_handler(request):
    """Stats endpoint."""
    return web.json_response({
        "cached_tokens": len(token_cache),
        "seen_transactions": len(sent_txs),
        "api_errors": api_error_count,
        "monitored_chats": len(ALL_CHAT_IDS),
        "status": tracker_status
    })
async def handle_telegram_commands(bot: Bot, session: aiohttp.ClientSession):
    """Handle Telegram commands like /soundmememc."""
    last_update_id = 0
   
    while True:
        try:
            # Get new updates
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            params = {"offset": last_update_id + 1, "timeout": 30}
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=35)) as r:
                if r.status == 200 and (data := await r.json()).get("ok"):
                    updates = data.get("result", [])
                   
                    for update in updates:
                        last_update_id = update.get("update_id", last_update_id)
                        message = update.get("message", {})
                        text = message.get("text", "")
                        chat_id = message.get("chat", {}).get("id")
                       
                        if not chat_id:
                            continue
                       
                        # Handle /last command
                        if text.startswith("/last"):
                            try:
                                last_tx_text = await format_last_transactions(session, 10)
                                await bot.send_message(chat_id, last_tx_text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
                            except Exception as e:
                                print(f"❌ /last error: {str(e)[:60]}")
                                await bot.send_message(chat_id, f"❌ Error: {str(e)[:40]}")
                       
                        # Handle /health command
                        elif text.startswith("/health"):
                            running_str = "✅ RUNNING" if tracker_status["running"] else "❌ STOPPED"
                            last_alert_str = tracker_status["last_alert"] or "Never"
                            health_text = f"""🏥 **Bot Health Status**
*Status:* {running_str}
*Total Alerts:* {tracker_status['total_alerts']}
*Last Alert:* {last_alert_str}
*Monitored Chats:* {len(ALL_CHAT_IDS)}
"""
                            await bot.send_message(chat_id, health_text, parse_mode=ParseMode.MARKDOWN)
                       
                        # Handle /help command
                        elif text.startswith("/help"):
                            help_text = """🎵 **Woodeng Tracker Commands**
/last - Last 10 Sound Meme purchases
/health - Bot status & health check
/help - This message
"""
                            await bot.send_message(chat_id, help_text, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            print(f"⚠️ Command handler error: {str(e)[:60]}")
            await asyncio.sleep(5)
async def start_http_server():
    """Start mini HTTP server on PORT."""
    app = web.Application()
    app.router.add_get("/", dashboard_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/stats", stats_handler)
   
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"🌐 HTTP server started on port {PORT}")
   
    # Keep server running
    await asyncio.Event().wait()
async def track_woodeng():
    """Main tracker loop."""
    if not TELEGRAM_TOKEN or not ALL_CHAT_IDS:
        print("❌ Error: Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        return
   
    tracker_status["running"] = True
    bot = Bot(token=TELEGRAM_TOKEN)
    async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0 (WoodengTracker/1.0)"}) as session:
        print(f"🎵 Woodeng Tracker started - monitoring {len(ALL_CHAT_IDS)} chat(s)")
       
        # Send startup message
        for chat_id in ALL_CHAT_IDS:
            try:
                await bot.send_message(chat_id, "🎵 Woodeng Tracker ON - monitoring purchases!")
            except Exception as e:
                print(f"Startup message error: {str(e)[:40]}")
       
        last_checked_sig = None
        global api_error_count
       
        while True:
            try:
                transactions = await get_recent_transactions(session)
                if transactions:
                    api_error_count = 0
                    for idx, tx in enumerate(transactions):
                        sig = tx.get("signature")
                        if sig and (last_checked_sig is None or sig != last_checked_sig):
                            if idx == 0:
                                last_checked_sig = sig
                            await send_transaction_alert(bot, sig, tx, session)
                else:
                    api_error_count += 1
                    if api_error_count <= 3 or api_error_count % 100 == 0:
                        print(f"⚠️ No transactions (errors: {api_error_count})")
            except Exception as e:
                api_error_count += 1
                if api_error_count <= 3 or api_error_count % 100 == 0:
                    print(f"❌ Error: {str(e)[:40]} (count: {api_error_count})")
           
            await asyncio.sleep(CHECK_INTERVAL)
async def main():
    """Run tracker, HTTP server, and command handler concurrently."""
    if not TELEGRAM_TOKEN:
        print("❌ Error: Missing TELEGRAM_BOT_TOKEN")
        return
    print("🚀 Starting Woodeng Telegram Tracker with HTTP server and commands...")
    bot = Bot(token=TELEGRAM_TOKEN)
   
    async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0 (WoodengTracker/1.0)"}) as session:
        await asyncio.gather(
            track_woodeng(),
            start_http_server(),
            handle_telegram_commands(bot, session)
        )
if __name__ == "__main__":

    asyncio.run(main())

# ==================== LANCEMENT RENDER (empêche le sleep) ====================
if __name__ == '__main__':
    # Ton bot Telegram continue de tourner normalement avec asyncio.run(main())
    # On ajoute juste Flask en parallèle pour que Render reste réveillé
    Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000))), daemon=True).start()
# =============================================================================



