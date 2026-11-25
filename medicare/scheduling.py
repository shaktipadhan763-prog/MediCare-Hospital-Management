from datetime import datetime, time, timedelta

from medicare.models import Appointment, DoctorAvailability, DoctorLeave


BUSY_APPOINTMENT_STATUSES = ("scheduled", "confirmed")


def available_slots(doctor_id, appointment_date, now=None):
    now = now or datetime.now()
    if appointment_date <= now.date() or appointment_date > now.date() + timedelta(days=180):
        return []

    schedule = DoctorAvailability.query.filter_by(
        doctor_id=doctor_id, weekday=appointment_date.weekday()
    ).first()
    if schedule is None:
        return []

    leave = DoctorLeave.query.filter(
        DoctorLeave.doctor_id == doctor_id,
        DoctorLeave.start_date <= appointment_date,
        DoctorLeave.end_date >= appointment_date,
    ).first()
    if leave:
        return []

    day_start = datetime.combine(appointment_date, time.min)
    day_end = day_start + timedelta(days=1)
    appointments = Appointment.query.filter(
        Appointment.doctor_id == doctor_id,
        Appointment.status.in_(BUSY_APPOINTMENT_STATUSES),
        Appointment.starts_at >= day_start - timedelta(days=1),
        Appointment.starts_at < day_end,
    ).all()

    shift_start = datetime.combine(appointment_date, schedule.start_time)
    shift_end = datetime.combine(appointment_date, schedule.end_time)
    break_start = (
        datetime.combine(appointment_date, schedule.break_start)
        if schedule.break_start
        else None
    )
    break_end = (
        datetime.combine(appointment_date, schedule.break_end)
        if schedule.break_end
        else None
    )
    slot_length = timedelta(minutes=schedule.slot_duration_minutes)
    slots = []
    slot_start = shift_start
    while slot_start + slot_length <= shift_end:
        slot_end = slot_start + slot_length
        overlaps_break = (
            break_start is not None
            and break_end is not None
            and slot_start < break_end
            and slot_end > break_start
        )
        overlaps_appointment = any(
            slot_start < appointment.starts_at
            + timedelta(minutes=appointment.duration_minutes)
            and slot_end > appointment.starts_at
            for appointment in appointments
        )
        if slot_start > now and not overlaps_break and not overlaps_appointment:
            slots.append(slot_start.time())
        slot_start += slot_length
    return slots


def has_appointment_conflict(doctor_id, starts_at, duration_minutes):
    ends_at = starts_at + timedelta(minutes=duration_minutes)
    day_start = datetime.combine(starts_at.date(), time.min)
    day_end = day_start + timedelta(days=1)
    appointments = Appointment.query.filter(
        Appointment.doctor_id == doctor_id,
        Appointment.status.in_(BUSY_APPOINTMENT_STATUSES),
        Appointment.starts_at >= day_start - timedelta(days=1),
        Appointment.starts_at < day_end,
    ).all()
    return any(
        starts_at
        < appointment.starts_at + timedelta(minutes=appointment.duration_minutes)
        and ends_at > appointment.starts_at
        for appointment in appointments
    )
