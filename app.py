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
    return jsonify({
        "status": "alive",
        "bot": "Woodeng Tracker",
        "total_alerts": tracker_status["total_alerts"],
        "last_alert": tracker_status["last_alert"] or "Jamais",
        "uptime": "100%",
        "chats": len(ALL_CHAT_IDS)
    }), 200
# ===========================================================================
import asyncio
import aiohttp
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from datetime import datetime
import logging
from typing import List, Dict, Set

# ==================== CONFIGURATION ====================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
GROUP_IDS = [gid.strip() for gid in os.environ.get("TELEGRAM_GROUP_IDS", "").split(",") if gid.strip()]

ALL_CHAT_IDS: List[str] = []
if CHAT_ID and CHAT_ID.lstrip("-").isdigit():
    ALL_CHAT_IDS.append(CHAT_ID)
for gid in GROUP_IDS:
    if gid.lstrip("-").isdigit():
        ALL_CHAT_IDS.append(gid)

# Conversion finale en int (Telegram exige des int)
ALL_CHAT_IDS = [int(cid) for cid in ALL_CHAT_IDS if cid.lstrip("-").isdigit()]

HELIUS_RPC_URL = os.environ.get("WOODENG_API_URL", "")  # Tu mettras ton URL Helius ici
WOODENG_PROGRAM_ID = "8YCde6Jm1Xz8FDiYS3R4AksgNVPEmrjNvkmdMnugEzrV"
WOODENG_MINT = "83zcTaQRqL1s3PxBRdGVkee9PiGLVP6JXg3oLVF6eAR5"
PINATA_GATEWAY = "https://gateway.pinata.cloud/ipfs"
CHECK_INTERVAL = 4.2
PORT = int(os.environ.get("PORT", 5000))

# ==================================== LOGGING & STATE ====================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
logger = logging.getLogger(__name__)

sent_txs: Set[str] = set()
token_cache: Dict[str, dict] = {}

tracker_status = {
    "total_alerts": 0,
    "last_alert": None
}

IPFS_GATEWAYS = [
    "https://gateway.pinata.cloud/ipfs",
    "https://ipfs.io/ipfs",
    "https://cloudflare-ipfs.com/ipfs",
]

# ==================================== HELPERS ====================================
def escape_md(text: str) -> str:
    for c in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(c, f"\\{c}")
    return text

def format_amount(n: float) -> str:
    return f"{n:,.2f}"

def convert_ipfs(uri: str) -> str:
    if not uri or uri.startswith("http"):
        return uri
    if uri.startswith("ipfs://"):
        uri = uri[7:]
    return f"https://gateway.pinata.cloud/ipfs/{uri}"

async def get_token_metadata(mint: str, session: aiohttp.ClientSession) -> dict:
    if mint in token_cache:
        return token_cache[mint]

    default = {"name": mint[:8], "symbol": "?", "image": None, "audio": None}

    if not HELIUS_RPC_URL:
        token_cache[mint] = default
        return default

    try:
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getAsset", "params": [mint]}
        async with session.post(HELIUS_RPC_URL, json=payload) as r:
            if r.status == 200:
                data = await r.json()
                asset = data.get("result")
                if asset:
                    meta = asset.get("content", {}).get("metadata", {})
                    json_uri = asset.get("content", {}).get("json_uri") or meta.get("uri", "")

                    result = {
                        "name": meta.get("name", mint[:8]),
                        "symbol": meta.get("symbol", "?"),
                        "image": None,
                        "audio": None
                    }

                    if json_uri:
                        ipfs_hash = json_uri.split("/")[-1]
                        for gw in IPFS_GATEWAYS:
                            try:
                                async with session.get(f"{gw}/{ipfs_hash}", timeout=10) as ipfs_r:
                                    if ipfs_r.status == 200:
                                        js = await ipfs_r.json()
                                        if js.get("image"):
                                            result["image"] = convert_ipfs(js["image"])
                                        if js.get("animation_url"):
                                            result["audio"] = convert_ipfs(js["animation_url"])
                                        break
                            except:
                                continue
                    token_cache[mint] = result
                    return result
    except Exception as e:
        logger.warning(f"Metadata error {mint[:8]}: {e}")

    token_cache[mint] = default
    return default

def calculate_changes(tx) -> dict:
    changes = {"woodeng_spent": 0.0, "tokens_bought": {}}
    if not tx or not tx.get("meta"):
        return changes

    pre = {f"{b.get('owner','')}-{b.get('mint','')}": float(b.get("uiTokenAmount", {}).get("uiAmount") or 0)
           for b in tx["meta"].get("preTokenBalances", [])}
    post = {f"{b.get('owner','')}-{b.get('mint','')}": float(b.get("uiTokenAmount", {}).get("uiAmount") or 0)
            for b in tx["meta"].get("postTokenBalances", [])}

    for key, pre_amt in pre.items():
        mint = key.split("-")[-1]
        post_amt = post.get(key, 0.0)
        diff = pre_amt - post_amt
        if diff > 0.01 and mint == WOODENG_MINT:
            changes["woodeng_spent"] += diff
        elif post_amt > pre_amt + 0.01 and mint != WOODENG_MINT:
            changes["tokens_bought"][mint] = changes["tokens_bought"].get(mint, 0) + (post_amt - pre_amt)
    return changes

# ==================================== ALERT ====================================
async def send_alert(bot: Bot, sig: str, changes: dict, session: aiohttp.ClientSession):
    if not changes.get("tokens_bought"):
        return

    mint = next(iter(changes["tokens_bought"]))
    meta = await get_token_metadata(mint, session)
    name = meta.get("name", "Unknown Sound")

    caption = (
        f"*PURCHASE DETECTED*\n\n"
        f"*{escape_md(name)}*\n"
        f"WOODENG spent: {format_amount(changes['woodeng_spent'])}\n\n"
        f"[Solscan](https://solscan.io/tx/{sig})\n"
        f"`{sig}`"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("View Tx", url=f"https://solscan.io/tx/{sig}")],
        [
            InlineKeyboardButton("Buy WOODENG", url="https://raydium.io/swap/?outputMint=83zcTaQRqL1s3PxBRdGVkee9PiGLVP6JXg3oLVF6eAR5"),
            InlineKeyboardButton("Buy Sound Meme", url="https://woodengsol.com/sound-memes")
        ]
    ])

    for chat_id in ALL_CHAT_IDS:
        try:
            if meta.get("image"):
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=meta["image"],
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=keyboard
                )
                if meta.get("audio"):
                    await bot.send_audio(chat_id=chat_id, audio=meta["audio"], title=name)
            else:
                await bot.send_message(
                    chat_id=chat_id,
                    text=caption,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=keyboard,
                    disable_web_page_preview=True
                )
        except Exception as e:
            logger.error(f"Erreur envoi → {chat_id}: {e}")

    tracker_status["total_alerts"] += 1
    tracker_status["last_alert"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    logger.info(f"Alerte envoyée — {format_amount(changes['woodeng_spent'])} WOODENG")

# ==================================== MAIN TRACKER ====================================
async def track_woodeng():
    if not TELEGRAM_TOKEN or not ALL_CHAT_IDS:
        logger.error("TOKEN ou CHAT_ID manquant !")
        return

    bot = Bot(token=TELEGRAM_TOKEN)

    # Message de démarrage
    for cid in ALL_CHAT_IDS:
        try:
            await bot.send_message(cid, "*Woodeng Tracker démarré – chasse ouverte...*")
        except:
            pass

    async with aiohttp.ClientSession() as session:
        last_sig = None
        while True:
            try:
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getSignaturesForAddress",
                    "params": [WOODENG_PROGRAM_ID, {"limit": 30, "before": last_sig}]
                }
                async with session.post(HELIUS_RPC_URL, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        signatures = data.get("result", [])

                        for item in reversed(signatures):
                            sig = item["signature"]
                            if sig in sent_txs:
                                continue
                            sent_txs.add(sig)

                            tx_payload = {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "getTransaction",
                                "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
                            }
                            async with session.post(HELIUS_RPC_URL, json=tx_payload) as tx_resp:
                                if tx_resp.status == 200:
                                    tx_data = await tx_resp.json()
                                    tx = tx_data.get("result")
                                    if tx:
                                        changes = calculate_changes(tx)
                                        if changes["woodeng_spent"] > 0.5:
                                            await send_alert(bot, sig, changes, session)

                            if not last_sig:
                                last_sig = sig

                await asyncio.sleep(CHECK_INTERVAL)
            except Exception as e:
                logger.error(f"Erreur tracker: {e}")
                await asyncio.sleep(10)

# ==================================== LANCEMENT FLASK + BOT ====================================
def run_flask():
    app.run(host="0.0.0.0", port=PORT, use_reloader=False)

if __name__ == "__main__":
    # Flask dans un thread séparé (obligatoire sur Render)
    Thread(target=run_flask, daemon=True).start()
    logger.info(f"Serveur Flask démarré sur le port {PORT}")
    
    # Lancement du tracker async
    asyncio.run(track_woodeng())
