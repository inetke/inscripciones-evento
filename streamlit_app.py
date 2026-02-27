import os
import re
import io
import pandas as pd
import streamlit as st
from supabase import create_client, Client

st.set_page_config(page_title="Inscripciones Evento", page_icon="✅", layout="centered")

APP_TITLE = "Inscripción a actividades"
ADMIN_TITLE = "Panel admin"
PHONE_REGEX = r"^[0-9+() \-]{7,20}$"

def get_admin_password() -> str:
    if "admin" in st.secrets and "password" in st.secrets["admin"]:
        return st.secrets["admin"]["password"]
    return os.environ.get("ADMIN_PASSWORD", "")

def get_supabase() -> Client:
    if "supabase" not in st.secrets:
        st.error("Faltan secrets de Supabase.")
        st.stop()
    url = st.secrets["supabase"]["url"]
    key = st.secrets["supabase"]["service_role_key"]
    return create_client(url, key)

sb = get_supabase()

# ---- Data helpers (REST) ----
def fetch_event_dates():
    resp = sb.table("sessions").select("event_date").execute()
    dates = sorted({row["event_date"] for row in resp.data})
    return dates

def fetch_sessions(event_date):
    # Trae sesiones del día
    resp_s = sb.table("sessions").select("id,activity,start_time,end_time,capacity").eq("event_date", event_date).execute()
    sessions = resp_s.data

    # Trae bookings del día y cuenta por session_id
    resp_b = (
        sb.table("bookings")
        .select("session_id, sessions!inner(event_date)")
        .eq("sessions.event_date", event_date)
        .execute()
    )
    counts = {}
    for row in resp_b.data:
        sid = row["session_id"]
        counts[sid] = counts.get(sid, 0) + 1

    # Calcula remaining
    for s in sessions:
        booked = counts.get(s["id"], 0)
        s["booked"] = booked
        s["remaining"] = int(s["capacity"]) - int(booked)

    # Orden
    sessions.sort(key=lambda x: (x["activity"], x["start_time"]))
    return sessions

def create_booking_atomic(session_id, full_name, phone, email):
    # Llamada RPC a la función SQL (atómica)
    payload = {
        "p_session_id": int(session_id),
        "p_full_name": full_name,
        "p_phone": phone,
        "p_email": email,
    }
    resp = sb.rpc("book_session", payload).execute()
    if not resp.data:
        return False, "Error inesperado en la reserva."
    return bool(resp.data["ok"]), resp.data["message"]

def fetch_bookings(event_date):
    resp = (
        sb.table("bookings")
        .select("full_name,phone,email,created_at, sessions!inner(event_date,activity,start_time,end_time)")
        .eq("sessions.event_date", event_date)
        .execute()
    )
    rows = []
    for r in resp.data:
        s = r["sessions"]
        rows.append({
            "event_date": s["event_date"],
            "activity": s["activity"],
            "start_time": s["start_time"],
            "end_time": s["end_time"],
            "full_name": r["full_name"],
            "phone": r["phone"],
            "email": r["email"],
            "created_at": r["created_at"],
        })
    rows.sort(key=lambda x: (x["activity"], x["start_time"], x["created_at"]))
    return rows

# ---- UI ----
st.title(APP_TITLE)

dates = fetch_event_dates()
if not dates:
    st.warning("No hay sesiones cargadas en la base de datos todavía.")
    st.stop()

event_date = st.selectbox("Fecha del evento", options=dates)

sessions = fetch_sessions(event_date)
activities = sorted(list({s["activity"] for s in sessions}))
activity = st.selectbox("Actividad", options=activities)

filtered = [s for s in sessions if s["activity"] == activity]

st.subheader("Horarios disponibles")
options = []
option_map = {}
for s in filtered:
    label = f"{str(s['start_time'])[:5]} - {str(s['end_time'])[:5]}  ·  Plazas: {s['remaining']}/{s['capacity']}"
    options.append(label)
    option_map[label] = s

selected_label = st.radio("Elige tu franja", options=options)
selected_session = option_map[selected_label]

st.divider()
st.subheader("Tus datos")

with st.form("booking_form", clear_on_submit=True):
    full_name = st.text_input("Nombre y Apellido", max_chars=80)
    phone = st.text_input("Móvil", max_chars=20, help="Ej: +34 600 123 456")
    email = st.text_input("Email", max_chars=120)
    consent = st.checkbox("Acepto que se usen mis datos solo para gestionar esta inscripción.")
    submit = st.form_submit_button("Reservar plaza ✅", use_container_width=True)

if submit:
    if selected_session["remaining"] <= 0:
        st.error("Esa sesión ya está llena. Elige otra franja.")
        st.stop()
    if not full_name.strip():
        st.error("Falta Nombre y Apellido.")
        st.stop()
    if not re.match(PHONE_REGEX, phone.strip()):
        st.error("Móvil inválido. Revisa el formato.")
        st.stop()
    if "@" not in email or "." not in email:
        st.error("Email inválido.")
        st.stop()
    if not consent:
        st.error("Necesitas aceptar el uso de datos para inscribirte.")
        st.stop()

    ok, msg = create_booking_atomic(selected_session["id"], full_name.strip(), phone.strip(), email.strip())
    if ok:
        st.success(msg)
        st.info(f"✅ {activity} · {str(selected_session['start_time'])[:5]}-{str(selected_session['end_time'])[:5]} · {event_date}")
        st.rerun()
    else:
        st.error(msg)

st.divider()

with st.expander(ADMIN_TITLE):
    admin_pw = st.text_input("Contraseña admin", type="password")
    if admin_pw and admin_pw == get_admin_password():
        st.success("Acceso concedido.")
        rows = fetch_bookings(event_date)
        df = pd.DataFrame(rows)

        if df.empty:
            st.write("Aún no hay inscripciones para esta fecha.")
        else:
            st.dataframe(df, use_container_width=True)
            csv_buf = io.StringIO()
            df.to_csv(csv_buf, index=False)
            st.download_button(
                "Descargar CSV",
                data=csv_buf.getvalue().encode("utf-8"),
                file_name=f"inscritos_{event_date}.csv",
                mime="text/csv",
                use_container_width=True,
            )
    elif admin_pw:
        st.error("Contraseña incorrecta.")