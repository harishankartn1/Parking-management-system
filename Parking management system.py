"""
Amrita Parking Management System
--------------------------------
Menu-driven parking management program (Python + MySQL).
Enhanced for 3NF Zonal Parking, ACID Transactions, and Aggregated Reporting.
"""

from datetime import datetime, timedelta
import mysql.connector
from mysql.connector import Error
from tabulate import tabulate
import os
from dotenv import load_dotenv

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

load_dotenv()

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
}
DB_NAME = os.getenv("DB_NAME", "project")


APP_NAME = "Amrita Parking Management System"
SUPER_KEYWORD = "super"
SUPER_PASSWORD = "superuser123"

# Expanded Zonal support for 3NF requirements
ZONES = ["Staff", "Student", "Visitor"]
SIZE_RANK = {"Small": 1, "Medium": 2, "Large": 3}
VEHICLE_SIZE = {"Bike": "Small", "Car": "Medium", "SUV": "Large"}

PASS_RATES = {"Hourly": 20, "Daily": 100, "Monthly": 1500, "Semester": 6000}
SHORT_TERM = ("Hourly", "Daily")
UNITS = {"Hourly": "hour", "Daily": "day", "Monthly": "month", "Semester": "semester"}

conn = None


# ----------------------------------------------------------------------------
# DATABASE HELPERS
# ----------------------------------------------------------------------------
def connect():
    """Connect to MySQL. Selects the project database if it exists."""
    global conn
    conn = mysql.connector.connect(**DB_CONFIG)
    conn.autocommit = True
    if db_exists():
        use_db(DB_NAME)


def db_exists():
    cur = conn.cursor()
    try:
        cur.execute("SHOW DATABASES LIKE %s", (DB_NAME,))
        return cur.fetchone() is not None
    finally:
        cur.close()


def fetch(sql, params=None):
    cur = conn.cursor()
    try:
        cur.execute(sql, params or ())
        rows = cur.fetchall()
        headers = [d[0] for d in cur.description] if cur.description else []
        return rows, headers
    finally:
        cur.close()


def execute(sql, params=None):
    cur = conn.cursor()
    try:
        cur.execute(sql, params or ())
        return cur.lastrowid
    finally:
        cur.close()


def execute_transaction(statements):
    """Executes multiple queries in a transaction to prevent partial updates."""
    cur = conn.cursor()
    try:
        conn.start_transaction()
        for sql, params in statements:
            cur.execute(sql, params or ())
        conn.commit()
        return True
    except Error as e:
        conn.rollback()
        print(f"\n[Transaction Error] {e} - Changes rolled back to maintain consistency.")
        return False
    finally:
        cur.close()


def use_db(name):
    execute(f"USE `{name}`")


# ----------------------------------------------------------------------------
# INPUT / DISPLAY HELPERS
# ----------------------------------------------------------------------------
def ask(prompt, required=True):
    while True:
        value = input(prompt).strip()
        if value or not required:
            return value
        print("This field cannot be empty.")


def ask_int(prompt, low=None, high=None):
    while True:
        raw = input(prompt).strip()
        try:
            val = int(raw)
        except ValueError:
            print("Please enter a whole number.")
            continue
        if (low is not None and val < low) or (high is not None and val > high):
            print("That number is out of range.")
            continue
        return val


def ask_choice(title, options):
    print(title)
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    return options[ask_int("Choose: ", 1, len(options)) - 1]


def confirm(prompt):
    return input(prompt + " (y/n): ").strip().lower() in ("y", "yes")


def menu(title, items, exit_label="Back"):
    print(f"\n--- {title} ---")
    for i, label in enumerate(items, 1):
        print(f"{i}. {label}")
    print(f"0. {exit_label}")
    return ask_int("Enter choice: ", 0, len(items))


def show(rows, headers, title=None):
    if title:
        print(f"\n=== {title} ===")
    if not rows:
        print("(no records found)")
        return
    print(tabulate(rows, headers=headers, tablefmt="grid"))


def safe(fn, *args):
    try:
        fn(*args)
    except Error as e:
        print(f"\n[Database error] {e}")


# ----------------------------------------------------------------------------
# SCHEMA (3NF Normalized for Zonal Parking)
# ----------------------------------------------------------------------------
SCHEMA = {
    "admins": """
        CREATE TABLE IF NOT EXISTS admins (
            admin_id INT AUTO_INCREMENT PRIMARY KEY,
            username VARCHAR(50) NOT NULL UNIQUE,
            password VARCHAR(50) NOT NULL,
            full_name VARCHAR(100) NOT NULL
        )""",
    "users": """
        CREATE TABLE IF NOT EXISTS users (
            user_id INT AUTO_INCREMENT PRIMARY KEY,
            username VARCHAR(50) NOT NULL UNIQUE,
            password VARCHAR(50) NOT NULL,
            full_name VARCHAR(100) NOT NULL,
            role ENUM('Staff','Student','Visitor') NOT NULL,
            phone VARCHAR(15),
            created_on DATETIME DEFAULT CURRENT_TIMESTAMP
        )""",
    "vehicles": """
        CREATE TABLE IF NOT EXISTS vehicles (
            vehicle_id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            plate_no VARCHAR(20) NOT NULL UNIQUE,
            vehicle_type ENUM('Bike','Car','SUV') NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
        )""",
    "parking_slots": """
        CREATE TABLE IF NOT EXISTS parking_slots (
            slot_id INT AUTO_INCREMENT PRIMARY KEY,
            slot_code VARCHAR(10) NOT NULL UNIQUE,
            zone ENUM('Staff','Student','Visitor') NOT NULL,
            size ENUM('Small','Medium','Large') NOT NULL,
            status ENUM('Free','Occupied','Maintenance') NOT NULL DEFAULT 'Free'
        )""",
    "parking_passes": """
        CREATE TABLE IF NOT EXISTS parking_passes (
            pass_id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            vehicle_id INT NOT NULL,
            slot_id INT NULL,
            pass_type ENUM('Hourly','Daily','Monthly','Semester') NOT NULL,
            quantity INT NOT NULL DEFAULT 1 CHECK (quantity > 0),
            fee DECIMAL(10,2) NOT NULL,
            status ENUM('Pending','Approved','Rejected','Closed') NOT NULL DEFAULT 'Pending',
            applied_on DATETIME DEFAULT CURRENT_TIMESTAMP,
            start_time DATETIME NULL,
            end_time DATETIME NULL,
            violation_flag BOOLEAN NOT NULL DEFAULT FALSE,
            FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
            FOREIGN KEY (vehicle_id) REFERENCES vehicles(vehicle_id) ON DELETE CASCADE,
            FOREIGN KEY (slot_id) REFERENCES parking_slots(slot_id) ON DELETE SET NULL
        )""",
    "violations": """
        CREATE TABLE IF NOT EXISTS violations (
            violation_id INT AUTO_INCREMENT PRIMARY KEY,
            pass_id INT NOT NULL,
            violation_type ENUM('Overstay','Unauthorized Zone','Other') NOT NULL,
            reason VARCHAR(255) NOT NULL,
            flagged_on DATETIME DEFAULT CURRENT_TIMESTAMP,
            resolved BOOLEAN NOT NULL DEFAULT FALSE,
            FOREIGN KEY (pass_id) REFERENCES parking_passes(pass_id) ON DELETE CASCADE
        )""",
}

DEPENDS_ON = {
    "admins": [],
    "users": [],
    "parking_slots": [],
    "vehicles": ["users"],
    "parking_passes": ["users", "vehicles", "parking_slots"],
    "violations": ["parking_passes"],
}


def existing_tables():
    if not db_exists():
        return []
    use_db(DB_NAME)
    return [r[0] for r in fetch("SHOW TABLES")[0]]


def seed_table(name):
    if name == "admins":
        execute("INSERT INTO admins (username, password, full_name) "
                "VALUES ('admin', 'admin123', 'Default Admin')")
    elif name == "parking_slots":
        layout = [("Small", 3), ("Medium", 4), ("Large", 3)]
        for zone, prefix in (("Staff", "S"), ("Student", "T"), ("Visitor", "V")):
            n = 1
            for size, count in layout:
                for _ in range(count):
                    execute("INSERT INTO parking_slots (slot_code, zone, size) VALUES (%s, %s, %s)",
                            (f"{prefix}{n:02d}", zone, size))
                    n += 1


def project_ready():
    if not db_exists():
        print(f"\n[!] Database '{DB_NAME}' does not exist. Ask the superuser to set it up first.")
        return False
    try:
        present = existing_tables()
    except Error as e:
        print(f"\n[Database error] {e}")
        return False
    missing = [t for t in SCHEMA if t not in present]
    if missing:
        print("\n[!] Missing tables: " + ", ".join(missing) + ". Ask the superuser to create them first.")
        return False
    return True


# ----------------------------------------------------------------------------
# SHARED PARKING LOGIC
# ----------------------------------------------------------------------------
PASS_SELECT = """
    SELECT p.pass_id, u.username, u.role, v.plate_no, v.vehicle_type,
           s.slot_code, s.zone AS slot_zone, p.pass_type, p.quantity AS qty, p.fee,
           p.status, p.start_time, p.end_time, IF(p.violation_flag, 'YES', 'no') AS flagged
    FROM parking_passes p
    JOIN users u ON p.user_id = u.user_id
    JOIN vehicles v ON p.vehicle_id = v.vehicle_id
    LEFT JOIN parking_slots s ON p.slot_id = s.slot_id
"""

def pass_duration(pass_type, qty):
    return {
        "Hourly": timedelta(hours=qty),
        "Daily": timedelta(days=qty),
        "Monthly": timedelta(days=30 * qty),
        "Semester": timedelta(days=180 * qty),
    }[pass_type]


def get_slots(zone=None, min_size=None, status=None):
    sql = "SELECT slot_id, slot_code, zone, size, status FROM parking_slots WHERE 1=1"
    params = []
    if zone:
        sql += " AND zone = %s"
        params.append(zone)
    if min_size:
        sql += " AND FIELD(size, 'Small', 'Medium', 'Large') >= %s"
        params.append(SIZE_RANK[min_size])
    if status:
        sql += " AND status = %s"
        params.append(status)
    sql += " ORDER BY zone, slot_code"
    return fetch(sql, params)


def browse_slots(user=None):
    admin = user is None
    zone_label = "Filter by zone" if admin else f"Filter by my authorized zone ({user['role']})"
    options = ["Show all slots",
               "Filter by vehicle type (size compatibility)",
               zone_label,
               "Filter by vehicle type AND zone"]
    idx = options.index(ask_choice("\nFilter options:", options))

    vtype = zone = None
    if idx in (1, 3):
        vtype = ask_choice("Vehicle type:", list(VEHICLE_SIZE))
    if idx in (2, 3):
        zone = ask_choice("Zone:", ZONES) if admin else user["role"]

    if admin:
        s = ask_choice("Slot status:", ["Any", "Free", "Occupied", "Maintenance"])
        status = None if s == "Any" else s
    else:
        status = "Free"

    rows, headers = get_slots(zone, VEHICLE_SIZE.get(vtype), status)
    show(rows, headers, "Parking Slots")


def pick_slot(zone=None, vehicle_type=None):
    rows, headers = get_slots(zone, VEHICLE_SIZE.get(vehicle_type), "Free")
    if not rows:
        print("No free compatible slots available.")
        return None
    show(rows, headers, "Free Compatible Slots")
    valid = {r[0] for r in rows}
    while True:
        sid = ask_int("Enter Slot ID (0 to skip/cancel): ", 0)
        if sid == 0:
            return None
        if sid in valid:
            return sid
        print("Please pick a Slot ID from the table above.")


def choose_pass_details(user_role):
    types = list(PASS_RATES)
    
    # Nontrivial Business Rule: Visitors can only access short-term parking
    if user_role == "Visitor":
        print("\n[Business Rule] Visitors are restricted to short-term passes (Hourly/Daily).")
        types = list(SHORT_TERM)
        
    labels = [f"{t} ({'short-term' if t in SHORT_TERM else 'long-term'}) - Rs.{PASS_RATES[t]} per {UNITS[t]}"
              for t in types]
    chosen = ask_choice("Select pass type:", labels)
    ptype = types[labels.index(chosen)]
    qty = ask_int(f"How many {UNITS[ptype]}(s)? ", 1, 365)
    print(f"Total fee: Rs.{PASS_RATES[ptype] * qty}")
    return ptype, qty


def choose_vehicle(user_id):
    rows, headers = fetch("SELECT vehicle_id, plate_no, vehicle_type FROM vehicles WHERE user_id = %s",
                          (user_id,))
    if not rows:
        return None
    show(rows, headers, "Vehicles")
    valid = {r[0]: r for r in rows}
    while True:
        vid = ask_int("Enter Vehicle ID: ", 1)
        if vid in valid:
            return valid[vid]
        print("Pick a Vehicle ID from the table above.")


def add_vehicle(user_id):
    plate = ask("Vehicle plate number: ").upper()
    vtype = ask_choice("Vehicle type:", list(VEHICLE_SIZE))
    execute("INSERT INTO vehicles (user_id, plate_no, vehicle_type) VALUES (%s, %s, %s)",
            (user_id, plate, vtype))
    print(f"Vehicle {plate} ({vtype}) added.")


def create_pass(user_id, vehicle_id, ptype, qty, slot_id=None, approve=False):
    fee = PASS_RATES[ptype] * qty
    if approve:
        start = datetime.now()
        end = start + pass_duration(ptype, qty)
        cur = conn.cursor()
        try:
            conn.start_transaction()
            cur.execute(
                "INSERT INTO parking_passes (user_id, vehicle_id, slot_id, pass_type, quantity, fee, "
                "status, start_time, end_time) VALUES (%s, %s, %s, %s, %s, %s, 'Approved', %s, %s)",
                (user_id, vehicle_id, slot_id, ptype, qty, fee, start, end))
            pid = cur.lastrowid
            cur.execute("UPDATE parking_slots SET status = 'Occupied' WHERE slot_id = %s", (slot_id,))
            conn.commit()
            print(f"Pass #{pid} issued. Valid {start:%d-%b-%Y %H:%M} to {end:%d-%b-%Y %H:%M}. Fee: Rs.{fee}")
            return pid
        except Error as e:
            conn.rollback()
            print(f"\n[Transaction Error] {e} - Failed to issue pass.")
            return None
        finally:
            cur.close()
    else:
        pid = execute(
            "INSERT INTO parking_passes (user_id, vehicle_id, slot_id, pass_type, quantity, fee) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (user_id, vehicle_id, slot_id, ptype, qty, fee))
        print(f"Application submitted (Pass #{pid}, fee Rs.{fee}). Waiting for admin approval.")
        return pid


# ----------------------------------------------------------------------------
# USER FEATURES
# ----------------------------------------------------------------------------
def user_apply_pass(user):
    vehicle = choose_vehicle(user["user_id"])
    if vehicle is None:
        print("You have no vehicles registered. Let's add one first.")
        add_vehicle(user["user_id"])
        vehicle = choose_vehicle(user["user_id"])
    vid, plate, vtype = vehicle

    ptype, qty = choose_pass_details(user["role"])

    print(f"\nFree slots in your authorized zone ({user['role']}) that fit a {vtype}:")
    slot_id = pick_slot(zone=user["role"], vehicle_type=vtype)
    if slot_id is None:
        print("No slot chosen - the admin will assign one on approval.")

    if confirm("Submit application?"):
        create_pass(user["user_id"], vid, ptype, qty, slot_id)


def user_view_passes(user):
    rows, headers = fetch(PASS_SELECT + " WHERE p.user_id = %s ORDER BY p.pass_id DESC", (user["user_id"],))
    show(rows, headers, "My Passes")


def user_view_vehicles(user):
    rows, headers = fetch("SELECT vehicle_id, plate_no, vehicle_type FROM vehicles WHERE user_id = %s",
                          (user["user_id"],))
    show(rows, headers, "My Vehicles")
    if confirm("Add a new vehicle?"):
        add_vehicle(user["user_id"])


def user_view_violations(user):
    rows, headers = fetch(
        """SELECT vi.violation_id, p.pass_id, ve.plate_no, vi.violation_type, vi.reason,
                  vi.flagged_on, IF(vi.resolved, 'Resolved', 'OPEN') AS status
           FROM violations vi
           JOIN parking_passes p ON vi.pass_id = p.pass_id
           JOIN vehicles ve ON p.vehicle_id = ve.vehicle_id
           WHERE p.user_id = %s ORDER BY vi.flagged_on DESC""", (user["user_id"],))
    show(rows, headers, "My Violations")


def user_menu(user):
    print(f"\nWelcome, {user['full_name']} ({user['role']})!")
    while True:
        c = menu("USER MENU", ["View free slots", "Apply for a parking pass", "My passes",
                               "My vehicles", "My violations"], "Logout")
        if c == 0:
            return
        if c == 1:
            safe(browse_slots, user)
        elif c == 2:
            safe(user_apply_pass, user)
        elif c == 3:
            safe(user_view_passes, user)
        elif c == 4:
            safe(user_view_vehicles, user)
        elif c == 5:
            safe(user_view_violations, user)


# ----------------------------------------------------------------------------
# ADMIN: SLOT MANAGEMENT
# ----------------------------------------------------------------------------
def admin_add_slot():
    code = ask("Slot code (e.g. S11): ").upper()
    zone = ask_choice("Zone:", ZONES)
    size = ask_choice("Size:", list(SIZE_RANK))
    execute("INSERT INTO parking_slots (slot_code, zone, size) VALUES (%s, %s, %s)", (code, zone, size))
    print("Slot added.")


def admin_slot_status():
    rows, headers = get_slots()
    show(rows, headers, "All Slots")
    sid = ask_int("Slot ID to update (0 to cancel): ", 0)
    if sid == 0:
        return
    status = ask_choice("New status:", ["Free", "Occupied", "Maintenance"])
    active = fetch("SELECT 1 FROM parking_passes WHERE slot_id = %s AND status = 'Approved'", (sid,))[0]
    if active and status != "Occupied":
        print("This slot has an active pass. Close the pass first.")
        return
    execute("UPDATE parking_slots SET status = %s WHERE slot_id = %s", (status, sid))
    print("Slot status updated.")


def admin_edit_slot():
    rows, headers = get_slots()
    show(rows, headers, "All Slots")
    sid = ask_int("Slot ID to edit (0 to cancel): ", 0)
    if sid == 0:
        return
    zone = ask_choice("New zone:", ZONES)
    size = ask_choice("New size:", list(SIZE_RANK))
    execute("UPDATE parking_slots SET zone = %s, size = %s WHERE slot_id = %s", (zone, size, sid))
    print("Slot updated.")


def admin_delete_slot():
    rows, headers = get_slots()
    show(rows, headers, "All Slots")
    sid = ask_int("Slot ID to delete (0 to cancel): ", 0)
    if sid == 0:
        return
    if fetch("SELECT 1 FROM parking_passes WHERE slot_id = %s AND status = 'Approved'", (sid,))[0]:
        print("Cannot delete: slot has an active pass.")
        return
    if confirm("Delete this slot?"):
        execute("DELETE FROM parking_slots WHERE slot_id = %s", (sid,))
        print("Slot deleted.")


def admin_slot_menu():
    while True:
        c = menu("SLOT MANAGEMENT", ["View / filter slots", "Add slot", "Change slot status",
                                      "Edit slot zone/size", "Delete slot"])
        if c == 0:
            return
        [None, lambda: safe(browse_slots), lambda: safe(admin_add_slot),
         lambda: safe(admin_slot_status), lambda: safe(admin_edit_slot),
         lambda: safe(admin_delete_slot)][c]()


# ----------------------------------------------------------------------------
# ADMIN: PASSES
# ----------------------------------------------------------------------------
def admin_view_passes():
    status = ask_choice("Show which passes?", ["All", "Pending", "Approved", "Rejected", "Closed"])
    if status == "All":
        rows, headers = fetch(PASS_SELECT + " ORDER BY p.pass_id DESC")
    else:
        rows, headers = fetch(PASS_SELECT + " WHERE p.status = %s ORDER BY p.pass_id DESC", (status,))
    show(rows, headers, f"{status} Passes")


def admin_approve_application():
    rows, headers = fetch(PASS_SELECT + " WHERE p.status = 'Pending' ORDER BY p.pass_id")
    show(rows, headers, "Pending Applications")
    if not rows:
        return
    pid = ask_int("Pass ID to approve (0 to cancel): ", 0)
    if pid == 0:
        return
    info = fetch(
        """SELECT p.pass_type, p.quantity, p.slot_id, u.role, v.vehicle_type
           FROM parking_passes p JOIN users u ON p.user_id = u.user_id
           JOIN vehicles v ON p.vehicle_id = v.vehicle_id
           WHERE p.pass_id = %s AND p.status = 'Pending'""", (pid,))[0]
    if not info:
        print("No pending application with that ID.")
        return
    ptype, qty, requested, role, vtype = info[0]

    slot_id = None
    if requested:
        st = fetch("SELECT status FROM parking_slots WHERE slot_id = %s", (requested,))[0]
        if st and st[0][0] == "Free" and confirm(f"User requested slot ID {requested}. Assign it?"):
            slot_id = requested
    if slot_id is None:
        slot_id = pick_slot(zone=role, vehicle_type=vtype)
        if slot_id is None:
            print("Approval cancelled (no slot assigned).")
            return

    start = datetime.now()
    end = start + pass_duration(ptype, qty)
    
    statements = [
        ("UPDATE parking_passes SET status = 'Approved', slot_id = %s, start_time = %s, end_time = %s WHERE pass_id = %s", (slot_id, start, end, pid)),
        ("UPDATE parking_slots SET status = 'Occupied' WHERE slot_id = %s", (slot_id,))
    ]
    if execute_transaction(statements):
        print(f"Pass #{pid} approved. Valid until {end:%d-%b-%Y %H:%M}.")


def admin_reject_application():
    rows, headers = fetch(PASS_SELECT + " WHERE p.status = 'Pending' ORDER BY p.pass_id")
    show(rows, headers, "Pending Applications")
    if not rows:
        return
    pid = ask_int("Pass ID to reject (0 to cancel): ", 0)
    if pid == 0:
        return
    execute("UPDATE parking_passes SET status = 'Rejected' WHERE pass_id = %s AND status = 'Pending'", (pid,))
    print("Application rejected (if it was pending).")


def admin_issue_pass_directly():
    rows, headers = fetch("SELECT user_id, username, full_name, role FROM users ORDER BY user_id")
    show(rows, headers, "Users")
    if not rows:
        return
    uid = ask_int("User ID (0 to cancel): ", 0)
    if uid == 0:
        return
    urow = fetch("SELECT role FROM users WHERE user_id = %s", (uid,))[0]
    if not urow:
        print("No such user.")
        return
    role = urow[0][0]

    vehicle = choose_vehicle(uid)
    if vehicle is None:
        print("This user has no vehicles.")
        if confirm("Add a vehicle for them now?"):
            add_vehicle(uid)
            vehicle = choose_vehicle(uid)
        if vehicle is None:
            return
    vid, plate, vtype = vehicle

    ptype, qty = choose_pass_details(role)
    restrict = confirm(f"Restrict slots to the user's authorized zone ({role})? 'n' shows all zones")
    slot_id = pick_slot(zone=role if restrict else None, vehicle_type=vtype)
    if slot_id is None:
        print("Cancelled (no slot chosen).")
        return
    create_pass(uid, vid, ptype, qty, slot_id, approve=True)


def admin_close_pass():
    rows, headers = fetch(PASS_SELECT + " WHERE p.status = 'Approved' ORDER BY p.pass_id")
    show(rows, headers, "Active Passes")
    if not rows:
        return
    pid = ask_int("Pass ID to close (0 to cancel): ", 0)
    if pid == 0:
        return
    info = fetch("SELECT slot_id FROM parking_passes WHERE pass_id = %s AND status = 'Approved'", (pid,))[0]
    if not info:
        print("No active pass with that ID.")
        return
        
    statements = [("UPDATE parking_passes SET status = 'Closed' WHERE pass_id = %s", (pid,))]
    if info[0][0]:
        statements.append(("UPDATE parking_slots SET status = 'Free' WHERE slot_id = %s AND status = 'Occupied'", (info[0][0],)))
        
    if execute_transaction(statements):
        print("Pass closed and slot freed.")


def admin_pass_menu():
    while True:
        c = menu("PASS MANAGEMENT", ["View passes", "Approve pending application",
                                      "Reject pending application", "Issue pass directly to a user",
                                      "Close an active pass (frees the slot)"])
        if c == 0:
            return
        safe([None, admin_view_passes, admin_approve_application, admin_reject_application,
              admin_issue_pass_directly, admin_close_pass][c])


# ----------------------------------------------------------------------------
# ADMIN: VIOLATIONS & REPORTS
# ----------------------------------------------------------------------------
def flag_pass(pass_id, vtype, reason):
    execute("UPDATE parking_passes SET violation_flag = TRUE WHERE pass_id = %s", (pass_id,))
    execute("INSERT INTO violations (pass_id, violation_type, reason) VALUES (%s, %s, %s)",
            (pass_id, vtype, reason[:255]))


def admin_scan_violations():
    candidates = []

    overstays, _ = fetch(
        """SELECT p.pass_id, u.username, v.plate_no, p.end_time,
                  TIMESTAMPDIFF(MINUTE, p.end_time, NOW())
           FROM parking_passes p JOIN users u ON p.user_id = u.user_id
           JOIN vehicles v ON p.vehicle_id = v.vehicle_id
           WHERE p.status = 'Approved' AND p.end_time < NOW() AND p.violation_flag = FALSE""")
    for pid, uname, plate, end, mins in overstays:
        candidates.append((pid, uname, plate, "Overstay",
                           f"Exceeded end time ({end:%d-%b-%Y %H:%M}) by {mins} minute(s)"))

    zones, _ = fetch(
        """SELECT p.pass_id, u.username, v.plate_no, s.slot_code, s.zone, u.role
           FROM parking_passes p JOIN users u ON p.user_id = u.user_id
           JOIN vehicles v ON p.vehicle_id = v.vehicle_id
           JOIN parking_slots s ON p.slot_id = s.slot_id
           WHERE p.status = 'Approved' AND s.zone <> u.role AND p.violation_flag = FALSE""")
    for pid, uname, plate, code, szone, role in zones:
        candidates.append((pid, uname, plate, "Unauthorized Zone",
                           f"Parked in {szone} zone (slot {code}) but authorized for {role} zone only"))

    if not candidates:
        print("\nNo new violations detected.")
        return
    show([list(c) for c in candidates], ["Pass ID", "User", "Plate", "Violation type", "Details"],
         "Detected Violations")

    choice = ask("Enter Pass ID to flag, 'all' to flag everything, or 0 to cancel: ").lower()
    if choice == "0":
        return
    if choice == "all":
        for pid, _, _, vtype, reason in candidates:
            flag_pass(pid, vtype, reason)
        print(f"{len(candidates)} violation(s) flagged.")
        return
    if not choice.isdigit():
        print("Invalid input.")
        return
    matches = [c for c in candidates if c[0] == int(choice)]
    if not matches:
        print("That Pass ID is not in the detected list.")
        return
    for pid, _, _, vtype, reason in matches:
        custom = ask(f"Reason [{reason}] (Enter to accept): ", required=False)
        flag_pass(pid, vtype, custom or reason)
    print("Violation flagged (violation_flag = TRUE).")


def admin_manual_flag():
    rows, headers = fetch(PASS_SELECT + " WHERE p.status = 'Approved' ORDER BY p.pass_id")
    show(rows, headers, "Active Passes")
    if not rows:
        return
    pid = ask_int("Pass ID to flag (0 to cancel): ", 0)
    if pid == 0:
        return
    if not fetch("SELECT 1 FROM parking_passes WHERE pass_id = %s AND status = 'Approved'", (pid,))[0]:
        print("No active pass with that ID.")
        return
    vtype = ask_choice("Violation type:", ["Overstay", "Unauthorized Zone", "Other"])
    reason = ask("Reason: ")
    flag_pass(pid, vtype, reason)
    print("Violation flagged (violation_flag = TRUE).")


def admin_view_violations():
    rows, headers = fetch(
        """SELECT vi.violation_id, p.pass_id, u.username, ve.plate_no, vi.violation_type,
                  vi.reason, vi.flagged_on, IF(vi.resolved, 'Resolved', 'OPEN') AS status
           FROM violations vi
           JOIN parking_passes p ON vi.pass_id = p.pass_id
           JOIN users u ON p.user_id = u.user_id
           JOIN vehicles ve ON p.vehicle_id = ve.vehicle_id
           ORDER BY vi.resolved, vi.flagged_on DESC""")
    show(rows, headers, "Violation Log")


def admin_resolve_violation():
    rows, headers = fetch(
        """SELECT vi.violation_id, p.pass_id, vi.violation_type, vi.reason, vi.flagged_on
           FROM violations vi JOIN parking_passes p ON vi.pass_id = p.pass_id
           WHERE vi.resolved = FALSE ORDER BY vi.flagged_on""")
    show(rows, headers, "Open Violations")
    if not rows:
        return
    vid = ask_int("Violation ID to resolve (0 to cancel): ", 0)
    if vid == 0:
        return
    info = fetch("SELECT pass_id FROM violations WHERE violation_id = %s AND resolved = FALSE", (vid,))[0]
    if not info:
        print("No open violation with that ID.")
        return
    pid = info[0][0]
    execute("UPDATE violations SET resolved = TRUE WHERE violation_id = %s", (vid,))
    if not fetch("SELECT 1 FROM violations WHERE pass_id = %s AND resolved = FALSE", (pid,))[0]:
        execute("UPDATE parking_passes SET violation_flag = FALSE WHERE pass_id = %s", (pid,))
        print("Violation resolved; flag cleared on the pass.")
    else:
        print("Violation resolved (pass still has other open violations).")


def admin_violation_menu():
    actions = [None, admin_scan_violations, admin_manual_flag, admin_view_violations, admin_resolve_violation]
    while True:
        c = menu("VIOLATION MANAGEMENT",
                 ["Scan for violations (overstay / unauthorized zone)", "Manually flag a pass",
                  "View violation log", "Resolve a violation (clear flag)"])
        if c == 0:
            return
        safe(actions[c])


def admin_revenue_report():
    sql = """
        SELECT s.zone, p.pass_type, COUNT(p.pass_id) AS total_passes, SUM(p.fee) AS total_revenue
        FROM parking_passes p
        JOIN parking_slots s ON p.slot_id = s.slot_id
        WHERE p.status IN ('Approved', 'Closed')
        GROUP BY s.zone, p.pass_type
        ORDER BY s.zone, p.pass_type
    """
    rows, headers = fetch(sql)
    show(rows, headers, "Revenue & Utilization Report (Aggregated)")
    print("\n[Report Derivation]: Derived using SQL JOINs between the parking_passes and parking_slots tables. Grouped by zone and pass_type to summarize pass counts and total fee revenues.")


def admin_view_users():
    rows, headers = fetch(
        """SELECT u.user_id, u.username, u.full_name, u.role, u.phone, COUNT(v.vehicle_id) AS vehicles
           FROM users u LEFT JOIN vehicles v ON u.user_id = v.user_id
           GROUP BY u.user_id ORDER BY u.user_id""")
    show(rows, headers, "Users")
    rows, headers = fetch(
        """SELECT v.vehicle_id, v.plate_no, v.vehicle_type, u.username AS owner
           FROM vehicles v JOIN users u ON v.user_id = u.user_id ORDER BY v.vehicle_id""")
    show(rows, headers, "Vehicles")


def admin_menu(admin):
    print(f"\nWelcome, admin {admin['full_name']}!")
    actions = [None, admin_slot_menu, admin_violation_menu, admin_pass_menu, admin_view_users, admin_revenue_report]
    while True:
        c = menu("ADMIN MENU", ["Slot management", "Violation flagging", "Issue / manage passes",
                                 "View users & vehicles", "Generate Revenue Report"], "Logout")
        if c == 0:
            return
        safe(actions[c])

# ====================================================================================================================================

# superuser
# ====================================================================================================================================
def su_status():
    has_db = db_exists()
    print(f"\nDatabase '{DB_NAME}': {'EXISTS' if has_db else 'NOT CREATED'}")
    present = existing_tables()
    rows = [[t, "exists" if t in present else "missing"] for t in SCHEMA]
    show(rows, ["Table", "Status"], "Project tables")


def su_create_database():
    if db_exists():
        print(f"Database '{DB_NAME}' already exists.")
        return
    execute(f"CREATE DATABASE `{DB_NAME}`")
    use_db(DB_NAME)
    print(f"Database '{DB_NAME}' created.")


def su_drop_database():
    if not db_exists():
        print(f"Database '{DB_NAME}' does not exist.")
        return
    if not confirm(f"This deletes '{DB_NAME}' and ALL its data. Continue?"):
        print("Cancelled.")
        return
    execute(f"DROP DATABASE `{DB_NAME}`")
    print(f"Database '{DB_NAME}' dropped.")


def create_one_table(name):
    present = existing_tables()
    if name in present:
        print(f"Table '{name}' already exists.")
        return False
    missing = [d for d in DEPENDS_ON[name] if d not in present]
    if missing:
        print(f"Cannot create '{name}' yet. Create these first: {', '.join(missing)}")
        return False
    execute(SCHEMA[name])
    seed_table(name)
    print(f"Table '{name}' created.")
    return True


def drop_one_table(name):
    present = existing_tables()
    if name not in present:
        print(f"Table '{name}' does not exist.")
        return False
    blockers = [t for t in present if name in DEPENDS_ON.get(t, [])]
    if blockers:
        print(f"Cannot drop '{name}'. Drop these first: {', '.join(blockers)}")
        return False
    execute(f"DROP TABLE `{name}`")
    print(f"Table '{name}' dropped.")
    return True


def su_require_db():
    if not db_exists():
        print(f"Create the '{DB_NAME}' database first.")
        return False
    use_db(DB_NAME)
    return True


def su_create_table():
    if su_require_db():
        create_one_table(ask_choice("Which table?", list(SCHEMA)))


def su_drop_table():
    if su_require_db():
        name = ask_choice("Which table?", list(SCHEMA))
        if confirm(f"Drop '{name}' and all its data?"):
            drop_one_table(name)


def su_create_all():
    if not su_require_db():
        return
    for name in SCHEMA:  # SCHEMA is ordered parents-first
        if name not in existing_tables():
            create_one_table(name)
    print("All project tables are in place.")
    print("Default admin login: admin / admin123")


def su_drop_all():
    if not su_require_db():
        return
    if not confirm("Drop ALL project tables and their data?"):
        print("Cancelled.")
        return
    for name in reversed(list(SCHEMA)):  # children first
        if name in existing_tables():
            drop_one_table(name)


def superuser_menu():
    items = [
        ("Show setup status", su_status),
        (f"Create database '{DB_NAME}'", su_create_database),
        (f"Drop database '{DB_NAME}'", su_drop_database),
        ("Create a table", su_create_table),
        ("Drop a table", su_drop_table),
        ("Create ALL tables", su_create_all),
        ("Drop ALL tables", su_drop_all),
    ]
    while True:
        c = menu("SUPERUSER MENU", [n for n, _ in items], "Logout")
        if c == 0:
            return
        safe(items[c - 1][1])


def super_login():
    if input("Enter superuser password: ").strip() != SUPER_PASSWORD:
        print("Incorrect superuser password.")
        return
    print("\nSuperuser access granted.")
    superuser_menu()
# ====================================================================================================================================

# login
# ====================================================================================================================================
def login(kind):
    for _ in range(3):
        username = ask("Username: ")
        password = ask("Password: ")
        if kind == "admin":
            rows, _ = fetch("SELECT admin_id, username, full_name FROM admins "
                            "WHERE username = %s AND password = %s", (username, password))
            if rows:
                return {"admin_id": rows[0][0], "username": rows[0][1], "full_name": rows[0][2]}
        else:
            rows, _ = fetch("SELECT user_id, username, full_name, role FROM users "
                            "WHERE username = %s AND password = %s", (username, password))
            if rows:
                return {"user_id": rows[0][0], "username": rows[0][1],
                        "full_name": rows[0][2], "role": rows[0][3]}
        print("Invalid username or password.")
    print("Too many failed attempts.")
    return None


def safe_login(kind):
    try:
        return login(kind)
    except Error as e:
        print(f"\n[Database error] {e}")
        return None


def sign_up():
    print("\n--- New User Sign Up ---")
    while True:
        username = ask("Choose a username: ")
        if fetch("SELECT 1 FROM users WHERE username = %s", (username,))[0]:
            print("That username is taken. Try another.")
        else:
            break
    password = ask("Choose a password: ")
    full_name = ask("Full name: ")
    role = ask_choice("You are a:", ZONES)
    phone = ask("Phone number (optional): ", required=False)
    uid = execute("INSERT INTO users (username, password, full_name, role, phone) VALUES (%s, %s, %s, %s, %s)",
                  (username, password, full_name, role, phone or None))
    print(f"Account created! You are registered as {role} (authorized for the {role} zone).")
    if confirm("Register a vehicle now?"):
        add_vehicle(uid)
# ====================================================================================================================================

# main
# ====================================================================================================================================
def main():
    try:
        connect()
    except Error as e:
        print(f"Could not connect to MySQL: {e}")
        return

    safe(project_ready)
    while True:
        print("\n" + "=" * 46)
        print(f"   {APP_NAME}")
        print("=" * 46)
        print("1. Sign in as Admin")
        print("2. Sign in as User")
        print("3. Sign up (new user)")
        print("4. Exit")
        choice = input("Enter your choice: ").strip().lower()

        if choice == SUPER_KEYWORD:
            super_login()
        elif choice in ("1", "2", "3") and not project_ready():
            continue
        elif choice == "1":
            admin = safe_login("admin")
            if admin:
                admin_menu(admin)
        elif choice == "2":
            user = safe_login("user")
            if user:
                user_menu(user)
        elif choice == "3":
            safe(sign_up)
        elif choice == "4":
            print("Goodbye!")
            break
        else:
            print("Invalid choice. Please enter 1, 2, 3 or 4.")

# ====================================================================================================================================

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nProgram closed.")
    finally:
        if conn is not None and conn.is_connected():
            conn.close()
