from flask import Flask, render_template, request, redirect, url_for, session, g, flash, jsonify, send_file
import sqlite3, random, threading
import datetime
import json
from io import BytesIO

# Added libs for downloads
import openpyxl
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

app = Flask(__name__)
app.secret_key = "your_secret_key"

DATABASE = "users.db"

# ---------------- Database Connection ----------------
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        # Use detect_types to allow datetime parsing if needed; row_factory for named access
        db = g._database = sqlite3.connect(DATABASE, timeout=10, detect_types=sqlite3.PARSE_DECLTYPES)
        db.row_factory = sqlite3.Row
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


# ------------------ Helper functions ------------------

def format_app_number(num):
    return f"PEC{num}"

# Concurrency lock for safety (sqlite BEGIN IMMEDIATE used as DB-level lock as well)
sequence_lock = threading.Lock()

def reserve_new_application_number(coordinator_name=None):
    """
    Reserves the next continuous application number atomically and
    inserts a 'reserved' applications row so it is not available to others.
    Returns the application number string (e.g., PEC4880) and numeric part.
    """
    # We'll open a new sqlite connection here and use BEGIN IMMEDIATE to lock
    conn = sqlite3.connect(DATABASE, timeout=10)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    try:
        # Begin immediate transaction to acquire RESERVED lock (prevents concurrent writes)
        cur.execute("BEGIN IMMEDIATE")
        # Ensure application_sequence exists (normally created in init_db)
        cur.execute("SELECT last_number FROM application_sequence WHERE id = 1")
        row = cur.fetchone()
        if row is None:
            # If not present, attempt to initialize based on current max application_number in applications table
            cur.execute("SELECT MAX(CAST(SUBSTR(application_number,4) AS INTEGER)) as mx FROM applications")
            r2 = cur.fetchone()
            mx = r2["mx"] if r2 and r2["mx"] is not None else None
            start = 4879
            if mx is not None:
                # ensure sequence starts at least at current max
                start = max(start, mx)
            cur.execute("INSERT OR REPLACE INTO application_sequence (id, last_number) VALUES (1, ?)", (start,))
            last_num = start
        else:
            last_num = int(row["last_number"])

        new_num = last_num + 1

        # update sequence
        cur.execute("UPDATE application_sequence SET last_number = ? WHERE id = 1", (new_num,))

        application_number = format_app_number(new_num)
        now = datetime.datetime.utcnow().isoformat(sep=' ', timespec='seconds')

        # Make sure applications table has required optional columns
        # Insert reserved row (status reserved). We'll attempt to insert necessary columns if present.
        # We'll use form_data column to store JSON (if exists), otherwise leave NULL.
        # Ensure columns exist in init_db; but safe-guard here with try/except.
        try:
            cur.execute("""
                INSERT INTO applications (application_number, numeric_part, coordinator, status, date_opened)
                VALUES (?, ?, ?, 'reserved', ?)
            """, (application_number, new_num, coordinator_name or '', now))
        except sqlite3.OperationalError:
            # If columns do not exist (older schema), try basic insert into legacy table (application_number only + names blank)
            # This ensures no crash; but init_db will upgrade schema on next run.
            try:
                cur.execute("""
                    INSERT INTO applications (application_number)
                    VALUES (?)
                """, (application_number,))
            except Exception:
                # fallback: if even this fails, rollback and raise
                conn.rollback()
                raise

        conn.commit()
        return application_number, new_num
    except Exception as e:
        conn.rollback()
        raise
    finally:
        conn.close()


def finalize_save_application(application_number, student_name, father_name, preferred_branch, form_data=None):
    """
    Finalize (save) the application: update reserved row to submitted and add fields.
    If reservation doesn't exist, create a new submitted row.
    """
    db = sqlite3.connect(DATABASE, timeout=10)
    db.row_factory = sqlite3.Row
    cur = db.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        # Check if application exists
        cur.execute("SELECT id FROM applications WHERE application_number = ?", (application_number,))
        row = cur.fetchone()
        now = datetime.datetime.utcnow().isoformat(sep=' ', timespec='seconds')
        if row:
            # update existing reserved row
            try:
                cur.execute("""
                    UPDATE applications
                    SET student_name=?, father_name=?, preferred_branch=?, status='submitted',
                        form_data=?, date_submitted=?
                    WHERE application_number=?
                """, (
                    student_name, father_name, preferred_branch,
                    json.dumps(form_data) if form_data is not None else None,
                    now, application_number
                ))
            except sqlite3.OperationalError:
                # fallback older schema: update only existing columns if present
                cur.execute("""
                    UPDATE applications
                    SET student_name=?, father_name=?, preferred_branch=?
                    WHERE application_number=?
                """, (student_name, father_name, preferred_branch, application_number))
        else:
            # If not found (no reservation), create a new submitted row
            numeric_part = None
            try:
                numeric_part = int(application_number.replace('PEC',''))
            except:
                numeric_part = None
            try:
                cur.execute("""
                    INSERT INTO applications (application_number, numeric_part, student_name, father_name, preferred_branch, status, form_data, date_opened, date_submitted)
                    VALUES (?, ?, ?, ?, ?, 'submitted', ?, ?, ?)
                """, (application_number, numeric_part, student_name, father_name, preferred_branch,
                      json.dumps(form_data) if form_data is not None else None, now, now))
            except sqlite3.OperationalError:
                # fallback minimal insert
                cur.execute("""
                    INSERT INTO applications (application_number, student_name, father_name, preferred_branch)
                    VALUES (?, ?, ?, ?)
                """, (application_number, student_name, father_name, preferred_branch))
        db.commit()
    except Exception as e:
        db.rollback()
        raise
    finally:
        db.close()


# ---------------- Home ----------------
@app.route('/')
def home():
    return render_template('index.html')


# ---------------- Admin ----------------
@app.route('/admin')
def admin_page():
    return render_template('admin_login.html')


@app.route('/admin_login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        email = request.form['email'].strip()
        password = request.form['password'].strip()
        db = get_db()
        cursor = db.cursor()
        cursor.execute("SELECT id, first_name, last_name, email FROM admins WHERE email=? AND password=?",
                       (email, password))
        user = cursor.fetchone()
        if user:
            session['admin_id'] = user[0]
            session['admin_name'] = f"{user[1]} {user[2]}"
            session['admin_email'] = user[3]
            return redirect(url_for('admin_dashboard'))
        else:
            flash("Invalid Admin credentials", "error")
            return redirect(url_for('admin_login'))
    return render_template('admin_login.html')


@app.route('/admin_dashboard')
def admin_dashboard():
    if 'admin_id' in session:
        db = get_db()
        cursor = db.cursor()
        cursor.execute("SELECT work FROM admins WHERE id=?", (session['admin_id'],))
        row = cursor.fetchone()
        admin_work = row[0] if row else ""

        # Fetch all coordinators
        cursor.execute("SELECT first_name, last_name, email, phone, work FROM coordinators")
        coordinators = cursor.fetchall() or []

        return render_template(
            'admin_dashboard.html',
            work=admin_work,
            coordinators=coordinators,
            admin_name=session.get('admin_name')
        )
    return redirect(url_for('admin_page'))


@app.route('/save_admin_work', methods=['POST'])
def save_admin_work():
    if 'admin_id' in session:
        work = request.form['work']
        db = get_db()
        cursor = db.cursor()
        cursor.execute("UPDATE admins SET work=? WHERE id=?", (work, session['admin_id']))
        db.commit()
    return redirect(url_for('admin_dashboard'))


# ---------------- Coordinator ----------------
@app.route('/coordinator')
def coordinator_page():
    return render_template('coordinator_login.html')


@app.route('/coordinator_login', methods=['POST'])
def coordinator_login():
    email = request.form['email']
    password = request.form['password']
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id, first_name, last_name, email FROM coordinators WHERE email=? AND password=?",
                   (email, password))
    user = cursor.fetchone()
    if user:
        session['coordinator_id'] = user[0]
        session['coordinator_name'] = f"{user[1]} {user[2]}"
        session['coordinator_email'] = user[3]
        return redirect(url_for('coordinator_dashboard'))
    flash("Invalid Coordinator credentials", "error")
    return redirect(url_for('coordinator_page'))


@app.route('/coordinator_signup', methods=['GET', 'POST'])
def coordinator_signup():
    if request.method == 'POST':
        first_name = request.form['first_name']
        last_name = request.form['last_name']
        email = request.form['email']
        phone = request.form['phone']
        password = request.form['password']

        db = get_db()
        cursor = db.cursor()
        try:
            cursor.execute("""
                INSERT INTO coordinators (first_name, last_name, email, phone, password, work) 
                VALUES (?,?,?,?,?,?)
            """, (first_name, last_name, email, phone, password, ""))
            db.commit()
            flash("Coordinator account created successfully! Please log in.", "success")
            return redirect(url_for('coordinator_page'))
        except sqlite3.IntegrityError:
            flash("Email already exists. Please use a different email.", "error")
            return redirect(url_for('coordinator_signup'))
    return render_template('coordinator_signup.html')


@app.route('/coordinator_dashboard')
def coordinator_dashboard():
    if 'coordinator_id' not in session:
        return redirect(url_for('coordinator_page'))

    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT first_name, last_name, email, phone, work
        FROM coordinators WHERE id=?
    """, (session['coordinator_id'],))
    row = cursor.fetchone()

    if row:
        coordinator_data = {
            "first_name": row[0],
            "last_name": row[1],
            "email": row[2],
            "phone": row[3],
            "work": row[4],
            "photo": None
        }
    else:
        coordinator_data = {
            "first_name": "",
            "last_name": "",
            "email": "",
            "phone": "",
            "work": "",
            "photo": None
        }

    return render_template(
        'coordinator_dashboard.html',
        coordinator_data=coordinator_data
    )
# ...existing code...
@app.route('/get_coordinator_applications')
def get_coordinator_applications():
    """
    Return JSON list of applications for the logged-in coordinator.
    """
    if 'coordinator_id' not in session:
        return jsonify({"applications": []}), 200

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            SELECT application_number, student_name, father_name, preferred_branch,
                   mobile, address, status, date_submitted, form_data
            FROM applications
            WHERE coordinator = ?
            ORDER BY id DESC
        """, (session.get('coordinator_name', ''),))
        rows = cur.fetchall()
        apps = []
        for r in rows:
            rd = dict(r)
            # parse form_data JSON if present
            form_json = None
            if rd.get('form_data'):
                try:
                    form_json = json.loads(rd['form_data'])
                except Exception:
                    form_json = None
            apps.append({
                "application_number": rd.get('application_number') or "",
                "student_name": rd.get('student_name') or (form_json.get('student_name') if form_json else "") ,
                "father_name": rd.get('father_name') or (form_json.get('father_name') if form_json else ""),
                "preferred_branch": rd.get('preferred_branch') or (form_json.get('preferred_branch') if form_json else ""),
                "mobile": rd.get('mobile') or (form_json.get('mobile') if form_json else ""),
                "address": rd.get('address') or (form_json.get('address') if form_json else ""),
                "status": rd.get('status'),
                "date_submitted": rd.get('date_submitted')
            })
        return jsonify({"applications": apps}), 200
    except Exception as e:
        return jsonify({"error": str(e), "applications": []}), 500
# ...existing code...


@app.route('/save_coordinator_work', methods=['POST'])
def save_coordinator_work():
    if 'coordinator_id' in session:
        work = request.form['work']
        db = get_db()
        cursor = db.cursor()
        cursor.execute("UPDATE coordinators SET work=? WHERE id=?", (work, session['coordinator_id']))
        db.commit()
    return redirect(url_for('coordinator_dashboard'))


# ---------------- Application Form ----------------
# Add this route after your existing routes
# ...existing code...
@app.route('/save_application', methods=['POST'])
def save_application():
    if 'coordinator_id' not in session:
        return jsonify({"error": "Not authorized"}), 401

    data = request.get_json()
    db = get_db()
    cursor = db.cursor()

    try:
        # Check if application exists
        cursor.execute("""
            SELECT id FROM applications 
            WHERE application_number = ?
        """, (data['application_number'],))
        exists = cursor.fetchone()

        if exists:
            # Try update with full schema
            try:
                cursor.execute("""
                    UPDATE applications 
                    SET student_name = ?,
                        father_name = ?,
                        preferred_branch = ?,
                        mobile = ?,
                        address = ?,
                        status = 'submitted',
                        last_modified = CURRENT_TIMESTAMP
                    WHERE application_number = ?
                """, (
                    data.get('student_name'),
                    data.get('father_name'),
                    data.get('preferred_branch'),
                    data.get('mobile'),
                    data.get('address'),
                    data['application_number']
                ))
            except sqlite3.OperationalError:
                # Fallback update for older schema without mobile/address columns
                cursor.execute("""
                    UPDATE applications 
                    SET student_name = ?,
                        father_name = ?,
                        preferred_branch = ?,
                        status = 'submitted',
                        last_modified = CURRENT_TIMESTAMP
                    WHERE application_number = ?
                """, (
                    data.get('student_name'),
                    data.get('father_name'),
                    data.get('preferred_branch'),
                    data['application_number']
                ))
        else:
            # Try insert with full schema
            try:
                cursor.execute("""
                    INSERT INTO applications (
                        application_number,
                        student_name,
                        father_name,
                        preferred_branch,
                        mobile,
                        address,
                        status,
                        coordinator,
                        date_submitted
                    ) VALUES (?, ?, ?, ?, ?, ?, 'submitted', ?, CURRENT_TIMESTAMP)
                """, (
                    data.get('application_number'),
                    data.get('student_name'),
                    data.get('father_name'),
                    data.get('preferred_branch'),
                    data.get('mobile'),
                    data.get('address'),
                    session.get('coordinator_name', '')
                ))
            except sqlite3.OperationalError:
                # Fallback insert for older schema without mobile/address/date_submitted columns
                cursor.execute("""
                    INSERT INTO applications (
                        application_number,
                        student_name,
                        father_name,
                        preferred_branch,
                        status,
                        coordinator
                    ) VALUES (?, ?, ?, ?, 'submitted', ?)
                """, (
                    data.get('application_number'),
                    data.get('student_name'),
                    data.get('father_name'),
                    data.get('preferred_branch'),
                    session.get('coordinator_name', '')
                ))

        db.commit()
        return jsonify({"success": True}), 200

    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
# ...existing code...
@app.route('/application_form', methods=['GET', 'POST'])
def application_form():
    if 'coordinator_id' not in session:
        flash("Please log in as coordinator to access the form", "error")
        return redirect(url_for('coordinator_page'))

    db = get_db()
    cursor = db.cursor()

    if request.method == 'POST':
        # On save: finalize the reserved application_number (submitted)
        app_number = request.form.get('application_number')
        student_name = request.form.get('student_name', '').strip()
        father_name = request.form.get('father_name', '').strip()
        preferred_branch = request.form.get('preferred_branch', '').strip()

        if not app_number:
            flash("No application number found. Please reopen the form.", "error")
            return redirect(url_for('application_form'))

        if not student_name or not father_name:
            flash("Please fill all required fields!", "error")
            # Re-render form with values (app_number preserved)
            return render_template('form.html', app_number=app_number, student_name=student_name,
                                   father_name=father_name, preferred_branch=preferred_branch)

        # Optionally gather any additional fields into form_data
        form_data = {
            # add more fields here if your form has them
        }

        try:
            finalize_save_application(app_number, student_name, father_name, preferred_branch, form_data=form_data)
        except Exception as e:
            flash(f"Error saving application: {e}", "error")
            return redirect(url_for('application_form'))

        flash(f"Application saved successfully! Application No: {app_number}", "success")
        return render_template('form.html', app_number=app_number, student_name=student_name,
                               father_name=father_name, preferred_branch=preferred_branch)

    # GET: when opening the form, reserve a new application number and show it on form
    try:
        coordinator_name = session.get('coordinator_name', '')
        app_number, numeric_part = reserve_new_application_number(coordinator_name=coordinator_name)
        # Render form with reserved number displayed and set into hidden field
        return render_template('form.html', app_number=app_number)
    except Exception as e:
        flash(f"Could not reserve application number: {e}", "error")
        # Fall back to previous behavior of random number (safe fallback)
        app_number = f"PEC{random.randint(1000,9999)}"
        return render_template('form.html', app_number=app_number)


@app.route('/delete_reserved_application', methods=['POST'])
def delete_reserved_application():
    data = request.get_json()
    appnum = data.get('application_number')
    if not appnum:
        return jsonify({"success": False, "error": "application_number required"}), 400

    db = get_db()
    cur = db.cursor()
    try:
        # Delete only if status is 'reserved'
        cur.execute("DELETE FROM applications WHERE application_number=? AND status='reserved'", (appnum,))
        db.commit()
        return jsonify({"success": True, "message": "Reserved application deleted"}), 200
    except Exception as e:
        db.rollback()
        return jsonify({"success": False, "error": str(e)}), 500




# ---------------- Search, Edit, Delete APIs ----------------

@app.route('/search_application', methods=['GET'])
def search_application():
    """
    Search by application_number (query param: application_number) and return JSON.
    """
    appnum = request.args.get('application_number', '').strip()
    if not appnum:
        return jsonify({"success": False, "error": "application_number query param required"}), 400

    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT id, application_number, numeric_part, coordinator, status,
               student_name, father_name, preferred_branch,
               form_data, date_opened, date_submitted, last_modified
        FROM applications WHERE application_number = ?
    """, (appnum,))
    row = cur.fetchone()
    if not row:
        return jsonify({"success": True, "found": False, "data": None}), 200

    data = dict(row)
    # Parse form_data JSON if present
    if data.get('form_data'):
        try:
            data['form_data'] = json.loads(data['form_data'])
        except Exception:
            pass
    return jsonify({"success": True, "found": True, "data": data}), 200


@app.route('/edit_application', methods=['POST'])
def edit_application():
    """
    Edit an application. Expects JSON or form data including application_number and fields to update.
    Fields supported: student_name, father_name, preferred_branch, form_data
    """
    data = request.get_json() or request.form
    appnum = data.get('application_number')
    if not appnum:
        return jsonify({"success": False, "error": "application_number required"}), 400

    fields = {}
    if 'student_name' in data:
        fields['student_name'] = data.get('student_name')
    if 'father_name' in data:
        fields['father_name'] = data.get('father_name')
    if 'preferred_branch' in data:
        fields['preferred_branch'] = data.get('preferred_branch')
    if 'form_data' in data:
        # ensure JSON string
        try:
            fields['form_data'] = json.dumps(data.get('form_data')) if not isinstance(data.get('form_data'), str) else data.get('form_data')
        except Exception:
            fields['form_data'] = data.get('form_data')

    if not fields:
        return jsonify({"success": False, "error": "No updatable fields provided"}), 400

    # Build SET clause
    set_clause = ", ".join([f"{k} = ?" for k in fields.keys()])
    params = list(fields.values())
    params.append(appnum)

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(f"UPDATE applications SET {set_clause}, last_modified = ? WHERE application_number = ?", (*list(fields.values()), datetime.datetime.utcnow().isoformat(sep=' ', timespec='seconds'), appnum))
        db.commit()
        return jsonify({"success": True, "message": "Updated"}), 200
    except Exception as e:
        db.rollback()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/delete_application', methods=['POST'])
def delete_application():
    """
    Delete an application. Expects form param or json: application_number
    """
    data = request.get_json() or request.form
    appnum = data.get('application_number')
    if not appnum:
        return jsonify({"success": False, "error": "application_number required"}), 400

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("DELETE FROM applications WHERE application_number = ?", (appnum,))
        db.commit()
        return jsonify({"success": True, "message": "Deleted"}), 200
    except Exception as e:
        db.rollback()
        return jsonify({"success": False, "error": str(e)}), 500


def get_date_column(table_name):
    conn = sqlite3.connect('your_database.db')
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table_name})")
    columns = [col[1] for col in cur.fetchall()]
    conn.close()
    
    # Check for likely date columns
    for col in ['date_submitted', 'submission_date', 'created_at']:
        if col in columns:
            return col
    return None

@app.route("/check_data")
def check_data():
    start = request.args.get("start_date")
    end = request.args.get("end_date")
    
    if not start or not end:
        return jsonify({"error": "Start and end dates required"}), 400

    table_name = "applications"  # Change to your actual table name
    date_col = get_date_column(table_name)
    
    if not date_col:
        return jsonify({"error": "No date column found in table"}), 500

    try:
        conn = sqlite3.connect('your_database.db')
        cur = conn.cursor()
        query = f"""
            SELECT COUNT(*) as count FROM {table_name}
            WHERE {date_col} BETWEEN ? AND ?
        """
        cur.execute(query, (start + " 00:00:00", end + " 23:59:59"))
        count = cur.fetchone()[0]
        conn.close()
        return jsonify({"count": count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/download_excel', methods=['GET'])
def download_excel():
    start = request.args.get('start_date')
    end = request.args.get('end_date')
    chart = request.args.get('chart', '0')
    db = get_db()
    cur = db.cursor()

    cur.execute("SELECT * FROM applications WHERE date_submitted BETWEEN ? AND ?",
                (start + " 00:00:00", end + " 23:59:59"))
    rows = cur.fetchall()

    if not rows:
        return "No data found for the selected dates.", 404

    import openpyxl
    from io import BytesIO
    from openpyxl.chart import PieChart, Reference

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Applications"

    headers = ["Application No", "Student Name", "Father Name", "Mobile", "Address", "Department", "Form Data", "Date Submitted"]
    ws.append(headers)

    dept_count = {}
    for r in rows:
        rdict = dict(r)
        form_json = rdict.get('form_data') or ''
        ws.append([
            rdict.get('application_number'),
            rdict.get('student_name'),
            rdict.get('father_name'),
            rdict.get('mobile'),
            rdict.get('address'),
            rdict.get('preferred_branch'),
            form_json,
            rdict.get('date_submitted')
        ])
        dept = rdict.get('preferred_branch')
        if dept:
            dept_count[dept] = dept_count.get(dept, 0) + 1

    if chart == '1' and dept_count:
        ws_chart = wb.create_sheet(title="Department Pie Chart")
        ws_chart.append(["Department", "Count"])
        for dept, count in dept_count.items():
            ws_chart.append([dept, count])
        pie = PieChart()
        data = Reference(ws_chart, min_col=2, min_row=1, max_row=len(dept_count)+1)
        labels = Reference(ws_chart, min_col=1, min_row=2, max_row=len(dept_count)+1)
        pie.add_data(data, titles_from_data=True)
        pie.set_categories(labels)
        pie.title = "Students by Department"
        ws_chart.add_chart(pie, "E5")

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    from flask import send_file
    return send_file(buf, as_attachment=True, download_name=f"applications_{start}_{end}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

@app.route('/download_pdf', methods=['GET'])
def download_pdf():
    start = request.args.get('start_date')
    end = request.args.get('end_date')
    db = get_db()
    cur = db.cursor()

    cur.execute("SELECT * FROM applications WHERE date_submitted BETWEEN ? AND ?",
                (start + " 00:00:00", end + " 23:59:59"))
    rows = cur.fetchall()

    if not rows:
        return "No data found for the selected dates.", 404

    from io import BytesIO
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    x_margin = 40
    y = height - 50
    line_height = 14

    headers = ["App No", "Student", "Father", "Mobile", "Address", "Dept", "Date Submitted"]
    p.setFont("Helvetica-Bold", 9)
    x_positions = [x_margin + i*70 for i in range(len(headers))]
    for i, h in enumerate(headers):
        p.drawString(x_positions[i], y, h)
    y -= line_height
    p.setFont("Helvetica", 9)

    for r in rows:
        rdict = dict(r)
        rowvals = [
            rdict.get('application_number') or '',
            rdict.get('student_name') or '',
            rdict.get('father_name') or '',
            rdict.get('mobile') or '',
            rdict.get('address') or '',
            rdict.get('preferred_branch') or '',
            rdict.get('date_submitted') or ''
        ]
        for i, val in enumerate(rowvals):
            p.drawString(x_positions[i], y, str(val)[:12])
        y -= line_height
        if y < 60:
            p.showPage()
            y = height - 50
            p.setFont("Helvetica-Bold", 9)
            for i, h in enumerate(headers):
                p.drawString(x_positions[i], y, h)
            y -= line_height
            p.setFont("Helvetica", 9)

    p.save()
    buffer.seek(0)
    from flask import send_file
    return send_file(buffer, as_attachment=True, download_name=f"applications_{start}_{end}.pdf", mimetype="application/pdf")

@app.route('/search_students')
def search_students():
    if 'coordinator_id' not in session:
        return jsonify({"error": "Not authorized"}), 401
        
    search_term = request.args.get('term', '').lower()
    
    try:
        db = get_db()
        cursor = db.cursor()
        
        cursor.execute("""
            SELECT * FROM applications 
            WHERE (LOWER(student_name) LIKE ? OR 
                  LOWER(application_number) LIKE ?) AND
                  coordinator = ?
        """, (f'%{search_term}%', f'%{search_term}%', session.get('coordinator_name', '')))
        
        students = []
        for row in cursor.fetchall():
            students.append({
                'application_number': row['application_number'],
                'student_name': row['student_name'],
                'father_name': row['father_name'],
                'preferred_branch': row['preferred_branch'],
                'mobile': row['mobile'],
                'address': row['address'],
                'next_visit': row.get('next_visit')
            })
            
        return jsonify({"students": students}), 200
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------- Logout ----------------
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))


# ---------------- Database Setup ----------------
def init_db():
    """
    Initialize or upgrade the database schema safely.
    """
    with sqlite3.connect(DATABASE, timeout=10) as db:
        cursor = db.cursor()

        # Admins
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                first_name TEXT,
                last_name TEXT,
                email TEXT UNIQUE,
                phone TEXT,
                password TEXT,
                work TEXT
            )
        """)

        # Coordinators
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS coordinators (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                first_name TEXT,
                last_name TEXT,
                email TEXT UNIQUE,
                phone TEXT,
                password TEXT,
                work TEXT
            )
        """)

        # Applications table with all needed columns
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_number TEXT,
                numeric_part INTEGER,
                coordinator TEXT,
                status TEXT,
                student_name TEXT,
                father_name TEXT,
                preferred_branch TEXT,
                mobile TEXT,
                address TEXT,
                form_data TEXT,
                date_opened TEXT,
                date_submitted TEXT,
                last_modified TEXT
            )
        """)

        # Sequence table for continuous application numbers
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS application_sequence (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_number INTEGER NOT NULL
            )
        """)

        # Initialize sequence if empty
        cursor.execute("SELECT COUNT(*) as cnt FROM application_sequence")
        if cursor.fetchone()['cnt'] == 0:
            cursor.execute("SELECT MAX(CAST(SUBSTR(application_number,4) AS INTEGER)) as mx FROM applications")
            r = cursor.fetchone()
            start = 4879
            if r and r['mx'] is not None:
                start = max(start, r['mx'])
            cursor.execute("INSERT INTO application_sequence (id, last_number) VALUES (1, ?)", (start,))

        # Add missing columns safely
        def add_column_if_not_exists(table, column_def):
            col_name = column_def.split()[0]
            try:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")
                print(f"Added column {col_name} to {table}")  # Debug log
            except sqlite3.OperationalError:
                pass

        # Add all possible missing columns
        add_column_if_not_exists("applications", "status TEXT")
        add_column_if_not_exists("applications", "mobile TEXT")
        add_column_if_not_exists("applications", "address TEXT")
        add_column_if_not_exists("applications", "date_submitted TEXT")
        add_column_if_not_exists("applications", "last_modified TEXT")

        # Default admin
        cursor.execute("SELECT * FROM admins WHERE email=?", ("admin@example.com",))
        if not cursor.fetchone():
            cursor.execute("""
                INSERT INTO admins (first_name, last_name, email, phone, password, work)
                VALUES (?, ?, ?, ?, ?, ?)
            """, ("Default", "Admin", "admin@example.com", "0000000000", "admin123", ""))

        db.commit()


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
