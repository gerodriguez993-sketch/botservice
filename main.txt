import os
import json
from datetime import datetime, timedelta
from typing import Optional

# Componentes esenciales de FastAPI
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse

# Librerías externas
from supabase import create_client, Client
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from openai import OpenAI

app = FastAPI(title="CRM Bot Core con IA")

# 1. Clientes de APIs y Base de Datos con verificación de seguridad
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", "mock-key"))

# 2. Calendario de Google (Opción B)
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
    return {"status": "online", "mensaje": "Servidor del CRM funcionando correctamente."}

# --- FUNCIONES DE ASISTENCIA ---
def verificar_y_reservar_logica(nombre, telefono, servicio_id, fecha_iso, hora_iso):
    if not calendar_service:
        return {"status": "error", "respuesta_chat": "Error de configuración en el calendario del servidor."}
        
    fecha_inicio_str = f"{fecha_iso}T{hora_iso}:00"
    dt_inicio = datetime.strptime(fecha_inicio_str, "%Y-%m-%dT%H:%M:%S")
    dt_fin = dt_inicio + timedelta(hours=1)
    
    events_result = calendar_service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=dt_inicio.isoformat() + "Z",
        timeMax=dt_fin.isoformat() + "Z",
        singleEvents=True
    ).execute()
    
    if len(events_result.get('items', [])) > 0:
        return {"status": "ocupado", "respuesta_chat": "Ufa, ese horario se acaba de ocupar. ¿Te queda bien otra hora o el día siguiente?"}

    cliente_query = supabase.table("clientes").select("id").eq("telefono", telefono).execute()
    cliente_id = cliente_query.data[0]["id"] if len(cliente_query.data) > 0 else supabase.table("clientes").insert({"nombre": nombre, "telefono": telefono}).execute().data[0]["id"]

    event_body = {
        'summary': f'⏳ SEÑA PENDIENTE - {nombre}',
        'description': f'Servicio: {servicio_id}',
        'start': {'dateTime': dt_inicio.isoformat(), 'timeZone': 'America/Argentina/Buenos_Aires'},
        'end': {'dateTime': dt_fin.isoformat(), 'timeZone': 'America/Argentina/Buenos_Aires'},
    }
    event = calendar_service.events().insert(calendarId=CALENDAR_ID, body=event_body).execute()

    supabase.table("turnos").insert({
        "cliente_id": cliente_id,
        "servicio_id": servicio_id,
        "fecha_hora_inicio": dt_inicio.isoformat(),
        "estado": "pendiente_pago",
        "google_event_id": event.get('id')
    }).execute()

    srv = supabase.table("servicios").select("monto_senia").eq("id", servicio_id).execute().data[0]

    return {
        "status": "exito",
        "respuesta_chat": f"¡Espectacular {nombre}! Ya te bloqueé el turno para el {fecha_iso} a las {hora_iso} hs.\n\n"
                          f"Para confirmarlo, necesitamos una seña de ${srv['monto_senia']} mediante transferencia bancaria:\n"
                          f"🏦 ALIAS: local.beauty.reserva\n"
                          f"Enviame el comprobante por acá mismo una vez hecho el pago para darte la confirmación final. ¡Muchas gracias!"
    }

# --- ENDPOINT DE VERIFICACIÓN PARA META (GET) ---
@app.get("/chat")
def verificar_webhook_meta(request: Request, hub_mode: Optional[str] = None, hub_challenge: Optional[str] = None, hub_verify_token: Optional[str] = None):
    TOKEN_VERIFICACION_LOCAL = "MI_BOT_SECRETO_2026"
    
    # Meta a veces manda los datos como parámetros de la URL directamente, los atrapamos acá:
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
    fecha_hoy = datetime.now().strftime("%Y-%m-%d")
    dia_semana_hoy = datetime.now().strftime("%A")

    system_prompt = (
        f"Sos el procesador analítico de turnos de un salón de belleza. Hoy es {dia_semana_hoy}, fecha: {fecha_hoy}.\n"
        "Tu única tarea es analizar el mensaje del usuario y extraer las siguientes entidades en el formato estructurado:\n"
        "- servicio_id: Poné 1 si pide corte/barba, 2 si pide cejas, 3 si pide tratamiento capilar. Si no especifica, poné null.\n"
        "- fecha_iso: La fecha del turno calculada en formato YYYY-MM-DD. Si dice 'mañana', calculala sumando un día a hoy.\n"
        "- hora_iso: La hora del turno en formato HH:MM.\n"
        "- intencion_clara: True solo si el usuario está confirmando o solicitando explícitamente agendar un espacio."
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
        
        datos_extraidos = completion.choices[0].message.parsed

        if (datos_extraidos.intencion_clara and 
            datos_extraidos.servicio_id and 
            datos_extraidos.fecha_iso and 
            datos_extraidos.hora_iso):
            
            resultado = verificar_y_reservar_logica(
                nombre=data.nombre_cliente,
                telefono=data.telefono,
                servicio_id=datos_extraidos.servicio_id,
                fecha_iso=datos_extraidos.fecha_iso,
                hora_iso=datos_extraidos.hora_iso
            )
            return {"respuesta": resultado["respuesta_chat"]}
        
    except Exception as e:
        pass

    # Si falla la extracción o faltan datos, responde la recepcionista amigable
    conversacion_amigable = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Sos la recepcionista amable de un salón de belleza. El cliente te está hablando para pedir un turno, pero te faltan datos (necesitás saber el servicio, qué día y qué horario quiere). Saludalo si es el primer mensaje y repreguntale lo que falta de forma muy natural, cortita y con emojis."},
            {"role": "user", "content": data.mensaje}
        ]
    )
    return {"respuesta": conversacion_amigable.choices[0].message.content}
