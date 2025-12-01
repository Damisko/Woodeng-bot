# ==================== RENDER 24/7 – /health ====================
from flask import Flask, jsonify
import os
from threading import Thread
import asyncio
import aiohttp
import io
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

app = Flask(__name__)

@app.route('/')
def home():
    return "<h1>Woodeng BUY-ONLY Tracker</h1><p>Images + Audio 100% – <a href='/health'>/health</a></p>"

@app.route('/health')
def health():
    return jsonify({"status": "alive", "bot": "Woodeng BUY-ONLY"}), 200
# ======================================================================

# ==================== CONFIGURATION ====================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "TON_TOKEN_ICI")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "TON_CHAT_ID_ICI")
ALL_CHAT_IDS = [int(x.strip()) for x in CHAT_ID.split(",") if x.strip().isdigit()]

HELIUS_RPC = os.environ.get("WOODENG_API_URL", "https://mainnet.helius-rpc.com/?api-key=")
PROGRAM_ID = "8YCde6Jm1Xz8FDiYS3R4AksgNVPEmrjNvkmdMnugEzrV"
WOODENG_MINT = "83zcTaQRqL1s3PxBRdGVkee9PiGLVP6JXg3oLVF6eAR5"
# ======================================================================

sent_txs = set()

def ipfs_to_https(url):
    if not url:
        return None
    if url.startswith("ipfs://"):
        return "https://gateway.pinata.cloud/ipfs/" + url[7:]
    return url

async def get_media(mint, session):
    try:
        async with session.post(HELIUS_RPC, json={"jsonrpc":"2.0","id":1,"method":"getAsset","params":[mint]}) as r:
            if r.status != 200:
                return {"name": mint[:8], "image": None, "audio": None}
            data = await r.json()
            asset = data.get("result", {})
            uri = asset.get("content", {}).get("metadata", {}).get("uri")
            if not uri:
                return {"name": mint[:8], "image": None, "audio": None}

            json_url = ipfs_to_https(uri)
            async with session.get(json_url) as r2:
                if r2.status != 200:
                    return {"name": mint[:8], "image": None, "audio": None}
                meta = await r2.json()

            name = meta.get("name", mint[:8])
            image = ipfs_to_https(meta.get("image"))
            audio = ipfs_to_https(meta.get("animation_url") or meta.get("external_url"))
            return {"name": name, "image": image, "audio": audio}
    except:
        return {"name": mint[:8], "image": None, "audio": None}

async def is_real_buy(tx):
    try:
        pre = {(b["owner"], b["mint"]): float(b["uiTokenAmount"]["uiAmount"] or 0)
               for b in tx["meta"].get("preTokenBalances", [])}
        post = {(b["owner"], b["mint"]): float(b["uiTokenAmount"]["uiAmount"] or 0)
                for b in tx["meta"].get("postTokenBalances", [])}
        for k, pre_amt in pre.items():
            if k[1] == WOODENG_MINT and pre_amt > post.get(k, 0):
                return True
    except:
        pass
    return False

async def send_alert(bot, sig, session):
    if sig in sent_txs:
        return
    sent_txs.add(sig)

    async with session.post(HELIUS_RPC, json={
        "jsonrpc":"2.0","id":1,"method":"getTransaction",
        "params":[sig, {"encoding":"jsonParsed"}]
    }) as r:
        tx = (await r.json()).get("result", {})

    if not tx or not await is_real_buy(tx):
        return

    spent = 0.0
    first_mint = None
    try:
        pre = {(b["owner"], b["mint"]): float(b["uiTokenAmount"]["uiAmount"] or 0)
               for b in tx["meta"].get("preTokenBalances", [])}
        post = {(b["owner"], b["mint"]): float(b["uiTokenAmount"]["uiAmount"] or 0)
                for b in tx["meta"].get("postTokenBalances", [])}
        for k, pre_amt in pre.items():
            if k[1] == WOODENG_MINT:
                spent += pre_amt - post.get(k, 0)
            elif post.get(k, 0) > pre_amt and not first_mint:
                first_mint = k[1]
    except:
        pass

    if spent <= 0 or not first_mint:
        return

    media = await get_media(first_mint, session)
    img_url = media.get("image")
    audio_url = media.get("audio")
    name = media.get("name", "Sound Meme")

    caption = f"""ACHAT DÉTECTÉ

{name}
WOODENG dépensé : {spent:,.2f}

https://solscan.io/tx/{sig}"""

    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton("Solscan", url=f"https://solscan.io/tx/{sig}"),
        InlineKeyboardButton("BUY Woodeng", url="https://raydium.io/swap/?outputMint=83zcTaQRqL1s3PxBRdGVkee9PiGLVP6JXg3oLVF6eAR5")
    ]])

    for cid in ALL_CHAT_IDS:
        try:
            if img_url:
                async with session.get(img_url) as r:
                    if r.status == 200:
                        img_data = await r.read()
                        img_file = io.BytesIO(img_data)
                        img_file.name = "soundmeme.jpg"
                        await bot.send_photo(cid, photo=img_file, caption=caption,
                                           parse_mode=ParseMode.MARKDOWN, reply_markup=buttons)
                        if audio_url:
                            await bot.send_audio(cid, audio=audio_url, title=name)
                        continue
            await bot.send_message(cid, caption, parse_mode=ParseMode.MARKDOWN,
                                 reply_markup=buttons, disable_web_page_preview=True)
        except Exception as e:
            print("Erreur envoi:", e)

async def tracker():
    if not ALL_CHAT_IDS:
        print("CHAT_ID manquant")
        return
    bot = Bot(TELEGRAM_TOKEN)
    await bot.send_message(ALL_CHAT_IDS[0], "Woodeng BUY-ONLY Tracker ON – images + audio 100%")

    async with aiohttp.ClientSession() as s:
        last = None
        while True:
            try:
                resp = await s.post(HELIUS_RPC, json={
                    "jsonrpc":"2.0","id":1,"method":"getSignaturesForAddress",
                    "params":[PROGRAM_ID, {"limit":10}]
                })
                data = await resp.json()
                for tx in data.get("result", [])[:5]:
                    sig = tx["signature"]
                    if sig != last:
                        await send_alert(bot, sig, s)
                        last = sig
            except Exception as e:
                print("Erreur:", e)
            await asyncio.sleep(3.8)

# ==================== LANCEMENT RENDER 24/7 ====================
if __name__ == '__main__':
    Thread(target=lambda: asyncio.run(tracker()), daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
