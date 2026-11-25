from datetime import date
from decimal import Decimal

from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    DateField,
    DecimalField,
    HiddenField,
    PasswordField,
    SelectField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.fields import TimeField
from wtforms.validators import (
    DataRequired,
    Email,
    EqualTo,
    InputRequired,
    Length,
    NumberRange,
    Optional,
    ValidationError,
)


def validate_currency_precision(form, field):
    value = field.data
    if value is not None and (
        not value.is_finite() or value.as_tuple().exponent < -2
    ):
        raise ValidationError("Use a valid amount with no more than two decimal places.")


class LoginForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=255)])
    password = PasswordField("Password", validators=[DataRequired()])
    submit = SubmitField("Sign in")


class RegistrationForm(FlaskForm):
    name = StringField("Full name", validators=[DataRequired(), Length(max=120)])
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=255)])
    phone = StringField("Phone", validators=[Optional(), Length(max=30)])
    password = PasswordField(
        "Password", validators=[DataRequired(), Length(min=12, max=128)]
    )
    confirm_password = PasswordField(
        "Confirm password", validators=[DataRequired(), EqualTo("password")]
    )
    submit = SubmitField("Create patient account")


class ProfileForm(FlaskForm):
    name = StringField("Full name", validators=[DataRequired(), Length(max=120)])
    phone = StringField("Phone", validators=[Optional(), Length(max=30)])
    date_of_birth = DateField("Date of birth", validators=[Optional()])
    address = StringField("Address", validators=[Optional(), Length(max=300)])
    submit = SubmitField("Save profile")


class AppointmentForm(FlaskForm):
    department_id = SelectField(
        "Department", coerce=int, validators=[DataRequired()]
    )
    doctor_id = SelectField("Doctor", coerce=int, validators=[DataRequired()])
    appointment_date = DateField("Date", validators=[DataRequired()])
    time_slot = SelectField(
        "Available time", choices=[], validators=[DataRequired()]
    )
    reason = TextAreaField("Reason for visit", validators=[DataRequired(), Length(max=500)])
    submit = SubmitField("Book appointment")


class DoctorAvailabilityForm(FlaskForm):
    weekday = SelectField(
        "Working day",
        coerce=int,
        choices=[
            (0, "Monday"),
            (1, "Tuesday"),
            (2, "Wednesday"),
            (3, "Thursday"),
            (4, "Friday"),
            (5, "Saturday"),
            (6, "Sunday"),
        ],
        validators=[InputRequired()],
    )
    start_time = TimeField("Start time", format="%H:%M", validators=[DataRequired()])
    end_time = TimeField("End time", format="%H:%M", validators=[DataRequired()])
    slot_duration_minutes = SelectField(
        "Appointment duration",
        coerce=int,
        choices=[(15, "15 minutes"), (20, "20 minutes"), (30, "30 minutes"),
                 (45, "45 minutes"), (60, "60 minutes")],
        validators=[DataRequired()],
    )
    break_start = TimeField(
        "Break starts", format="%H:%M", validators=[Optional()]
    )
    break_end = TimeField(
        "Break ends", format="%H:%M", validators=[Optional()]
    )
    submit = SubmitField("Save working hours")


class DoctorLeaveForm(FlaskForm):
    start_date = DateField("First unavailable date", validators=[DataRequired()])
    end_date = DateField("Last unavailable date", validators=[DataRequired()])
    reason = StringField("Reason (optional)", validators=[Optional(), Length(max=300)])
    submit = SubmitField("Add unavailable dates")


class DoctorProfileForm(FlaskForm):
    name = StringField("Full name", validators=[DataRequired(), Length(max=120)])
    specialty = StringField("Specialty", validators=[DataRequired(), Length(max=120)])
    qualifications = StringField(
        "Qualifications", validators=[Optional(), Length(max=300)]
    )
    submit = SubmitField("Save profile")


class RecordForm(FlaskForm):
    symptoms = TextAreaField("Symptoms", validators=[DataRequired(), Length(max=5000)])
    diagnosis = TextAreaField("Diagnosis", validators=[DataRequired(), Length(max=1000)])
    consultation_notes = TextAreaField(
        "Treatment notes", validators=[DataRequired(), Length(max=10000)]
    )
    visit_date = DateField("Visit date", validators=[DataRequired()])
    prescription = TextAreaField(
        "Prescription notes (legacy)", validators=[Optional(), Length(max=5000)]
    )
    submit = SubmitField("Save medical record")


class PrescriptionForm(FlaskForm):
    prescription_id = HiddenField()
    medicine_name = StringField(
        "Medicine name", validators=[DataRequired(), Length(max=200)]
    )
    dosage = StringField("Dosage", validators=[DataRequired(), Length(max=120)])
    frequency = StringField(
        "Frequency", validators=[DataRequired(), Length(max=120)]
    )
    duration = StringField("Duration", validators=[DataRequired(), Length(max=120)])
    instructions = TextAreaField(
        "Instructions", validators=[Optional(), Length(max=5000)]
    )
    submit = SubmitField("Save prescription")


class DoctorCreateForm(FlaskForm):
    name = StringField("Full name", validators=[DataRequired(), Length(max=120)])
    email = StringField(
        "Email",
        validators=[DataRequired(), Email(), Length(max=255)],
        render_kw={"type": "email", "autocomplete": "email"},
    )
    password = PasswordField(
        "Temporary password", validators=[DataRequired(), Length(min=12, max=128)]
    )
    phone = StringField("Phone", validators=[DataRequired(), Length(max=30)])
    department_id = SelectField("Department", coerce=int, validators=[DataRequired()])
    specialty = StringField("Specialty", validators=[DataRequired(), Length(max=120)])
    qualifications = StringField(
        "Qualifications", validators=[DataRequired(), Length(max=300)]
    )
    consultation_fee = DecimalField(
        "Consultation fee", validators=[DataRequired(), NumberRange(min=0)]
    )
    submit = SubmitField("Create doctor")


class DoctorEditForm(FlaskForm):
    name = StringField("Full name", validators=[DataRequired(), Length(max=120)])
    email = StringField(
        "Email",
        validators=[DataRequired(), Email(), Length(max=255)],
        render_kw={"type": "email", "autocomplete": "email"},
    )
    phone = StringField("Phone", validators=[DataRequired(), Length(max=30)])
    department_id = SelectField("Department", coerce=int, validators=[DataRequired()])
    specialty = StringField("Specialty", validators=[DataRequired(), Length(max=120)])
    qualifications = StringField(
        "Qualifications", validators=[DataRequired(), Length(max=300)]
    )
    consultation_fee = DecimalField(
        "Consultation fee", validators=[DataRequired(), NumberRange(min=0)]
    )
    submit = SubmitField("Save doctor")


class DepartmentForm(FlaskForm):
    name = StringField("Department name", validators=[DataRequired(), Length(max=100)])
    description = TextAreaField(
        "Description", validators=[Optional(), Length(max=500)]
    )
    submit = SubmitField("Add department")


class DepartmentEditForm(FlaskForm):
    name = StringField("Department name", validators=[DataRequired(), Length(max=100)])
    description = TextAreaField(
        "Description", validators=[Optional(), Length(max=500)]
    )
    submit = SubmitField("Save changes")


class BillForm(FlaskForm):
    patient_id = SelectField("Patient", coerce=int, validators=[DataRequired()])
    appointment_id = SelectField(
        "Appointment (optional)", coerce=int, validators=[Optional()]
    )
    description = StringField(
        "Description", validators=[DataRequired(), Length(max=300)]
    )
    consultation_fee = DecimalField(
        "Consultation fee",
        default=Decimal("0.00"),
        validators=[
            InputRequired(),
            validate_currency_precision,
            NumberRange(min=0, max=Decimal("99999999.99")),
        ],
    )
    service_charges = DecimalField(
        "Service charges",
        default=Decimal("0.00"),
        validators=[
            InputRequired(),
            validate_currency_precision,
            NumberRange(min=0, max=Decimal("99999999.99")),
        ],
    )
    discount_amount = DecimalField(
        "Discount",
        default=Decimal("0.00"),
        validators=[
            InputRequired(),
            validate_currency_precision,
            NumberRange(min=0, max=Decimal("99999999.99")),
        ],
    )
    submit = SubmitField("Create bill")


class BillEditForm(FlaskForm):
    description = StringField(
        "Description", validators=[DataRequired(), Length(max=300)]
    )
    consultation_fee = DecimalField(
        "Consultation fee",
        validators=[
            InputRequired(),
            validate_currency_precision,
            NumberRange(min=0, max=Decimal("99999999.99")),
        ],
    )
    service_charges = DecimalField(
        "Service charges",
        validators=[
            InputRequired(),
            validate_currency_precision,
            NumberRange(min=0, max=Decimal("99999999.99")),
        ],
    )
    discount_amount = DecimalField(
        "Discount",
        validators=[
            InputRequired(),
            validate_currency_precision,
            NumberRange(min=0, max=Decimal("99999999.99")),
        ],
    )
    submit = SubmitField("Save bill")


class PaymentForm(FlaskForm):
    amount = DecimalField(
        "Payment amount",
        validators=[
            InputRequired(),
            validate_currency_precision,
            NumberRange(min=Decimal("0.01"), max=Decimal("99999999.99")),
        ],
    )
    payment_method = SelectField(
        "Payment method",
        choices=[
            ("cash", "Cash"),
            ("upi", "UPI"),
            ("card", "Card"),
            ("bank_transfer", "Bank Transfer"),
        ],
        validators=[DataRequired()],
    )
    submit = SubmitField("Record payment")


class ReportFilterForm(FlaskForm):
    class Meta:
        csrf = False

    start_date = DateField("From", validators=[DataRequired()])
    end_date = DateField("To", validators=[DataRequired()])

    def validate_end_date(self, field):
        if self.start_date.data and field.data and field.data < self.start_date.data:
            raise ValidationError("End date must be on or after the start date.")
        if field.data == date.max:
            raise ValidationError("End date is outside the supported range.")


class AppointmentStatusForm(FlaskForm):
    status = SelectField(
        "Status",
        choices=[
            ("scheduled", "Scheduled"),
            ("confirmed", "Confirmed"),
            ("completed", "Completed"),
            ("cancelled", "Cancelled"),
        ],
        validators=[DataRequired()],
    )
    submit = SubmitField("Update status")


class ConfirmForm(FlaskForm):
    submit = SubmitField("Confirm")


class PatientAccountForm(FlaskForm):
    is_active = BooleanField("Account active")
    submit = SubmitField("Save account")
