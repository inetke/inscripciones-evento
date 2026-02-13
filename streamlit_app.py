import os
import re
import io
import pandas as pd
import streamlit as st
import psycopg2
from psycopg2.extras import RealDictCursor

st.set_page_config(page_title="Inscripciones Evento", page_icon="✅", layout="centered")

# ---------- Config ----------
APP_TITLE = "Inscripción a actividades"
ADMIN_TITLE = "Panel admin"
PHONE_REGEX = r"^[0-9+() \-]{7,20}$"

# Secrets expected:
# st.secrets["db"]["url"]  -> Postgres connection string
# st.secrets["admin"]["password"] -> admin password
def get_db_url() -> str:
    # Prefer Streamlit secrets, fallback to env var
    if "db" in st.secrets and "url" in st.secrets["db"]:
        return st.secrets["db"]["url"]
    return os.environ.get("DATABASE_URL", "")

def get_admin_password() -> str:
    if "admin" in st.secrets and "password" in st.secrets["admin"]:
        return st.secrets["admin"]["password"]
    return os.environ.get("ADMIN_PASSWORD", "")

def connect():
    db_url = get_db_url()
    if not db_url:
        st.error("Falta configurar la conexión a la base de datos (DATABASE_URL / secrets).")
        st.stop()
    return psycopg2.connect(db_url)

# ---------- DB ops ----------
def fetch_event_dates():
    with connect() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("select distinct event_date from public.sessions order by event_date;")
        rows = cur.fetchall()
    return [r["event_date"] for r in rows]

def fetch_sessions(event_date):
    sql = """
    select
      s.id,
      s.activity,
      s.start_time,
      s.end_time,
      s.capacity,
      count(b.id) as booked
    from public.sessions s
    left join public.bookings b on b.session_id = s.id
    where s.event_date = %s
    group by s.id
    order by s.activity, s.start_time;
    """
    with connect() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, (event_date,))
        rows = cur.fetchall()
    for r in rows:
        r["remaining"] = int(r["capacity"]) - int(r["booked"])
    return rows

def create_booking_atomic(session_id, full_name, phone, email):
    """
    Atomic booking:
    - lock the session row
    - check current booked count
    - insert booking if remaining > 0
    """
    with connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Lock session row to avoid race conditions
            cur.execute("select id, capacity from public.sessions where id=%s for update;", (session_id,))
            srow = cur.fetchone()
            if not srow:
                return False, "Sesión no encontrada."

            cur.execute("select count(*)::int as booked from public.bookings where session_id=%s;", (session_id,))
            booked = cur.fetchone()["booked"]
            capacity = int(srow["capacity"])
            remaining = capacity - booked

            if remaining <= 0:
                conn.rollback()
                return False, "Lo sentimos: esa sesión se acaba de llenar."

            cur.execute(
                """
                insert into public.bookings (session_id, full_name, phone, email)
                values (%s, %s, %s, %s)
                returning id;
                """,
                (session_id, full_name, phone, email),
            )
            _ = cur.fetchone()["id"]
        conn.commit()
    return True, "¡Reserva confirmada!"

def fetch_bookings(event_date):
    sql = """
    select
      s.event_date,
      s.activity,
      s.start_time,
      s.end_time,
      b.full_name,
      b.phone,
      b.email,
      b.created_at
    from public.bookings b
    join public.sessions s on s.id = b.session_id
    where s.event_date = %s
    order by s.activity, s.start_time, b.created_at;
    """
    with connect() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, (event_date,))
        return cur.fetchall()

# ---------- UI ----------
st.title(APP_TITLE)

dates = fetch_event_dates()
if not dates:
    st.warning("No hay sesiones cargadas en la base de datos todavía. (Revisa el SQL de inserción).")
    st.stop()

event_date = st.selectbox("Fecha del evento", options=dates, format_func=lambda d: d.strftime("%Y-%m-%d"))

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
    # Basic validation
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

    ok, msg = create_booking_atomic(
        selected_session["id"],
        full_name.strip(),
        phone.strip(),
        email.strip().lower(),
    )
    if ok:
        st.success(msg)
        st.info(f"✅ {activity} · {str(selected_session['start_time'])[:5]}-{str(selected_session['end_time'])[:5]} · {event_date.strftime('%Y-%m-%d')}")
        st.rerun()
    else:
        st.error(msg)

st.divider()

# ---------- Admin ----------
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

            # CSV download
            csv_buf = io.StringIO()
            df.to_csv(csv_buf, index=False)
            st.download_button(
                "Descargar CSV",
                data=csv_buf.getvalue().encode("utf-8"),
                file_name=f"inscritos_{event_date.strftime('%Y-%m-%d')}.csv",
                mime="text/csv",
                use_container_width=True,
            )

        st.caption("Tip: si quieres ocultar el panel admin, puedes quitar este expander.")
    elif admin_pw:
        st.error("Contraseña incorrecta.")