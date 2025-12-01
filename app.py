# main.py – Version finale 100% stable pour Render.com
import os
import asyncio
import logging
from datetime import datetime

import requests
from fastapi import FastAPI, Request
from telegram import Bot, Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

# ===================== CONFIG =====================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_IDS = [int(x) for x in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",") if x.strip()]
HELIUS_RPC = os.environ["HELIUS_RPC_URL"]

WOODENG_PROGRAM = "8YCde6Jm1Xz8FDiYS3R4AksgNVPEmrjNvkmdMnugEzrV"
WOODENG_MINT = "83zcTaQRqL1s3PxBRdGVkee9PiGLVP6JXg3oLVF6eAR5"

sent_txs = set()
token_cache = {}

app = FastAPI()
bot = Bot(TELEGRAM_TOKEN)
application = Application.builder().token(TELEGRAM_TOKEN).build()

# ===================== HELPERS =====================
def rpc(payload):
    try:
        r = requests.post(HELIUS_RPC, json=payload, timeout=10)
        return r.json() if r.status_code == 200 else None
    except:
        return None

def get_metadata(mint):
    if mint in token_cache:
        return token_cache[mint]
    
    data = rpc({"jsonrpc":"2.0","id":1,"method":"getAsset","params":[mint]})
    if not data or not data.get("result"):
        token_cache[mint] = {"name": mint[:8], "image": None, "audio": None}
        return token_cache[mint]
    
    asset = data["result"]
    meta = asset.get("content", {}).get("metadata", {})
    uri = asset.get("content", {}).get("json_uri")
    
    name = meta.get("name", mint[:8])
    image = audio = None
    
    if uri and uri.startswith("ipfs://"):
        try:
            ipfs = requests.get(f"https://gateway.pinata.cloud/ipfs/{uri[7:]}", timeout=8).json()
            if ipfs.get("image"):
                image = ipfs["image"].replace("ipfs://", "https://gateway.pinata.cloud/ipfs/")
            if ipfs.get("animation_url"):
                audio = ipfs["animation_url"].replace("ipfs://", "https://gateway.pinata.cloud/ipfs/")
        except:
            pass
    
    result = {"name": name, "image": image, "audio": audio}
    token_cache[mint] = result
    return result

# ===================== TRACKING =====================
async def track():
    while True:
        try:
            sigs = rpc({
                "jsonrpc": "2.0", "id": 1,
                "method": "getSignaturesForAddress",
                "params": [WOODENG_PROGRAM, {"limit": 20}]
            })
            
            if not sigs or not sigs.get("result"):
                await asyncio.sleep(5)
                continue
                
            for item in sigs["result"]:
                sig = item["signature"]
                if sig in sent_txs:
                    continue
                    
                tx = rpc({
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getTransaction",
                    "params": [sig, {"encoding": "jsonParsed"}]
                })
                
                if not tx or not tx.get("result"):
                    continue
                    
                t = tx["result"]
                pre = {f"{b['owner']}-{b['mint']}": float(b["uiTokenAmount"]["uiAmount"] or 0)
                       for b in t["meta"].get("preTokenBalances", [])}
                post = {f"{b['owner']}-{b['mint']}": float(b["uiTokenAmount"]["uiAmount"] or 0)
                        for b in t["meta"].get("postTokenBalances", [])}
                
                spent = 0
                bought = None
                for key, pre_amt in pre.items():
                    mint = key.split("-")[1]
                    post_amt = post.get(key, 0)
                    if mint == WOODENG_MINT and pre_amt > post_amt + 0.1:
                        spent += pre_amt - post_amt
                    elif post_amt > pre_amt + 0.1 and mint != WOODENG_MINT and not bought:
                        bought = mint
                        
                if spent > 0.1 and bought:
                    sent_txs.add(sig)
                    meta = get_metadata(bought)
                    
                    text = f"NEW PURCHASE\n\n*{meta['name']}*\nWOODENG spent: {spent:,.2f}\n\n[Solscan](https://solscan.io/tx/{sig})"
                    keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton("View TX", url=f"https://solscan.io/tx/{sig}")],
                        [InlineKeyboardButton("Buy WOODENG", url="https://raydium.io/swap/?outputMint=83zcTaQRqL1s3PxBRdGVkee9PiGLVP6JXg3oLVF6eAR5")]
                    ])
                    
                    for chat_id in CHAT_IDS:
                        try:
                            if meta["image"]:
                                await bot.send_photo(chat_id, meta["image"], caption=text,
                                                   parse_mode=ParseMode.MARKDOWN_V2, reply_markup=keyboard)
                                if meta["audio"]:
                                    await bot.send_audio(chat_id, meta["audio"], title=meta["name"])
                            else:
                                await bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN_V2,
                                                     reply_markup=keyboard, disable_web_page_preview=True)
                        except Exception as e:
                            logger.error(f"Send failed: {e}")
                            
        except Exception as e:
            logger.error(f"Track error: {e}")
        
        await asyncio.sleep(5)

# ===================== COMMANDS =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Woodeng Tracker 24/7 ON – scan toutes les 5s")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update
    
