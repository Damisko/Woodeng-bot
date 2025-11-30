import asyncio
import os
import aiohttp
from aiohttp import web
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.constants import ParseMode
from datetime import datetime
import io
import mimetypes  # Nouveau: pour détecter type depuis URL/content
# ==================== CONFIGURATION ====================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
GROUP_IDS = [gid.strip() for gid in os.environ.get("TELEGRAM_GROUP_IDS", "").split(",") if gid.strip()]
ALL_CHAT_IDS = [CHAT_ID] if CHAT_ID else []
ALL_CHAT_IDS.extend(GROUP_IDS)
HELIUS_RPC_URL = os.environ.get("WOODENG_API_URL", "")
WOODENG_PROGRAM_ID = "8YCde6Jm1Xz8FDiYS3R4AksgNVPEmrjNvkmdMnugEzrV"
WOODENG_MINT = "83zcTaQRqL1s3PxBRdGVkee9PiGLVP6JXg3oLVF6eAR5"
PINATA_GATEWAY = "https://gateway.pinata.cloud/ipfs"
CHECK_INTERVAL = 4.2
PORT = int(os.environ.get("PORT", 5000))
MIN_AMOUNT = 0.01  # Nouveau: seuil min pour alerte (évite micro-tx)
# ==================== GLOBAL STATE ====================
sent_txs = set()
api_error_count = 0
token_cache = {}
tracker_status = {"running": False, "last_alert": None, "total_alerts": 0}
def format_amount(amount: float) -> str:
    if amount % 1 != 0:
        return f"{amount:.2f}"
    return str(int(amount))
def convert_ipfs_to_pinata(uri: str) -> str:
    if not uri or uri.startswith("http"):
        return uri
    if uri.startswith("ipfs://"):
        return f"{PINATA_GATEWAY}/{uri.replace('ipfs://', '')}"
    return uri
async def fetch_ipfs_json(uri: str, session: aiohttp.ClientSession) -> dict:
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
    """Extract media. Retiré validation ext stricte."""
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
   
    print(f"🖼️ Extracted image: {image[:40] if image else 'None'}, audio: {audio[:40] if audio else 'None'}")  # Log toujours
    return image or None, audio or None
async def download_media(url: str, session: aiohttp.ClientSession, media_type: str = "image") -> tuple[bytes, str]:
    """Download + détection type pour filename. Retourne (data, guessed_filename)."""
    if not url:
        return b"", ""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (WoodengTracker/1.0)"}  # Ajout headers
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:  # Timeout +5s
            if r.status == 200:
                data = await r.read()
                size_mb = len(data) / (1024 * 1024)
                if size_mb > 20:  # Augmenté limite Telegram (50MB max, mais safe 20)
                    print(f"⚠️ {media_type} trop grand ({size_mb:.1f}MB): skip")
                    return b"", ""
                
                # Détection type pour filename
                content_type = r.headers.get("content-type", "")
                ext = mimetypes.guess_extension(content_type) or f".{media_type}"
                filename = f"sound_meme{ext}"
                print(f"✅ {media_type} DL OK ({size_mb:.1f}MB, type: {content_type}, fn: {filename}): {url[:50]}")
                return data, filename
    except Exception as e:
        print(f"❌ DL {media_type} failed ({str(e)[:80]}): {url[:50]}")
    return b"", ""
# ... (get_token_metadata, get_transaction_full identiques à la version précédente)
async def get_token_metadata(mint: str, session: aiohttp.ClientSession) -> dict:
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
    tokens = {}
    for (owner, mint), post_amount in post_map.items():
        if mint == WOODENG_MINT:
            continue
        pre_amount = pre_map.get((owner, mint), 0)
        delta = post_amount - pre_amount
        if delta > MIN_AMOUNT:
            tokens[mint] = tokens.get(mint, 0) + delta
    return tokens
def _collect_tokens_sold(pre_map: dict, post_map: dict) -> dict:
    tokens = {}
    for (owner, mint), post_amount in post_map.items():
        if mint == WOODENG_MINT:
            continue
        pre_amount = pre_map.get((owner, mint), 0)
        delta = post_amount - pre_amount
        if delta < -MIN_AMOUNT:
            tokens[mint] = tokens.get(mint, 0) - delta
    return tokens
def calculate_token_changes(tx_data: dict) -> dict:
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
       
        # Logs détaillés WOODENG
        woodeng_changes = []
        for key in set(list(pre_map.keys()) + list(post_map.keys())):
            if key[1] == WOODENG_MINT:
                pre = pre_map.get(key, 0)
                post = post_map.get(key, 0)
                delta = post - pre
                if abs(delta) > MIN_AMOUNT:
                    if delta > 0:
                        result["woodeng_received"] += delta
                        woodeng_changes.append(f"{key[0][:8]}: +{delta:.4f} (received)")
                    else:
                        result["woodeng_spent"] += -delta
                        woodeng_changes.append(f"{key[0][:8]}: -{-delta:.4f} (spent)")
        print(f"🔍 WOODENG changes: {', '.join(woodeng_changes) if woodeng_changes else 'None (micro or no change)'}")
       
        if result["woodeng_spent"] > MIN_AMOUNT:
            result["tokens_bought"] = _collect_tokens_bought(pre_map, post_map)
            print(f"✅ Buy detected: Spent {result['woodeng_spent']:.4f}")
        elif result["woodeng_received"] > MIN_AMOUNT:
            result["tokens_sold"] = _collect_tokens_sold(pre_map, post_map)
            print(f"💸 Sell detected: Received {result['woodeng_received']:.4f}")
        else:
            print("⚠️ No buy/sell (below threshold or other tx)")
    except Exception as e:
        print(f"Calc error: {str(e)[:40]}")
   
    return result
async def send_transaction_alert(bot: Bot, tx_sig: str, tx_data: dict, session: aiohttp.ClientSession):
    if tx_sig in sent_txs:
        return
    sent_txs.add(tx_sig)
    if len(sent_txs) > 500:
        sent_txs.clear()
   
    tx_details = await get_transaction_full(session, tx_sig)
    changes = calculate_token_changes(tx_details)
    spent = changes["woodeng_spent"]
    received = changes["woodeng_received"]
    if spent < MIN_AMOUNT and received < MIN_AMOUNT:
        print(f"⏭️ Skip: No significant buy/sell in {tx_sig[:20]}")
        return
   
    block_time = tx_data.get("blockTime", 0)
    timestamp = datetime.fromtimestamp(block_time).strftime("%Y-%m-%d %H:%M:%S") if block_time else "Unknown"
    status = "✅ Success" if tx_data.get("err") is None else "❌ Failed"
   
    first_image = None
    first_audio = None
    token_lines = []
    tokens_dict = changes["tokens_bought"] if spent > MIN_AMOUNT else changes["tokens_sold"]
    for mint, amount in tokens_dict.items():
        metadata = await get_token_metadata(mint, session)
        token_lines.append(f"*{metadata['name']}* ({metadata['symbol']}): {format_amount(amount)}")
        if not first_image and metadata.get("image"):
            first_image = metadata["image"]
        if not first_audio and metadata.get("audio"):
            first_audio = metadata["audio"]
   
    token_text = "\n".join(token_lines) if token_lines else "N/A"
   
    if spent > MIN_AMOUNT:
        title = "🚀 Sound Meme Purchase!"
        woodeng_text = format_amount(spent)
        woodeng_label = "*WOODENG Spent:*"
        tokens_label = "*Tokens Bought:*"
    else:
        title = "💸 Sound Meme Sale!"
        woodeng_text = format_amount(received)
        woodeng_label = "*WOODENG Received:*"
        tokens_label = "*Tokens Sold:*"
   
    message = f"{title}\n\n{woodeng_label} {woodeng_text}\n\n{tokens_label}\n{token_text}\n*Status:* {status}\n*Time:* {timestamp}"
   
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("Solscan", url=f"https://solscan.io/tx/{tx_sig}"), InlineKeyboardButton("Helius", url=f"https://explorer.helius.xyz/tx/{tx_sig}")],
        [InlineKeyboardButton("🛒 BUY Sound", url="https://woodengsol.com/sound-memes"), InlineKeyboardButton("💰 BUY Woodeng", url="https://raydium.io/swap/?inputMint=sol&outputMint={WOODENG_MINT}")]
    ])
   
    for chat_id in ALL_CHAT_IDS:
        try:
            # Image
            image_data, img_fn = await download_media(first_image, session, "image") if first_image else (b"", "")
            sent_media = False
            if image_data:
                try:
                    photo_file = InputFile(io.BytesIO(image_data), filename=img_fn or "sound_meme.jpg")
                    await bot.send_photo(chat_id=chat_id, photo=photo_file, caption=message, parse_mode=ParseMode.MARKDOWN)
                    print(f"✅ Image upload OK pour {tx_sig[:20]}!")
                    sent_media = True
                    await asyncio.sleep(0.5)  # Rate limit
                except Exception as e:
                    print(f"❌ Image upload fail ({str(e)[:80]}): fallback URL")
                    # Fallback: send_photo avec URL (si DL OK mais send fail)
                    if first_image.startswith("http"):
                        await bot.send_photo(chat_id=chat_id, photo=first_image, caption=message, parse_mode=ParseMode.MARKDOWN)
                        print(f"✅ Fallback URL image OK")
                        sent_media = True
            if not sent_media:
                await bot.send_message(chat_id=chat_id, text=message, reply_markup=buttons, parse_mode=ParseMode.MARKDOWN)
           
            # Audio
            if first_audio:
                audio_data, aud_fn = await download_media(first_audio, session, "audio")
                if audio_data:
                    try:
                        audio_file = InputFile(io.BytesIO(audio_data), filename=aud_fn or "sound_meme.mp3")
                        await bot.send_audio(chat_id=chat_id, audio=audio_file, title="🔊 Sound Meme", performer="Woodeng")
                        print(f"✅ Audio upload OK!")
                        await asyncio.sleep(0.5)
                    except Exception as e:
                        print(f"⚠️ Audio upload fail ({str(e)[:60]}): fallback URL")
                        if first_audio.startswith("http"):
                            await bot.send_audio(chat_id=chat_id, audio=first_audio, title="🔊 Sound Meme", performer="Woodeng")
                            print(f"✅ Fallback URL audio OK")
           
            await bot.send_message(chat_id=chat_id, text="👇 Actions:", reply_markup=buttons)
            await asyncio.sleep(0.5)
        except Exception as e:
            print(f"❌ Send error {chat_id}: {str(e)[:80]}")
   
    tracker_status["total_alerts"] += 1
    tracker_status["last_alert"] = datetime.now().isoformat()
    print(f"✅ Alert {'buy' if spent > MIN_AMOUNT else 'sell'}: {woodeng_text} WOODENG → {len(ALL_CHAT_IDS)} chats")
# ... (Reste identique: get_recent_transactions, format_last_transactions, HTTP, commands, track_woodeng, main)
# (Omet pour brièveté, copie de la version précédente)
