import csv
import io
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from functools import wraps
from xml.sax.saxutils import escape

from flask import (
    Blueprint,
    abort,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import selectinload

from medicare.extensions import db
from medicare.forms import (
    AppointmentStatusForm,
    BillEditForm,
    BillForm,
    DepartmentEditForm,
    DepartmentForm,
    DoctorCreateForm,
    DoctorEditForm,
    PatientAccountForm,
    PaymentForm,
    ReportFilterForm,
)
from medicare.models import (
    Appointment,
    Bill,
    Department,
    Doctor,
    MedicalRecord,
    Patient,
    Payment,
    User,
)

bp = Blueprint("admin", __name__, url_prefix="/admin")


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user.role != "admin":
            abort(403)
        return view(*args, **kwargs)

    return wrapped


@bp.get("/")
@admin_required
def dashboard():
    counts = db.session.execute(
        select(
            select(func.count(Patient.id)).scalar_subquery(),
            select(func.count(Doctor.id)).scalar_subquery(),
            select(func.count(Appointment.id)).scalar_subquery(),
            select(func.count(MedicalRecord.id)).scalar_subquery(),
            select(func.count(Bill.id))
            .where(Bill.status.in_(("unpaid", "partial")))
            .scalar_subquery(),
            select(func.count(Department.id))
            .where(Department.is_active.is_(True))
            .scalar_subquery(),
        )
    ).one()
    appointment_statuses = {
        status: 0
        for status in ("scheduled", "confirmed", "completed", "cancelled")
    }
    appointment_statuses.update(
        {
            status: count
            for status, count in db.session.query(
                Appointment.status, func.count(Appointment.id)
            ).group_by(Appointment.status)
        }
    )
    recent_appointments = (
        Appointment.query.options(
            selectinload(Appointment.patient).selectinload(Patient.user),
            selectinload(Appointment.doctor).selectinload(Doctor.user),
        )
        .order_by(Appointment.starts_at.desc())
        .limit(8)
        .all()
    )
    return render_template(
        "admin/dashboard.html",
        patient_count=counts[0],
        doctor_count=counts[1],
        appointment_count=counts[2],
        record_count=counts[3],
        unpaid_count=counts[4],
        department_count=counts[5],
        appointment_statuses=appointment_statuses,
        appointments=recent_appointments,
    )


@bp.route("/doctors", methods=["GET", "POST"])
@admin_required
def doctors():
    form = DoctorCreateForm()
    departments = Department.query.order_by(Department.name).all()
    active_departments = [department for department in departments if department.is_active]
    active_department_ids = {department.id for department in active_departments}
    form.department_id.choices = [
        (department.id, department.name) for department in active_departments
    ]
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        if db.session.query(User.id).filter(func.lower(User.email) == email).first():
            flash("An account already uses this email.", "danger")
        elif form.department_id.data not in active_department_ids:
            abort(400)
        else:
            user = User(name=form.name.data.strip(), email=email, role="doctor")
            user.set_password(form.password.data)
            user.doctor_profile = Doctor(
                department_id=form.department_id.data,
                phone=form.phone.data.strip(),
                specialty=form.specialty.data.strip(),
                qualifications=form.qualifications.data.strip() or None,
                consultation_fee=form.consultation_fee.data,
            )
            db.session.add(user)
            db.session.commit()
            flash("Doctor account created.", "success")
            return redirect(url_for("admin.doctors"))

    search = request.args.get("q", "").strip()
    department_id = request.args.get("department_id", type=int)
    status = request.args.get("status", "")
    doctor_query = Doctor.query.join(User).join(Department)
    if search:
        search_pattern = f"%{search.lower()}%"
        doctor_query = doctor_query.filter(
            db.or_(
                func.lower(User.name).like(search_pattern),
                func.lower(User.email).like(search_pattern),
                func.lower(Doctor.specialty).like(search_pattern),
            )
        )
    if department_id:
        doctor_query = doctor_query.filter(Doctor.department_id == department_id)
    if status == "active":
        doctor_query = doctor_query.filter(User.is_active.is_(True))
    elif status == "inactive":
        doctor_query = doctor_query.filter(User.is_active.is_(False))

    return render_template(
        "admin/doctors.html",
        form=form,
        doctors=doctor_query.options(
            selectinload(Doctor.user),
            selectinload(Doctor.department),
        )
        .order_by(User.name)
        .all(),
        departments=departments,
        search=search,
        selected_department=department_id,
        selected_status=status,
    )


@bp.route("/doctors/<int:doctor_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_doctor(doctor_id):
    doctor = db.get_or_404(Doctor, doctor_id)
    departments = Department.query.order_by(Department.name).all()
    available_departments = [
        department
        for department in departments
        if department.is_active or department.id == doctor.department_id
    ]
    active_department_ids = {
        department.id for department in departments if department.is_active
    }
    form = DoctorEditForm(
        name=doctor.user.name,
        email=doctor.user.email,
        phone=doctor.phone,
        department_id=doctor.department_id,
        specialty=doctor.specialty,
        qualifications=doctor.qualifications,
        consultation_fee=doctor.consultation_fee,
    )
    form.department_id.choices = [
        (
            department.id,
            department.name
            if department.is_active
            else f"{department.name} (inactive)",
        )
        for department in available_departments
    ]
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        if db.session.query(User.id).filter(
            func.lower(User.email) == email, User.id != doctor.user_id
        ).first():
            flash("An account already uses this email.", "danger")
        elif (
            form.department_id.data not in active_department_ids
            and form.department_id.data != doctor.department_id
        ):
            abort(400)
        else:
            doctor.user.name = form.name.data.strip()
            doctor.user.email = email
            doctor.phone = form.phone.data.strip()
            doctor.department_id = form.department_id.data
            doctor.specialty = form.specialty.data.strip()
            doctor.qualifications = form.qualifications.data.strip() or None
            doctor.consultation_fee = form.consultation_fee.data
            db.session.commit()
            flash("Doctor profile updated.", "success")
            return redirect(url_for("admin.doctors"))
    return render_template(
        "admin/edit_doctor.html", doctor=doctor, form=form
    )


@bp.post("/doctors/<int:doctor_id>/delete")
@admin_required
def delete_doctor(doctor_id):
    doctor = db.get_or_404(Doctor, doctor_id)
    user = doctor.user
    user.is_active = False
    db.session.commit()
    flash("Doctor account deactivated; historical records were preserved.", "success")
    return redirect(url_for("admin.doctors"))


@bp.post("/doctors/<int:doctor_id>/reactivate")
@admin_required
def reactivate_doctor(doctor_id):
    doctor = db.get_or_404(Doctor, doctor_id)
    doctor.user.is_active = True
    db.session.commit()
    flash("Doctor account reactivated.", "success")
    return redirect(url_for("admin.doctors"))


@bp.route("/departments", methods=["GET", "POST"])
@admin_required
def departments():
    form = DepartmentForm()
    if form.validate_on_submit():
        name = form.name.data.strip()
        if db.session.query(Department.id).filter(
            func.lower(Department.name) == name.lower()
        ).first():
            flash("That department already exists.", "danger")
        else:
            db.session.add(
                Department(
                    name=name,
                    description=form.description.data.strip() or None,
                    is_active=True,
                )
            )
            db.session.commit()
            flash("Department added.", "success")
            return redirect(url_for("admin.departments"))
    department_rows = Department.query.order_by(Department.name).all()
    doctor_counts = dict(
        db.session.query(Doctor.department_id, func.count(Doctor.id)).group_by(
            Doctor.department_id
        )
    )
    return render_template(
        "admin/departments.html",
        form=form,
        departments=department_rows,
        doctor_counts=doctor_counts,
    )


@bp.route("/departments/<int:department_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_department(department_id):
    department = db.get_or_404(Department, department_id)
    form = DepartmentEditForm(
        name=department.name,
        description=department.description,
    )
    if form.validate_on_submit():
        name = form.name.data.strip()
        duplicate = db.session.query(Department.id).filter(
            func.lower(Department.name) == name.lower(),
            Department.id != department.id,
        ).first()
        if duplicate:
            flash("That department already exists.", "danger")
        else:
            department.name = name
            department.description = form.description.data.strip() or None
            db.session.commit()
            flash("Department updated.", "success")
            return redirect(url_for("admin.departments"))
    return render_template(
        "admin/edit_department.html", department=department, form=form
    )


@bp.post("/departments/<int:department_id>/deactivate")
@admin_required
def deactivate_department(department_id):
    department = db.get_or_404(Department, department_id)
    if department.is_active:
        department.is_active = False
        db.session.commit()
        flash("Department deactivated; existing records and doctor assignments were preserved.", "success")
    else:
        flash("Department is already inactive.", "info")
    return redirect(url_for("admin.departments"))


@bp.post("/departments/<int:department_id>/reactivate")
@admin_required
def reactivate_department(department_id):
    department = db.get_or_404(Department, department_id)
    if not department.is_active:
        department.is_active = True
        db.session.commit()
        flash("Department reactivated.", "success")
    else:
        flash("Department is already active.", "info")
    return redirect(url_for("admin.departments"))


@bp.get("/patients")
@admin_required
def patients():
    patient_rows = (
        Patient.query.join(User)
        .options(selectinload(Patient.user))
        .order_by(User.name)
        .all()
    )
    appointment_counts = dict(
        db.session.query(
            Appointment.patient_id,
            func.count(Appointment.id),
        ).group_by(Appointment.patient_id)
    )
    return render_template(
        "admin/patients.html",
        patients=patient_rows,
        appointment_counts=appointment_counts,
    )


@bp.route("/patients/<int:patient_id>", methods=["GET", "POST"])
@admin_required
def manage_patient(patient_id):
    patient = db.get_or_404(Patient, patient_id)
    form = PatientAccountForm(is_active=patient.user.is_active)
    if form.validate_on_submit():
        patient.user.is_active = form.is_active.data
        db.session.commit()
        flash("Patient account updated.", "success")
        return redirect(url_for("admin.patients"))
    return render_template(
        "admin/manage_patient.html", patient=patient, form=form
    )


@bp.get("/appointments")
@admin_required
def appointments():
    return render_template(
        "appointments.html",
        appointments=Appointment.query.options(
            selectinload(Appointment.patient).selectinload(Patient.user),
            selectinload(Appointment.doctor).selectinload(Doctor.user),
        )
        .order_by(Appointment.starts_at.desc())
        .all(),
        admin_view=True,
        now=datetime.now(),
    )


@bp.get("/records")
@admin_required
def medical_records():
    records = (
        MedicalRecord.query.options(
            selectinload(MedicalRecord.patient).selectinload(Patient.user),
            selectinload(MedicalRecord.doctor).selectinload(Doctor.user),
            selectinload(MedicalRecord.prescriptions),
        )
        .order_by(MedicalRecord.visit_date.desc(), MedicalRecord.created_at.desc())
        .all()
    )
    return render_template("admin/records.html", records=records)


@bp.post("/appointments/<int:appointment_id>/status")
@admin_required
def update_appointment(appointment_id):
    appointment = db.get_or_404(Appointment, appointment_id)
    form = AppointmentStatusForm()
    if not form.validate_on_submit():
        abort(400)
    appointment.status = form.status.data
    db.session.commit()
    flash("Appointment status updated.", "success")
    return redirect(url_for("admin.appointments"))


@bp.route("/billing", methods=["GET", "POST"])
@admin_required
def billing():
    form = BillForm()
    patients = Patient.query.join(User).order_by(User.name).all()
    form.patient_id.choices = [(p.id, p.user.name) for p in patients]
    appointments = (
        Appointment.query.filter(~Appointment.bill.has())
        .order_by(Appointment.starts_at.desc())
        .all()
    )
    form.appointment_id.choices = [(0, "No appointment")] + [
        (a.id, f"#{a.id} · {a.patient.user.name} · {a.starts_at:%Y-%m-%d %H:%M}")
        for a in appointments
    ]
    if form.validate_on_submit():
        if not patients or not any(p.id == form.patient_id.data for p in patients):
            abort(400)
        appointment_id = form.appointment_id.data or None
        appointment = (
            db.session.get(Appointment, appointment_id) if appointment_id else None
        )
        if appointment and (
            appointment.patient_id != form.patient_id.data or appointment.bill
        ):
            abort(400)
        total_amount = _bill_total(form)
        if total_amount is None:
            return _render_billing(form, patients, appointments)
        bill = Bill(
            patient_id=form.patient_id.data,
            appointment_id=appointment_id,
            description=form.description.data.strip(),
            consultation_fee=form.consultation_fee.data,
            service_charges=form.service_charges.data,
            discount_amount=form.discount_amount.data,
            amount=total_amount,
        )
        db.session.add(bill)
        db.session.flush()
        bill.invoice_number = f"INV-{bill.issued_at.year}-{bill.id:06d}"
        db.session.commit()
        flash(f"Invoice {bill.invoice_number} created.", "success")
        return redirect(url_for("admin.billing"))
    return _render_billing(form, patients, appointments)


@bp.post("/billing/<int:bill_id>/mark-paid")
@admin_required
def mark_bill_paid(bill_id):
    db.get_or_404(Bill, bill_id)
    flash(
        "Record a payment amount and method; bill status is calculated from payments.",
        "warning",
    )
    return redirect(url_for("admin.billing"))


def _bill_total(form):
    consultation_fee = form.consultation_fee.data
    service_charges = form.service_charges.data
    discount_amount = form.discount_amount.data
    subtotal = consultation_fee + service_charges
    if discount_amount > subtotal:
        form.discount_amount.errors.append(
            "Discount cannot exceed consultation fees and service charges."
        )
        return None
    total_amount = subtotal - discount_amount
    if total_amount <= 0:
        form.discount_amount.errors.append(
            "The invoice total must be greater than zero."
        )
        return None
    if total_amount > Decimal("99999999.99"):
        form.discount_amount.errors.append("The invoice total is too large.")
        return None
    return total_amount


def _render_billing(form, patients, appointments):
    bills = (
        Bill.query.options(
            selectinload(Bill.payments),
            selectinload(Bill.patient).selectinload(Patient.user),
        )
        .order_by(Bill.issued_at.desc())
        .all()
    )
    total_billed = sum((bill.amount for bill in bills), Decimal("0.00"))
    total_paid = sum((bill.paid_amount for bill in bills), Decimal("0.00"))
    outstanding_total = sum(
        (bill.outstanding_balance for bill in bills), Decimal("0.00")
    )
    return render_template(
        "admin/billing.html",
        form=form,
        bills=bills,
        patients=patients,
        appointments=appointments,
        total_billed=total_billed,
        total_paid=total_paid,
        outstanding_total=outstanding_total,
    )


@bp.route("/billing/<int:bill_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_bill(bill_id):
    bill = db.get_or_404(Bill, bill_id)
    if bill.payments:
        flash("Bills cannot be edited after a payment has been recorded.", "warning")
        return redirect(url_for("admin.billing"))
    form = BillEditForm(obj=bill)
    if form.validate_on_submit():
        total_amount = _bill_total(form)
        if total_amount is None:
            return render_template("admin/edit_bill.html", bill=bill, form=form)
        bill.description = form.description.data.strip()
        bill.consultation_fee = form.consultation_fee.data
        bill.service_charges = form.service_charges.data
        bill.discount_amount = form.discount_amount.data
        bill.amount = total_amount
        db.session.commit()
        flash("Bill updated.", "success")
        return redirect(url_for("admin.billing"))
    return render_template("admin/edit_bill.html", bill=bill, form=form)


@bp.post("/billing/<int:bill_id>/payments")
@admin_required
def record_payment(bill_id):
    bill = (
        Bill.query.filter_by(id=bill_id)
        .with_for_update()
        .first()
    )
    if bill is None:
        abort(404)
    form = PaymentForm()
    if not form.validate_on_submit():
        flash("Enter a valid payment amount and method.", "danger")
        return redirect(url_for("admin.billing"))

    paid_so_far = (
        db.session.query(func.coalesce(func.sum(Payment.amount), 0))
        .filter_by(bill_id=bill.id)
        .scalar()
    )
    paid_so_far = Decimal(str(paid_so_far or 0))
    balance = bill.amount - paid_so_far
    if balance <= 0:
        flash("This bill is already fully paid.", "warning")
        return redirect(url_for("admin.billing"))
    if form.amount.data > balance:
        flash("Payment cannot exceed the outstanding balance.", "danger")
        return redirect(url_for("admin.billing"))

    payment = Payment(
        bill_id=bill.id,
        amount=form.amount.data,
        payment_method=form.payment_method.data,
    )
    db.session.add(payment)
    bill.apply_payment_status(paid_so_far + form.amount.data)
    db.session.commit()
    flash("Payment recorded.", "success")
    return redirect(url_for("admin.billing"))


@bp.get("/reports")
@admin_required
def reports():
    form, date_range = _validated_report_range()
    if date_range is None:
        return render_template("admin/reports.html", form=form, report=None), 400
    report = _build_analytics(*date_range)
    return render_template("admin/reports.html", form=form, report=report)


@bp.get("/reports.csv")
@admin_required
def export_reports_csv():
    _, date_range = _validated_report_range()
    if date_range is None:
        abort(400, description="Provide a valid start and end date.")
    report = _build_analytics(*date_range)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "section",
            "period",
            "label",
            "count",
            "completed",
            "cancelled",
            "billed_amount",
            "collected_amount",
            "outstanding_amount",
        ]
    )

    def write_row(
        section,
        period="",
        label="",
        count="",
        completed="",
        cancelled="",
        billed_amount="",
        collected_amount="",
        outstanding_amount="",
    ):
        writer.writerow(
            _safe_csv_cell(value)
            for value in (
                section,
                period,
                label,
                count,
                completed,
                cancelled,
                billed_amount,
                collected_amount,
                outstanding_amount,
            )
        )

    write_row(
        "summary",
        report["date_range"],
        "Patients (all time)",
        report["patient_count"],
    )
    write_row(
        "summary",
        report["date_range"],
        "Doctors (all time)",
        report["doctor_count"],
    )
    write_row(
        "summary",
        report["date_range"],
        "Appointments",
        report["appointment_count"],
        report["completed_count"],
        report["cancelled_count"],
    )
    write_row(
        "summary",
        report["date_range"],
        "Total billed revenue",
        billed_amount=report["billed_amount"],
    )
    write_row(
        "summary",
        report["date_range"],
        "Payments received",
        collected_amount=report["collected_amount"],
    )
    write_row(
        "summary",
        report["date_range"],
        "Outstanding balance",
        outstanding_amount=report["outstanding_amount"],
    )

    for granularity in ("daily", "weekly", "monthly"):
        for row in report["appointment_periods"][granularity]:
            write_row(
                f"{granularity} appointments",
                row["period"],
                "Appointments",
                row["appointments"],
                row["completed"],
                row["cancelled"],
            )
    for row in report["doctor_stats"]:
        write_row(
            "doctor appointments",
            report["date_range"],
            f'{row["name"]} - {row["department"]}',
            row["appointments"],
            row["completed"],
            row["cancelled"],
        )
    for row in report["department_stats"]:
        write_row(
            "department patients",
            report["date_range"],
            row["department"],
            row["patients"],
        )
    for row in report["monthly_revenue"]:
        write_row(
            "monthly collected revenue",
            row["month"],
            "Recorded payments",
            collected_amount=row["amount"],
        )
    for row in report["payment_status"]:
        write_row(
            "payment status",
            report["date_range"],
            row["status"],
            row["bills"],
            billed_amount=row["billed"],
            collected_amount=row["collected"],
            outstanding_amount=row["outstanding"],
        )

    response = make_response(output.getvalue())
    response.mimetype = "text/csv"
    response.headers["Content-Disposition"] = (
        f'attachment; filename="medicare-report-{report["start_date"]}-'
        f'{report["end_date"]}.csv"'
    )
    return response


@bp.get("/reports.pdf")
@admin_required
def export_reports_pdf():
    _, date_range = _validated_report_range()
    if date_range is None:
        abort(400, description="Provide a valid start and end date.")
    report = _build_analytics(*date_range)
    pdf = _build_report_pdf(report)
    return send_file(
        pdf,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=(
            f'medicare-report-{report["start_date"]}-{report["end_date"]}.pdf'
        ),
    )


def _validated_report_range():
    form = ReportFilterForm(request.args)
    if not request.args:
        end_date = date.today()
        form.start_date.data = end_date - timedelta(days=29)
        form.end_date.data = end_date
    if not form.validate():
        return form, None
    return form, (form.start_date.data, form.end_date.data)


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _period_rows(daily_counts, granularity):
    grouped = defaultdict(Counter)
    for day, counts in daily_counts.items():
        if granularity == "daily":
            period = day
        elif granularity == "weekly":
            period = day - timedelta(days=day.weekday())
        else:
            period = day.replace(day=1)
        grouped[period].update(counts)

    rows = []
    for period, counts in sorted(grouped.items()):
        if granularity == "weekly":
            label = f"Week of {period.isoformat()}"
        elif granularity == "monthly":
            label = period.strftime("%Y-%m")
        else:
            label = period.isoformat()
        rows.append(
            {
                "period": label,
                "appointments": sum(counts.values()),
                "scheduled": counts["scheduled"],
                "confirmed": counts["confirmed"],
                "completed": counts["completed"],
                "cancelled": counts["cancelled"],
            }
        )
    return rows


def _decimal_amount(value):
    return Decimal(str(value or 0))


def _build_analytics(start_date, end_date):
    start_at = datetime.combine(start_date, time.min)
    end_exclusive = datetime.combine(end_date + timedelta(days=1), time.min)

    lifetime_counts = db.session.execute(
        select(
            select(func.count(Patient.id)).scalar_subquery(),
            select(func.count(Doctor.id)).scalar_subquery(),
        )
    ).one()

    day_expression = func.date(Appointment.starts_at)
    day_rows = (
        db.session.query(
            day_expression.label("day"),
            Appointment.status,
            func.count(Appointment.id).label("appointments"),
        )
        .filter(
            Appointment.starts_at >= start_at,
            Appointment.starts_at < end_exclusive,
        )
        .group_by(day_expression, Appointment.status)
        .order_by(day_expression, Appointment.status)
        .all()
    )
    daily_counts = defaultdict(Counter)
    for row in day_rows:
        daily_counts[_as_date(row.day)][row.status] += row.appointments
    appointment_periods = {
        granularity: _period_rows(daily_counts, granularity)
        for granularity in ("daily", "weekly", "monthly")
    }
    appointment_count = sum(
        count for counts in daily_counts.values() for count in counts.values()
    )
    completed_count = sum(
        counts["completed"] for counts in daily_counts.values()
    )
    cancelled_count = sum(
        counts["cancelled"] for counts in daily_counts.values()
    )

    completed_expression = func.sum(
        case((Appointment.status == "completed", 1), else_=0)
    )
    cancelled_expression = func.sum(
        case((Appointment.status == "cancelled", 1), else_=0)
    )
    doctor_rows = (
        db.session.query(
            Doctor.id.label("doctor_id"),
            User.name.label("name"),
            Department.name.label("department"),
            func.count(Appointment.id).label("appointments"),
            completed_expression.label("completed"),
            cancelled_expression.label("cancelled"),
        )
        .join(Appointment, Appointment.doctor_id == Doctor.id)
        .join(User, User.id == Doctor.user_id)
        .join(Department, Department.id == Doctor.department_id)
        .filter(
            Appointment.starts_at >= start_at,
            Appointment.starts_at < end_exclusive,
        )
        .group_by(Doctor.id, User.name, Department.name)
        .order_by(User.name)
        .all()
    )
    doctor_stats = [
        {
            "doctor_id": row.doctor_id,
            "name": row.name,
            "department": row.department,
            "appointments": row.appointments,
            "completed": row.completed or 0,
            "cancelled": row.cancelled or 0,
        }
        for row in doctor_rows
    ]

    department_rows = (
        db.session.query(
            Department.name.label("department"),
            func.count(func.distinct(Appointment.patient_id)).label("patients"),
        )
        .outerjoin(Doctor, Doctor.department_id == Department.id)
        .outerjoin(
            Appointment,
            and_(
                Appointment.doctor_id == Doctor.id,
                Appointment.starts_at >= start_at,
                Appointment.starts_at < end_exclusive,
                Appointment.status != "cancelled",
            ),
        )
        .group_by(Department.id, Department.name)
        .order_by(Department.name)
        .all()
    )
    department_stats = [
        {"department": row.department, "patients": row.patients}
        for row in department_rows
    ]

    payment_month_expression = (
        func.strftime("%Y-%m", Payment.received_at)
        if db.engine.dialect.name == "sqlite"
        else func.date_format(Payment.received_at, "%Y-%m")
    )
    revenue_rows = (
        db.session.query(
            payment_month_expression.label("month"),
            func.sum(Payment.amount).label("collected"),
        )
        .filter(
            Payment.received_at >= start_at,
            Payment.received_at < end_exclusive,
        )
        .group_by(payment_month_expression)
        .order_by(payment_month_expression)
        .all()
    )
    monthly_revenue = [
        {"month": row.month, "amount": _decimal_amount(row.collected)}
        for row in revenue_rows
    ]
    collected_amount = sum(
        (row["amount"] for row in monthly_revenue), Decimal("0.00")
    )
    max_monthly_revenue = max(
        (row["amount"] for row in monthly_revenue), default=Decimal("0.00")
    )
    for row in monthly_revenue:
        row["bar_width"] = (
            round(float(row["amount"] / max_monthly_revenue * 100), 1)
            if max_monthly_revenue
            else 0
        )

    payment_totals = (
        db.session.query(
            Payment.bill_id.label("bill_id"),
            func.sum(Payment.amount).label("paid"),
        )
        .join(Bill, Bill.id == Payment.bill_id)
        .filter(Bill.issued_at >= start_at, Bill.issued_at < end_exclusive)
        .group_by(Payment.bill_id)
        .subquery()
    )
    paid_expression = func.coalesce(payment_totals.c.paid, 0)
    status_expression = case(
        (
            and_(Bill.amount > 0, paid_expression >= Bill.amount),
            "paid",
        ),
        (paid_expression > 0, "partial"),
        else_="unpaid",
    )
    outstanding_expression = case(
        (Bill.amount > paid_expression, Bill.amount - paid_expression),
        else_=0,
    )
    bill_status_rows = (
        db.session.query(
            status_expression.label("status"),
            func.count(Bill.id).label("bills"),
            func.sum(Bill.amount).label("billed"),
            func.sum(paid_expression).label("collected"),
            func.sum(outstanding_expression).label("outstanding"),
        )
        .outerjoin(payment_totals, payment_totals.c.bill_id == Bill.id)
        .filter(Bill.issued_at >= start_at, Bill.issued_at < end_exclusive)
        .group_by(status_expression)
        .all()
    )
    payment_status_map = {
        status: {
            "status": status,
            "bills": 0,
            "billed": Decimal("0.00"),
            "collected": Decimal("0.00"),
            "outstanding": Decimal("0.00"),
        }
        for status in ("unpaid", "partial", "paid")
    }
    for row in bill_status_rows:
        payment_status_map[row.status] = {
            "status": row.status,
            "bills": row.bills,
            "billed": _decimal_amount(row.billed),
            "collected": _decimal_amount(row.collected),
            "outstanding": _decimal_amount(row.outstanding),
        }
    payment_status = [
        payment_status_map[status] for status in ("unpaid", "partial", "paid")
    ]
    billed_amount = sum(
        (row["billed"] for row in payment_status), Decimal("0.00")
    )
    outstanding_amount = sum(
        (row["outstanding"] for row in payment_status), Decimal("0.00")
    )

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "date_range": f"{start_date.isoformat()} to {end_date.isoformat()}",
        "patient_count": lifetime_counts[0],
        "doctor_count": lifetime_counts[1],
        "appointment_count": appointment_count,
        "completed_count": completed_count,
        "cancelled_count": cancelled_count,
        "appointment_periods": appointment_periods,
        "doctor_stats": doctor_stats,
        "department_stats": department_stats,
        "monthly_revenue": monthly_revenue,
        "payment_status": payment_status,
        "billed_amount": billed_amount,
        "collected_amount": collected_amount,
        "outstanding_amount": outstanding_amount,
    }


def _safe_csv_cell(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _build_report_pdf(report):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buffer = io.BytesIO()
    styles = getSampleStyleSheet()
    story = [
        Paragraph("MediCare Hospital - Reports and Analytics", styles["Title"]),
        Paragraph(
            f"Date range: {escape(report['date_range'])}",
            styles["Normal"],
        ),
        Spacer(1, 6 * mm),
    ]

    def add_table(title, headers, rows, widths):
        story.append(Paragraph(escape(title), styles["Heading2"]))
        if not rows:
            rows = [["No data for this period."] + [""] * (len(headers) - 1)]
        table_data = [
            [Paragraph(escape(str(cell)), styles["BodyText"]) for cell in headers]
        ]
        table_data.extend(
            [Paragraph(escape(str(cell)), styles["BodyText"]) for cell in row]
            for row in rows
        )
        table = Table(table_data, colWidths=widths, repeatRows=1, hAlign="LEFT")
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#176b87")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#dce5e9")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            (
                                "ROWBACKGROUNDS",
                                (0, 1),
                                (-1, -1),
                                [colors.white, colors.HexColor("#f5f8fa")],
                            ),
                    ("PADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        story.extend([table, Spacer(1, 5 * mm)])

    add_table(
        "Summary",
        ["Metric", "Value"],
        [
            ["Patients (all time)", report["patient_count"]],
            ["Doctors (all time)", report["doctor_count"]],
            ["Appointments in range", report["appointment_count"]],
            ["Completed appointments", report["completed_count"]],
            ["Cancelled appointments", report["cancelled_count"]],
            ["Total billed revenue", f'{report["billed_amount"]:.2f}'],
            ["Payments received", f'{report["collected_amount"]:.2f}'],
            ["Outstanding balance", f'{report["outstanding_amount"]:.2f}'],
        ],
        [125 * mm, 55 * mm],
    )
    for granularity in ("daily", "weekly", "monthly"):
        rows = [
            [
                row["period"],
                row["appointments"],
                row["completed"],
                row["cancelled"],
            ]
            for row in report["appointment_periods"][granularity]
        ]
        add_table(
            f"{granularity.title()} appointments",
            ["Period", "Appointments", "Completed", "Cancelled"],
            rows,
            [100 * mm, 40 * mm, 40 * mm, 40 * mm],
        )
    add_table(
        "Doctor-wise appointments",
        ["Doctor", "Department", "Appointments", "Completed", "Cancelled"],
        [
            [
                row["name"],
                row["department"],
                row["appointments"],
                row["completed"],
                row["cancelled"],
            ]
            for row in report["doctor_stats"]
        ],
        [65 * mm, 55 * mm, 40 * mm, 40 * mm, 40 * mm],
    )
    add_table(
        "Department-wise patients",
        ["Department", "Patients with non-cancelled appointments"],
        [[row["department"], row["patients"]] for row in report["department_stats"]],
        [125 * mm, 75 * mm],
    )
    add_table(
        "Monthly collected revenue",
        ["Month", "Recorded payments"],
        [
            [row["month"], f'{row["amount"]:.2f}']
            for row in report["monthly_revenue"]
        ],
        [125 * mm, 75 * mm],
    )
    add_table(
        "Payment status",
        ["Status", "Bills", "Billed", "Collected", "Outstanding"],
        [
            [
                row["status"].title(),
                row["bills"],
                f'{row["billed"]:.2f}',
                f'{row["collected"]:.2f}',
                f'{row["outstanding"]:.2f}',
            ]
            for row in report["payment_status"]
        ],
        [45 * mm, 25 * mm, 40 * mm, 40 * mm, 45 * mm],
    )
    document = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title="MediCare Reports and Analytics",
    )
    document.build(story)
    buffer.seek(0)
    return buffer
