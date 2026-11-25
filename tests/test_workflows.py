import csv
import io
import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path

from flask import abort
from medicare import create_app
from medicare.extensions import db
from medicare.models import (
    Appointment,
    Bill,
    Department,
    Doctor,
    DoctorAvailability,
    DoctorLeave,
    MedicalRecord,
    Patient,
    Payment,
    Prescription,
    User,
)


class MediCareWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(
            {
                "TESTING": True,
                "WTF_CSRF_ENABLED": False,
                "SQLALCHEMY_DATABASE_URI": "sqlite://",
                "SECRET_KEY": "test-key",
            }
        )
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.client = self.app.test_client()

        department = Department(name="General Medicine")
        self.patient_user = User(name="Pat Example", email="patient@example.com")
        self.patient_user.set_password("patient-password-123")
        self.patient_user.patient_profile = Patient()
        self.doctor_user = User(name="Doc Example", email="doctor@example.com", role="doctor")
        self.doctor_user.set_password("doctor-password-123")
        self.doctor_user.doctor_profile = Doctor(
            department=department, specialty="General Medicine"
        )
        other_doctor_user = User(
            name="Other Doctor", email="other@example.com", role="doctor"
        )
        other_doctor_user.set_password("other-password-123")
        other_doctor_user.doctor_profile = Doctor(
            department=department, specialty="Cardiology"
        )
        self.admin_user = User(name="Admin Example", email="admin@example.com", role="admin")
        self.admin_user.set_password("admin-password-123")
        db.session.add_all(
            [
                department,
                self.patient_user,
                self.doctor_user,
                other_doctor_user,
                self.admin_user,
            ]
        )
        db.session.commit()
        self.patient = self.patient_user.patient_profile
        self.doctor = self.doctor_user.doctor_profile
        self.other_doctor = other_doctor_user.doctor_profile
        for doctor in (self.doctor, self.other_doctor):
            db.session.add_all(
                [
                    DoctorAvailability(
                        doctor=doctor,
                        weekday=weekday,
                        start_time=time(0, 0),
                        end_time=time(23, 59),
                        slot_duration_minutes=30,
                    )
                    for weekday in range(7)
                ]
            )
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
        self.context.pop()

    def sign_in(self, email, password):
        return self.client.post(
            "/auth/login",
            data={"email": email, "password": password},
            follow_redirects=False,
        )

    def test_patient_registration_hashes_password(self):
        response = self.client.post(
            "/auth/register",
            data={
                "name": "New Patient",
                "email": "new@example.com",
                "phone": "555-0101",
                "password": "a-very-strong-password",
                "confirm_password": "a-very-strong-password",
            },
        )

        self.assertEqual(response.status_code, 302)
        new_user = User.query.filter_by(email="new@example.com").one()
        self.assertNotEqual(new_user.password_hash, "a-very-strong-password")
        self.assertTrue(new_user.check_password("a-very-strong-password"))
        self.assertIsNotNone(new_user.patient_profile)

    def test_seed_admin_rejects_invalid_email(self):
        result = self.app.test_cli_runner().invoke(
            args=["seed-admin"],
            env={
                "ADMIN_EMAIL": "not-an-email",
                "ADMIN_PASSWORD": "a-very-strong-password",
            },
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("ADMIN_EMAIL must be a valid email address.", result.output)
        self.assertEqual(User.query.filter_by(role="admin").count(), 1)

    def test_authentication_sets_secure_local_session_and_rejects_open_redirect(self):
        response = self.client.post(
            "/auth/login?next=https%3A%2F%2Fevil.example",
            data={
                "email": "patient@example.com",
                "password": "patient-password-123",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/dashboard"))
        set_cookie = response.headers["Set-Cookie"]
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Lax", set_cookie)

        self.client.post("/auth/logout")
        response = self.client.post(
            "/auth/login?next=%2Fappointments",
            data={
                "email": "patient@example.com",
                "password": "patient-password-123",
            },
        )
        self.assertEqual(response.location, "/appointments")

    def test_csrf_rejects_state_changing_request_without_token(self):
        self.app.config["WTF_CSRF_ENABLED"] = True
        response = self.client.post(
            "/auth/register",
            data={
                "name": "Forged Patient",
                "email": "forged@example.com",
                "password": "a-very-strong-password",
                "confirm_password": "a-very-strong-password",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(User.query.filter_by(email="forged@example.com").first())

    def test_search_is_parameterized_and_html_escapes_user_input(self):
        self.sign_in("admin@example.com", "admin-password-123")
        injection = self.client.get(
            "/admin/doctors",
            query_string={"q": "%' OR 1=1 --"},
        )
        xss = self.client.get(
            "/admin/doctors",
            query_string={"q": "<script>alert(1)</script>"},
        )
        self.assertEqual(injection.status_code, 200)
        self.assertIn(b"No doctors match these filters.", injection.data)
        self.assertEqual(xss.status_code, 200)
        self.assertNotIn(b"<script>alert(1)</script>", xss.data)
        self.assertIn(b"&lt;script&gt;", xss.data)

    def test_error_pages_hide_exception_details(self):
        def bad_request_view():
            return abort(400)

        def forbidden_view():
            return abort(403)

        self.app.add_url_rule(
            "/_test_bad_request",
            endpoint="test_bad_request_view",
            view_func=bad_request_view,
        )
        self.app.add_url_rule(
            "/_test_forbidden",
            endpoint="test_forbidden_view",
            view_func=forbidden_view,
        )

        def raise_unexpected_error():
            raise RuntimeError("test-only sentinel")

        self.app.add_url_rule(
            "/_test_server_error",
            view_func=raise_unexpected_error,
        )
        self.app.config["PROPAGATE_EXCEPTIONS"] = False
        for path, code in (
            ("/_test_bad_request", 400),
            ("/_test_forbidden", 403),
            ("/missing-page", 404),
            ("/_test_server_error", 500),
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, code)
                self.assertIn(str(code).encode(), response.data)
                self.assertNotIn(b"test-only sentinel", response.data)
        self.assertNotIn(b"RuntimeError", response.data)

    def test_app_rejects_non_sqlite_database_configuration(self):
        with self.assertRaisesRegex(RuntimeError, "local SQLite only"):
            create_app(
                {
                    "TESTING": True,
                    "SQLALCHEMY_DATABASE_URI": "mysql+pymysql://localhost/medicare",
                }
            )

    def test_booking_rejects_overlapping_doctor_slot(self):
        self.sign_in("patient@example.com", "patient-password-123")
        appointment_date = date.today() + timedelta(days=2)
        first = self.client.post(
            "/appointments/book",
            data={
                "department_id": str(self.doctor.department_id),
                "doctor_id": str(self.doctor.id),
                "appointment_date": appointment_date.isoformat(),
                "time_slot": "10:00",
                "reason": "Consultation",
            },
        )
        second = self.client.post(
            "/appointments/book",
            data={
                "department_id": str(self.doctor.department_id),
                "doctor_id": str(self.doctor.id),
                "appointment_date": appointment_date.isoformat(),
                "time_slot": "10:00",
                "reason": "Follow-up",
            },
        )

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(Appointment.query.filter_by(doctor_id=self.doctor.id).count(), 1)

    def test_booking_form_displays_available_dates_and_slots(self):
        self.sign_in("patient@example.com", "patient-password-123")
        appointment_date = date.today() + timedelta(days=2)

        response = self.client.get(
            "/appointments/book",
            query_string={
                "department_id": self.doctor.department_id,
                "doctor_id": self.doctor.id,
                "appointment_date": appointment_date.isoformat(),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Available dates", response.data)
        self.assertIn(b"Available times", response.data)
        self.assertIn(b"10:00 AM", response.data)

    def test_booking_within_working_hours_is_allowed(self):
        self.sign_in("patient@example.com", "patient-password-123")
        appointment_date = date.today() + timedelta(days=2)

        response = self.client.post(
            "/appointments/book",
            data={
                "department_id": str(self.doctor.department_id),
                "doctor_id": str(self.doctor.id),
                "appointment_date": appointment_date.isoformat(),
                "time_slot": "10:00",
                "reason": "Consultation",
            },
        )

        appointment = Appointment.query.one()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(appointment.starts_at, datetime.combine(appointment_date, time(10)))
        self.assertEqual(appointment.duration_minutes, 30)

    def test_booking_outside_working_hours_is_rejected(self):
        appointment_date = date.today() + timedelta(days=2)
        db.session.query(DoctorAvailability).filter_by(
            doctor_id=self.doctor.id, weekday=appointment_date.weekday()
        ).update(
            {
                DoctorAvailability.start_time: time(9),
                DoctorAvailability.end_time: time(17),
            }
        )
        db.session.commit()
        self.sign_in("patient@example.com", "patient-password-123")

        response = self.client.post(
            "/appointments/book",
            data={
                "department_id": str(self.doctor.department_id),
                "doctor_id": str(self.doctor.id),
                "appointment_date": appointment_date.isoformat(),
                "time_slot": "08:30",
                "reason": "Consultation",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Appointment.query.count(), 0)

    def test_booking_during_break_is_rejected(self):
        appointment_date = date.today() + timedelta(days=2)
        db.session.query(DoctorAvailability).filter_by(
            doctor_id=self.doctor.id, weekday=appointment_date.weekday()
        ).update(
            {
                DoctorAvailability.start_time: time(9),
                DoctorAvailability.end_time: time(13),
                DoctorAvailability.break_start: time(10),
                DoctorAvailability.break_end: time(11),
            }
        )
        db.session.commit()
        self.sign_in("patient@example.com", "patient-password-123")

        response = self.client.post(
            "/appointments/book",
            data={
                "department_id": str(self.doctor.department_id),
                "doctor_id": str(self.doctor.id),
                "appointment_date": appointment_date.isoformat(),
                "time_slot": "10:00",
                "reason": "Consultation",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Appointment.query.count(), 0)

    def test_doctor_can_create_and_edit_weekly_availability(self):
        weekday = 0
        db.session.query(DoctorAvailability).filter_by(
            doctor_id=self.doctor.id, weekday=weekday
        ).delete()
        db.session.commit()
        self.sign_in("doctor@example.com", "doctor-password-123")
        schedule_data = {
            "weekday": str(weekday),
            "start_time": "09:00",
            "end_time": "17:00",
            "slot_duration_minutes": "45",
            "break_start": "12:00",
            "break_end": "12:30",
        }

        create_response = self.client.post(
            "/availability/schedule", data=schedule_data
        )
        schedule = DoctorAvailability.query.filter_by(
            doctor_id=self.doctor.id, weekday=weekday
        ).one()
        schedule_data.update({"start_time": "10:00", "end_time": "18:00"})
        edit_response = self.client.post(
            "/availability/schedule", data=schedule_data
        )

        self.assertEqual(create_response.status_code, 302)
        self.assertEqual(edit_response.status_code, 302)
        self.assertEqual(
            DoctorAvailability.query.filter_by(
                doctor_id=self.doctor.id, weekday=weekday
            ).count(),
            1,
        )
        self.assertEqual(schedule.start_time, time(10))
        self.assertEqual(schedule.end_time, time(18))
        self.assertEqual(schedule.slot_duration_minutes, 45)
        self.assertEqual(schedule.break_start, time(12))
        self.assertEqual(schedule.break_end, time(12, 30))
        page = self.client.get("/availability")
        edit_page = self.client.get("/availability?weekday=0")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(edit_page.status_code, 200)
        self.assertIn(b"Weekly schedule", page.data)
        self.assertIn(b'value="10:00"', edit_page.data)

    def test_admin_can_manage_doctor_availability_but_patient_cannot(self):
        weekday = (date.today() + timedelta(days=7)).weekday()
        self.sign_in("admin@example.com", "admin-password-123")

        response = self.client.post(
            "/availability/schedule",
            data={
                "doctor_id": str(self.other_doctor.id),
                "weekday": str(weekday),
                "start_time": "08:00",
                "end_time": "16:00",
                "slot_duration_minutes": "30",
                "break_start": "",
                "break_end": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        other_schedule = DoctorAvailability.query.filter_by(
            doctor_id=self.other_doctor.id, weekday=weekday
        ).one()
        self.assertEqual(other_schedule.start_time, time(8))
        admin_page = self.client.get(
            "/availability", query_string={"doctor_id": self.other_doctor.id}
        )
        self.assertEqual(admin_page.status_code, 200)

        self.client.post("/auth/logout")
        self.sign_in("patient@example.com", "patient-password-123")
        forbidden = self.client.get("/availability")
        self.assertEqual(forbidden.status_code, 403)

    def test_doctor_leave_blocks_booking_for_unavailable_date(self):
        unavailable_date = date.today() + timedelta(days=2)
        self.sign_in("doctor@example.com", "doctor-password-123")

        leave_response = self.client.post(
            "/availability/leave",
            data={
                "start_date": unavailable_date.isoformat(),
                "end_date": unavailable_date.isoformat(),
                "reason": "On leave",
            },
        )
        self.assertEqual(leave_response.status_code, 302)
        self.assertEqual(DoctorLeave.query.count(), 1)

        self.client.post("/auth/logout")
        self.sign_in("patient@example.com", "patient-password-123")
        booking_response = self.client.post(
            "/appointments/book",
            data={
                "department_id": str(self.doctor.department_id),
                "doctor_id": str(self.doctor.id),
                "appointment_date": unavailable_date.isoformat(),
                "time_slot": "10:00",
                "reason": "Consultation",
            },
        )

        self.assertEqual(booking_response.status_code, 200)
        self.assertEqual(Appointment.query.count(), 0)

    def test_patient_can_cancel_only_their_own_future_appointment(self):
        other_patient_user = User(
            name="Other Patient", email="other.patient@example.com"
        )
        other_patient_user.set_password("other-patient-password")
        other_patient_user.patient_profile = Patient()
        db.session.add(other_patient_user)
        db.session.flush()
        own_appointment = Appointment(
            patient_id=self.patient.id,
            doctor_id=self.doctor.id,
            starts_at=datetime.now() + timedelta(days=3),
            reason="My appointment",
            status="confirmed",
        )
        other_appointment = Appointment(
            patient_id=other_patient_user.patient_profile.id,
            doctor_id=self.doctor.id,
            starts_at=datetime.now() + timedelta(days=4),
            reason="Other patient's appointment",
        )
        db.session.add_all([own_appointment, other_appointment])
        db.session.commit()
        self.sign_in("patient@example.com", "patient-password-123")

        own_response = self.client.post(
            f"/appointments/{own_appointment.id}/cancel"
        )
        other_response = self.client.post(
            f"/appointments/{other_appointment.id}/cancel"
        )

        self.assertEqual(own_response.status_code, 302)
        self.assertEqual(own_appointment.status, "cancelled")
        self.assertEqual(other_response.status_code, 403)
        self.assertEqual(other_appointment.status, "scheduled")

    def test_doctor_appointment_list_contains_only_assigned_appointments(self):
        own_appointment = Appointment(
            patient_id=self.patient.id,
            doctor_id=self.doctor.id,
            starts_at=datetime.now() + timedelta(days=3),
            reason="Assigned appointment",
        )
        other_appointment = Appointment(
            patient_id=self.patient.id,
            doctor_id=self.other_doctor.id,
            starts_at=datetime.now() + timedelta(days=4),
            reason="Private other-doctor appointment",
        )
        db.session.add_all([own_appointment, other_appointment])
        db.session.commit()
        self.sign_in("doctor@example.com", "doctor-password-123")

        response = self.client.get("/appointments")
        update_response = self.client.post(
            f"/appointments/{own_appointment.id}/status",
            data={"status": "confirmed"},
        )
        forbidden_update = self.client.post(
            f"/appointments/{other_appointment.id}/status",
            data={"status": "confirmed"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Assigned appointment", response.data)
        self.assertNotIn(b"Private other-doctor appointment", response.data)
        self.assertEqual(update_response.status_code, 302)
        self.assertEqual(own_appointment.status, "confirmed")
        self.assertEqual(forbidden_update.status_code, 403)

    def test_admin_can_manage_all_appointment_statuses_without_deleting_history(self):
        past_appointment = Appointment(
            patient_id=self.patient.id,
            doctor_id=self.doctor.id,
            starts_at=datetime.now() - timedelta(days=2),
            reason="Historical appointment",
            status="completed",
        )
        current_appointment = Appointment(
            patient_id=self.patient.id,
            doctor_id=self.other_doctor.id,
            starts_at=datetime.now() + timedelta(days=3),
            reason="Admin-managed appointment",
        )
        db.session.add_all([past_appointment, current_appointment])
        db.session.commit()
        past_id = past_appointment.id
        current_id = current_appointment.id
        self.sign_in("admin@example.com", "admin-password-123")

        listing = self.client.get("/admin/appointments")
        update = self.client.post(
            f"/admin/appointments/{current_id}/status",
            data={"status": "confirmed"},
        )

        self.assertEqual(listing.status_code, 200)
        self.assertIn(b"Historical appointment", listing.data)
        self.assertIn(b"Admin-managed appointment", listing.data)
        self.assertEqual(update.status_code, 302)
        self.assertEqual(db.session.get(Appointment, current_id).status, "confirmed")
        self.assertIsNotNone(db.session.get(Appointment, past_id))

    def test_doctor_cannot_edit_another_doctors_appointment(self):
        appointment = Appointment(
            patient_id=self.patient.id,
            doctor_id=self.other_doctor.id,
            starts_at=datetime.now() + timedelta(days=1),
            reason="Consultation",
        )
        db.session.add(appointment)
        db.session.commit()
        self.sign_in("doctor@example.com", "doctor-password-123")

        response = self.client.get(f"/appointments/{appointment.id}/record")

        self.assertEqual(response.status_code, 403)

    def test_doctor_can_create_and_update_medical_record(self):
        appointment = Appointment(
            patient_id=self.patient.id,
            doctor_id=self.doctor.id,
            starts_at=datetime.now() + timedelta(days=1),
            reason="Consultation",
        )
        db.session.add(appointment)
        db.session.commit()
        self.sign_in("doctor@example.com", "doctor-password-123")
        record_data = {
            "symptoms": "Headache and fatigue",
            "diagnosis": "Migraine",
            "consultation_notes": "Rest and monitor symptoms.",
            "visit_date": date.today().isoformat(),
            "prescription": "Legacy note retained",
        }

        create_response = self.client.post(
            f"/appointments/{appointment.id}/record", data=record_data
        )
        record = MedicalRecord.query.one()
        record_id = record.id
        update_data = {
            "symptoms": "Headache, fatigue and nausea",
            "diagnosis": "Updated diagnosis",
            "consultation_notes": "Follow-up treatment plan.",
            "visit_date": (date.today() - timedelta(days=1)).isoformat(),
            "prescription": "Updated historical note",
        }
        update_response = self.client.post(
            f"/appointments/{appointment.id}/record", data=update_data
        )

        self.assertEqual(create_response.status_code, 302)
        self.assertEqual(update_response.status_code, 302)
        self.assertEqual(MedicalRecord.query.count(), 1)
        self.assertEqual(record.id, record_id)
        self.assertEqual(record.patient_id, self.patient.id)
        self.assertEqual(record.doctor_id, self.doctor.id)
        self.assertEqual(record.appointment_id, appointment.id)
        self.assertEqual(record.symptoms, update_data["symptoms"])
        self.assertEqual(record.diagnosis, update_data["diagnosis"])
        self.assertEqual(record.treatment_notes, update_data["consultation_notes"])
        self.assertEqual(record.consultation_notes, update_data["consultation_notes"])
        self.assertEqual(record.visit_date, date.fromisoformat(update_data["visit_date"]))
        self.assertEqual(record.prescription, update_data["prescription"])

    def test_doctor_can_create_and_edit_structured_prescriptions(self):
        record = MedicalRecord(
            patient_id=self.patient.id,
            doctor_id=self.doctor.id,
            symptoms="Cough",
            diagnosis="Seasonal illness",
            consultation_notes="Hydrate and rest.",
            treatment_notes="Hydrate and rest.",
            visit_date=date.today(),
        )
        db.session.add(record)
        db.session.commit()
        self.sign_in("doctor@example.com", "doctor-password-123")
        prescription_data = {
            "medicine_name": "Example Medicine",
            "dosage": "10 mg",
            "frequency": "Twice daily",
            "duration": "5 days",
            "instructions": "Take after meals.",
        }

        create_response = self.client.post(
            f"/records/{record.id}/prescriptions", data=prescription_data
        )
        prescription = Prescription.query.one()
        edit_page = self.client.get(
            f"/records/{record.id}",
            query_string={"edit_prescription": prescription.id},
        )
        edit_response = self.client.post(
            f"/records/{record.id}/prescriptions",
            data={
                "prescription_id": str(prescription.id),
                "medicine_name": "Example Medicine",
                "dosage": "20 mg",
                "frequency": "Once daily",
                "duration": "7 days",
                "instructions": "Take with water.",
            },
        )
        detail_response = self.client.get(f"/records/{record.id}")

        self.assertEqual(create_response.status_code, 302)
        self.assertEqual(edit_page.status_code, 200)
        self.assertIn(
            f'name="prescription_id" type="hidden" value="{prescription.id}"'.encode(),
            edit_page.data,
        )
        self.assertEqual(edit_response.status_code, 302)
        self.assertEqual(Prescription.query.count(), 1)
        self.assertEqual(prescription.dosage, "20 mg")
        self.assertEqual(prescription.frequency, "Once daily")
        self.assertEqual(prescription.duration, "7 days")
        self.assertEqual(prescription.instructions, "Take with water.")
        self.assertEqual(detail_response.status_code, 200)
        self.assertIn(b"Example Medicine", detail_response.data)
        self.assertIn(b"Take with water.", detail_response.data)

    def test_medical_record_and_prescription_forms_validate_required_fields(self):
        appointment = Appointment(
            patient_id=self.patient.id,
            doctor_id=self.doctor.id,
            starts_at=datetime.now() + timedelta(days=1),
            reason="Consultation",
        )
        db.session.add(appointment)
        db.session.commit()
        self.sign_in("doctor@example.com", "doctor-password-123")

        record_response = self.client.post(
            f"/appointments/{appointment.id}/record",
            data={
                "diagnosis": "Diagnosis",
                "consultation_notes": "Treatment",
                "visit_date": date.today().isoformat(),
            },
        )
        record = MedicalRecord.query.one_or_none()
        self.assertEqual(record_response.status_code, 200)
        self.assertIsNone(record)

        record = MedicalRecord(
            patient_id=self.patient.id,
            doctor_id=self.doctor.id,
            symptoms="Symptoms",
            diagnosis="Diagnosis",
            consultation_notes="Treatment",
            treatment_notes="Treatment",
            visit_date=date.today(),
        )
        db.session.add(record)
        db.session.commit()
        prescription_response = self.client.post(
            f"/records/{record.id}/prescriptions",
            data={
                "medicine_name": "Medicine",
                "dosage": "5 mg",
                "frequency": "",
                "duration": "3 days",
            },
        )

        self.assertEqual(prescription_response.status_code, 200)
        self.assertEqual(Prescription.query.count(), 0)

    def test_patient_views_only_their_medical_history_and_prescriptions(self):
        other_patient_user = User(
            name="Other Patient", email="other.patient@example.com"
        )
        other_patient_user.set_password("other-patient-password")
        other_patient_user.patient_profile = Patient()
        db.session.add(other_patient_user)
        db.session.flush()
        own_record = MedicalRecord(
            patient_id=self.patient.id,
            doctor_id=self.doctor.id,
            symptoms="Own symptoms",
            diagnosis="Own diagnosis",
            consultation_notes="Own treatment",
            treatment_notes="Own treatment",
            visit_date=date.today(),
        )
        other_record = MedicalRecord(
            patient_id=other_patient_user.patient_profile.id,
            doctor_id=self.doctor.id,
            symptoms="Private symptoms",
            diagnosis="Private diagnosis",
            consultation_notes="Private treatment",
            treatment_notes="Private treatment",
            visit_date=date.today(),
        )
        own_record.prescriptions.append(
            Prescription(
                medicine_name="Patient medicine",
                dosage="1 tablet",
                frequency="Daily",
                duration="3 days",
                instructions="With food",
            )
        )
        db.session.add_all([own_record, other_record])
        db.session.commit()
        other_record_id = other_record.id
        self.sign_in("patient@example.com", "patient-password-123")

        listing = self.client.get("/records")
        detail = self.client.get(f"/records/{own_record.id}")
        forbidden = self.client.get(f"/records/{other_record_id}")

        self.assertEqual(listing.status_code, 200)
        self.assertIn(b"Own diagnosis", listing.data)
        self.assertIn(b"Patient medicine", listing.data)
        self.assertNotIn(b"Private diagnosis", listing.data)
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b"With food", detail.data)
        self.assertEqual(forbidden.status_code, 403)

    def test_doctor_cannot_access_another_doctors_records_or_prescriptions(self):
        other_record = MedicalRecord(
            patient_id=self.patient.id,
            doctor_id=self.other_doctor.id,
            symptoms="Private symptoms",
            diagnosis="Private diagnosis",
            consultation_notes="Private treatment",
            treatment_notes="Private treatment",
            visit_date=date.today(),
        )
        db.session.add(other_record)
        db.session.commit()
        self.sign_in("doctor@example.com", "doctor-password-123")

        detail = self.client.get(f"/records/{other_record.id}")
        edit = self.client.get(f"/records/{other_record.id}/edit")
        add_prescription = self.client.post(
            f"/records/{other_record.id}/prescriptions",
            data={
                "medicine_name": "Unauthorized medicine",
                "dosage": "1",
                "frequency": "Daily",
                "duration": "1 day",
            },
        )

        self.assertEqual(detail.status_code, 403)
        self.assertEqual(edit.status_code, 403)
        self.assertEqual(add_prescription.status_code, 403)
        self.assertEqual(Prescription.query.count(), 0)

    def test_admin_can_monitor_records_without_editing_them(self):
        record = MedicalRecord(
            patient_id=self.patient.id,
            doctor_id=self.doctor.id,
            symptoms="Monitor symptoms",
            diagnosis="Monitored diagnosis",
            consultation_notes="Monitored treatment",
            treatment_notes="Monitored treatment",
            visit_date=date.today(),
        )
        db.session.add(record)
        db.session.commit()
        self.sign_in("admin@example.com", "admin-password-123")

        listing = self.client.get("/admin/records")
        detail = self.client.get(f"/records/{record.id}")
        forbidden_edit = self.client.get(f"/records/{record.id}/edit")

        self.assertEqual(listing.status_code, 200)
        self.assertIn(b"Monitored diagnosis", listing.data)
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b"Monitor symptoms", detail.data)
        self.assertEqual(forbidden_edit.status_code, 403)

    def test_medical_record_migration_backfills_legacy_data_and_is_repeatable(self):
        record = MedicalRecord(
            patient_id=self.patient.id,
            doctor_id=self.doctor.id,
            diagnosis="Legacy diagnosis",
            consultation_notes="Legacy treatment",
            created_at=datetime(2024, 5, 6, 12, 0),
        )
        db.session.add(record)
        db.session.commit()

        runner = self.app.test_cli_runner()
        first = runner.invoke(args=["migrate-medical-records"])
        second = runner.invoke(args=["migrate-medical-records"])
        db.session.expire_all()
        migrated_record = db.session.get(MedicalRecord, record.id)

        self.assertEqual(first.exit_code, 0, first.output)
        self.assertEqual(second.exit_code, 0, second.output)
        self.assertEqual(MedicalRecord.query.count(), 1)
        self.assertEqual(migrated_record.treatment_notes, "Legacy treatment")
        self.assertEqual(migrated_record.visit_date, date(2024, 5, 6))

    def test_role_dashboards_and_management_pages_render(self):
        role_pages = {
            "patient@example.com": (
                "patient-password-123",
                ["/dashboard", "/profile", "/appointments", "/records", "/bills"],
            ),
            "doctor@example.com": (
                "doctor-password-123",
                ["/dashboard", "/doctor/profile", "/appointments", "/records"],
            ),
            "admin@example.com": (
                "admin-password-123",
                [
                    "/dashboard",
                    "/admin/",
                    "/admin/doctors",
                    "/admin/departments",
                    "/admin/patients",
                    "/admin/appointments",
                    "/admin/records",
                    "/admin/billing",
                ],
            ),
        }
        for email, (password, paths) in role_pages.items():
            with self.subTest(role=email):
                self.sign_in(email, password)
                for path in paths:
                    response = self.client.get(path, follow_redirects=True)
                    self.assertEqual(response.status_code, 200, path)
                self.client.post("/auth/logout")

    def test_admin_can_create_bill_and_record_payment(self):
        self.sign_in("admin@example.com", "admin-password-123")
        response = self.client.post(
            "/admin/billing",
            data={
                "patient_id": str(self.patient.id),
                "appointment_id": "0",
                "description": "Consultation charge",
                "consultation_fee": "30.00",
                "service_charges": "10.00",
                "discount_amount": "4.50",
            },
        )
        bill = Bill.query.one()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(bill.status, "unpaid")
        self.assertEqual(str(bill.amount), "35.50")
        self.assertRegex(bill.invoice_number, r"^INV-\d{4}-\d+$")

        response = self.client.post(f"/admin/billing/{bill.id}/mark-paid")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(bill.status, "unpaid")
        self.assertEqual(Payment.query.count(), 0)

        response = self.client.post(
            f"/admin/billing/{bill.id}/payments",
            data={"amount": "10.00", "payment_method": "upi"},
        )
        db.session.refresh(bill)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(bill.status, "partial")
        self.assertEqual(str(bill.paid_amount), "10.00")
        self.assertEqual(str(bill.outstanding_balance), "25.50")

        response = self.client.post(
            f"/admin/billing/{bill.id}/payments",
            data={"amount": "25.50", "payment_method": "card"},
        )
        db.session.refresh(bill)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(bill.status, "paid")
        self.assertEqual(str(bill.paid_amount), "35.50")
        self.assertEqual(bill.outstanding_balance, 0)
        self.assertIsNotNone(bill.paid_at)

    def test_bill_rejects_excess_discount_and_overpayment(self):
        self.sign_in("admin@example.com", "admin-password-123")
        response = self.client.post(
            "/admin/billing",
            data={
                "patient_id": str(self.patient.id),
                "appointment_id": "0",
                "description": "Consultation charge",
                "consultation_fee": "20.00",
                "service_charges": "5.00",
                "discount_amount": "25.01",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Bill.query.count(), 0)

        response = self.client.post(
            "/admin/billing",
            data={
                "patient_id": str(self.patient.id),
                "appointment_id": "0",
                "description": "Consultation charge",
                "consultation_fee": "20.001",
                "service_charges": "5.00",
                "discount_amount": "0.00",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Bill.query.count(), 0)

        self.client.post(
            "/admin/billing",
            data={
                "patient_id": str(self.patient.id),
                "appointment_id": "0",
                "description": "Consultation charge",
                "consultation_fee": "20.00",
                "service_charges": "5.00",
                "discount_amount": "5.00",
            },
        )
        bill = Bill.query.one()
        invalid_method = self.client.post(
            f"/admin/billing/{bill.id}/payments",
            data={"amount": "1.00", "payment_method": "crypto"},
        )
        self.assertEqual(invalid_method.status_code, 302)
        self.assertEqual(Payment.query.count(), 0)
        response = self.client.post(
            f"/admin/billing/{bill.id}/payments",
            data={"amount": "20.01", "payment_method": "cash"},
        )
        db.session.refresh(bill)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(bill.status, "unpaid")
        self.assertEqual(Payment.query.count(), 0)

    def test_admin_can_edit_unpaid_bill_but_not_after_payment(self):
        self.sign_in("admin@example.com", "admin-password-123")
        self.client.post(
            "/admin/billing",
            data={
                "patient_id": str(self.patient.id),
                "appointment_id": "0",
                "description": "Initial consultation",
                "consultation_fee": "50.00",
                "service_charges": "0.00",
                "discount_amount": "0.00",
            },
        )
        bill = Bill.query.one()
        edit_url = f"/admin/billing/{bill.id}/edit"
        response = self.client.post(
            edit_url,
            data={
                "description": "Updated consultation",
                "consultation_fee": "60.00",
                "service_charges": "5.00",
                "discount_amount": "10.00",
            },
        )
        db.session.refresh(bill)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(bill.description, "Updated consultation")
        self.assertEqual(str(bill.amount), "55.00")

        self.client.post(
            f"/admin/billing/{bill.id}/payments",
            data={"amount": "5.00", "payment_method": "cash"},
        )
        response = self.client.post(
            edit_url,
            data={
                "description": "Should not change",
                "consultation_fee": "1.00",
                "service_charges": "0.00",
                "discount_amount": "0.00",
            },
        )
        db.session.refresh(bill)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(bill.description, "Updated consultation")
        self.assertEqual(str(bill.amount), "55.00")

    def test_invoice_access_is_limited_to_patient_owner_or_admin(self):
        self.sign_in("admin@example.com", "admin-password-123")
        self.client.post(
            "/admin/billing",
            data={
                "patient_id": str(self.patient.id),
                "appointment_id": "0",
                "description": "Consultation charge",
                "consultation_fee": "35.00",
                "service_charges": "0.00",
                "discount_amount": "0.00",
            },
        )
        bill = Bill.query.one()
        self.client.post("/auth/logout")

        self.sign_in("patient@example.com", "patient-password-123")
        own_invoice = self.client.get(f"/invoices/{bill.id}")
        self.assertEqual(own_invoice.status_code, 200)
        self.assertIn(bill.invoice_number.encode(), own_invoice.data)
        self.assertEqual(self.client.get("/admin/billing").status_code, 403)
        self.assertEqual(
            self.client.post(
                f"/admin/billing/{bill.id}/payments",
                data={"amount": "1.00", "payment_method": "cash"},
            ).status_code,
            403,
        )

        other_user = User(name="Other Patient", email="other-patient@example.com")
        other_user.set_password("other-patient-password")
        other_user.patient_profile = Patient()
        db.session.add(other_user)
        db.session.commit()
        self.client.post("/auth/logout")
        self.sign_in("other-patient@example.com", "other-patient-password")
        other_invoice = self.client.get(f"/invoices/{bill.id}")
        self.assertEqual(other_invoice.status_code, 403)

        self.client.post("/auth/logout")
        self.sign_in("doctor@example.com", "doctor-password-123")
        doctor_invoice = self.client.get(f"/invoices/{bill.id}")
        self.assertEqual(doctor_invoice.status_code, 403)

    def test_billing_migration_backfills_paid_legacy_bill_repeatably(self):
        with tempfile.TemporaryDirectory(dir=self.app.instance_path) as temp_dir:
            relative_dir = os.path.relpath(temp_dir, self.app.instance_path)
            relative_dir = relative_dir.replace("\\", "/")
            database_uri = f"sqlite:///{relative_dir}/legacy.db"
            legacy_path = os.path.join(temp_dir, "legacy.db")
            connection = sqlite3.connect(legacy_path)
            connection.execute(
                "CREATE TABLE bill ("
                "id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, "
                "appointment_id INTEGER UNIQUE, description VARCHAR(300) NOT NULL, "
                "amount NUMERIC(10, 2) NOT NULL, status VARCHAR(20) NOT NULL, "
                "issued_at DATETIME NOT NULL, paid_at DATETIME)"
            )
            connection.execute(
                "INSERT INTO bill "
                "(id, patient_id, description, amount, status, issued_at, paid_at) "
                "VALUES (7, 1, 'Legacy consultation', 42.75, 'paid', "
                "'2024-03-10 09:00:00', '2024-03-11 10:00:00')"
            )
            connection.commit()
            connection.close()

            migration_app = create_app(
                {
                    "TESTING": True,
                    "WTF_CSRF_ENABLED": False,
                    "SQLALCHEMY_DATABASE_URI": database_uri,
                    "SECRET_KEY": "migration-test-key",
                }
            )
            runner = migration_app.test_cli_runner()
            first = runner.invoke(args=["migrate-billing"])
            second = runner.invoke(args=["migrate-billing"])
            self.assertEqual(first.exit_code, 0, first.output)
            self.assertEqual(second.exit_code, 0, second.output)

            with migration_app.app_context():
                bill = Bill.query.one()
                payment = Payment.query.one()
                self.assertEqual(bill.invoice_number, "INV-2024-000007")
                self.assertEqual(bill.amount, payment.amount)
                self.assertEqual(bill.service_charges, bill.amount)
                self.assertEqual(bill.status, "paid")
                self.assertIsNone(payment.payment_method)
                db.session.remove()
                db.engine.dispose()

    def test_sqlite_backup_restore_and_integrity_commands_preserve_records(self):
        with tempfile.TemporaryDirectory(dir=self.app.instance_path) as temp_dir:
            relative_dir = os.path.relpath(temp_dir, self.app.instance_path).replace(
                "\\", "/"
            )
            local_app = create_app(
                {
                    "TESTING": True,
                    "WTF_CSRF_ENABLED": False,
                    "SQLALCHEMY_DATABASE_URI": f"sqlite:///{relative_dir}/active.db",
                    "SECRET_KEY": "database-safety-test-key",
                }
            )
            active_path = Path(temp_dir) / "active.db"
            backup_path = Path(temp_dir) / "backup.db"
            restored_path = Path(temp_dir) / "restored.db"
            try:
                with local_app.app_context():
                    db.create_all()
                    admin = User(
                        name="Backup Admin",
                        email="backup-admin@example.com",
                        role="admin",
                    )
                    admin.set_password("backup-test-password")
                    db.session.add(admin)
                    db.session.commit()
                    db.session.remove()
                    db.engine.dispose()

                runner = local_app.test_cli_runner()
                backup_result = runner.invoke(
                    args=["backup-db", "--destination", str(backup_path)]
                )
                verify_result = runner.invoke(args=["verify-db"])
                restore_result = runner.invoke(
                    args=[
                        "restore-db",
                        str(backup_path),
                        "--destination",
                        str(restored_path),
                    ]
                )
                overwrite_result = runner.invoke(
                    args=[
                        "restore-db",
                        str(backup_path),
                        "--destination",
                        str(active_path),
                    ]
                )
                self.assertEqual(backup_result.exit_code, 0, backup_result.output)
                self.assertEqual(verify_result.exit_code, 0, verify_result.output)
                self.assertEqual(restore_result.exit_code, 0, restore_result.output)
                self.assertNotEqual(overwrite_result.exit_code, 0)
                self.assertIn("cannot overwrite the active database", overwrite_result.output)

                for database_path in (active_path, backup_path, restored_path):
                    connection = sqlite3.connect(database_path)
                    try:
                        self.assertEqual(
                            connection.execute("PRAGMA integrity_check").fetchone()[0],
                            "ok",
                        )
                        self.assertEqual(
                            connection.execute("SELECT COUNT(*) FROM user").fetchone()[0],
                            1,
                        )
                    finally:
                        connection.close()
            finally:
                with local_app.app_context():
                    db.session.remove()
                    db.engine.dispose()

    def test_admin_reports_use_date_range_and_recorded_revenue(self):
        start_date = date.today() - timedelta(days=40)
        end_date = date.today()
        second_patient_user = User(
            name="Second Patient", email="second-patient@example.com"
        )
        second_patient_user.set_password("second-patient-password")
        second_patient_user.patient_profile = Patient()
        db.session.add(second_patient_user)
        db.session.flush()
        second_patient = second_patient_user.patient_profile

        appointments = [
            Appointment(
                patient_id=self.patient.id,
                doctor_id=self.doctor.id,
                starts_at=datetime.combine(start_date + timedelta(days=1), time(9)),
                reason="Completed visit",
                status="completed",
            ),
            Appointment(
                patient_id=self.patient.id,
                doctor_id=self.doctor.id,
                starts_at=datetime.combine(start_date + timedelta(days=2), time(9)),
                reason="Cancelled visit",
                status="cancelled",
            ),
            Appointment(
                patient_id=second_patient.id,
                doctor_id=self.other_doctor.id,
                starts_at=datetime.combine(start_date + timedelta(days=3), time(9)),
                reason="Scheduled visit",
                status="scheduled",
            ),
            Appointment(
                patient_id=second_patient.id,
                doctor_id=self.doctor.id,
                starts_at=datetime.combine(end_date + timedelta(days=1), time(9)),
                reason="Outside selected period",
                status="completed",
            ),
        ]
        partial_bill = Bill(
            patient_id=self.patient.id,
            description="Partial bill",
            amount=100,
            status="partial",
            issued_at=datetime.combine(start_date + timedelta(days=1), time(12)),
        )
        paid_bill = Bill(
            patient_id=second_patient.id,
            description="Paid bill",
            amount=50,
            status="paid",
            issued_at=datetime.combine(start_date + timedelta(days=2), time(12)),
        )
        partial_payment = Payment(
            bill=partial_bill,
            amount=30,
            payment_method="cash",
            received_at=datetime.combine(start_date + timedelta(days=4), time(12)),
        )
        later_payment = Payment(
            bill=paid_bill,
            amount=50,
            payment_method="card",
            received_at=datetime.combine(end_date + timedelta(days=1), time(12)),
        )
        partial_bill.apply_payment_status(30)
        paid_bill.apply_payment_status(50)
        db.session.add_all(
            [*appointments, partial_bill, paid_bill, partial_payment, later_payment]
        )
        db.session.commit()

        self.sign_in("admin@example.com", "admin-password-123")
        query = {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()}
        response = self.client.get("/admin/reports", query_string=query)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Appointments in range", response.data)
        self.assertIn(b"Monthly collected revenue", response.data)
        self.assertIn(b"70.00", response.data)

        csv_response = self.client.get("/admin/reports.csv", query_string=query)
        rows = list(csv.reader(io.StringIO(csv_response.get_data(as_text=True))))
        received_row = next(
            row for row in rows if row[0] == "summary" and row[2] == "Payments received"
        )
        appointments_row = next(
            row for row in rows if row[0] == "summary" and row[2] == "Appointments"
        )
        billed_row = next(
            row
            for row in rows
            if row[0] == "summary" and row[2] == "Total billed revenue"
        )
        outstanding_row = next(
            row
            for row in rows
            if row[0] == "summary" and row[2] == "Outstanding balance"
        )
        doctor_row = next(
            row
            for row in rows
            if row[0] == "doctor appointments" and row[2].startswith("Doc Example")
        )
        department_row = next(
            row
            for row in rows
            if row[0] == "department patients" and row[2] == "General Medicine"
        )
        partial_status_row = next(
            row for row in rows if row[0] == "payment status" and row[2] == "partial"
        )
        paid_status_row = next(
            row for row in rows if row[0] == "payment status" and row[2] == "paid"
        )
        self.assertEqual(csv_response.status_code, 200)
        self.assertEqual(received_row[7], "30.00")
        self.assertEqual(appointments_row[3:6], ["3", "1", "1"])
        self.assertEqual(billed_row[6], "150.00")
        self.assertEqual(outstanding_row[8], "70.00")
        self.assertEqual(doctor_row[3:6], ["2", "1", "1"])
        self.assertEqual(department_row[3], "2")
        self.assertEqual(partial_status_row[3], "1")
        self.assertEqual(partial_status_row[7:9], ["30.00", "70.00"])
        self.assertEqual(paid_status_row[3], "1")
        for granularity in ("daily", "weekly", "monthly"):
            self.assertTrue(any(row[0] == f"{granularity} appointments" for row in rows))
        self.assertTrue(any(row[0] == "monthly collected revenue" for row in rows))

        pdf_response = self.client.get("/admin/reports.pdf", query_string=query)
        self.assertEqual(pdf_response.status_code, 200)
        self.assertEqual(pdf_response.mimetype, "application/pdf")
        self.assertTrue(pdf_response.data.startswith(b"%PDF"))

    def test_analytics_date_validation_and_admin_only_access(self):
        self.sign_in("admin@example.com", "admin-password-123")
        default_report = self.client.get("/admin/reports")
        invalid_range = self.client.get(
            "/admin/reports",
            query_string={"start_date": "2026-03-10", "end_date": "2026-03-01"},
        )
        malformed_date = self.client.get(
            "/admin/reports.csv",
            query_string={"start_date": "not-a-date", "end_date": "2026-03-01"},
        )
        self.assertEqual(default_report.status_code, 200)
        self.assertEqual(invalid_range.status_code, 400)
        self.assertIn(b"End date must be on or after the start date.", invalid_range.data)
        self.assertEqual(malformed_date.status_code, 400)
        self.client.post("/auth/logout")

        for email, password in (
            ("patient@example.com", "patient-password-123"),
            ("doctor@example.com", "doctor-password-123"),
        ):
            with self.subTest(role=email):
                self.sign_in(email, password)
                for path in (
                    "/admin/reports",
                    "/admin/reports.csv",
                    "/admin/reports.pdf",
                ):
                    self.assertEqual(self.client.get(path).status_code, 403, path)
                self.client.post("/auth/logout")

    def test_admin_can_create_doctor(self):
        self.sign_in("admin@example.com", "admin-password-123")

        response = self.client.post(
            "/admin/doctors",
            data={
                "name": "New Doctor",
                "email": "new.doctor@example.com",
                "password": "secure-doctor-password",
                "phone": "555-0123",
                "department_id": str(self.doctor.department_id),
                "specialty": "Neurology",
                "qualifications": "MD, Board Certified",
                "consultation_fee": "125.50",
            },
        )

        new_user = User.query.filter_by(email="new.doctor@example.com").one()
        new_doctor = new_user.doctor_profile
        self.assertEqual(response.status_code, 302)
        self.assertEqual(new_user.role, "doctor")
        self.assertTrue(new_user.check_password("secure-doctor-password"))
        self.assertEqual(new_doctor.phone, "555-0123")
        self.assertEqual(new_doctor.specialty, "Neurology")
        self.assertEqual(new_doctor.qualifications, "MD, Board Certified")
        self.assertEqual(str(new_doctor.consultation_fee), "125.50")
        listing = self.client.get("/admin/doctors?q=new&status=active")
        self.assertEqual(listing.status_code, 200)
        self.assertIn(b"new.doctor@example.com", listing.data)

    def test_admin_cannot_create_doctor_without_required_fields(self):
        self.sign_in("admin@example.com", "admin-password-123")

        response = self.client.post(
            "/admin/doctors",
            data={
                "name": "Incomplete Doctor",
                "email": "incomplete.doctor@example.com",
                "password": "secure-doctor-password",
                "department_id": str(self.doctor.department_id),
                "specialty": "Neurology",
                "consultation_fee": "125.50",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(
            User.query.filter_by(email="incomplete.doctor@example.com").first()
        )

    def test_admin_can_edit_doctor_details_without_losing_history(self):
        appointment = Appointment(
            patient_id=self.patient.id,
            doctor_id=self.doctor.id,
            starts_at=datetime.now() + timedelta(days=1),
            reason="Consultation",
        )
        db.session.add(appointment)
        db.session.flush()
        record = MedicalRecord(
            patient_id=self.patient.id,
            doctor_id=self.doctor.id,
            appointment_id=appointment.id,
            diagnosis="Existing diagnosis",
            consultation_notes="Existing consultation notes",
        )
        db.session.add(record)
        db.session.commit()
        doctor_id = self.doctor.id
        appointment_id = appointment.id
        record_id = record.id
        self.sign_in("admin@example.com", "admin-password-123")

        response = self.client.post(
            f"/admin/doctors/{doctor_id}/edit",
            data={
                "name": "Updated Doctor",
                "email": "updated.doctor@example.com",
                "phone": "555-0144",
                "department_id": str(self.doctor.department_id),
                "specialty": "Updated Specialty",
                "qualifications": "Updated Qualifications",
                "consultation_fee": "175.00",
            },
        )

        updated_doctor = db.session.get(Doctor, doctor_id)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(updated_doctor.user.name, "Updated Doctor")
        self.assertEqual(updated_doctor.user.email, "updated.doctor@example.com")
        self.assertEqual(updated_doctor.phone, "555-0144")
        self.assertEqual(updated_doctor.specialty, "Updated Specialty")
        self.assertEqual(updated_doctor.qualifications, "Updated Qualifications")
        self.assertEqual(str(updated_doctor.consultation_fee), "175.00")
        self.assertIsNotNone(db.session.get(Appointment, appointment_id))
        self.assertIsNotNone(db.session.get(MedicalRecord, record_id))

    def test_admin_can_deactivate_doctor_and_history_remains_available(self):
        appointment = Appointment(
            patient_id=self.patient.id,
            doctor_id=self.doctor.id,
            starts_at=datetime.now() + timedelta(days=1),
            reason="Consultation",
        )
        db.session.add(appointment)
        db.session.flush()
        record = MedicalRecord(
            patient_id=self.patient.id,
            doctor_id=self.doctor.id,
            appointment_id=appointment.id,
            diagnosis="Historical diagnosis",
            consultation_notes="Historical consultation notes",
        )
        db.session.add(record)
        db.session.commit()
        appointment_id = appointment.id
        record_id = record.id
        self.sign_in("admin@example.com", "admin-password-123")

        response = self.client.post(f"/admin/doctors/{self.doctor.id}/delete")

        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.doctor_user.is_active)
        self.assertIsNotNone(db.session.get(Appointment, appointment_id))
        self.assertIsNotNone(db.session.get(MedicalRecord, record_id))
        history_response = self.client.get("/admin/appointments")
        self.assertEqual(history_response.status_code, 200)
        self.assertIn(b"Consultation", history_response.data)
        self.assertIn(b"Doc Example", history_response.data)

    def test_admin_can_reactivate_doctor(self):
        self.doctor_user.is_active = False
        db.session.commit()
        self.sign_in("admin@example.com", "admin-password-123")

        response = self.client.post(
            f"/admin/doctors/{self.doctor.id}/reactivate"
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.doctor_user.is_active)

        self.client.post("/auth/logout")
        self.sign_in("patient@example.com", "patient-password-123")
        appointment_date = date.today() + timedelta(days=2)
        booking_response = self.client.post(
            "/appointments/book",
            data={
                "department_id": str(self.doctor.department_id),
                "doctor_id": str(self.doctor.id),
                "appointment_date": appointment_date.isoformat(),
                "time_slot": "10:00",
                "reason": "Consultation",
            },
        )
        self.assertEqual(booking_response.status_code, 302)
        self.assertEqual(Appointment.query.filter_by(doctor_id=self.doctor.id).count(), 1)

    def test_doctor_email_duplicates_are_rejected_on_create_and_edit(self):
        self.sign_in("admin@example.com", "admin-password-123")
        initial_doctor_count = Doctor.query.count()

        create_response = self.client.post(
            "/admin/doctors",
            data={
                "name": "Duplicate Doctor",
                "email": "DOCTOR@example.com",
                "password": "secure-doctor-password",
                "phone": "555-0123",
                "department_id": str(self.doctor.department_id),
                "specialty": "Neurology",
                "qualifications": "MD",
                "consultation_fee": "125.50",
            },
        )
        edit_response = self.client.post(
            f"/admin/doctors/{self.other_doctor.id}/edit",
            data={
                "name": self.other_doctor.user.name,
                "email": "DOCTOR@example.com",
                "phone": "555-0144",
                "department_id": str(self.other_doctor.department_id),
                "specialty": self.other_doctor.specialty,
                "qualifications": "MD",
                "consultation_fee": "125.50",
            },
        )

        self.assertEqual(create_response.status_code, 200)
        self.assertEqual(edit_response.status_code, 200)
        self.assertIn(b"An account already uses this email.", create_response.data)
        self.assertIn(b"An account already uses this email.", edit_response.data)
        self.assertEqual(Doctor.query.count(), initial_doctor_count)
        self.assertEqual(self.other_doctor.user.email, "other@example.com")

    def test_inactive_doctor_cannot_receive_new_appointments(self):
        self.doctor_user.is_active = False
        db.session.commit()
        self.sign_in("patient@example.com", "patient-password-123")
        appointment_date = date.today() + timedelta(days=2)

        response = self.client.post(
            "/appointments/book",
            data={
                "department_id": str(self.doctor.department_id),
                "doctor_id": str(self.doctor.id),
                "appointment_date": appointment_date.isoformat(),
                "time_slot": "10:00",
                "reason": "Consultation",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Appointment.query.filter_by(doctor_id=self.doctor.id).count(), 0)

    def test_patient_cannot_access_doctor_management(self):
        self.sign_in("patient@example.com", "patient-password-123")

        response = self.client.get("/admin/doctors")

        self.assertEqual(response.status_code, 403)

    def test_doctor_cannot_access_doctor_management_or_other_profiles(self):
        self.sign_in("doctor@example.com", "doctor-password-123")

        responses = [
            self.client.get("/admin/doctors"),
            self.client.get(f"/admin/doctors/{self.other_doctor.id}/edit"),
            self.client.post(f"/admin/doctors/{self.other_doctor.id}/delete"),
        ]

        self.assertEqual([response.status_code for response in responses], [403, 403, 403])

    def test_department_migration_seeds_standard_departments_repeatably(self):
        department_id = self.doctor.department_id
        appointment = Appointment(
            patient_id=self.patient.id,
            doctor_id=self.doctor.id,
            starts_at=datetime.now() + timedelta(days=4),
            reason="Department migration preservation check",
        )
        db.session.add(appointment)
        db.session.flush()
        record = MedicalRecord(
            patient_id=self.patient.id,
            doctor_id=self.doctor.id,
            appointment_id=appointment.id,
            diagnosis="Existing diagnosis",
            consultation_notes="Existing consultation notes",
        )
        db.session.add(record)
        db.session.commit()
        appointment_id = appointment.id
        record_id = record.id

        runner = self.app.test_cli_runner()
        first = runner.invoke(args=["migrate-departments"])
        second = runner.invoke(args=["migrate-departments"])
        db.session.expire_all()

        expected_names = {
            "Cardiology",
            "Dermatology",
            "Neurology",
            "Pediatrics",
            "Orthopedics",
            "Gynecology",
            "General Medicine",
            "Ophthalmology",
            "ENT",
            "Dentistry",
        }
        seeded_departments = Department.query.all()
        self.assertEqual(first.exit_code, 0, first.output)
        self.assertEqual(second.exit_code, 0, second.output)
        self.assertEqual({department.name for department in seeded_departments}, expected_names)
        self.assertEqual(len(seeded_departments), len(expected_names))
        self.assertTrue(all(department.description for department in seeded_departments))
        self.assertTrue(all(department.is_active for department in seeded_departments))
        self.assertEqual(db.session.get(Doctor, self.doctor.id).department_id, department_id)
        self.assertIsNotNone(db.session.get(Appointment, appointment_id))
        self.assertIsNotNone(db.session.get(MedicalRecord, record_id))

    def test_admin_can_create_edit_deactivate_and_reactivate_departments(self):
        appointment = Appointment(
            patient_id=self.patient.id,
            doctor_id=self.doctor.id,
            starts_at=datetime.now() + timedelta(days=6),
            reason="Historical appointment",
        )
        db.session.add(appointment)
        db.session.flush()
        record = MedicalRecord(
            patient_id=self.patient.id,
            doctor_id=self.doctor.id,
            appointment_id=appointment.id,
            diagnosis="Historical diagnosis",
            consultation_notes="Historical consultation notes",
        )
        db.session.add(record)
        db.session.commit()
        department_id = self.doctor.department_id
        appointment_id = appointment.id
        record_id = record.id
        self.sign_in("admin@example.com", "admin-password-123")

        create_response = self.client.post(
            "/admin/departments",
            data={"name": "Urgent Care", "description": "Urgent medical services."},
        )
        created = Department.query.filter_by(name="Urgent Care").one()
        duplicate_response = self.client.post(
            "/admin/departments",
            data={"name": "urgent care", "description": "Duplicate department."},
        )
        edit_response = self.client.post(
            f"/admin/departments/{created.id}/edit",
            data={
                "name": "Emergency Care",
                "description": "Emergency and urgent medical services.",
            },
        )
        department_edit_response = self.client.post(
            f"/admin/departments/{department_id}/edit",
            data={
                "name": "General Medicine and Primary Care",
                "description": "Primary and general medical care.",
            },
        )
        deactivate_response = self.client.post(
            f"/admin/departments/{department_id}/deactivate"
        )

        self.assertEqual(create_response.status_code, 302)
        self.assertEqual(duplicate_response.status_code, 200)
        self.assertIn(b"That department already exists.", duplicate_response.data)
        self.assertEqual(edit_response.status_code, 302)
        self.assertEqual(department_edit_response.status_code, 302)
        self.assertEqual(deactivate_response.status_code, 302)
        self.assertEqual(created.name, "Emergency Care")
        self.assertFalse(db.session.get(Department, department_id).is_active)
        self.assertEqual(db.session.get(Doctor, self.doctor.id).department_id, department_id)
        self.assertIsNotNone(db.session.get(Appointment, appointment_id))
        self.assertIsNotNone(db.session.get(MedicalRecord, record_id))

        reactivate_response = self.client.post(
            f"/admin/departments/{department_id}/reactivate"
        )
        self.assertEqual(reactivate_response.status_code, 302)
        self.assertTrue(db.session.get(Department, department_id).is_active)
        self.assertEqual(Department.query.count(), 2)

    def test_inactive_departments_are_excluded_from_doctor_creation_and_booking(self):
        department_id = self.doctor.department_id
        self.doctor.department.is_active = False
        db.session.commit()
        self.sign_in("admin@example.com", "admin-password-123")

        doctor_page = self.client.get("/admin/doctors")
        inactive_doctor_response = self.client.post(
            "/admin/doctors",
            data={
                "name": "Invalid Department Doctor",
                "email": "inactive.department.doctor@example.com",
                "password": "secure-doctor-password",
                "phone": "555-0123",
                "department_id": str(department_id),
                "specialty": "General Medicine",
                "qualifications": "MD",
                "consultation_fee": "100.00",
            },
        )
        self.assertEqual(doctor_page.status_code, 200)
        self.assertIn(
            f'value="{department_id}" >General Medicine (inactive)</option>'.encode(),
            doctor_page.data,
        )
        self.assertNotIn(
            f'value="{department_id}" >General Medicine</option>'.encode(),
            doctor_page.data,
        )
        self.assertEqual(inactive_doctor_response.status_code, 200)
        self.assertIsNone(
            User.query.filter_by(email="inactive.department.doctor@example.com").first()
        )

        self.client.post("/auth/logout")
        self.sign_in("patient@example.com", "patient-password-123")
        appointment_date = date.today() + timedelta(days=2)
        booking_page = self.client.get("/appointments/book")
        booking_response = self.client.post(
            "/appointments/book",
            data={
                "department_id": str(department_id),
                "doctor_id": str(self.doctor.id),
                "appointment_date": appointment_date.isoformat(),
                "time_slot": "10:00",
                "reason": "Consultation",
            },
        )

        self.assertEqual(booking_page.status_code, 200)
        self.assertNotIn(b"General Medicine", booking_page.data)
        self.assertNotIn(b"Doc Example", booking_page.data)
        self.assertEqual(booking_response.status_code, 200)
        self.assertEqual(Appointment.query.filter_by(doctor_id=self.doctor.id).count(), 0)

    def test_patients_and_doctors_cannot_manage_departments(self):
        for email, password in (
            ("patient@example.com", "patient-password-123"),
            ("doctor@example.com", "doctor-password-123"),
        ):
            with self.subTest(role=email):
                self.sign_in(email, password)
                self.assertEqual(self.client.get("/admin/departments").status_code, 403)
                self.assertEqual(
                    self.client.get(
                        f"/admin/departments/{self.doctor.department_id}/edit"
                    ).status_code,
                    403,
                )
                self.assertEqual(
                    self.client.post(
                        f"/admin/departments/{self.doctor.department_id}/deactivate"
                    ).status_code,
                    403,
                )
                self.client.post("/auth/logout")


if __name__ == "__main__":
    unittest.main()
