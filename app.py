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
# Configure logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configuration
HELIUS_RPC_URL = os.environ.get("HELIUS_RPC_URL", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_GROUP_IDS = [
    gid.strip() for gid in os.environ.get("TELEGRAM_GROUP_IDS", "").split(",")
    if gid.strip()
]

# Constants
WOODENG_PROGRAM_ID = "8YCde6Jm1Xz8FDiYS3R4AksgNVPEmrjNvkmdMnugEzrV"
WOODENG_MINT = "83zcTaQRqL1s3PxBRdGVkee9PiGLVP6JXg3oLVF6eAR5"
PINATA_GATEWAY = "https://gateway.pinata.cloud/ipfs"
IPFS_GATEWAYS = [
    "https://gateway.pinata.cloud/ipfs",
    "https://ipfs.io/ipfs",
    "https://cloudflare-ipfs.com/ipfs",
]

# State tracking
sent_txs: Set[str] = set()
token_cache: Dict[str, Dict] = {}
last_transactions: List[Dict] = []  # Track last 10 transactions


def get_chat_ids() -> List[int]:
    """Get all chat IDs from environment variables"""
    chat_ids = []

    if TELEGRAM_CHAT_ID and TELEGRAM_CHAT_ID.isdigit():
        chat_ids.append(int(TELEGRAM_CHAT_ID))

    for gid in TELEGRAM_GROUP_IDS:
        if gid.isdigit():
            chat_ids.append(int(gid))

    return chat_ids


def convert_ipfs_to_url(uri: str) -> str:
    """Convert IPFS URI to HTTP gateway URL"""
    if not uri or uri.startswith("http"):
        return uri
    if uri.startswith("ipfs://"):
        return f"{PINATA_GATEWAY}/{uri.replace('ipfs://', '')}"
    return uri


def fetch_ipfs_json(uri: str) -> dict:
    """Fetch JSON from IPFS with multiple gateway fallback"""
    url = convert_ipfs_to_url(uri)

    for gateway in IPFS_GATEWAYS:
        try:
            if uri.startswith("ipfs://"):
                fetch_url = f"{gateway}/{uri.replace('ipfs://', '')}"
            else:
                fetch_url = url

            logger.info(f"📡 Fetching IPFS from {gateway}")
            response = requests.get(fetch_url, timeout=15)

            if response.status_code == 200:
                logger.info("✅ IPFS fetch successful")
                return response.json()
        except Exception as e:
            logger.warning(f"⚠️ IPFS gateway failed: {e}")

    return {}


def get_token_metadata(mint: str) -> dict:
    """Fetch token metadata from Solana"""
    if mint in token_cache:
        return token_cache[mint]

    result = {"name": mint[:8], "symbol": "?", "image": None, "audio": None}

    if not HELIUS_RPC_URL:
        logger.error("Missing HELIUS_RPC_URL")
        return result

    try:
        payload = {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "getAsset",
            "params": [mint]
        }

        response = requests.post(HELIUS_RPC_URL, json=payload, timeout=15)

        if response.status_code == 200:
            data = response.json()
            asset = data.get("result")

            if asset:
                content = asset.get("content", {})
                meta = content.get("metadata", {})

                result["name"] = meta.get("name", mint[:8])
                result["symbol"] = meta.get("symbol", "?")

                # Try to get metadata URI
                metadata_uri = content.get("json_uri") or meta.get("uri")

                if metadata_uri:
                    ipfs_data = fetch_ipfs_json(metadata_uri)

                    if ipfs_data:
                        if ipfs_data.get("image"):
                            result["image"] = convert_ipfs_to_url(
                                ipfs_data["image"])
                        if ipfs_data.get("animation_url"):
                            result["audio"] = convert_ipfs_to_url(
                                ipfs_data["animation_url"])

                token_cache[mint] = result
                return result
    except Exception as e:
        logger.error(f"❌ Error fetching token metadata: {e}")

    token_cache[mint] = result
    return result


def calculate_token_changes(tx_data: dict) -> dict:
    """Calculate token changes in a transaction"""
    result = {"woodeng_spent": 0, "tokens_bought": {}}

    if not tx_data or not tx_data.get("meta"):
        return result

    try:
        meta = tx_data["meta"]
        pre_balances = {}
        post_balances = {}

        for balance in meta.get("preTokenBalances", []):
            key = f"{balance['owner']}-{balance['mint']}"
            pre_balances[key] = float(
                balance.get("uiTokenAmount", {}).get("uiAmount") or 0)

        for balance in meta.get("postTokenBalances", []):
            key = f"{balance['owner']}-{balance['mint']}"
            post_balances[key] = float(
                balance.get("uiTokenAmount", {}).get("uiAmount") or 0)

        for key, pre_amt in pre_balances.items():
            post_amt = post_balances.get(key, 0)
            _, mint = key.split("-")

            if mint == WOODENG_MINT and pre_amt > post_amt:
                result["woodeng_spent"] += pre_amt - post_amt
            elif post_amt > pre_amt and mint != WOODENG_MINT:
                result["tokens_bought"][mint] = result["tokens_bought"].get(
                    mint, 0) + (post_amt - pre_amt)
    except Exception as e:
        logger.error(f"Error calculating token changes: {e}")

    return result


async def send_telegram_alert(bot: Bot,
                              chat_id: int,
                              tx_sig: str,
                              token_name: str,
                              woodeng_spent: float,
                              image_url: str = None,
                              audio_url: str = None):
    """Send Telegram alert for purchase"""
    caption = f"🚀 *PURCHASE DETECTED*\n\n*{escape_markdown(token_name)}*\n💰 WOODENG spent: {format_amount(woodeng_spent)}\n\n🔗 [View on Solscan](https://solscan.io/tx/{tx_sig})\n\n📝 TX: `{tx_sig}`"

    reply_markup = {
        "inline_keyboard":
        [[{
            "text": "🔍 View Transaction on Solscan",
            "url": f"https://solscan.io/tx/{tx_sig}"
        }],
         [{
             "text":
             "💎 Buy WOODENG",
             "url":
             "https://raydium.io/swap/?outputMint=83zcTaQRqL1s3PxBRdGVkee9PiGLVP6JXg3oLVF6eAR5"
         }, {
             "text": "🎵 Buy Sound Meme",
             "url": "https://woodengsol.com/sound-memes"
         }]]
    }

    try:
        if image_url:
            await bot.send_photo(chat_id=chat_id,
                                 photo=image_url,
                                 caption=caption,
                                 parse_mode=ParseMode.MARKDOWN,
                                 reply_markup=reply_markup)
            logger.info(f"✅ Photo sent to {chat_id}")

            if audio_url:
                await bot.send_audio(chat_id=chat_id,
                                     audio=audio_url,
                                     title=token_name)
                logger.info(f"✅ Audio sent to {chat_id}")
        else:
            await bot.send_message(chat_id=chat_id,
                                   text=caption,
                                   parse_mode=ParseMode.MARKDOWN,
                                   reply_markup=reply_markup,
                                   disable_web_page_preview=True)
            logger.info(f"✅ Message sent to {chat_id}")
    except Exception as e:
        logger.error(f"❌ Error sending alert to {chat_id}: {e}")


def escape_markdown(text: str) -> str:
    """Escape special characters for Markdown"""
    special_chars = "_*[]()~`>#+-=|{}.!"
    for char in special_chars:
        text = text.replace(char, f"\\{char}")
    return text


def format_amount(amount: float) -> str:
    """Format amount with thousands separator"""
    return f"{amount:,.2f}"


def fetch_recent_transactions(limit: int = 5) -> List[dict]:
    """Fetch recent transactions from WOODENG program"""
    if not HELIUS_RPC_URL:
        logger.error("Missing HELIUS_RPC_URL")
        return []

    try:
        payload = {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "getSignaturesForAddress",
            "params": [WOODENG_PROGRAM_ID, {
                "limit": 20
            }]
        }

        response = requests.post(HELIUS_RPC_URL, json=payload, timeout=15)

        if response.status_code == 200:
            data = response.json()
            signatures = data.get("result", [])
            logger.info(f"📝 Found {len(signatures)} signatures")

            purchases = []

            for sig_data in signatures[:limit]:
                sig = sig_data["signature"]

                try:
                    tx_payload = {
                        "jsonrpc":
                        "2.0",
                        "id":
                        "1",
                        "method":
                        "getTransaction",
                        "params": [
                            sig, {
                                "encoding": "jsonParsed",
                                "maxSupportedTransactionVersion": 0
                            }
                        ]
                    }

                    tx_response = requests.post(HELIUS_RPC_URL,
                                                json=tx_payload,
                                                timeout=15)

                    if tx_response.status_code == 200:
                        tx_data = tx_response.json()
                        tx_result = tx_data.get("result")

                        if tx_result:
                            changes = calculate_token_changes(tx_result)

                            if changes["woodeng_spent"] > 0:
                                purchases.append({
                                    "signature":
                                    sig,
                                    "woodengSpent":
                                    changes["woodeng_spent"],
                                    "tokensBought":
                                    changes["tokens_bought"]
                                })
                                logger.info(
                                    f"✅ Found purchase: {sig[:16]}... ({changes['woodeng_spent']:.2f} WOODENG)"
                                )
                except Exception as e:
                    logger.warning(
                        f"Error processing transaction {sig[:16]}...: {e}")

            return purchases
    except Exception as e:
        logger.error(f"❌ Error fetching transactions: {e}")

    return []


def get_last_transactions() -> List[dict]:
    """Get last 10 tracked transactions"""
    return last_transactions[-10:]


async def track_woodeng():
    """Main tracker function"""
    logger.info("🚀 WOODENG Tracker Starting")

    if not TELEGRAM_BOT_TOKEN:
        logger.error("❌ Missing TELEGRAM_BOT_TOKEN")
        return False

    chat_ids = get_chat_ids()
    if not chat_ids:
        logger.error("❌ No chat IDs configured")
        return False

    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)

        # Send startup message
        await bot.send_message(
            chat_id=chat_ids[0],
            text="🚀 Woodeng Tracker started – let the purchases begin")
        logger.info("✅ Startup message sent")
    except Exception as e:
        logger.error(f"❌ Error sending startup message: {e}")

    # Fetch transactions
    logger.info("📊 Fetching transactions...")
    transactions = fetch_recent_transactions(limit=10)

    if not transactions:
        logger.info("📝 No purchase transactions found")
        return True

    logger.info(f"📬 Processing {len(transactions)} purchases...")

    # Process each transaction
    for tx in transactions:
        if tx["signature"] in sent_txs:
            logger.info(
                f"⏭️ Skipping already sent transaction: {tx['signature'][:16]}..."
            )
            continue

        sent_txs.add(tx["signature"])
        # Track in last_transactions
        if len(last_transactions) >= 10:
            last_transactions.pop(0)
        last_transactions.append(tx)

        # Get first token mint
        first_mint = next(iter(tx["tokensBought"].keys()),
                          None) if tx["tokensBought"] else None

        if not first_mint:
            logger.warning(
                f"⚠️ No tokens bought in transaction {tx['signature'][:16]}..."
            )
            continue

        logger.info(f"📦 Fetching metadata for {first_mint[:8]}...")
        metadata = get_token_metadata(first_mint)

        token_name = metadata.get("name", "Sound Meme")
        image_url = metadata.get("image")
        audio_url = metadata.get("audio")

        logger.info(f"📝 Sending alerts for {token_name}...")

        # Send to all chat IDs
        for chat_id in chat_ids:
            await send_telegram_alert(bot, chat_id, tx["signature"],
                                      token_name, tx["woodengSpent"],
                                      image_url, audio_url)

    logger.info("✅ Monitoring cycle complete")
    return True


if __name__ == "__main__":
    asyncio.run(track_woodeng())
   
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

