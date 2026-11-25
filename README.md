# MediCare Hospital Management

A local-only Flask application for patient accounts, doctor workflows, appointments, medical records, administration, billing, and reports. It uses SQLite and is intended to run from the VS Code terminal on Windows.

## Features

- Patient registration, sign-in, profile editing, doctor directory, appointment booking/cancellation, medical history and prescription viewing, and invoice/payment history.
- Doctor sign-in, profile editing, assigned appointments, status updates, consultation records with symptoms, diagnoses, treatment notes and visit dates, and structured prescriptions.
- Doctor-configurable weekly availability, breaks, leave dates, and appointment slots.
- Appointment conflict checks, patient cancellation, and scheduled/confirmed/completed/cancelled statuses.
- Admin dashboard, doctor and department management, patient account controls, appointment status management, medical record monitoring, invoice management, and payment recording.
- Admin-managed departments with descriptions and active/inactive status; doctors remain linked to their departments when a department is deactivated.
- Admin analytics with date-filtered appointment, department, doctor, payment, and revenue reports, plus CSV and PDF exports.
- Password hashing, CSRF protection, role-based access checks, and server-side form validation.

## Run locally on Windows in VS Code

Open the MediCare project folder in VS Code and use its PowerShell terminal. The existing database is `instance\medicare.db`; the SQLite URL below maps to that instance file. Do not run `init-db` on each startup.

```powershell
Set-Location 'D:\SSMC project\MediCare-Hospital-Management'
$python = (Resolve-Path '..\.venv\Scripts\python.exe').Path
& $python -m pip install -r requirements.txt
$env:SECRET_KEY = (& $python -c 'import secrets; print(secrets.token_hex(32))').Trim()
$env:DATABASE_URL = 'sqlite:///medicare.db'
& $python run.py
```

The server listens only on `http://127.0.0.1:5000`. Keep the same terminal open while using the app. A newly generated secret key invalidates existing sessions when the server restarts; sign in again after restarting.

For a new local database only, create its tables once before starting the server:

```powershell
& $python -m flask --app run.py init-db
```

Create the first administrator only if one does not already exist. Use a unique password of at least 12 characters:

```powershell
$env:ADMIN_EMAIL = "admin@example.com"
$env:ADMIN_PASSWORD = "use-a-unique-strong-password"
& $python -m flask --app run.py seed-admin
```

Existing installations may need these additive migrations once if they have not already been applied. They do not drop or recreate the database:

```powershell
& $python -m flask --app run.py migrate-doctor-phone
```

Initialize or update department management and seed the standard 10 departments. This command is safe to repeat and preserves existing department IDs and doctor associations:

```powershell
& $python -m flask --app run.py migrate-departments
```

For an existing database, add appointment availability tables and appointment durations without recreating it:

```powershell
& $python -m flask --app run.py migrate-appointments
```

For an existing database, add medical-record fields and structured prescriptions without recreating it:

```powershell
& $python -m flask --app run.py migrate-medical-records
```

For an existing database, add invoice and payment tracking without recreating it. Existing bill totals and statuses are preserved; previously paid bills receive a migration payment entry with an unspecified method because the old system did not store payment details:

```powershell
& $python -m flask --app run.py migrate-billing
```

For an existing database, add the indexes used by the analytics date filters:

```powershell
& $python -m flask --app run.py migrate-analytics
```

Administrators can use Reports from the admin navigation to filter activity by date and export aggregated reports as CSV or PDF. Revenue collected is based on recorded payment dates and payment amounts; outstanding is calculated from recorded payments against invoices issued within the selected range.

### Local database safety

Check database integrity and foreign-key consistency:

```powershell
& $python -m flask --app run.py verify-db
```

Create a consistent backup (the command refuses to overwrite an existing file):

```powershell
& $python -m flask --app run.py backup-db --destination 'instance\medicare-backup.db'
```

Restore a backup into a **new** SQLite file. The active database is never overwritten:

```powershell
& $python -m flask --app run.py restore-db 'instance\medicare-backup.db' --destination 'instance\medicare-restored.db'
```

To run the restored copy for review, point the current terminal at it and start the local server:

```powershell
$env:DATABASE_URL = 'sqlite:///medicare-restored.db'
& $python run.py
```

The original `instance\medicare.db` remains untouched. Restore destinations must not already exist. Patient self-registration is public; administrators create doctor accounts.

## Notes

- This is an educational, local-only application and is not certified for clinical or regulated use. It does not configure hosting, public network access, or cloud services.
- Appointment times are stored as naive server-local timestamps; configure the server to the hospital's timezone. Doctors configure working days, hours, breaks, and appointment lengths from their Availability page; admins can manage availability from Doctor Management.
- Billing creates numbered invoices with consultation fees, service charges, discounts, recorded partial/full payments, and patient-visible payment history. It does not collect card details or process online payments.
- Session cookies are HTTP-only and SameSite=Lax. The Secure cookie flag is disabled because the local development server uses loopback HTTP; do not expose this server to a public network.
