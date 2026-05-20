import os
import json
from datetime import datetime, timedelta
from typing import Optional
from pydantic import BaseModel

# Componentes esenciales de FastAPI
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse

# Librerías externas
from supabase import create_client, Client
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from openai import OpenAI

app = FastAPI(title="CRM Bot Core con IA")

# 1. Clientes de APIs y Base de Datos BLINDADO contra apagones
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()

supabase: Optional[Client] = None

# Si las credenciales existen, intentamos conectar sin apagar el servidor si fallan
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("Conexión inicial con Supabase establecida.")
    except Exception as e:
        print(f"⚠️ Alerta Supabase: No se pudo conectar de entrada ({e}). El servidor seguirá activo.")

openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", "mock-key"))

# 2. Calendario de Google
CALENDAR_ID = os.environ.get("BOT_EMAIL_CALENDAR", "")
calendar_service = None
if os.path.exists('credentials.json'):
    try:
        SCOPES = ['https://www.googleapis.com/auth/calendar']
        creds = Credentials.from_service_account_file('credentials.json', scopes=SCOPES)
        calendar_service = build('calendar', 'v3', credentials=creds)
    except Exception as e:
        print(f"Error al iniciar calendario: {e}")

# --- MODELOS DE DATOS ---
class InformacionTurnoExtraida(BaseModel):
    servicio_id: Optional[int] = None
    fecha_iso: Optional[str] = None
    hora_iso: Optional[str] = None
    intencion_clara: bool

class MensajeCliente(BaseModel):
    nombre_cliente: str
    telefono: str
    mensaje: str

# Endpoint de inicio básico para chequear que esté vivo el servidor
@app.get("/")
def inicio():
    return {"status": "online", "mensaje": "Servidor del CRM blindado y funcionando correctamente."}

# --- ENDPOINT DE VERIFICACIÓN PARA META (GET) ---
@app.get("/chat")
def verificar_webhook_meta(request: Request, hub_mode: Optional[str] = None, hub_challenge: Optional[str] = None, hub_verify_token: Optional[str] = None):
    TOKEN_VERIFICACION_LOCAL = "MI_BOT_SECRETO_2026"
    
    params = request.query_params
    mode = hub_mode or params.get("hub.mode")
    token = hub_verify_token or params.get("hub.verify_token")
    challenge = hub_challenge or params.get("hub.challenge")

    if mode == "subscribe" and token == TOKEN_VERIFICACION_LOCAL:
        return PlainTextResponse(content=str(challenge))
    return Response(content="Token de verificación inválido", status_code=403)

# --- ENDPOINT PRINCIPAL DE CHAT CON IA (POST) ---
@app.post("/chat")
async def procesar_chat_inteligente(data: MensajeCliente):
    if not supabase:
        return {"respuesta": "Lo siento, el servicio de base de datos no está disponible temporalmente."}
        
    fecha_hoy = datetime.now().strftime("%Y-%m-%d")
    dia_semana_hoy = datetime.now().strftime("%A")

    system_prompt = (
        f"Sos el procesador analítico de turnos de un salón de belleza. Hoy es {dia_semana_hoy}, fecha: {fecha_hoy}.\n"
        "Tu única tarea es analizar el mensaje del usuario y extraer las entidades en formato estructurado."
    )

    try:
        completion = openai_client.beta.chat.completions.parse(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": data.mensaje}
            ],
            response_format=InformacionTurnoExtraida,
        )
        return {"respuesta": "Mensaje procesado correctamente"}
    except Exception as e:
        return {"respuesta": "Hubo un pequeño inconveniente al procesar tu mensaje."}
