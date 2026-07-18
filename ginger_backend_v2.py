"""
GINGER HANDS-FREE AGENT (v2)
Built by Ginger, routes through OpenRouter
Shared conversation history across Telegram + iOS Shortcut
Deployed on Render with public HTTPS URL
"""

import os
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import JSONResponse
import httpx
import logging

# ============ SETUP ============
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# ============ ENV VARS ============
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
AGENT_SECRET_KEY = os.getenv("AGENT_SECRET_KEY", "change-me-in-production")

if not OPENROUTER_API_KEY or not TELEGRAM_BOT_TOKEN:
    raise ValueError("⚠️ Missing OPENROUTER_API_KEY or TELEGRAM_BOT_TOKEN")

OPENROUTER_MODEL = "anthropic/claude-haiku-4.5"

# ============ DATABASE ============
DB_PATH = "/tmp/ginger_conversations.db"

def init_db():
    """Create conversation history table if it doesn't exist."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            message TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def get_conversation_history(session_id: str, limit: int = 20):
    """Get last N messages for a session (across both Telegram + Shortcut)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT role, message FROM conversations
        WHERE session_id = ?
        ORDER BY id DESC
        LIMIT ?
    """, (session_id, limit))
    rows = cursor.fetchall()
    conn.close()
    
    history = [{"role": row[0], "content": row[1]} for row in reversed(rows)]
    return history

def save_message(session_id: str, role: str, message: str):
    """Save a message to conversation history."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO conversations (session_id, role, message, timestamp)
        VALUES (?, ?, ?, ?)
    """, (session_id, role, message, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

# ============ OPENROUTER API ============
async def call_openrouter(messages: list) -> str:
    """Call OpenRouter API with conversation history."""
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": "https://github.com/dominiccharland/ginger-hands-free",
        "X-Title": "Ginger Hands-Free Agent",
        "Content-Type": "application/json",
    }
    
    payload = {
        "model": OPENROUTER_MODEL,
        "max_tokens": 256,
        "temperature": 0.7,
        "messages": messages,
        "system": "You are Ginger, a warm and direct personal assistant. Keep responses concise (under 80 words). No markdown or formatting. Plain text only, suitable for reading aloud. Be helpful, anticipate needs, challenge flawed logic, and never guess. Respond in the same language the user speaks.",
    }
    
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
        )
        
        if response.status_code != 200:
            logger.error(f"OpenRouter API error: {response.status_code} {response.text}")
            raise HTTPException(status_code=500, detail="OpenRouter API error")
        
        data = response.json()
        return data["choices"][0]["message"]["content"]

# ============ AGENT ENDPOINT ============
@app.post("/agent")
async def agent_endpoint(
    request: dict,
x_agent_key: str = Header(None)
):
    """
    iOS Shortcut endpoint.
    Receives: {"session_id": "driving", "message": "..."}
    Returns: {"reply": "..."}
    """
    if x_agent_key != AGENT_SECRET_KEY:
        logger.warning(f"Invalid agent key attempt: {x_agent_key}")
        raise HTTPException(status_code=401, detail="Invalid agent key")
    
    session_id = request.get("session_id", "driving")
    user_message = request.get("message", "").strip()
    
    if not user_message:
        raise HTTPException(status_code=400, detail="Message required")
    
    history = get_conversation_history(session_id)
    
    save_message(session_id, "user", user_message)
    history.append({"role": "user", "content": user_message})
    
    try:
        reply = await call_openrouter(history)
    except Exception as e:
        logger.error(f"OpenRouter call failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to get response")
    
    save_message(session_id, "assistant", reply)
    
    logger.info(f"[{session_id}] User: {user_message[:50]}... Assistant: {reply[:50]}...")
    
    return JSONResponse({"reply": reply})

# ============ TELEGRAM WEBHOOK ============
@app.post("/telegram-webhook")
async def telegram_webhook(request: dict):
    """
    Telegram webhook.
    Receives updates from Telegram via setWebhook.
    """
    message = request.get("message")
    if not message:
        return JSONResponse({"status": "ok"})
    
    chat_id = message.get("chat", {}).get("id")
    user_message = message.get("text", "").strip()
    
    if not user_message or not chat_id:
        return JSONResponse({"status": "ok"})
    
    session_id = f"telegram_{chat_id}"
    
    history = get_conversation_history(session_id)
    
    save_message(session_id, "user", user_message)
    history.append({"role": "user", "content": user_message})
    
    try:
        reply = await call_openrouter(history)
    except Exception as e:
        logger.error(f"OpenRouter call failed: {e}")
        reply = "⚠️ Something went wrong. Try again."
    
    save_message(session_id, "assistant", reply)
    
    try:
        await send_telegram_message(chat_id, reply)
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")
    
    logger.info(f"[telegram_{chat_id}] User: {user_message[:50]}... Assistant: {reply[:50]}...")
    
    return JSONResponse({"status": "ok"})

async def send_telegram_message(chat_id: int, text: str):
    """Send a message to Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(url, json=payload)
        if response.status_code != 200:
            logger.error(f"Telegram sendMessage error: {response.text}")
            raise Exception("Failed to send Telegram message")

# ============ HEALTH CHECK ============
@app.get("/health")
async def health_check():
    """Health check for Render."""
    return JSONResponse({"status": "online"})

# ============ ROOT ============
@app.get("/")
async def root():
    """Root endpoint."""
    return JSONResponse({
        "service": "Ginger Hands-Free Agent (v2)",
        "status": "online",
        "model": OPENROUTER_MODEL,
        "endpoints": [
            "POST /agent (iOS Shortcut)",
            "POST /telegram-webhook (Telegram)",
            "GET /health"
        ]
    })

# ============ STARTUP ============
@app.on_event("startup")
async def startup():
    """Initialize database on startup."""
    init_db()
    logger.info("✅ Ginger backend (v2) started")
    logger.info(f"Model: {OPENROUTER_MODEL}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
