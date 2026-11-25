from datetime import date, datetime, timedelta
from functools import wraps

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from medicare.extensions import db
from medicare.forms import (
    AppointmentForm,
    AppointmentStatusForm,
    DoctorAvailabilityForm,
    DoctorLeaveForm,
    DoctorProfileForm,
    ProfileForm,
    PrescriptionForm,
    RecordForm,
)
from medicare.models import (
    Appointment,
    Bill,
    Department,
    Doctor,
    DoctorAvailability,
    DoctorLeave,
    MedicalRecord,
    Patient,
    Prescription,
    User,
)
from medicare.scheduling import available_slots, has_appointment_conflict

bp = Blueprint("views", __name__)


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user.role not in roles:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


@bp.get("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("views.dashboard"))
    return render_template(
        "index.html",
        departments=Department.query.filter(Department.is_active.is_(True))
        .order_by(Department.name)
        .all(),
        doctors=Doctor.query.join(User)
        .join(Department)
        .filter(
            User.is_active.is_(True),
            Department.is_active.is_(True),
        )
        .all(),
    )


@bp.get("/dashboard")
@login_required
def dashboard():
    if current_user.role == "admin":
        return redirect(url_for("admin.dashboard"))
    if current_user.role == "doctor":
        doctor = current_user.doctor_profile
        appointments = (
            Appointment.query.options(
                selectinload(Appointment.patient).selectinload(Patient.user)
            )
            .filter_by(doctor_id=doctor.id)
            .order_by(Appointment.starts_at.desc())
            .limit(8)
            .all()
        )
        return render_template(
            "dashboard.html", appointments=appointments
        )
    patient = current_user.patient_profile
    counts = db.session.execute(
        select(
            select(func.count(Appointment.id))
            .where(Appointment.patient_id == patient.id)
            .scalar_subquery(),
            select(func.count(MedicalRecord.id))
            .where(MedicalRecord.patient_id == patient.id)
            .scalar_subquery(),
            select(func.count(Bill.id))
            .where(
                Bill.patient_id == patient.id,
                Bill.status.in_(("unpaid", "partial")),
            )
            .scalar_subquery(),
        )
    ).one()
    appointments = (
        Appointment.query.options(
            selectinload(Appointment.doctor).selectinload(Doctor.user)
        )
        .filter_by(patient_id=patient.id)
        .order_by(Appointment.starts_at.desc())
        .limit(8)
        .all()
    )
    return render_template(
        "dashboard.html",
        appointments=appointments,
        patient_appointment_count=counts[0],
        patient_record_count=counts[1],
        patient_outstanding_bill_count=counts[2],
    )


@bp.route("/profile", methods=["GET", "POST"])
@role_required("patient")
def patient_profile():
    profile = current_user.patient_profile
    form = ProfileForm(obj=profile)
    if request.method == "GET":
        form.name.data = current_user.name
    if form.validate_on_submit():
        current_user.name = form.name.data.strip()
        profile.phone = form.phone.data.strip() or None
        profile.date_of_birth = form.date_of_birth.data
        profile.address = form.address.data.strip() or None
        db.session.commit()
        flash("Your profile has been updated.", "success")
        return redirect(url_for("views.patient_profile"))
    return render_template("form_page.html", title="My profile", form=form)

@bp.route("/doctor/profile", methods=["GET", "POST"])
@role_required("doctor")
def doctor_profile():
    doctor = current_user.doctor_profile
    form = DoctorProfileForm(
        name=current_user.name,
        specialty=doctor.specialty,
        qualifications=doctor.qualifications,
    )
    if form.validate_on_submit():
        current_user.name = form.name.data.strip()
        doctor.specialty = form.specialty.data.strip()
        doctor.qualifications = form.qualifications.data.strip() or None
        db.session.commit()
        flash("Your profile has been updated.", "success")
        return redirect(url_for("views.doctor_profile"))
    return render_template("form_page.html", title="My profile", form=form)


@bp.route("/appointments/book", methods=["GET", "POST"])
@role_required("patient")
def book_appointment():
    form = AppointmentForm()
    if request.method == "GET":
        form.department_id.data = request.args.get("department_id", type=int)
        form.doctor_id.data = request.args.get("doctor_id", type=int)
        appointment_date_value = request.args.get("appointment_date", "")
        try:
            form.appointment_date.data = date.fromisoformat(appointment_date_value)
        except ValueError:
            form.appointment_date.data = None
    departments = (
        Department.query.filter(Department.is_active.is_(True))
        .order_by(Department.name)
        .all()
    )
    active_doctors = (
        Doctor.query.join(User)
        .join(Department)
        .filter(
            User.is_active.is_(True),
            Department.is_active.is_(True),
        )
        .order_by(User.name)
        .all()
    )
    form.department_id.choices = [(d.id, d.name) for d in departments]
    department_id = form.department_id.data
    valid_department_ids = {department.id for department in departments}
    doctors = []
    if department_id in valid_department_ids:
        doctors = (
            Doctor.query.join(User)
            .join(Department)
            .filter(
                Doctor.department_id == department_id,
                User.is_active.is_(True),
                Department.is_active.is_(True),
            )
            .order_by(User.name)
            .all()
        )
    form.doctor_id.choices = [
        (doctor.id, f"Dr. {doctor.user.name} — {doctor.specialty}")
        for doctor in doctors
    ]

    appointment_date = form.appointment_date.data
    doctor = next(
        (candidate for candidate in doctors if candidate.id == form.doctor_id.data),
        None,
    )
    slot_times = (
        available_slots(doctor.id, appointment_date)
        if doctor and appointment_date
        else []
    )
    available_date_options = []
    if doctor:
        for day_offset in range(1, 31):
            candidate_date = date.today() + timedelta(days=day_offset)
            candidate_slots = available_slots(doctor.id, candidate_date)
            if candidate_slots:
                available_date_options.append(
                    (candidate_date, len(candidate_slots))
                )
    form.time_slot.choices = [
        (slot.strftime("%H:%M"), slot.strftime("%I:%M %p")) for slot in slot_times
    ]

    if form.validate_on_submit():
        doctor = (
            Doctor.query.join(User)
            .join(Department)
            .filter(
                Doctor.id == form.doctor_id.data,
                Doctor.department_id == form.department_id.data,
                User.is_active.is_(True),
                Department.is_active.is_(True),
            )
            .first()
        )
        if doctor is None:
            abort(400)
        schedule = DoctorAvailability.query.filter_by(
            doctor_id=doctor.id, weekday=form.appointment_date.data.weekday()
        ).first()
        starts_at = datetime.combine(
            form.appointment_date.data,
            datetime.strptime(form.time_slot.data, "%H:%M").time(),
        )
        if (
            schedule is None
            or starts_at.time() not in slot_times
            or has_appointment_conflict(
                doctor.id, starts_at, schedule.slot_duration_minutes
            )
        ):
            flash("That time is no longer available. Choose another slot.", "warning")
        else:
            appointment = Appointment(
                patient_id=current_user.patient_profile.id,
                doctor_id=doctor.id,
                starts_at=starts_at,
                duration_minutes=schedule.slot_duration_minutes,
                reason=form.reason.data.strip(),
            )
            db.session.add(appointment)
            db.session.commit()
            flash("Appointment booked.", "success")
            return redirect(url_for("views.appointments"))
    return render_template(
        "appointments/book.html",
        form=form,
        departments=departments,
        doctors=doctors,
        active_doctors=active_doctors,
        slot_times=slot_times,
        selected_department=department_id,
        selected_doctor=form.doctor_id.data,
        selected_date=appointment_date,
        available_date_options=available_date_options,
        today=date.today().isoformat(),
        last_bookable_date=(date.today() + timedelta(days=180)).isoformat(),
    )


@bp.get("/appointments")
@login_required
def appointments():
    if current_user.role == "patient":
        query = Appointment.query.filter_by(patient_id=current_user.patient_profile.id)
    elif current_user.role == "doctor":
        query = Appointment.query.filter_by(doctor_id=current_user.doctor_profile.id)
    else:
        return redirect(url_for("admin.appointments"))
    rows = query.order_by(Appointment.starts_at.desc()).all()
    return render_template(
        "appointments.html", appointments=rows, now=datetime.now()
    )


@bp.post("/appointments/<int:appointment_id>/cancel")
@role_required("patient")
def cancel_appointment(appointment_id):
    appointment = db.get_or_404(Appointment, appointment_id)
    if appointment.patient_id != current_user.patient_profile.id:
        abort(403)
    if appointment.status not in ("scheduled", "confirmed"):
        flash("Only scheduled or confirmed appointments can be cancelled.", "warning")
    elif appointment.starts_at <= datetime.now():
        flash("An appointment that has started cannot be cancelled online.", "warning")
    else:
        appointment.status = "cancelled"
        db.session.commit()
        flash("Appointment cancelled.", "success")
    return redirect(url_for("views.appointments"))


def _availability_doctor():
    if current_user.role == "doctor":
        return current_user.doctor_profile
    if current_user.role != "admin":
        abort(403)
    doctor_id = request.values.get("doctor_id", type=int)
    return db.get_or_404(Doctor, doctor_id) if doctor_id else None


def _render_availability(doctor, availability_form=None, leave_form=None):
    doctors = (
        Doctor.query.join(User).order_by(User.name).all()
        if current_user.role == "admin"
        else []
    )
    schedules = (
        DoctorAvailability.query.filter_by(doctor_id=doctor.id)
        .order_by(DoctorAvailability.weekday)
        .all()
        if doctor
        else []
    )
    leaves = (
        DoctorLeave.query.filter_by(doctor_id=doctor.id)
        .order_by(DoctorLeave.start_date)
        .all()
        if doctor
        else []
    )
    return render_template(
        "availability.html",
        doctor=doctor,
        doctors=doctors,
        schedules=schedules,
        leaves=leaves,
        weekdays=(
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ),
        availability_form=availability_form or DoctorAvailabilityForm(),
        leave_form=leave_form or DoctorLeaveForm(),
    )


@bp.get("/availability")
@role_required("doctor", "admin")
def doctor_availability():
    doctor = _availability_doctor()
    form = DoctorAvailabilityForm()
    weekday = request.args.get("weekday", type=int)
    if doctor and weekday is not None:
        schedule = DoctorAvailability.query.filter_by(
            doctor_id=doctor.id, weekday=weekday
        ).first()
        if schedule:
            form = DoctorAvailabilityForm(
                data={
                    "weekday": schedule.weekday,
                    "start_time": schedule.start_time,
                    "end_time": schedule.end_time,
                    "slot_duration_minutes": schedule.slot_duration_minutes,
                    "break_start": schedule.break_start,
                    "break_end": schedule.break_end,
                }
            )
    return _render_availability(doctor, availability_form=form)


@bp.post("/availability/schedule")
@role_required("doctor", "admin")
def save_doctor_availability():
    doctor = _availability_doctor()
    if doctor is None:
        abort(400)
    form = DoctorAvailabilityForm()
    if form.validate_on_submit():
        invalid_field = None
        if form.start_time.data >= form.end_time.data:
            invalid_field = (form.end_time, "End time must be later than start time.")
        elif bool(form.break_start.data) != bool(form.break_end.data):
            invalid_field = (form.break_end, "Enter both break times or leave both blank.")
        elif form.break_start.data and not (
            form.start_time.data <= form.break_start.data
            < form.break_end.data
            <= form.end_time.data
        ):
            invalid_field = (
                form.break_end,
                "The break must fall inside working hours and have a positive duration.",
            )
        if invalid_field:
            invalid_field[0].errors.append(invalid_field[1])
        else:
            schedule = DoctorAvailability.query.filter_by(
                doctor_id=doctor.id, weekday=form.weekday.data
            ).first()
            if schedule is None:
                schedule = DoctorAvailability(
                    doctor_id=doctor.id, weekday=form.weekday.data
                )
                db.session.add(schedule)
            schedule.start_time = form.start_time.data
            schedule.end_time = form.end_time.data
            schedule.slot_duration_minutes = form.slot_duration_minutes.data
            schedule.break_start = form.break_start.data
            schedule.break_end = form.break_end.data
            db.session.commit()
            flash("Working hours saved.", "success")
            return redirect(
                url_for("views.doctor_availability", doctor_id=doctor.id)
            )
    return _render_availability(doctor, availability_form=form)


@bp.post("/availability/leave")
@role_required("doctor", "admin")
def add_doctor_leave():
    doctor = _availability_doctor()
    if doctor is None:
        abort(400)
    form = DoctorLeaveForm()
    if form.validate_on_submit():
        if form.end_date.data < form.start_date.data:
            form.end_date.errors.append(
                "The last unavailable date must be on or after the first date."
            )
        else:
            db.session.add(
                DoctorLeave(
                    doctor_id=doctor.id,
                    start_date=form.start_date.data,
                    end_date=form.end_date.data,
                    reason=form.reason.data.strip() or None,
                )
            )
            db.session.commit()
            flash("Unavailable dates added.", "success")
            return redirect(
                url_for("views.doctor_availability", doctor_id=doctor.id)
            )
    return _render_availability(doctor, leave_form=form)


@bp.post("/availability/leave/<int:leave_id>/delete")
@role_required("doctor", "admin")
def delete_doctor_leave(leave_id):
    doctor = _availability_doctor()
    if doctor is None:
        abort(400)
    leave = db.get_or_404(DoctorLeave, leave_id)
    if leave.doctor_id != doctor.id:
        abort(403)
    db.session.delete(leave)
    db.session.commit()
    flash("Unavailable dates removed.", "success")
    return redirect(url_for("views.doctor_availability", doctor_id=doctor.id))


@bp.post("/appointments/<int:appointment_id>/status")
@role_required("doctor")
def update_appointment_status(appointment_id):
    appointment = db.get_or_404(Appointment, appointment_id)
    if appointment.doctor_id != current_user.doctor_profile.id:
        abort(403)
    form = AppointmentStatusForm()
    if not form.validate_on_submit():
        abort(400)
    appointment.status = form.status.data
    db.session.commit()
    flash("Appointment status updated.", "success")
    return redirect(url_for("views.appointments"))


@bp.route("/appointments/<int:appointment_id>/record", methods=["GET", "POST"])
@role_required("doctor")
def edit_record(appointment_id):
    appointment = db.get_or_404(Appointment, appointment_id)
    if appointment.doctor_id != current_user.doctor_profile.id:
        abort(403)
    record = appointment.medical_record
    if record and record.doctor_id != current_user.doctor_profile.id:
        abort(403)
    return _edit_medical_record(
        record,
        appointment=appointment,
        patient_id=appointment.patient_id,
        doctor_id=current_user.doctor_profile.id,
    )


@bp.route("/records/<int:record_id>/edit", methods=["GET", "POST"])
@role_required("doctor")
def edit_existing_record(record_id):
    record = db.get_or_404(MedicalRecord, record_id)
    if record.doctor_id != current_user.doctor_profile.id:
        abort(403)
    return _edit_medical_record(record)


def _edit_medical_record(record, appointment=None, patient_id=None, doctor_id=None):
    if request.method == "POST":
        form = RecordForm()
    else:
        visit_date = (
            record.visit_date
            if record and record.visit_date
            else record.created_at.date()
            if record
            else appointment.starts_at.date()
        )
        form = RecordForm(
            data={
                "symptoms": record.symptoms if record else None,
                "diagnosis": record.diagnosis if record else None,
                "consultation_notes": (
                    (record.treatment_notes or record.consultation_notes)
                    if record
                    else None
                ),
                "visit_date": visit_date,
                "prescription": record.prescription if record else None,
            }
        )
    if form.validate_on_submit():
        if not record:
            record = MedicalRecord(
                patient_id=patient_id,
                doctor_id=doctor_id,
                appointment_id=appointment.id if appointment else None,
            )
            db.session.add(record)
        record.symptoms = form.symptoms.data.strip()
        record.diagnosis = form.diagnosis.data.strip()
        record.consultation_notes = form.consultation_notes.data.strip()
        record.treatment_notes = form.consultation_notes.data.strip()
        record.visit_date = form.visit_date.data
        record.prescription = form.prescription.data.strip() or None
        db.session.commit()
        flash("Medical record saved.", "success")
        return redirect(url_for("views.record_detail", record_id=record.id))
    return render_template(
        "records/edit.html",
        record=record,
        appointment=appointment,
        form=form,
    )


def _check_record_access(record):
    if current_user.role == "patient":
        if record.patient_id != current_user.patient_profile.id:
            abort(403)
    elif current_user.role == "doctor":
        if record.doctor_id != current_user.doctor_profile.id:
            abort(403)
    elif current_user.role != "admin":
        abort(403)


def _render_record_detail(record, prescription_form=None):
    return render_template(
        "records/detail.html",
        record=record,
        prescription_form=prescription_form,
        can_manage_prescriptions=(
            current_user.role == "doctor"
            and record.doctor_id == current_user.doctor_profile.id
        ),
    )


@bp.get("/records/<int:record_id>")
@login_required
def record_detail(record_id):
    record = db.get_or_404(MedicalRecord, record_id)
    _check_record_access(record)
    prescription_form = None
    if current_user.role == "doctor":
        prescription_id = request.args.get("edit_prescription", type=int)
        if prescription_id:
            prescription = db.get_or_404(Prescription, prescription_id)
            if prescription.medical_record_id != record.id:
                abort(403)
            prescription_form = PrescriptionForm(obj=prescription)
            prescription_form.prescription_id.data = str(prescription.id)
    return _render_record_detail(record, prescription_form)


@bp.route("/records/<int:record_id>/prescriptions", methods=["GET", "POST"])
@role_required("doctor")
def manage_prescription(record_id):
    record = db.get_or_404(MedicalRecord, record_id)
    _check_record_access(record)
    prescription_id = request.values.get("prescription_id", type=int)
    if request.method == "GET":
        prescription_id = request.args.get("edit_prescription", type=int)
    prescription = (
        db.get_or_404(Prescription, prescription_id) if prescription_id else None
    )
    if prescription and prescription.medical_record_id != record.id:
        abort(403)
    if request.method == "GET" and prescription:
        form = PrescriptionForm(obj=prescription)
        form.prescription_id.data = str(prescription.id)
    else:
        form = PrescriptionForm()
    if form.validate_on_submit():
        if form.prescription_id.data:
            try:
                prescription_id = int(form.prescription_id.data)
            except ValueError:
                abort(400)
            prescription = db.get_or_404(Prescription, prescription_id)
            if prescription.medical_record_id != record.id:
                abort(403)
        else:
            prescription = Prescription(medical_record_id=record.id)
            db.session.add(prescription)
        prescription.medicine_name = form.medicine_name.data.strip()
        prescription.dosage = form.dosage.data.strip()
        prescription.frequency = form.frequency.data.strip()
        prescription.duration = form.duration.data.strip()
        prescription.instructions = form.instructions.data.strip() or None
        db.session.commit()
        flash("Prescription saved.", "success")
        return redirect(url_for("views.record_detail", record_id=record.id))
    return _render_record_detail(record, form)


@bp.get("/records")
@login_required
def medical_records():
    if current_user.role == "patient":
        query = MedicalRecord.query.filter_by(
            patient_id=current_user.patient_profile.id
        )
    elif current_user.role == "doctor":
        query = MedicalRecord.query.filter_by(
            doctor_id=current_user.doctor_profile.id
        )
    else:
        abort(403)
    records = query.order_by(MedicalRecord.created_at.desc()).all()
    return render_template(
        "records.html", records=records, now=datetime.now()
    )


@bp.get("/bills")
@role_required("patient")
def patient_bills():
    bills = (
        Bill.query.options(selectinload(Bill.payments))
        .filter_by(patient_id=current_user.patient_profile.id)
        .order_by(Bill.issued_at.desc())
        .all()
    )
    return render_template("bills.html", bills=bills)


@bp.get("/invoices/<int:bill_id>")
@login_required
def invoice(bill_id):
    bill = (
        Bill.query.options(selectinload(Bill.payments))
        .filter_by(id=bill_id)
        .first_or_404()
    )
    if current_user.role == "patient":
        if bill.patient_id != current_user.patient_profile.id:
            abort(403)
    elif current_user.role != "admin":
        abort(403)
    return render_template("invoices/detail.html", bill=bill)
