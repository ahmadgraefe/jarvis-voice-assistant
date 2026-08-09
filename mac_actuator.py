"""
Jarvis — Mac-Actuator (Server-Migration, Hetzner)
Laeuft LOKAL auf dem Mac und stellt die vier Mac-gebundenen Faehigkeiten
(WhatsApp, App-Steuerung, Bildschirm-Klicks ueber Vision) per HTTP bereit,
damit der Hetzner-Server (das "Gehirn") sie ueber Tailscale fernsteuern
kann, ohne selbst osascript/pyautogui/ein echtes Display zu brauchen.

Bindet bewusst NUR an die eigene Tailscale-IP, nicht an 0.0.0.0 — nur
Geraete im selben Tailnet (der Hetzner-Server) koennen diesen Dienst
ueberhaupt erreichen, kein zusaetzliches Firewall-Regelwerk noetig.
"""

import base64
import json
import os
import re
import subprocess

import anthropic
from fastapi import FastAPI
from pydantic import BaseModel

import app_control
import whatsapp_tools
import screen_control
import claude_code_context
import claude_code_tool

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
with open(CONFIG_PATH) as f:
    config = json.load(f)

ai = anthropic.AsyncAnthropic(api_key=config["anthropic_api_key"])

app = FastAPI()


def _tailscale_ip() -> str:
    # Ueber die Tailscale-CLI abfragen scheitert zuverlaessig, wenn dieser
    # Prozess von launchd (statt einer interaktiven Sitzung) gestartet wird
    # ("The Tailscale GUI failed to start", IPC braucht die GUI-Session) —
    # stattdessen direkt die Netzwerk-Interfaces nach der Tailscale-eigenen
    # CGNAT-Adresse (100.64.0.0/10) durchsuchen, das ist unabhaengig davon.
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5)
        match = re.search(r'inet (100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+)', out.stdout)
        if match:
            return match.group(1)
    except Exception:
        pass
    return "127.0.0.1"  # Fallback: nur lokal erreichbar, falls Tailscale (noch) nicht laeuft


class AppOpenReq(BaseModel):
    app_name: str


@app.post("/app/open")
async def app_open(req: AppOpenReq):
    return {"result": await app_control.open_app(req.app_name)}


class WhatsappSendReq(BaseModel):
    recipient: str
    message: str


@app.post("/whatsapp/send")
async def whatsapp_send(req: WhatsappSendReq):
    return {"result": await app_control.send_whatsapp(req.recipient, req.message)}


class ResolveContactReq(BaseModel):
    name_or_phone: str


@app.post("/whatsapp/resolve_contact")
async def whatsapp_resolve_contact(req: ResolveContactReq):
    phone = await app_control.resolve_contact_phone(req.name_or_phone)
    return {"phone": phone, "error": app_control.last_contacts_error()}


@app.post("/whatsapp/check_new")
async def whatsapp_check_new():
    return {"result": await whatsapp_tools.check_new_messages(ai)}


class OpenChatReq(BaseModel):
    phone: str


@app.post("/whatsapp/open_chat_screenshot")
async def whatsapp_open_chat_screenshot(req: OpenChatReq):
    png = await whatsapp_tools.open_chat_and_screenshot(req.phone)
    return {"image_b64": base64.b64encode(png).decode()}


class SummarizeChatReq(BaseModel):
    image_b64: str
    contact_label: str


@app.post("/whatsapp/summarize_chat")
async def whatsapp_summarize_chat(req: SummarizeChatReq):
    png = base64.b64decode(req.image_b64)
    return {"result": await whatsapp_tools.summarize_chat(ai, png, req.contact_label)}


class SelfChatHistoryReq(BaseModel):
    frames: int = 3
    scroll_amount: int = 12


@app.post("/whatsapp/self_chat_history")
async def whatsapp_self_chat_history(req: SelfChatHistoryReq):
    own_phone = config.get("alert_phone", "")
    frames = await whatsapp_tools.capture_self_chat_history(own_phone, req.frames, req.scroll_amount)
    return {"images_b64": [base64.b64encode(f).decode() for f in frames]}


class ScreenClickReq(BaseModel):
    description: str
    action: str = "click"


@app.post("/screen/click")
async def screen_click(req: ScreenClickReq):
    return {"result": await screen_control.click_on(req.description, ai, req.action)}


class ScreenTypeReq(BaseModel):
    text: str


@app.post("/screen/type")
def screen_type(req: ScreenTypeReq):
    return {"result": screen_control.type_text(req.text)}


class ClaudeCodeContextReq(BaseModel):
    question: str = ""


@app.post("/claude-code/context")
def claude_code_context_endpoint(req: ClaudeCodeContextReq):
    # Bewusst SYNCHRON (kein async def) — claude_code_context.get_recent_context
    # ist selbst sync (reine Datei-I/O), FastAPI fuehrt sync-Handler automatisch
    # in einem Threadpool aus, blockiert also nicht den Event-Loop.
    return {"result": claude_code_context.get_recent_context(req.question)}


class ClaudeCodeExecReq(BaseModel):
    task: str
    timeout: int = 600


@app.post("/claude-code/exec")
async def claude_code_exec_endpoint(req: ClaudeCodeExecReq):
    result = await claude_code_tool.run_claude_code(req.task, req.timeout)
    return {"result": result}


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    host = _tailscale_ip()
    print(f"Mac-Actuator startet auf {host}:8420 (nur ueber Tailscale erreichbar)")
    uvicorn.run(app, host=host, port=8420)
