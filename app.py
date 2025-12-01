#!/usr/bin/env python3
"""
WOODENG Tracker – Version 24/7 INDESTRUCTIBLE
Tourne toutes les 5 secondes, ne s'arrête JAMAIS
Déploiement Render.com (Web Service)
"""

import os
import asyncio
import logging
import time
from datetime import datetime
from typing import Dict, List, Set

import requests
from fastapi import FastAPI, Request, Response
from telegram import Update, Bot, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from telegram.error import TimedOut, NetworkError

# ========================= CONFIG 24/7 =========================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("WOODENG-24/7")

# Variables critiques
HELIUS_RPC_URL = os.environ.get("HELIUS_RPC_URL", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_GROUP_IDS = [g.strip() for g in os.environ.get("TELEGRAM_GROUP_IDS", "").split(",") if g.strip()]
UPTIME_ROBOT_URL = os.environ.get("UPTIME_ROBOT_URL", "")  # Optionnel : https://uptimerobot.com

# 5 secondes forcées
TRACK_INTERVAL = 5
MAX_RETRIES = 5
RETRY_DELAY = 3

# Constants
WOODENG_PROGRAM_ID = "8YCde6Jm1Xz8FDiYS3R4AksgNVPEmrjNvkmdMnugEzrV"
WOODENG_MINT = "83zcTaQRqL1s3PxBRdGVkee9PiGLVP6JXg3oLVF6eAR5"
IPFS_GATEWAYS = [
    "https://gateway.pinata.cloud/ipfs",
    "https://ipfs.io/ipfs",
    "https://cloudflare-ipfs.com/ipfs",
    "https://dweb.link/ipfs",
]

# State persistant
sent_txs: Set[str] = set()
token_cache: Dict[str, Dict] = {}
last_transactions: List[Dict] = []
restart_count = 0

app = FastAPI(title="WOODENG Tracker 24/7")
bot = Bot(token=TELEGRAM_BOT_TOKEN)
application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

# ========================= ROBUSTE HELPERS =========================
def get_chat_ids() -> List[int]:
    ids = []
    if TELEGRAM_CHAT_ID and TELEGRAM_CHAT_ID.lstrip("-").isdigit():
        ids.append(int(TELEGRAM_CHAT_ID))
    for gid in TELEGRAM_GROUP_IDS:
        if gid.lstrip("-").isdigit():
            ids.append(int(gid))
    return ids

async def safe_request(url: str, json=None, timeout=15, retries=MAX_RETRIES):
    for i in range(retries):
        try:
            resp = requests.post(url, json=json, timeout=timeout) if json else requests.get(url, timeout=timeout)
            if resp.status_code in (200, 429, 500):
                return resp.json() if "application/json" in resp.headers.get("content-type", "") else resp.text
        except:
            if i == retries - 1:
                raise
            await asyncio.sleep(RETRY_DELAY * (i + 1))
    return None

def convert_ipfs(uri: str) -> str:
    return uri.replace("ipfs://", "https://gateway.pinata.cloud/ipfs/") if uri and uri.startswith("ipfs://") else uri

async def fetch_metadata(mint: str) -> dict:
   
