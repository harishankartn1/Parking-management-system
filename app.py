from datetime import datetime, time, timedelta
import mysql.connector
from mysql.connector import Error
import pandas as pd
import streamlit as st
import os
from dotenv import load_dotenv

# ----------------------------------------------------------------------------
# PAGE CONFIG & SESSION STATE
# ----------------------------------------------------------------------------
st.set_page_config(page_title="Amrita Parking System", layout="wide")

if "theme" not in st.session_state:
    st.session_state.theme = "dark"
if "auth_view" not in st.session_state:
    st.session_state.auth_view = "login"
if "user" not in st.session_state:
    st.session_state.user = None
if "role_type" not in st.session_state:
    st.session_state.role_type = None


def toggle_theme():
    st.session_state.theme = "light" if st.session_state.theme == "dark" else "dark"


def sign_out():
    st.session_state.user = None
    st.session_state.role_type = None
    st.session_state.auth_view = "login"
    for k in ("login_user", "login_pass"):
        if k in st.session_state:
            del st.session_state[k]


# ----------------------------------------------------------------------------
# DATABASE CONFIG & HELPERS
# ----------------------------------------------------------------------------

load_dotenv()

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
}
DB_NAME = os.getenv("DB_NAME", "project")


ZONES = ["Staff", "Student", "Visitor"]
SIZE_RANK = {"Small": 1, "Medium": 2, "Large": 3}
VEHICLE_SIZE = {"Bike": "Small", "Car": "Medium", "SUV": "Large"}

PASS_RATES = {"Hourly": 20, "Daily": 100, "Monthly": 1500, "Semester": 6000}
SHORT_TERM = ("Hourly", "Daily")
UNITS = {"Hourly": "hour", "Daily": "day", "Monthly": "month", "Semester": "semester"}

REQUIRED_TABLES = {
    "admins",
    "users",
    "vehicles",
    "parking_slots",
    "parking_passes",
    "violations",
}


def get_conn(use_project_db=True):
    conn = mysql.connector.connect(**DB_CONFIG)
    conn.autocommit = True
    if use_project_db:
        cur = conn.cursor()
        cur.execute(f"USE `{DB_NAME}`")
        cur.close()
    return conn


def ensure_schema_migrations(cur):
    """Upgrades existing tables safely to support Visitor zone, Cancelled status, and scheduled timestamps."""
    cur.execute("SHOW COLUMNS FROM users LIKE 'role'")
    col_info = cur.fetchone()
    if col_info and "Visitor" not in col_info[1]:
        cur.execute("ALTER TABLE users MODIFY COLUMN role ENUM('Staff','Student','Visitor') NOT NULL")

    cur.execute("SHOW COLUMNS FROM parking_slots LIKE 'zone'")
    slot_info = cur.fetchone()
    if slot_info and "Visitor" not in slot_info[1]:
        cur.execute("ALTER TABLE parking_slots MODIFY COLUMN zone ENUM('Staff','Student','Visitor') NOT NULL")
        cur.execute("SELECT COUNT(*) FROM parking_slots WHERE zone = 'Visitor'")
        if cur.fetchone()[0] == 0:
            layout = [("Small", 3), ("Medium", 4), ("Large", 3)]
            n = 1
            for size, count in layout:
                for _ in range(count):
                    cur.execute(
                        "INSERT INTO parking_slots (slot_code, zone, size) VALUES (%s, 'Visitor', %s)",
                        (f"V{n:02d}", size),
                    )
                    n += 1

    cur.execute("SHOW COLUMNS FROM parking_passes LIKE 'status'")
    p_status = cur.fetchone()
    if p_status and "Cancelled" not in p_status[1]:
        cur.execute(
            "ALTER TABLE parking_passes MODIFY COLUMN status ENUM('Pending','Approved','Rejected','Closed','Cancelled') NOT NULL DEFAULT 'Pending'"
        )

    cur.execute("SHOW COLUMNS FROM parking_passes LIKE 'start_time'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE parking_passes ADD COLUMN start_time DATETIME NULL, ADD COLUMN end_time DATETIME NULL")


def check_db_ready():
    try:
        conn = get_conn(use_project_db=False)
        cur = conn.cursor()
        cur.execute("SHOW DATABASES LIKE %s", (DB_NAME,))
        if cur.fetchone() is None:
            cur.close()
            conn.close()
            return False, f"Database '{DB_NAME}' not found. Run the CLI script with 'super' first to initialize."
        cur.execute(f"USE `{DB_NAME}`")
        cur.execute("SHOW TABLES")
        tables = {r[0] for r in cur.fetchall()}
        missing = REQUIRED_TABLES - tables
        if missing:
            cur.close()
            conn.close()
            return False, f"Missing tables: {', '.join(missing)}. Run the CLI script with 'super' to create all tables."

        ensure_schema_migrations(cur)

        cur.close()
        conn.close()
        return True, ""
    except Error as e:
        return False, f"MySQL Connection Error: {e}"


def fetch_df(sql, params=None):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(sql, params or ())
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
        return pd.DataFrame(rows, columns=cols)
    finally:
        cur.close()
        conn.close()


def fetch_rows(sql, params=None):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(sql, params or ())
        return cur.fetchall()
    finally:
        cur.close()
        conn.close()


def execute_sql(sql, params=None):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(sql, params or ())
        return cur.lastrowid
    finally:
        cur.close()
        conn.close()


def execute_transaction(statements):
    """ACID Transaction wrapper to guarantee atomicity across multi-table writes."""
    conn = get_conn()
    conn.autocommit = False
    cur = conn.cursor()
    try:
        conn.start_transaction()
        for sql, params in statements:
            cur.execute(sql, params or ())
        conn.commit()
        return True, "Transaction committed successfully."
    except Error as e:
        conn.rollback()
        return False, f"Transaction failed and rolled back: {e}"
    finally:
        cur.close()
        conn.close()


def pass_duration(pass_type, qty):
    return {
        "Hourly": timedelta(hours=qty),
        "Daily": timedelta(days=qty),
        "Monthly": timedelta(days=30 * qty),
        "Semester": timedelta(days=180 * qty),
    }[pass_type]


def find_available_slot(zone, vtype, start_dt, end_dt, exclude_pass_id=None):
    """Finds a compatible slot in the user's zone with zero overlapping approved bookings."""
    sql = """
        SELECT s.slot_id, s.slot_code
        FROM parking_slots s
        WHERE s.zone = %s
          AND s.status = 'Free'
          AND FIELD(s.size, 'Small', 'Medium', 'Large') >= %s
          AND NOT EXISTS (
              SELECT 1 FROM parking_passes p
              WHERE p.slot_id = s.slot_id
                AND p.status = 'Approved'
                AND p.pass_id <> %s
                AND p.start_time < %s
                AND p.end_time > %s
          )
        ORDER BY s.slot_code
        LIMIT 1
    """
    rows = fetch_rows(
        sql,
        (zone, SIZE_RANK[VEHICLE_SIZE[vtype]], exclude_pass_id or -1, end_dt, start_dt),
    )
    return rows[0] if rows else None


# ----------------------------------------------------------------------------
# YOUTUBE-THEMED CSS
# ----------------------------------------------------------------------------
def inject_custom_css():
    if st.session_state.theme == "dark":
        bg_color = "#0F0F0F"
        card_bg = "#181818"
        text_color = "#F1F1F1"
        subtext_color = "#AAAAAA"
        red_accent = "#FF0000"
        red_hover = "#CC0000"
        btn_secondary_bg = "#272727"
        btn_secondary_hover = "#3F3F3F"
        input_bg = "#121212"
        border_color = "#3F3F3F"
    else:
        bg_color = "#FFFFFF"
        card_bg = "#F9F9F9"
        text_color = "#0F0F0F"
        subtext_color = "#606060"
        red_accent = "#FF0000"
        red_hover = "#CC0000"
        btn_secondary_bg = "#F2F2F2"
        btn_secondary_hover = "#E5E5E5"
        input_bg = "#FFFFFF"
        border_color = "#CCCCCC"

    custom_css = f"""
    <style>
    .stApp {{
        background-color: {bg_color};
        color: {text_color};
    }}
    h1, h2, h3, h4, p, span, label, div {{
        color: {text_color} !important;
    }}
    header {{visibility: hidden;}}

    /* Hide 'Press Enter to apply' & ALL Heading Anchor Link Icons */
    div[data-testid="InputInstructions"],
    [data-testid="stHeaderActionElements"],
    a.anchor-link {{
        display: none !important;
        visibility: hidden !important;
    }}

    div[data-testid="stForm"] {{
        border: none !important;
        padding: 0 !important;
    }}

    /* Top Right Theme Toggle Button */
    div[data-testid="stHorizontalBlock"]:first-of-type div[data-testid="column"]:last-child button {{
        background-color: transparent !important;
        border: none !important;
        box-shadow: none !important;
        font-size: 24px !important;
        padding: 0 !important;
    }}
    div[data-testid="stHorizontalBlock"]:first-of-type div[data-testid="column"]:last-child button:hover {{
        background-color: transparent !important;
        transform: scale(1.15);
    }}

    /* Primary Button (YouTube Red Pill) */
    button[kind="primary"],
    div[data-testid="stFormSubmitButton"] button {{
        background-color: {red_accent} !important;
        border: none !important;
        border-radius: 18px !important;
        color: #FFFFFF !important;
        font-weight: 500 !important;
        padding: 8px 20px !important;
        transition: background-color 0.2s ease !important;
    }}
    button[kind="primary"]:hover,
    div[data-testid="stFormSubmitButton"] button:hover {{
        background-color: {red_hover} !important;
        color: #FFFFFF !important;
    }}

    /* Secondary Button (YouTube Gray Pill) */
    button[kind="secondary"] {{
        background-color: {btn_secondary_bg} !important;
        border: none !important;
        border-radius: 18px !important;
        color: {text_color} !important;
        font-weight: 500 !important;
        padding: 8px 20px !important;
        transition: background-color 0.2s ease !important;
    }}
    button[kind="secondary"]:hover {{
        background-color: {btn_secondary_hover} !important;
        color: {text_color} !important;
    }}

    /* Text, Number, Date & Time Inputs */
    div[data-testid="stTextInput"] > div > div,
    div[data-testid="stNumberInput"] > div > div,
    div[data-testid="stDateInput"] > div > div,
    div[data-testid="stTimeInput"] > div > div {{
        background-color: {input_bg} !important;
        border: 1px solid {border_color} !important;
        border-radius: 8px !important;
    }}
    div[data-testid="stTextInput"] input,
    div[data-testid="stNumberInput"] input,
    div[data-testid="stDateInput"] input,
    div[data-testid="stTimeInput"] input {{
        background-color: transparent !important;
        color: {text_color} !important;
        -webkit-text-fill-color: {text_color} !important;
    }}
    div[data-testid="stTextInput"] svg,
    div[data-testid="stDateInput"] svg,
    div[data-testid="stTimeInput"] svg {{
        fill: {text_color} !important;
        color: {text_color} !important;
    }}
    div[data-testid="stTextInput"] > div > div:focus-within,
    div[data-testid="stNumberInput"] > div > div:focus-within,
    div[data-testid="stDateInput"] > div > div:focus-within,
    div[data-testid="stTimeInput"] > div > div:focus-within {{
        border: 1px solid {text_color} !important;
    }}

    /* Selectbox styling */
    div[data-testid="stSelectbox"] > div > div {{
        background-color: {input_bg} !important;
        border: 1px solid {border_color} !important;
        border-radius: 8px !important;
        color: {text_color} !important;
    }}

    /* Clean Static Table Styling (st.table) */
    table {{
        width: 100% !important;
        border-collapse: collapse !important;
        background-color: {card_bg} !important;
        color: {text_color} !important;
        border: 1px solid {border_color} !important;
    }}
    th {{
        background-color: {btn_secondary_bg} !important;
        color: {text_color} !important;
        border-bottom: 1px solid {border_color} !important;
        text-align: left !important;
        padding: 10px !important;
    }}
    td {{
        border-bottom: 1px solid {border_color} !important;
        color: {text_color} !important;
        padding: 10px !important;
    }}

    /* Tabs styling */
    button[data-baseweb="tab"] {{
        background-color: transparent !important;
        color: {subtext_color} !important;
        font-weight: 600 !important;
    }}
    button[data-baseweb="tab"][aria-selected="true"] {{
        color: {text_color} !important;
        border-bottom-color: {red_accent} !important;
    }}

    /* Metric cards */
    div[data-testid="stMetric"] {{
        background-color: {card_bg};
        border: 1px solid {border_color};
        padding: 15px;
        border-radius: 12px;
    }}

    hr {{
        border-bottom-color: {border_color} !important;
    }}
    </style>
    """
    st.markdown(custom_css, unsafe_allow_html=True)
    return text_color


current_text_color = inject_custom_css()


def render_clean_table(df):
    """Renders an auto-sized, non-interactive table without toolbars or pinned columns."""
    if df.empty:
        st.info("No records found.")
    else:
        st.table(df)


# ----------------------------------------------------------------------------
# TOP BAR (Header & Theme Switch)
# ----------------------------------------------------------------------------
top_left, top_mid, top_right = st.columns([2, 7, 1])
with top_left:
    if st.session_state.user is not None:
        if st.button("Sign out", type="secondary", on_click=sign_out):
            st.rerun()
with top_right:
    emoji = "☀️" if st.session_state.theme == "dark" else "🌙"
    st.button(emoji, on_click=toggle_theme)

st.markdown(
    f"<h1 style='text-align: center; color: {current_text_color}; margin-top: -15px;'>Amrita Parking System</h1>",
    unsafe_allow_html=True,
)
st.markdown("---")

# Verify DB connection & auto-migrate schema before rendering views
db_ok, db_msg = check_db_ready()
if not db_ok:
    st.error(db_msg)
    st.stop()


# ----------------------------------------------------------------------------
# SHARED QUERIES
# ----------------------------------------------------------------------------
PASS_SELECT_SQL = """
    SELECT p.pass_id AS 'Pass ID', u.username AS 'Username', u.role AS 'Zone',
           v.plate_no AS 'Plate No', v.vehicle_type AS 'Vehicle',
           COALESCE(s.slot_code, 'Pending Assignment') AS 'Slot',
           p.pass_type AS 'Pass Type', p.quantity AS 'Qty', p.fee AS 'Fee (Rs)',
           p.status AS 'Status', p.start_time AS 'Start Time', p.end_time AS 'End Time',
           IF(p.violation_flag, 'YES', 'No') AS 'Flagged'
    FROM parking_passes p
    JOIN users u ON p.user_id = u.user_id
    JOIN vehicles v ON p.vehicle_id = v.vehicle_id
    LEFT JOIN parking_slots s ON p.slot_id = s.slot_id
"""


# ----------------------------------------------------------------------------
# VIEW 1: AUTHENTICATION (LOGIN & SIGN UP)
# ----------------------------------------------------------------------------
def render_auth():
    _, col_main, _ = st.columns([1, 1.3, 1])

    with col_main:
        if st.session_state.auth_view == "login":
            st.markdown("<h3 style='text-align: center;'>Sign in</h3>", unsafe_allow_html=True)
            st.markdown(
                "<p style='text-align: center; margin-bottom: 20px;'>Use your Amrita Account</p>",
                unsafe_allow_html=True,
            )

            with st.form("login_form", clear_on_submit=False):
                account_type = st.selectbox(
                    "Account Type",
                    ["User (Staff / Student / Visitor)", "Administrator"],
                    key="login_role",
                )
                username = st.text_input("Username", key="login_user")
                password = st.text_input("Password", type="password", key="login_pass")

                st.write("")
                submitted = st.form_submit_button("Next", type="primary", use_container_width=True)

            if submitted:
                u_clean = username.strip()
                p_clean = password.strip()
                if not u_clean or not p_clean:
                    st.error("Please enter both username and password.")
                elif account_type == "Administrator":
                    rows = fetch_rows(
                        "SELECT admin_id, username, full_name FROM admins WHERE username = %s AND password = %s",
                        (u_clean, p_clean),
                    )
                    if rows:
                        st.session_state.user = {
                            "admin_id": rows[0][0],
                            "username": rows[0][1],
                            "full_name": rows[0][2],
                        }
                        st.session_state.role_type = "admin"
                        st.rerun()
                    else:
                        st.error("Invalid username or password.")
                else:
                    rows = fetch_rows(
                        "SELECT user_id, username, full_name, role FROM users WHERE username = %s AND password = %s",
                        (u_clean, p_clean),
                    )
                    if rows:
                        st.session_state.user = {
                            "user_id": rows[0][0],
                            "username": rows[0][1],
                            "full_name": rows[0][2],
                            "role": rows[0][3],
                        }
                        st.session_state.role_type = "user"
                        st.rerun()
                    else:
                        st.error("Invalid username or password.")

            if st.button("Create account", type="secondary", use_container_width=True):
                st.session_state.auth_view = "signup"
                st.rerun()

        else:
            st.markdown("<h3 style='text-align: center;'>Create your Account</h3>", unsafe_allow_html=True)
            st.markdown(
                "<p style='text-align: center; margin-bottom: 20px;'>Your parking zone is automatically assigned by your role</p>",
                unsafe_allow_html=True,
            )

            with st.form("signup_form"):
                new_user = st.text_input("Choose Username *")
                new_pass = st.text_input("Choose Password *", type="password")
                full_name = st.text_input("Full Name *")
                role = st.selectbox("User Category *", ZONES)
                phone = st.text_input("Phone Number (Optional)")

                st.markdown("---")
                st.markdown("**Register your vehicle (can be done later also)**")
                plate_no = st.text_input("Vehicle Plate Number (e.g., KA01AB1234)")
                v_type = st.selectbox("Vehicle Type", list(VEHICLE_SIZE.keys()))

                st.write("")
                reg_submitted = st.form_submit_button("Register", type="primary", use_container_width=True)

            if reg_submitted:
                if not new_user.strip() or not new_pass.strip() or not full_name.strip():
                    st.error("Username, Password, and Full Name are required.")
                elif fetch_rows("SELECT 1 FROM users WHERE username = %s", (new_user.strip(),)):
                    st.error("That username is already taken. Choose another.")
                elif plate_no.strip() and fetch_rows(
                    "SELECT 1 FROM vehicles WHERE plate_no = %s", (plate_no.strip().upper(),)
                ):
                    st.error("That vehicle plate number is already registered.")
                else:
                    try:
                        uid = execute_sql(
                            "INSERT INTO users (username, password, full_name, role, phone) VALUES (%s, %s, %s, %s, %s)",
                            (
                                new_user.strip(),
                                new_pass.strip(),
                                full_name.strip(),
                                role,
                                phone.strip() or None,
                            ),
                        )
                        if plate_no.strip():
                            execute_sql(
                                "INSERT INTO vehicles (user_id, plate_no, vehicle_type) VALUES (%s, %s, %s)",
                                (uid, plate_no.strip().upper(), v_type),
                            )
                        st.success(f"Account created! Automatically assigned to {role} Zone. Please sign in.")
                        st.session_state.auth_view = "login"
                        st.rerun()
                    except Error as e:
                        st.error(f"Database error: {e}")

            if st.button("Back to Sign in", type="secondary", use_container_width=True):
                st.session_state.auth_view = "login"
                st.rerun()


# ----------------------------------------------------------------------------
# VIEW 2: USER DASHBOARD (STAFF / STUDENT / VISITOR)
# ----------------------------------------------------------------------------
def render_user_dashboard():
    user = st.session_state.user
    st.markdown(
        f"<div style='font-size: 24px; font-weight: 700; margin-bottom: 12px;'>Welcome, {user['full_name']} ({user['role']} Zone)</div>",
        unsafe_allow_html=True,
    )

    tab_slots, tab_apply, tab_passes, tab_vehicles, tab_violations = st.tabs(
        ["Available Slots", "Apply for Parking Pass", "My Passes", "My Vehicles", "My Violations"]
    )

    # TAB 1: AUTOMATIC ZONAL SLOTS
    with tab_slots:
        st.markdown(
            f"<div style='font-size: 18px; font-weight: 600; margin-bottom: 8px;'>Free Slots in Your Assigned Zone ({user['role']})</div>",
            unsafe_allow_html=True,
        )
        col1, _ = st.columns([1, 2])
        with col1:
            filter_vtype = st.selectbox("Filter by Vehicle Size Compatibility", ["All Sizes", "Bike", "Car", "SUV"])
        sql = "SELECT slot_code AS 'Slot Code', zone AS 'Assigned Zone', size AS 'Slot Size', status AS 'Status' FROM parking_slots WHERE zone = %s AND status = 'Free'"
        params = [user["role"]]
        if filter_vtype != "All Sizes":
            sql += " AND FIELD(size, 'Small', 'Medium', 'Large') >= %s"
            params.append(SIZE_RANK[VEHICLE_SIZE[filter_vtype]])
        sql += " ORDER BY slot_code"
        df_slots = fetch_df(sql, params)
        render_clean_table(df_slots)

    # TAB 2: APPLY FOR PARKING PASS (WITH SCHEDULED DATE & TIME)
    with tab_apply:
        st.markdown(
            f"<div style='font-size: 18px; font-weight: 600; margin-bottom: 8px;'>Book {user['role']} Zone Parking Pass</div>",
            unsafe_allow_html=True,
        )
        vehicles = fetch_rows(
            "SELECT vehicle_id, plate_no, vehicle_type FROM vehicles WHERE user_id = %s", (user["user_id"],)
        )
        if not vehicles:
            st.warning("You have no registered vehicles. Add a vehicle in the 'My Vehicles' tab first.")
        else:
            veh_map = {f"{r[1]} ({r[2]})": (r[0], r[2]) for r in vehicles}
            selected_veh_label = st.selectbox("Select Your Vehicle", list(veh_map.keys()))
            vid, vtype = veh_map[selected_veh_label]

            allowed_passes = list(SHORT_TERM) if user["role"] == "Visitor" else list(PASS_RATES.keys())
            if user["role"] == "Visitor":
                st.info("Visitor accounts are eligible for short-term passes (Hourly or Daily).")

            c1, c2 = st.columns(2)
            with c1:
                ptype = st.selectbox("Pass Type", allowed_passes)
            with c2:
                qty = st.number_input(f"Duration ({UNITS[ptype]}(s))", min_value=1, max_value=365, value=1)

            d1, d2 = st.columns(2)
            now_dt = datetime.now()
            with d1:
                book_date = st.date_input("Start Date", value=now_dt.date(), min_value=now_dt.date())
            with d2:
                default_t = (now_dt + timedelta(minutes=5)).time().replace(second=0, microsecond=0)
                book_time = st.time_input("Start Time", value=default_t)

            start_dt = datetime.combine(book_date, book_time)
            end_dt = start_dt + pass_duration(ptype, qty)
            total_fee = PASS_RATES[ptype] * qty

            st.markdown(
                f"**Assigned Zone:** `{user['role']}` &nbsp;|&nbsp; "
                f"**Schedule:** `{start_dt:%d-%b-%Y %H:%M}` → `{end_dt:%d-%b-%Y %H:%M}` &nbsp;|&nbsp; "
                f"**Total Fee:** `Rs. {total_fee}`"
            )

            if st.button("Submit Booking Application", type="primary"):
                if start_dt < datetime.now() - timedelta(minutes=5):
                    st.error("Start date and time cannot be in the past.")
                else:
                    slot_match = find_available_slot(user["role"], vtype, start_dt, end_dt)
                    auto_slot_id = slot_match[0] if slot_match else None
                    try:
                        pid = execute_sql(
                            """INSERT INTO parking_passes (user_id, vehicle_id, slot_id, pass_type, quantity, fee, start_time, end_time)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                            (user["user_id"], vid, auto_slot_id, ptype, qty, total_fee, start_dt, end_dt),
                        )
                        st.success(
                            f"Booking submitted (Pass #{pid}, {start_dt:%d-%b %H:%M} to {end_dt:%d-%b %H:%M}, Fee: Rs.{total_fee}). Awaiting admin approval."
                        )
                    except Error as e:
                        st.error(f"Error submitting application: {e}")

    # TAB 3: MY PASSES (VIEW, MODIFY, EXTEND, CANCEL)
    with tab_passes:
        st.markdown("<div style='font-size: 18px; font-weight: 600; margin-bottom: 8px;'>My Parking Passes</div>", unsafe_allow_html=True)
        df_passes = fetch_df(
            PASS_SELECT_SQL + " WHERE p.user_id = %s ORDER BY p.pass_id DESC", (user["user_id"],)
        )
        render_clean_table(df_passes)

        manageable = fetch_rows(
            """SELECT p.pass_id, p.pass_type, p.quantity, p.fee, p.status, p.start_time, p.end_time, p.slot_id, v.vehicle_type
               FROM parking_passes p
               JOIN vehicles v ON p.vehicle_id = v.vehicle_id
               WHERE p.user_id = %s AND p.status IN ('Pending', 'Approved')
               ORDER BY p.pass_id DESC""",
            (user["user_id"],),
        )

        if manageable:
            st.markdown("---")
            st.markdown(
                "<div style='font-size: 18px; font-weight: 600; margin-bottom: 8px;'>Modify, Extend, or Cancel a Pass</div>",
                unsafe_allow_html=True,
            )
            p_map = {
                f"Pass #{r[0]} — {r[1]} ({r[2]} {UNITS[r[1]]}(s)) [{r[4]}]": r for r in manageable
            }
            sel_label = st.selectbox("Select Pass to Manage", list(p_map.keys()))
            pid, cur_ptype, cur_qty, cur_fee, cur_status, cur_start, cur_end, cur_slot_id, vtype = p_map[sel_label]

            allowed_passes = list(SHORT_TERM) if user["role"] == "Visitor" else list(PASS_RATES.keys())

            col_mod, col_ext, col_can = st.columns(3)

            # 1. CHANGE PASS TYPE / SCHEDULE (Only while Pending)
            with col_mod:
                st.markdown("**Change Pass Type / Schedule**")
                if cur_status != "Pending":
                    st.caption("Approved passes cannot change tier; you can extend or cancel them.")
                else:
                    new_ptype = st.selectbox(
                        "New Pass Type",
                        allowed_passes,
                        index=allowed_passes.index(cur_ptype) if cur_ptype in allowed_passes else 0,
                        key=f"mod_pt_{pid}",
                    )
                    new_qty = st.number_input(
                        f"New Duration ({UNITS[new_ptype]}(s))",
                        min_value=1,
                        max_value=365,
                        value=int(cur_qty),
                        key=f"mod_qty_{pid}",
                    )
                    base_start = cur_start or datetime.now()
                    mod_date = st.date_input("New Start Date", value=base_start.date(), key=f"mod_d_{pid}")
                    mod_time = st.time_input("New Start Time", value=base_start.time().replace(second=0, microsecond=0), key=f"mod_t_{pid}")
                    new_start = datetime.combine(mod_date, mod_time)
                    new_end = new_start + pass_duration(new_ptype, new_qty)
                    new_fee = PASS_RATES[new_ptype] * new_qty
                    st.caption(f"Updated Fee: Rs. {new_fee} | Ends: {new_end:%d-%b %H:%M}")

                    if st.button("Update Pass", type="primary", key=f"btn_mod_{pid}"):
                        if new_start < datetime.now() - timedelta(minutes=5):
                            st.error("Start time cannot be in the past.")
                        else:
                            slot_match = find_available_slot(user["role"], vtype, new_start, new_end, exclude_pass_id=pid)
                            new_sid = slot_match[0] if slot_match else None
                            execute_sql(
                                """UPDATE parking_passes
                                   SET pass_type = %s, quantity = %s, fee = %s, start_time = %s, end_time = %s, slot_id = %s
                                   WHERE pass_id = %s""",
                                (new_ptype, new_qty, new_fee, new_start, new_end, new_sid, pid),
                            )
                            st.success(f"Pass #{pid} updated to {new_ptype} ({new_qty} {UNITS[new_ptype]}(s)).")
                            st.rerun()

            # 2. EXTEND PASS DURATION (Pending or Approved)
            with col_ext:
                st.markdown("**Extend Pass Duration**")
                add_units = st.number_input(
                    f"Add Extra {UNITS[cur_ptype]}(s)",
                    min_value=1,
                    max_value=180,
                    value=1,
                    key=f"ext_qty_{pid}",
                )
                extra_fee = PASS_RATES[cur_ptype] * add_units
                base_end = cur_end or ((cur_start or datetime.now()) + pass_duration(cur_ptype, cur_qty))
                extended_end = base_end + pass_duration(cur_ptype, add_units)
                st.caption(f"Extra Cost: +Rs. {extra_fee} | New End: {extended_end:%d-%b %H:%M}")

                if st.button("Extend Pass", type="secondary", key=f"btn_ext_{pid}"):
                    # Check if another approved booking overlaps the extended window on the same slot
                    conflict = []
                    if cur_slot_id:
                        conflict = fetch_rows(
                            """SELECT 1 FROM parking_passes
                               WHERE slot_id = %s AND status = 'Approved' AND pass_id <> %s
                                 AND start_time < %s AND end_time > %s""",
                            (cur_slot_id, pid, extended_end, base_end),
                        )
                    if conflict:
                        st.error("Cannot extend: another approved reservation is scheduled on this slot after your end time.")
                    else:
                        execute_sql(
                            """UPDATE parking_passes
                               SET quantity = quantity + %s, fee = fee + %s, end_time = %s
                               WHERE pass_id = %s""",
                            (add_units, extra_fee, extended_end, pid),
                        )
                        st.success(f"Pass #{pid} extended by {add_units} {UNITS[cur_ptype]}(s) until {extended_end:%d-%b %H:%M}.")
                        st.rerun()

            # 3. CANCEL PASS (ACID Transaction frees slot if Approved)
            with col_can:
                st.markdown("**Cancel Pass**")
                st.caption(
                    "Cancelling immediately voids the pass"
                    + (" and releases your assigned parking slot." if cur_status == "Approved" else ".")
                )
                if st.button("Cancel Pass", type="secondary", key=f"btn_can_{pid}"):
                    stmts = [("UPDATE parking_passes SET status = 'Cancelled' WHERE pass_id = %s", (pid,))]
                    if cur_status == "Approved" and cur_slot_id:
                        stmts.append(
                            (
                                "UPDATE parking_slots SET status = 'Free' WHERE slot_id = %s AND status = 'Occupied'",
                                (cur_slot_id,),
                            )
                        )
                    ok, msg = execute_transaction(stmts)
                    if ok:
                        st.warning(f"Pass #{pid} has been cancelled.")
                        st.rerun()
                    else:
                        st.error(msg)

    # TAB 4: MY VEHICLES
    with tab_vehicles:
        st.markdown("<div style='font-size: 18px; font-weight: 600; margin-bottom: 8px;'>Registered Vehicles</div>", unsafe_allow_html=True)
        df_veh = fetch_df(
            "SELECT plate_no AS 'Plate Number', vehicle_type AS 'Vehicle Type' FROM vehicles WHERE user_id = %s",
            (user["user_id"],),
        )
        render_clean_table(df_veh)

        st.markdown("---")
        st.markdown("<div style='font-size: 18px; font-weight: 600; margin-bottom: 8px;'>Add New Vehicle</div>", unsafe_allow_html=True)
        with st.form("add_vehicle_form", clear_on_submit=True):
            vc1, vc2 = st.columns([2, 1.5])
            with vc1:
                new_plate = st.text_input("Plate Number")
            with vc2:
                new_vtype = st.selectbox("Vehicle Type", list(VEHICLE_SIZE.keys()))
            add_v_sub = st.form_submit_button("Add Vehicle", type="primary")

        if add_v_sub:
            plate_clean = new_plate.strip().upper()
            if not plate_clean:
                st.error("Enter a valid plate number.")
            elif fetch_rows("SELECT 1 FROM vehicles WHERE plate_no = %s", (plate_clean,)):
                st.error("That plate number is already registered in the system.")
            else:
                execute_sql(
                    "INSERT INTO vehicles (user_id, plate_no, vehicle_type) VALUES (%s, %s, %s)",
                    (user["user_id"], plate_clean, new_vtype),
                )
                st.success(f"Vehicle {plate_clean} added.")
                st.rerun()

    # TAB 5: MY VIOLATIONS
    with tab_violations:
        st.markdown("<div style='font-size: 18px; font-weight: 600; margin-bottom: 8px;'>My Violation History</div>", unsafe_allow_html=True)
        df_viol = fetch_df(
            """SELECT p.pass_id AS 'Pass ID', ve.plate_no AS 'Plate No',
                      vi.violation_type AS 'Violation Type', vi.reason AS 'Details', vi.flagged_on AS 'Flagged On',
                      IF(vi.resolved, 'Resolved', 'OPEN') AS 'Status'
               FROM violations vi
               JOIN parking_passes p ON vi.pass_id = p.pass_id
               JOIN vehicles ve ON p.vehicle_id = ve.vehicle_id
               WHERE p.user_id = %s ORDER BY vi.flagged_on DESC""",
            (user["user_id"],),
        )
        render_clean_table(df_viol)


# ----------------------------------------------------------------------------
# VIEW 3: ADMINISTRATOR DASHBOARD
# ----------------------------------------------------------------------------
def render_admin_dashboard():
    admin = st.session_state.user
    st.markdown(
        f"<div style='font-size: 24px; font-weight: 700; margin-bottom: 12px;'>Administrator Console — {admin['full_name']}</div>",
        unsafe_allow_html=True,
    )

    m1, m2, m3, m4 = st.columns(4)
    total_free = fetch_rows("SELECT COUNT(*) FROM parking_slots WHERE status = 'Free'")[0][0]
    total_occ = fetch_rows("SELECT COUNT(*) FROM parking_slots WHERE status = 'Occupied'")[0][0]
    pending_cnt = fetch_rows("SELECT COUNT(*) FROM parking_passes WHERE status = 'Pending'")[0][0]
    open_viol = fetch_rows("SELECT COUNT(*) FROM violations WHERE resolved = FALSE")[0][0]

    m1.metric("Free Slots", total_free)
    m2.metric("Occupied Slots", total_occ)
    m3.metric("Pending Passes", pending_cnt)
    m4.metric("Open Violations", open_viol)

    tab_passes, tab_slots, tab_viol, tab_reports, tab_users = st.tabs(
        ["Pass Management", "Slot Management", "Violation Center", "Revenue Report", "Users & Vehicles"]
    )

    # TAB 1: PASS MANAGEMENT
    with tab_passes:
        st.markdown("<div style='font-size: 18px; font-weight: 600; margin-bottom: 8px;'>Pending Pass Applications</div>", unsafe_allow_html=True)
        df_pending = fetch_df(PASS_SELECT_SQL + " WHERE p.status = 'Pending' ORDER BY p.pass_id")
        render_clean_table(df_pending)

        if not df_pending.empty:
            st.markdown("<div style='font-size: 18px; font-weight: 600; margin: 12px 0 8px 0;'>Review Application</div>", unsafe_allow_html=True)
            pending_ids = df_pending["Pass ID"].tolist()
            sel_pid = st.selectbox("Select Pending Pass ID", pending_ids)

            info = fetch_rows(
                """SELECT p.pass_type, p.quantity, p.slot_id, u.role, v.vehicle_type, p.start_time, p.end_time
                   FROM parking_passes p
                   JOIN users u ON p.user_id = u.user_id
                   JOIN vehicles v ON p.vehicle_id = v.vehicle_id
                   WHERE p.pass_id = %s""",
                (sel_pid,),
            )[0]
            ptype, qty, req_slot_id, urole, vtype, req_start, req_end = info

            compat_slots = fetch_rows(
                """SELECT slot_id, slot_code, size FROM parking_slots
                   WHERE zone = %s AND status = 'Free'
                   AND FIELD(size, 'Small', 'Medium', 'Large') >= %s
                   ORDER BY slot_code""",
                (urole, SIZE_RANK[VEHICLE_SIZE[vtype]]),
            )
            slot_choices = {f"{scode} ({ssize}) [ID:{sid}]": sid for sid, scode, ssize in compat_slots}
            default_idx = 0
            if req_slot_id:
                for idx, val in enumerate(slot_choices.values()):
                    if val == req_slot_id:
                        default_idx = idx
                        break

            col_a, col_b, col_c = st.columns([2, 1, 1])
            with col_a:
                if slot_choices:
                    chosen_label = st.selectbox(
                        f"Assign Slot ({urole} Zone, fits {vtype})", list(slot_choices.keys()), index=default_idx
                    )
                    assign_sid = slot_choices[chosen_label]
                else:
                    st.warning(f"No free compatible slots available in {urole} zone.")
                    assign_sid = None

            with col_b:
                st.write("")
                st.write("")
                if st.button("Approve & Assign", type="primary", use_container_width=True, disabled=(assign_sid is None)):
                    start = req_start if req_start else datetime.now()
                    end = req_end if req_end else (start + pass_duration(ptype, qty))
                    ok, msg = execute_transaction(
                        [
                            (
                                "UPDATE parking_passes SET status = 'Approved', slot_id = %s, start_time = %s, end_time = %s WHERE pass_id = %s",
                                (assign_sid, start, end, sel_pid),
                            ),
                            ("UPDATE parking_slots SET status = 'Occupied' WHERE slot_id = %s", (assign_sid,)),
                        ]
                    )
                    if ok:
                        st.success(f"Pass #{sel_pid} approved! {msg}")
                        st.rerun()
                    else:
                        st.error(msg)

            with col_c:
                st.write("")
                st.write("")
                if st.button("Reject Application", type="secondary", use_container_width=True):
                    execute_sql("UPDATE parking_passes SET status = 'Rejected' WHERE pass_id = %s", (sel_pid,))
                    st.warning(f"Pass #{sel_pid} rejected.")
                    st.rerun()

        st.markdown("---")
        st.markdown("<div style='font-size: 18px; font-weight: 600; margin-bottom: 8px;'>All Passes & Close Active Pass</div>", unsafe_allow_html=True)
        status_filter = st.selectbox(
            "Filter Passes by Status", ["All", "Approved", "Pending", "Closed", "Cancelled", "Rejected"]
        )
        if status_filter == "All":
            df_all_passes = fetch_df(PASS_SELECT_SQL + " ORDER BY p.pass_id DESC")
        else:
            df_all_passes = fetch_df(PASS_SELECT_SQL + " WHERE p.status = %s ORDER BY p.pass_id DESC", (status_filter,))
        render_clean_table(df_all_passes)

        active_rows = fetch_rows("SELECT pass_id, slot_id FROM parking_passes WHERE status = 'Approved' ORDER BY pass_id")
        if active_rows:
            c1, c2 = st.columns([2, 1])
            active_map = {r[0]: r[1] for r in active_rows}
            with c1:
                close_pid = st.selectbox("Select Active Pass ID to Close (Frees Slot)", list(active_map.keys()))
            with c2:
                st.write("")
                st.write("")
                if st.button("Close Pass", type="secondary", use_container_width=True):
                    sid_to_free = active_map[close_pid]
                    stmts = [("UPDATE parking_passes SET status = 'Closed' WHERE pass_id = %s", (close_pid,))]
                    if sid_to_free:
                        stmts.append(
                            (
                                "UPDATE parking_slots SET status = 'Free' WHERE slot_id = %s AND status = 'Occupied'",
                                (sid_to_free,),
                            )
                        )
                    ok, msg = execute_transaction(stmts)
                    if ok:
                        st.success(f"Pass #{close_pid} closed and slot freed.")
                        st.rerun()
                    else:
                        st.error(msg)

    # TAB 2: SLOT MANAGEMENT
    with tab_slots:
        st.markdown("<div style='font-size: 18px; font-weight: 600; margin-bottom: 8px;'>Zonal Parking Slots</div>", unsafe_allow_html=True)
        sc1, sc2 = st.columns(2)
        with sc1:
            z_filt = st.selectbox("Filter Zone", ["All"] + ZONES)
        with sc2:
            st_filt = st.selectbox("Filter Status", ["All", "Free", "Occupied", "Maintenance"])

        q = "SELECT slot_id AS 'Slot ID', slot_code AS 'Code', zone AS 'Zone', size AS 'Size', status AS 'Status' FROM parking_slots WHERE 1=1"
        p = []
        if z_filt != "All":
            q += " AND zone = %s"
            p.append(z_filt)
        if st_filt != "All":
            q += " AND status = %s"
            p.append(st_filt)
        q += " ORDER BY zone, slot_code"
        df_s = fetch_df(q, p)
        render_clean_table(df_s)

        col_add, col_mod = st.columns(2)
        with col_add:
            st.markdown("<div style='font-size: 18px; font-weight: 600; margin: 12px 0 8px 0;'>Add New Slot</div>", unsafe_allow_html=True)
            with st.form("add_slot_form", clear_on_submit=True):
                new_code = st.text_input("Slot Code (e.g., V11)")
                new_szone = st.selectbox("Zone", ZONES)
                new_ssize = st.selectbox("Size", list(SIZE_RANK.keys()))
                create_slot_sub = st.form_submit_button("Create Slot", type="primary")

            if create_slot_sub:
                code_up = new_code.strip().upper()
                if not code_up:
                    st.error("Slot code required.")
                elif fetch_rows("SELECT 1 FROM parking_slots WHERE slot_code = %s", (code_up,)):
                    st.error("Slot code already exists.")
                else:
                    execute_sql(
                        "INSERT INTO parking_slots (slot_code, zone, size) VALUES (%s, %s, %s)",
                        (code_up, new_szone, new_ssize),
                    )
                    st.success(f"Slot {code_up} created.")
                    st.rerun()

        with col_mod:
            st.markdown("<div style='font-size: 18px; font-weight: 600; margin: 12px 0 8px 0;'>Update Slot Status / Delete</div>", unsafe_allow_html=True)
            all_slot_ids = fetch_rows("SELECT slot_id, slot_code, status FROM parking_slots ORDER BY slot_code")
            if all_slot_ids:
                s_map = {f"{r[1]} (Currently: {r[2]})": r[0] for r in all_slot_ids}
                target_label = st.selectbox("Select Slot", list(s_map.keys()))
                target_sid = s_map[target_label]
                new_status = st.selectbox("Set Status", ["Free", "Occupied", "Maintenance"])

                b1, b2 = st.columns(2)
                with b1:
                    if st.button("Update Status", type="secondary", use_container_width=True):
                        has_active = fetch_rows(
                            "SELECT 1 FROM parking_passes WHERE slot_id = %s AND status = 'Approved'", (target_sid,)
                        )
                        if has_active and new_status != "Occupied":
                            st.error("Cannot change status: slot has an active approved pass.")
                        else:
                            execute_sql("UPDATE parking_slots SET status = %s WHERE slot_id = %s", (new_status, target_sid))
                            st.success("Slot status updated.")
                            st.rerun()
                with b2:
                    if st.button("Delete Slot", type="secondary", use_container_width=True):
                        has_active = fetch_rows(
                            "SELECT 1 FROM parking_passes WHERE slot_id = %s AND status = 'Approved'", (target_sid,)
                        )
                        if has_active:
                            st.error("Cannot delete slot with an active pass.")
                        else:
                            execute_sql("DELETE FROM parking_slots WHERE slot_id = %s", (target_sid,))
                            st.warning("Slot deleted.")
                            st.rerun()

    # TAB 3: VIOLATION CENTER
    with tab_viol:
        st.markdown("<div style='font-size: 18px; font-weight: 600; margin-bottom: 8px;'>Automated & Manual Violation Flagging</div>", unsafe_allow_html=True)

        if st.button("Run Automated Overstay & Zone Violation Scan", type="primary"):
            candidates = []
            overstays = fetch_rows(
                """SELECT p.pass_id, p.end_time, TIMESTAMPDIFF(MINUTE, p.end_time, NOW())
                   FROM parking_passes p
                   WHERE p.status = 'Approved' AND p.end_time < NOW() AND p.violation_flag = FALSE"""
            )
            for pid, end_t, mins in overstays:
                candidates.append(
                    (pid, "Overstay", f"Exceeded end time ({end_t:%d-%b-%Y %H:%M}) by {mins} minute(s)")
                )

            unauth = fetch_rows(
                """SELECT p.pass_id, s.slot_code, s.zone, u.role
                   FROM parking_passes p
                   JOIN users u ON p.user_id = u.user_id
                   JOIN parking_slots s ON p.slot_id = s.slot_id
                   WHERE p.status = 'Approved' AND s.zone <> u.role AND p.violation_flag = FALSE"""
            )
            for pid, scode, szone, urole in unauth:
                candidates.append(
                    (pid, "Unauthorized Zone", f"Parked in {szone} zone (slot {scode}) but authorized for {urole}")
                )

            if not candidates:
                st.info("No new overstay or unauthorized zone violations detected.")
            else:
                for pid, vtype, reason in candidates:
                    execute_transaction(
                        [
                            ("UPDATE parking_passes SET violation_flag = TRUE WHERE pass_id = %s", (pid,)),
                            (
                                "INSERT INTO violations (pass_id, violation_type, reason) VALUES (%s, %s, %s)",
                                (pid, vtype, reason[:255]),
                            ),
                        ]
                    )
                st.warning(f"Flagged {len(candidates)} new violation(s)!")
                st.rerun()

        st.markdown("---")
        st.markdown("<div style='font-size: 18px; font-weight: 600; margin-bottom: 8px;'>Violation Log</div>", unsafe_allow_html=True)
        df_v = fetch_df(
            """SELECT vi.violation_id AS 'Violation ID', p.pass_id AS 'Pass ID', u.username AS 'User',
                      ve.plate_no AS 'Plate', vi.violation_type AS 'Type', vi.reason AS 'Reason',
                      vi.flagged_on AS 'Flagged On', IF(vi.resolved, 'Resolved', 'OPEN') AS 'Status'
               FROM violations vi
               JOIN parking_passes p ON vi.pass_id = p.pass_id
               JOIN users u ON p.user_id = u.user_id
               JOIN vehicles ve ON p.vehicle_id = ve.vehicle_id
               ORDER BY vi.resolved, vi.flagged_on DESC"""
        )
        render_clean_table(df_v)

        open_v = fetch_rows("SELECT violation_id, pass_id FROM violations WHERE resolved = FALSE ORDER BY violation_id")
        if open_v:
            vc1, vc2 = st.columns([2, 1])
            v_map = {f"Violation #{r[0]} (Pass #{r[1]})": (r[0], r[1]) for r in open_v}
            with vc1:
                sel_v = st.selectbox("Select Open Violation to Resolve", list(v_map.keys()))
            with vc2:
                st.write("")
                st.write("")
                if st.button("Resolve Violation", type="secondary", use_container_width=True):
                    vid, pid = v_map[sel_v]
                    execute_sql("UPDATE violations SET resolved = TRUE WHERE violation_id = %s", (vid,))
                    still_open = fetch_rows(
                        "SELECT 1 FROM violations WHERE pass_id = %s AND resolved = FALSE", (pid,)
                    )
                    if not still_open:
                        execute_sql("UPDATE parking_passes SET violation_flag = FALSE WHERE pass_id = %s", (pid,))
                    st.success(f"Violation #{vid} resolved.")
                    st.rerun()

    # TAB 4: AGGREGATED REVENUE REPORT
    with tab_reports:
        st.markdown("<div style='font-size: 18px; font-weight: 600; margin-bottom: 8px;'>Zonal Revenue & Pass Utilization Report</div>", unsafe_allow_html=True)
        report_sql = """
            SELECT s.zone AS 'Parking Zone',
                   p.pass_type AS 'Pass Tier',
                   COUNT(p.pass_id) AS 'Total Passes Issued',
                   SUM(p.fee) AS 'Total Revenue (Rs)'
            FROM parking_passes p
            JOIN parking_slots s ON p.slot_id = s.slot_id
            WHERE p.status IN ('Approved', 'Closed')
            GROUP BY s.zone, p.pass_type
            ORDER BY s.zone, p.pass_type
        """
        df_rep = fetch_df(report_sql)
        render_clean_table(df_rep)

        if not df_rep.empty:
            total_rev = df_rep["Total Revenue (Rs)"].sum()
            st.metric("Total System Revenue Collected", f"Rs. {total_rev:,.2f}")

    # TAB 5: USERS & VEHICLES
    with tab_users:
        st.markdown("<div style='font-size: 18px; font-weight: 600; margin-bottom: 8px;'>Registered Users</div>", unsafe_allow_html=True)
        df_u = fetch_df(
            """SELECT u.user_id AS 'User ID', u.username AS 'Username', u.full_name AS 'Full Name',
                      u.role AS 'Zone Role', u.phone AS 'Phone', COUNT(v.vehicle_id) AS 'Vehicles'
               FROM users u
               LEFT JOIN vehicles v ON u.user_id = v.user_id
               GROUP BY u.user_id ORDER BY u.user_id"""
        )
        render_clean_table(df_u)

        st.markdown("<div style='font-size: 18px; font-weight: 600; margin: 12px 0 8px 0;'>All Registered Vehicles</div>", unsafe_allow_html=True)
        df_all_v = fetch_df(
            """SELECT v.vehicle_id AS 'Vehicle ID', v.plate_no AS 'Plate No',
                      v.vehicle_type AS 'Type', u.username AS 'Owner', u.role AS 'Owner Zone'
               FROM vehicles v
               JOIN users u ON v.user_id = u.user_id
               ORDER BY v.vehicle_id"""
        )
        render_clean_table(df_all_v)


# ----------------------------------------------------------------------------
# MAIN ROUTER
# ----------------------------------------------------------------------------
if st.session_state.user is None:
    render_auth()
elif st.session_state.role_type == "admin":
    render_admin_dashboard()
else:
    render_user_dashboard()
