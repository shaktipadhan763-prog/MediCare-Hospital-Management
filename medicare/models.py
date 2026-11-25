from datetime import datetime
from decimal import Decimal

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from medicare.extensions import db


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="patient")
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)

    patient_profile = db.relationship(
        "Patient", back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    doctor_profile = db.relationship(
        "Doctor", back_populates="user", uselist=False, cascade="all, delete-orphan"
    )

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Department(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    description = db.Column(db.String(500))
    is_active = db.Column(
        db.Boolean, nullable=False, default=True, server_default="1"
    )
    doctors = db.relationship("Doctor", back_populates="department")
    __table_args__ = (
        db.Index(
            "uq_department_name_nocase",
            db.func.lower(name),
            unique=True,
        ),
    )


class Patient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    phone = db.Column(db.String(30))
    date_of_birth = db.Column(db.Date)
    address = db.Column(db.String(300))
    user = db.relationship("User", back_populates="patient_profile")
    appointments = db.relationship("Appointment", back_populates="patient")
    records = db.relationship("MedicalRecord", back_populates="patient")
    bills = db.relationship("Bill", back_populates="patient")


class Doctor(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"), nullable=False)
    phone = db.Column(db.String(30))
    specialty = db.Column(db.String(120), nullable=False)
    qualifications = db.Column(db.String(300))
    consultation_fee = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    user = db.relationship("User", back_populates="doctor_profile")
    department = db.relationship("Department", back_populates="doctors")
    appointments = db.relationship("Appointment", back_populates="doctor")
    records = db.relationship("MedicalRecord", back_populates="doctor")
    availability = db.relationship(
        "DoctorAvailability", back_populates="doctor", cascade="all, delete-orphan"
    )
    leaves = db.relationship(
        "DoctorLeave", back_populates="doctor", cascade="all, delete-orphan"
    )


class DoctorAvailability(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    doctor_id = db.Column(
        db.Integer, db.ForeignKey("doctor.id", ondelete="CASCADE"), nullable=False
    )
    weekday = db.Column(db.Integer, nullable=False)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    slot_duration_minutes = db.Column(db.Integer, nullable=False, default=30)
    break_start = db.Column(db.Time)
    break_end = db.Column(db.Time)
    doctor = db.relationship("Doctor", back_populates="availability")
    __table_args__ = (
        db.UniqueConstraint("doctor_id", "weekday", name="uq_doctor_availability_weekday"),
    )


class DoctorLeave(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    doctor_id = db.Column(
        db.Integer, db.ForeignKey("doctor.id", ondelete="CASCADE"), nullable=False
    )
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    reason = db.Column(db.String(300))
    doctor = db.relationship("Doctor", back_populates="leaves")


class Appointment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patient.id"), nullable=False)
    doctor_id = db.Column(db.Integer, db.ForeignKey("doctor.id"), nullable=False)
    starts_at = db.Column(db.DateTime, nullable=False, index=True)
    duration_minutes = db.Column(
        db.Integer, nullable=False, default=30, server_default="30"
    )
    reason = db.Column(db.String(500), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="scheduled", index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    patient = db.relationship("Patient", back_populates="appointments")
    doctor = db.relationship("Doctor", back_populates="appointments")
    medical_record = db.relationship(
        "MedicalRecord", back_populates="appointment", uselist=False
    )
    bill = db.relationship("Bill", back_populates="appointment", uselist=False)


class MedicalRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patient.id"), nullable=False)
    doctor_id = db.Column(db.Integer, db.ForeignKey("doctor.id"), nullable=False)
    appointment_id = db.Column(
        db.Integer, db.ForeignKey("appointment.id"), unique=True
    )
    symptoms = db.Column(db.Text)
    diagnosis = db.Column(db.String(1000), nullable=False)
    consultation_notes = db.Column(db.Text, nullable=False)
    treatment_notes = db.Column(db.Text)
    visit_date = db.Column(db.Date)
    prescription = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )
    patient = db.relationship("Patient", back_populates="records")
    doctor = db.relationship("Doctor", back_populates="records")
    appointment = db.relationship("Appointment", back_populates="medical_record")
    prescriptions = db.relationship(
        "Prescription", back_populates="medical_record", cascade="all, delete-orphan"
    )


class Prescription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    medical_record_id = db.Column(
        db.Integer,
        db.ForeignKey("medical_record.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    medicine_name = db.Column(db.String(200), nullable=False)
    dosage = db.Column(db.String(120), nullable=False)
    frequency = db.Column(db.String(120), nullable=False)
    duration = db.Column(db.String(120), nullable=False)
    instructions = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )
    medical_record = db.relationship("MedicalRecord", back_populates="prescriptions")


class Bill(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patient.id"), nullable=False)
    appointment_id = db.Column(
        db.Integer, db.ForeignKey("appointment.id"), unique=True
    )
    description = db.Column(db.String(300), nullable=False)
    invoice_number = db.Column(db.String(40), nullable=True)
    consultation_fee = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    service_charges = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    discount_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="unpaid")
    issued_at = db.Column(db.DateTime, nullable=False, default=datetime.now, index=True)
    paid_at = db.Column(db.DateTime)
    patient = db.relationship("Patient", back_populates="bills")
    appointment = db.relationship("Appointment", back_populates="bill")
    payments = db.relationship("Payment", back_populates="bill")
    __table_args__ = (
        db.Index("uq_bill_invoice_number", "invoice_number", unique=True),
    )

    @property
    def paid_amount(self):
        return sum(
            (payment.amount for payment in self.payments),
            Decimal("0.00"),
        )

    @property
    def outstanding_balance(self):
        return max(self.amount - self.paid_amount, Decimal("0.00"))

    @property
    def calculated_status(self):
        if self.amount > 0 and self.paid_amount >= self.amount:
            return "paid"
        if self.paid_amount > 0:
            return "partial"
        return "unpaid"

    def apply_payment_status(self, paid_amount):
        if self.amount > 0 and paid_amount >= self.amount:
            self.status = "paid"
            self.paid_at = self.paid_at or datetime.now()
        elif paid_amount > 0:
            self.status = "partial"
            self.paid_at = None
        else:
            self.status = "unpaid"
            self.paid_at = None


class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    bill_id = db.Column(
        db.Integer, db.ForeignKey("bill.id"), nullable=False, index=True
    )
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    payment_method = db.Column(db.String(30))
    received_at = db.Column(
        db.DateTime, nullable=False, default=datetime.now, index=True
    )
    bill = db.relationship("Bill", back_populates="payments")
