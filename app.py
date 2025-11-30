import asyncio
import os
import aiohttp
from aiohttp import web
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.constants import ParseMode
from datetime import datetime
import io  # Pour BytesIO en upload
# ==================== CONFIGURATION ====================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
GROUP_IDS = [gid.strip() for gid in os.environ.get("TELEGRAM_GROUP_IDS", "").split(",") if gid.strip()]
ALL_CHAT_IDS = [CHAT_ID] if CHAT_ID else []
ALL_CHAT_IDS.extend(GROUP_IDS)
HELIUS_RPC_URL = os.environ.get("WOODENG_API_URL", "") # À configurer via env pour sécurité
WOODENG_PROGRAM_ID = "8YCde6Jm1Xz8FDiYS3R4AksgNVPEmrjNvkmdMnugEzrV"
WOODENG_MINT = "83zcTaQRqL1s3PxBRdGVkee9PiGLVP6JXg3oLVF6eAR5"
PINATA_GATEWAY = "https://gateway.pinata.cloud/ipfs"
CHECK_INTERVAL = 4.2
PORT = int(os.environ.get("PORT", 5000)) # Render utilise $PORT
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
    """Extract image and audio URLs from Metaplex metadata. Validation extensions."""
    image_uri = metadata.get("image")
    audio_uri = metadata.get("animation_url")
    image = convert_ipfs_to_pinata(image_uri) if image_uri else None
    audio = convert_ipfs_to_pinata(audio_uri) if audio_uri else None
   
    # Validation extensions
    if image and not any(ext in image.lower() for ext in ['.jpg', '.jpeg', '.png', '.gif']):
        print(f"⚠️ Image sans ext valide: {image[:50]}")
        image = None
    if audio and not any(ext in audio.lower() for ext in ['.mp3', '.ogg', '.wav', '.m4a']):
        print(f"⚠️ Audio sans ext valide: {audio[:50]}")
        audio = None
   
    if not image or not audio:
        for file in metadata.get("properties", {}).get("files", []):
            file_type = file.get("type", "").lower()
            file_uri = file.get("uri", "")
            if not image and "image" in file_type:
                converted = convert_ipfs_to_pinata(file_uri)
                if any(ext in converted.lower() for ext in ['.jpg', '.jpeg', '.png', '.gif']):
                    image = converted
            if not audio and "audio" in file_type:
                converted = convert_ipfs_to_pinata(file_uri)
                if any(ext in converted.lower() for ext in ['.mp3', '.ogg', '.wav', '.m4a']):
                    audio = converted
   
    return image or None, audio or None
async def download_media(url: str, session: aiohttp.ClientSession, media_type: str = "image") -> bytes:
    """Télécharge média en bytes pour upload fiable."""
    if not url:
        return b""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                data = await r.read()
                size_mb = len(data) / (1024 * 1024)
                if size_mb > 10:
                    print(f"⚠️ {media_type} trop grand ({size_mb:.1f}MB): {url[:50]}")
                    return b""
                print(f"✅ {media_type} DL OK ({size_mb:.1f}MB): {url[:50]}")
                return data
    except Exception as e:
        print(f"❌ DL {media_type} failed: {str(e)[:80]} {url[:50]}")
    return b""
async def get_token_metadata(mint: str, session: aiohttp.ClientSession) -> dict:
    """Get token metadata from Helius or Solscan."""
    if not mint or mint == "Unknown":
        return {"name": "Unknown", "symbol": "?", "image": None, "audio": None}
   
    if mint in token_cache:
        return token_cache[mint]
   
    result = {"name": mint[:8], "symbol": "?", "image": None, "audio": None}
   
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
                    print(f"🔗 Fetch URI Helius: {uri[:50]}...")
                    ipfs_data = await fetch_ipfs_json(uri, session)
                    if ipfs_data:
                        print(f"✅ IPFS keys: {list(ipfs_data.keys())}")
                else:
                    print(f"📡 No URI, try Solscan...")
                    ipfs_data = await get_solscan_nft_metadata(mint, session)
               
                if ipfs_data:
                    img, aud = extract_media_from_metadata(ipfs_data)
                    print(f"🖼️ Img: {img[:40] if img else 'None'}, Aud: {aud[:40] if aud else 'None'}")
                    result["image"], result["audio"] = img, aud
                else:
                    print(f"❌ No data for {mint[:8]}")
               
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
        print(f"Tx fetch error: {str(e)[:60]}")
    return {}
def _collect_tokens_bought(pre_map: dict, post_map: dict) -> dict:
    """Tokens bought (delta > 0, exclude WOODENG)."""
    tokens = {}
    for (owner, mint), post_amount in post_map.items():
        if mint == WOODENG_MINT:
            continue
        pre_amount = pre_map.get((owner, mint), 0)
        delta = post_amount - pre_amount
        if delta > 0:
            tokens[mint] = tokens.get(mint, 0) + delta
    return tokens
def _collect_tokens_sold(pre_map: dict, post_map: dict) -> dict:
    """Tokens sold (delta < 0, exclude WOODENG). Nouveau pour ventes."""
    tokens = {}
    for (owner, mint), post_amount in post_map.items():
        if mint == WOODENG_MINT:
            continue
        pre_amount = pre_map.get((owner, mint), 0)
        delta = post_amount - pre_amount
        if delta < 0:
            tokens[mint] = tokens.get(mint, 0) - delta  # Absolu pour quantité vendue
    return tokens
def calculate_token_changes(tx_data: dict) -> dict:
    """Calcule spent/received WOODENG et tokens. Différencie buy/sell avec logs."""
    result = {"woodeng_spent": 0, "tokens_bought": {}, "woodeng_received": 0, "tokens_sold": {}}
    if not tx_data:
        return result
   
    try:
        meta = tx_data.get("meta", {})
        pre_balances = meta.get("preTokenBalances", [])
        post_balances = meta.get("postTokenBalances", [])
       
        pre_map = {(b.get("owner"), b.get("mint")): float(b.get("uiTokenAmount", {}).get("uiAmount", 0))
                   for b in pre_balances if b.get("mint")}
        post_map = {(b.get("owner"), b.get("mint")): float(b.get("uiTokenAmount", {}).get("uiAmount", 0))
                    for b in post_balances if b.get("mint")}
       
        # Logs WOODENG changes
        woodeng_changes = []
        for (owner, mint), post_amount in post_map.items():
            if mint == WOODENG_MINT:
                pre_amount = pre_map.get((owner, mint), 0)
                delta = post_amount - pre_amount
                if delta > 0:
                    result["woodeng_received"] += delta
                    woodeng_changes.append(f"{owner[:8]}: +{delta} (received)")
                elif delta < 0:
                    result["woodeng_spent"] += -delta
                    woodeng_changes.append(f"{owner[:8]}: -{ -delta} (spent)")
        print(f"🔍 WOODENG changes: {', '.join(woodeng_changes) if woodeng_changes else 'None'}")
       
        # Collect si achat (spent > 0)
        if result["woodeng_spent"] > 0:
            result["tokens_bought"] = _collect_tokens_bought(pre_map, post_map)
            print("✅ Buy detected: WOODENG spent")
        # Collect si vente (received > 0)
        elif result["woodeng_received"] > 0:
            result["tokens_sold"] = _collect_tokens_sold(pre_map, post_map)
            print("💸 Sell detected: WOODENG received")
        else:
            print("⚠️ No buy/sell: Other tx type")
       
        # Fallback pour spent (si pas détecté initialement)
        if result["woodeng_spent"] == 0 and result["woodeng_received"] == 0:
            for (owner, mint), pre_amount in pre_map.items():
                if mint == WOODENG_MINT:
                    post_amount = post_map.get((owner, mint), 0)
                    if pre_amount > post_amount:
                        result["woodeng_spent"] = pre_amount - post_amount
                        result["tokens_bought"] = _collect_tokens_bought(pre_map, post_map)
                        print("✅ Buy via fallback")
                        break
                    elif post_amount > pre_amount:
                        result["woodeng_received"] = post_amount - pre_amount
                        result["tokens_sold"] = _collect_tokens_sold(pre_map, post_map)
                        print("💸 Sell via fallback")
                        break
    except Exception as e:
        print(f"Calc error: {str(e)[:40]}")
   
    return result
async def send_transaction_alert(bot: Bot, tx_sig: str, tx_data: dict, session: aiohttp.ClientSession):
    """Envoie alerte buy ou sell. Upload médias via DL."""
    if tx_sig in sent_txs:
        return
    sent_txs.add(tx_sig)
    if len(sent_txs) > 500:
        sent_txs.clear()
   
    tx_details = await get_transaction_full(session, tx_sig)
    changes = calculate_token_changes(tx_details)
    spent = changes["woodeng_spent"]
    received = changes["woodeng_received"]
    if spent == 0 and received == 0:
        print(f"⏭️ Skip: No buy/sell in {tx_sig[:20]}")
        return
   
    # Format base
    block_time = tx_data.get("blockTime", "Unknown")
    timestamp = datetime.fromtimestamp(block_time).strftime("%Y-%m-%d %H:%M:%S") if isinstance(block_time, int) else "Unknown"
    status = "✅ Success" if tx_data.get("err") is None else "❌ Failed"
   
    # Première média/token
    first_image = None
    first_audio = None
    token_lines = []
    tokens_dict = changes["tokens_bought"] if spent > 0 else changes["tokens_sold"]
    for mint, amount in tokens_dict.items():
        metadata = await get_token_metadata(mint, session)
        token_lines.append(f"*{metadata['name']}* ({metadata['symbol']}): {format_amount(amount)}")
        if not first_image and metadata.get("image"):
            first_image = metadata["image"]
        if not first_audio and metadata.get("audio"):
            first_audio = metadata["audio"]
   
    token_text = "\n".join(token_lines) if token_lines else "N/A"
   
    # Message buy ou sell
    if spent > 0:
        title = "🚀 Sound Meme Purchase!"
        woodeng_text = format_amount(spent)
        woodeng_label = "*WOODENG Spent:*"
        tokens_label = "*Tokens Bought:*"
    else:  # Sell
        title = "💸 Sound Meme Sale!"
        woodeng_text = format_amount(received)
        woodeng_label = "*WOODENG Received:*"
        tokens_label = "*Tokens Sold:*"
   
    message = f"{title}\n\n{woodeng_label} {woodeng_text}\n\n{tokens_label}\n{token_text}\n*Status:* {status}\n*Time:* {timestamp}"
   
    # Buttons
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
   
    # Envoi par chat
    for chat_id in ALL_CHAT_IDS:
        try:
            # Image upload
            image_data = await download_media(first_image, session, "image") if first_image else b""
            if image_data:
                try:
                    photo_file = InputFile(io.BytesIO(image_data), filename="sound_meme.jpg")
                    await bot.send_photo(chat_id=chat_id, photo=photo_file, caption=message, parse_mode=ParseMode.MARKDOWN)
                    print(f"✅ Image upload OK!")
                except Exception as e:
                    print(f"❌ Image upload fail: {str(e)[:80]}")
                    await bot.send_message(chat_id=chat_id, text=message, reply_markup=buttons, parse_mode=ParseMode.MARKDOWN)
            else:
                await bot.send_message(chat_id=chat_id, text=message, reply_markup=buttons, parse_mode=ParseMode.MARKDOWN)
           
            # Audio upload
            if first_audio:
                audio_data = await download_media(first_audio, session, "audio")
                if audio_data:
                    try:
                        audio_file = InputFile(io.BytesIO(audio_data), filename="sound_meme.mp3")
                        await bot.send_audio(chat_id=chat_id, audio=audio_file, title="🔊 Sound Meme", performer="Woodeng")
                        print(f"✅ Audio upload OK!")
                    except Exception as e:
                        print(f"⚠️ Audio upload fail: {str(e)[:60]}")
           
            await bot.send_message(chat_id=chat_id, text="👇 Actions:", reply_markup=buttons)
        except Exception as e:
            print(f"❌ Send error {chat_id}: {str(e)[:80]}")
   
    tracker_status["total_alerts"] += 1
    tracker_status["last_alert"] = datetime.now().isoformat()
    print(f"✅ Alert {'buy' if spent > 0 else 'sell'}: {woodeng_text} WOODENG → {len(ALL_CHAT_IDS)} chats")
# ... (Reste du code identique : get_recent_transactions, format_last_transactions, HTTP handlers, track_woodeng, main)
async def get_recent_transactions(session: aiohttp.ClientSession) -> list:
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
        print(f"Tx list error: {str(e)[:60]}")
    return []
async def format_last_transactions(session: aiohttp.ClientSession, limit: int = 10) -> str:
    transactions = await get_recent_transactions(session)
    if not transactions:
        return "❌ No recent tx"
   
    response = f"📊 **Last {min(limit, len(transactions))} Sound Meme Tx**\n\n"
   
    for idx, tx in enumerate(transactions[:limit], 1):
        sig = tx.get("signature", "Unknown")
        block_time = tx.get("blockTime", 0)
        timestamp = datetime.fromtimestamp(block_time).strftime("%H:%M:%S") if isinstance(block_time, int) else "Unknown"
        status = "✅" if tx.get("err") is None else "❌"
       
        tx_details = await get_transaction_full(session, sig)
        changes = calculate_token_changes(tx_details)
        if changes["woodeng_spent"] > 0:
            woodeng_text = f"-{format_amount(changes['woodeng_spent'])} (buy)"
        elif changes["woodeng_received"] > 0:
            woodeng_text = f"+{format_amount(changes['woodeng_received'])} (sell)"
        else:
            woodeng_text = "?"
       
        token_names = []
        tokens = changes["tokens_bought"] if changes["woodeng_spent"] > 0 else changes["tokens_sold"]
        for mint in list(tokens.keys())[:1]:
            metadata = token_cache.get(mint, {})
            token_name = metadata.get("name", "?")
            token_names.append(f"{token_name}")
       
        tokens_bought = ", ".join(token_names) if token_names else "Sound Meme"
       
        response += f"{idx}. {status} {woodeng_text} WOODENG ↔ *{tokens_bought}* @ {timestamp}\n"
   
    return response
# HTTP Server (identique)
async def dashboard_handler(request):
    try:
        with open("dashboard.html", "r") as f:
            html = f.read()
        return web.Response(text=html, content_type="text/html")
    except Exception as e:
        return web.Response(text=f"Error dashboard: {e}", status=500)
async def health_handler(request):
    return web.json_response({
        "status": "healthy",
        "service": "Woodeng Tracker",
        "running": tracker_status["running"],
        "total_alerts": tracker_status["total_alerts"],
        "last_alert": tracker_status["last_alert"]
    })
async def stats_handler(request):
    return web.json_response({
        "cached_tokens": len(token_cache),
        "seen_transactions": len(sent_txs),
        "api_errors": api_error_count,
        "monitored_chats": len(ALL_CHAT_IDS),
        "status": tracker_status
    })
async def handle_telegram_commands(bot: Bot, session: aiohttp.ClientSession):
    last_update_id = 0
   
    while True:
        try:
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
                       
                        if text.startswith("/last"):
                            try:
                                last_tx_text = await format_last_transactions(session, 10)
                                await bot.send_message(chat_id, last_tx_text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
                            except Exception as e:
                                print(f"❌ /last error: {str(e)[:60]}")
                                await bot.send_message(chat_id, f"❌ Error: {str(e)[:40]}")
                       
                        elif text.startswith("/health"):
                            running_str = "✅ RUNNING" if tracker_status["running"] else "❌ STOPPED"
                            last_alert_str = tracker_status["last_alert"] or "Never"
                            health_text = f"""🏥 **Bot Health**
*Status:* {running_str}
*Total Alerts:* {tracker_status['total_alerts']}
*Last Alert:* {last_alert_str}
*Chats:* {len(ALL_CHAT_IDS)}
"""
                            await bot.send_message(chat_id, health_text, parse_mode=ParseMode.MARKDOWN)
                       
                        elif text.startswith("/help"):
                            help_text = """🎵 **Woodeng Commands**
/last - Last 10 tx
/health - Status
/help - This
"""
                            await bot.send_message(chat_id, help_text, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            print(f"⚠️ Cmd handler error: {str(e)[:60]}")
            await asyncio.sleep(5)
async def start_http_server():
    app = web.Application()
    app.router.add_get("/", dashboard_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/stats", stats_handler)
   
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"🌐 Server on port {PORT}")
   
    await asyncio.Event().wait()
async def track_woodeng():
    if not TELEGRAM_TOKEN or not ALL_CHAT_IDS:
        print("❌ Missing TOKEN or CHAT_ID")
        return
   
    tracker_status["running"] = True
    bot = Bot(token=TELEGRAM_TOKEN)
    async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0 (WoodengTracker/1.0)"}) as session:
        print(f"🎵 Tracker ON - {len(ALL_CHAT_IDS)} chats")
       
        for chat_id in ALL_CHAT_IDS:
            try:
                await bot.send_message(chat_id, "🎵 Woodeng Tracker ON - monitoring buys/sells!")
            except Exception as e:
                print(f"Startup error: {str(e)[:40]}")
       
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
                        print(f"⚠️ No tx (errors: {api_error_count})")
            except Exception as e:
                api_error_count += 1
                if api_error_count <= 3 or api_error_count % 100 == 0:
                    print(f"❌ Error: {str(e)[:40]} (count: {api_error_count})")
           
            await asyncio.sleep(CHECK_INTERVAL)
async def main():
    if not TELEGRAM_TOKEN:
        print("❌ Missing TOKEN")
        return
    print("🚀 Starting Woodeng Tracker...")
    bot = Bot(token=TELEGRAM_TOKEN)
   
    async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0 (WoodengTracker/1.0)"}) as session:
        await asyncio.gather(
            track_woodeng(),
            start_http_server(),
            handle_telegram_commands(bot, session)
        )
if __name__ == "__main__":
    asyncio.run(main())
