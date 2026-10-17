
import os
import sqlite3
from datetime import datetime
from functools import wraps

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    g,
)
from werkzeug.security import check_password_hash, generate_password_hash


# --------------------------------------------------
# FLASK CONFIGURATION
# --------------------------------------------------

app = Flask(__name__)
app.secret_key = os.environ.get(
    "SECRET_KEY",
    "change-this-secret-key-before-deployment"
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, "hospital.db")

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Admin@123")

WAITING_STATUSES = ("Waiting", "In Progress")
ALL_STATUSES = ("Waiting", "In Progress", "Completed", "Cancelled")

DEPARTMENTS = [
    "General Medicine",
    "Cardiology",
    "Orthopedics",
    "Neurology",
    "Pediatrics",
    "Dermatology",
    "ENT",
    "Other",
]

GENDERS = ["Male", "Female", "Other"]


# --------------------------------------------------
# DATABASE CONNECTION
# --------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# --------------------------------------------------
# DATABASE INITIALIZATION
# --------------------------------------------------

def init_db():
    connection = sqlite3.connect(DATABASE)

    try:
        connection.execute("PRAGMA foreign_keys = ON")

        connection.execute("""
            CREATE TABLE IF NOT EXISTS patients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                age INTEGER NOT NULL,
                gender TEXT NOT NULL,
                phone TEXT NOT NULL,
                department TEXT NOT NULL,
                symptoms TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Waiting',
                created_at TEXT NOT NULL
            )
        """)

        connection.commit()
    finally:
        connection.close()


init_db()


# --------------------------------------------------
# ADMIN AUTHENTICATION
# --------------------------------------------------

def admin_required(view_function):
    @wraps(view_function)
    def wrapped_view(*args, **kwargs):
        if not session.get("admin_logged_in"):
            flash("Please log in to access the admin dashboard.", "error")
            return redirect(url_for("admin_login"))
        return view_function(*args, **kwargs)

    return wrapped_view


# --------------------------------------------------
# TOKEN GENERATION
# --------------------------------------------------

def generate_token():
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"OT-{today}-"

    db = get_db()

    row = db.execute(
        """
        SELECT token
        FROM patients
        WHERE token LIKE ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (prefix + "%",),
    ).fetchone()

    if row:
        try:
            last_number = int(row["token"].split("-")[-1])
        except (ValueError, IndexError):
            last_number = 0
    else:
        last_number = 0

    return f"{prefix}{last_number + 1:03d}"


# --------------------------------------------------
# WAITING-TIME ESTIMATION
# --------------------------------------------------

def estimate_wait(patient):
    status = patient["status"]

    if status == "Completed":
        return "Your consultation is complete."

    if status == "Cancelled":
        return "This token has been cancelled."

    db = get_db()

    ahead = db.execute(
        """
        SELECT COUNT(*) AS total
        FROM patients
        WHERE id < ?
          AND status IN (?, ?)
          AND department = ?
        """,
        (
            patient["id"],
            "Waiting",
            "In Progress",
            patient["department"],
        ),
    ).fetchone()["total"]

    if status == "In Progress":
        return "Your consultation is in progress."

    estimated_minutes = ahead * 15

    if estimated_minutes == 0:
        return "You are next in the queue."

    return f"Approximately {estimated_minutes} minutes"


# --------------------------------------------------
# HOME PAGE
# --------------------------------------------------

@app.route("/")
def home():
    db = get_db()

    total = db.execute(
        "SELECT COUNT(*) AS count FROM patients"
    ).fetchone()["count"]

    waiting = db.execute(
        "SELECT COUNT(*) AS count FROM patients WHERE status = ?",
        ("Waiting",),
    ).fetchone()["count"]

    in_progress = db.execute(
        "SELECT COUNT(*) AS count FROM patients WHERE status = ?",
        ("In Progress",),
    ).fetchone()["count"]

    return render_template(
        "index.html",
        total=total,
        waiting=waiting,
        in_progress=in_progress,
    )


# --------------------------------------------------
# PATIENT REGISTRATION
# --------------------------------------------------

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        age_value = request.form.get("age", "").strip()
        gender = request.form.get("gender", "").strip()
        phone = request.form.get("phone", "").strip()
        department = request.form.get("department", "").strip()
        symptoms = request.form.get("symptoms", "").strip()

        if not all([name, age_value, gender, phone, department, symptoms]):
            flash("Please fill in all the required fields.", "error")
            return redirect(url_for("register"))

        try:
            age = int(age_value)
        except ValueError:
            flash("Please enter a valid age.", "error")
            return redirect(url_for("register"))

        if not 0 <= age <= 120:
            flash("Age must be between 0 and 120.", "error")
            return redirect(url_for("register"))

        if gender not in GENDERS:
            flash("Please select a valid gender.", "error")
            return redirect(url_for("register"))

        if department not in DEPARTMENTS:
            flash("Please select a valid department.", "error")
            return redirect(url_for("register"))

        db = get_db()
        token = generate_token()
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            cursor = db.execute(
                """
                INSERT INTO patients (
                    token, name, age, gender, phone,
                    department, symptoms, status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token,
                    name,
                    age,
                    gender,
                    phone,
                    department,
                    symptoms,
                    "Waiting",
                    created_at,
                ),
            )
            db.commit()

        except sqlite3.IntegrityError:
            flash("Unable to generate a unique token. Please try again.", "error")
            return redirect(url_for("register"))

        patient = db.execute(
            "SELECT * FROM patients WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()

        return render_template(
            "patient_status.html",
            patient=patient,
            estimated_wait=estimate_wait(patient),
        )

    return render_template(
        "register.html",
        departments=DEPARTMENTS,
        genders=GENDERS,
    )


# --------------------------------------------------
# PATIENT STATUS LOOKUP
# --------------------------------------------------

@app.route("/status", methods=["GET", "POST"])
def patient_status():
    patient = None
    estimated_wait = None

    if request.method == "POST":
        token = request.form.get("token", "").strip().upper()

        if not token:
            flash("Please enter your patient token.", "error")
            return redirect(url_for("patient_status"))

        db = get_db()

        patient = db.execute(
            "SELECT * FROM patients WHERE token = ?",
            (token,),
        ).fetchone()

        if patient is None:
            flash("No patient was found with that token.", "error")
        else:
            estimated_wait = estimate_wait(patient)

    return render_template(
        "patient_status.html",
        patient=patient,
        estimated_wait=estimated_wait,
    )


# --------------------------------------------------
# ADMIN LOGIN
# --------------------------------------------------

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("admin_logged_in"):
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if (
            username == ADMIN_USERNAME
            and password == ADMIN_PASSWORD
        ):
            session.clear()
            session["admin_logged_in"] = True
            session["admin_username"] = username

            flash("Login successful.", "success")
            return redirect(url_for("admin_dashboard"))

        flash("Invalid username or password.", "error")

    return render_template("admin_login.html")


# --------------------------------------------------
# ADMIN DASHBOARD
# --------------------------------------------------

@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    db = get_db()

    patients = db.execute(
        """
        SELECT *
        FROM patients
        ORDER BY
            CASE status
                WHEN 'In Progress' THEN 1
                WHEN 'Waiting' THEN 2
                WHEN 'Completed' THEN 3
                WHEN 'Cancelled' THEN 4
                ELSE 5
            END,
            id ASC
        """
    ).fetchall()

    total = db.execute(
        "SELECT COUNT(*) AS count FROM patients"
    ).fetchone()["count"]

    waiting = db.execute(
        "SELECT COUNT(*) AS count FROM patients WHERE status = ?",
        ("Waiting",),
    ).fetchone()["count"]

    in_progress = db.execute(
        "SELECT COUNT(*) AS count FROM patients WHERE status = ?",
        ("In Progress",),
    ).fetchone()["count"]

    completed = db.execute(
        "SELECT COUNT(*) AS count FROM patients WHERE status = ?",
        ("Completed",),
    ).fetchone()["count"]

    cancelled = db.execute(
        "SELECT COUNT(*) AS count FROM patients WHERE status = ?",
        ("Cancelled",),
    ).fetchone()["count"]

    return render_template(
        "dashboard.html",
        patients=patients,
        total=total,
        waiting=waiting,
        in_progress=in_progress,
        completed=completed,
        cancelled=cancelled,
        statuses=ALL_STATUSES,
    )


# --------------------------------------------------
# UPDATE PATIENT STATUS
# --------------------------------------------------

@app.route("/admin/update/<int:patient_id>", methods=["POST"])
@admin_required
def update_patient_status(patient_id):
    new_status = request.form.get("status", "").strip()

    if new_status not in ALL_STATUSES:
        flash("Invalid patient status.", "error")
        return redirect(url_for("admin_dashboard"))

    db = get_db()

    patient = db.execute(
        "SELECT id FROM patients WHERE id = ?",
        (patient_id,),
    ).fetchone()

    if patient is None:
        flash("Patient not found.", "error")
        return redirect(url_for("admin_dashboard"))

    db.execute(
        "UPDATE patients SET status = ? WHERE id = ?",
        (new_status, patient_id),
    )
    db.commit()

    flash("Patient status updated successfully.", "success")
    return redirect(url_for("admin_dashboard"))


# --------------------------------------------------
# ADMIN LOGOUT
# --------------------------------------------------

@app.route("/admin/logout")
def admin_logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("home"))


# --------------------------------------------------
# RUN APPLICATION
# --------------------------------------------------

if __name__ == "__main__":
    print("Starting Hospital Operation Management System...")
    app.run(debug=True)
