import logging
import os
import secrets
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

import click
from flask import Flask, current_app, render_template, request
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url

from config import Config
from medicare.extensions import csrf, db, login_manager


def _local_sqlite_path():
    if db.engine.dialect.name != "sqlite":
        raise click.ClickException("Only a local SQLite database is supported.")
    database = db.engine.url.database
    if not database or database == ":memory:":
        raise click.ClickException("A file-backed SQLite database is required.")
    path = Path(database)
    if not path.is_absolute():
        path = Path(current_app.instance_path) / path
    return path.resolve()


def _sqlite_integrity(path):
    with closing(sqlite3.connect(str(path), timeout=30)) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_violations = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
    return integrity, foreign_key_violations


def _copy_sqlite_database(source_path, destination_path):
    if destination_path.exists():
        raise click.ClickException(
            f"Destination already exists; refusing to overwrite: {destination_path}"
        )
    if not destination_path.parent.is_dir():
        raise click.ClickException(
            f"Destination folder does not exist: {destination_path.parent}"
        )

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination_path.name}.",
        suffix=".tmp",
        dir=str(destination_path.parent),
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        with closing(sqlite3.connect(str(source_path), timeout=30)) as source:
            with closing(sqlite3.connect(str(temporary_path), timeout=30)) as target:
                source.backup(target)
                integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    raise click.ClickException(
                        f"SQLite integrity check failed for the copy: {integrity}"
                    )
        temporary_path.rename(destination_path)
    except sqlite3.Error as error:
        raise click.ClickException(f"SQLite database copy failed: {error}") from error
    except FileExistsError as error:
        raise click.ClickException(
            f"Destination already exists; refusing to overwrite: {destination_path}"
        ) from error
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def create_app(test_config=None):
    app = Flask(
        __name__,
        template_folder="../templates",
        static_folder="../static",
        instance_relative_config=True,
    )
    app.config.from_object(Config)
    app.config["DEBUG"] = os.environ.get("FLASK_DEBUG", "0") == "1"
    if test_config:
        app.config.update(test_config)
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = secrets.token_hex(32)
    database_url = make_url(app.config["SQLALCHEMY_DATABASE_URI"])
    if database_url.get_backend_name() != "sqlite":
        raise RuntimeError("MediCare is configured for local SQLite only.")
    database_path = database_url.database
    if not app.config.get("TESTING") and database_path in (None, "", ":memory:"):
        raise RuntimeError("MediCare requires a local, file-backed SQLite database.")
    if os.name == "nt" and database_path and str(database_path).startswith(("\\\\", "//")):
        raise RuntimeError("MediCare database files must be on a local Windows drive.")
    app.logger.setLevel(logging.INFO)

    os.makedirs(app.instance_path, exist_ok=True)
    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message_category = "warning"
    login_manager.session_protection = "strong"

    from medicare import admin, auth, views
    from medicare.models import User

    app.register_blueprint(auth.bp)
    app.register_blueprint(views.bp)
    app.register_blueprint(admin.bp)

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    @app.context_processor
    def inject_template_values():
        return {"current_year": datetime.now().year}

    @app.errorhandler(400)
    def bad_request(error):
        return render_template("errors/400.html"), error.code or 400

    @app.errorhandler(403)
    def forbidden(error):
        return render_template("errors/403.html"), error.code or 403

    @app.errorhandler(404)
    def not_found(error):
        return render_template("errors/404.html"), error.code or 404

    def log_server_error(error):
        exception = getattr(error, "original_exception", None) or error
        app.logger.error(
            "Unhandled application error at %s (%s)",
            request.path,
            type(exception).__name__,
        )

    @app.errorhandler(500)
    def internal_server_error(error):
        log_server_error(error)
        return render_template("errors/500.html"), 500

    @app.cli.command("init-db")
    def init_db_command():
        """Create the database tables."""
        with app.app_context():
            db.create_all()
        click.echo("Database tables created.")

    @app.cli.command("backup-db")
    @click.option(
        "--destination",
        type=click.Path(path_type=Path, dir_okay=False),
        help="New backup file path (defaults beside the active database).",
    )
    def backup_db_command(destination):
        """Create a consistent SQLite backup without overwriting existing files."""
        with app.app_context():
            source_path = _local_sqlite_path()
            if not source_path.is_file():
                raise click.ClickException(
                    f"Active SQLite database does not exist: {source_path}"
                )
            if destination is None:
                timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                destination_path = source_path.with_name(
                    f"{source_path.stem}.backup-{timestamp}{source_path.suffix}"
                )
            else:
                destination_path = destination.expanduser().resolve()
            if destination_path == source_path:
                raise click.ClickException(
                    "Backup destination must not be the active database."
                )
            _copy_sqlite_database(source_path, destination_path)
            integrity, foreign_key_violations = _sqlite_integrity(destination_path)
        click.echo(f"SQLite backup created: {destination_path}")
        click.echo(f"Integrity check: {integrity}")
        if foreign_key_violations:
            click.echo(
                f"Foreign key violations found: {len(foreign_key_violations)}"
            )

    @app.cli.command("restore-db")
    @click.argument("backup_path", type=click.Path(exists=True, dir_okay=False))
    @click.option(
        "--destination",
        required=True,
        type=click.Path(path_type=Path, dir_okay=False),
        help="New SQLite file to restore into; the active database is never replaced.",
    )
    def restore_db_command(backup_path, destination):
        """Restore a backup into a new SQLite file, leaving the active database intact."""
        with app.app_context():
            active_path = _local_sqlite_path()
            source_path = Path(backup_path).expanduser().resolve()
            destination_path = destination.expanduser().resolve()
            if destination_path == active_path:
                raise click.ClickException(
                    "Restoration cannot overwrite the active database."
                )
            if destination_path == source_path:
                raise click.ClickException(
                    "Restoration destination must differ from the backup file."
                )
            try:
                integrity, _ = _sqlite_integrity(source_path)
            except sqlite3.Error as error:
                raise click.ClickException(
                    f"Cannot verify the SQLite backup: {error}"
                ) from error
            if integrity != "ok":
                raise click.ClickException(
                    f"SQLite backup failed integrity check: {integrity}"
                )
            _copy_sqlite_database(source_path, destination_path)
            restored_integrity, foreign_key_violations = _sqlite_integrity(
                destination_path
            )
        click.echo(f"Restored SQLite copy created: {destination_path}")
        click.echo(f"Integrity check: {restored_integrity}")
        if foreign_key_violations:
            click.echo(
                f"Foreign key violations found: {len(foreign_key_violations)}"
            )

    @app.cli.command("verify-db")
    def verify_db_command():
        """Check SQLite integrity and foreign-key consistency without modifying data."""
        with app.app_context():
            database_path = _local_sqlite_path()
            if not database_path.is_file():
                raise click.ClickException(
                    f"Active SQLite database does not exist: {database_path}"
                )
            try:
                integrity, foreign_key_violations = _sqlite_integrity(database_path)
            except sqlite3.Error as error:
                raise click.ClickException(
                    f"SQLite integrity verification failed: {error}"
                ) from error
        click.echo(f"Integrity check: {integrity}")
        click.echo(f"Foreign key violations: {len(foreign_key_violations)}")
        if integrity != "ok" or foreign_key_violations:
            raise click.ClickException("Database verification reported a problem.")

    @app.cli.command("migrate-doctor-phone")
    def migrate_doctor_phone_command():
        """Add the doctor phone column without recreating existing records."""
        with app.app_context():
            columns = inspect(db.engine).get_columns("doctor")
            if any(column["name"] == "phone" for column in columns):
                click.echo("Doctor phone column already exists.")
                return
            db.session.execute(text("ALTER TABLE doctor ADD COLUMN phone VARCHAR(30)"))
            db.session.commit()
        click.echo("Doctor phone column added.")

    @app.cli.command("migrate-departments")
    def migrate_departments_command():
        """Add department status and seed the standard departments repeatably."""
        from medicare.models import Department

        default_departments = (
            ("Cardiology", "Heart and cardiovascular care."),
            ("Dermatology", "Skin, hair, and nail care."),
            ("Neurology", "Brain, spine, and nervous system care."),
            ("Pediatrics", "Medical care for infants, children, and adolescents."),
            ("Orthopedics", "Bone, joint, and musculoskeletal care."),
            ("Gynecology", "Women's reproductive health services."),
            ("General Medicine", "Primary and general medical care."),
            ("Ophthalmology", "Eye care and vision health."),
            ("ENT", "Ear, nose, and throat care."),
            ("Dentistry", "Oral and dental health services."),
        )

        with app.app_context():
            db.create_all()
            existing_names = db.session.execute(
                text("SELECT name FROM department")
            ).scalars().all()
            normalized_names = [name.casefold() for name in existing_names]
            if len(normalized_names) != len(set(normalized_names)):
                raise click.ClickException(
                    "Department names contain case-insensitive duplicates; "
                    "resolve them before applying the migration."
                )

            columns = {
                column["name"]
                for column in inspect(db.engine).get_columns("department")
            }
            if "is_active" not in columns:
                db.session.execute(
                    text(
                        "ALTER TABLE department "
                        "ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 1"
                    )
                )

            index_names = set(
                db.session.execute(
                    text(
                        "SELECT name FROM sqlite_master "
                        "WHERE type = 'index' AND tbl_name = 'department'"
                    )
                ).scalars()
            )
            if "uq_department_name_nocase" not in index_names:
                db.session.execute(
                    text(
                        "CREATE UNIQUE INDEX uq_department_name_nocase "
                        "ON department (lower(name))"
                    )
                )

            departments_by_name = {
                department.name.casefold(): department
                for department in Department.query.all()
            }
            added_count = 0
            for name, description in default_departments:
                department = departments_by_name.get(name.casefold())
                if department is None:
                    department = Department(
                        name=name,
                        description=description,
                        is_active=True,
                    )
                    db.session.add(department)
                    departments_by_name[name.casefold()] = department
                    added_count += 1
                elif not department.description or not department.description.strip():
                    department.description = description
            db.session.commit()
        click.echo(
            f"Department schema is ready; added {added_count} missing departments."
        )

    @app.cli.command("migrate-appointments")
    def migrate_appointments_command():
        """Add appointment scheduling tables and duration without data loss."""
        with app.app_context():
            db.create_all()
            columns = inspect(db.engine).get_columns("appointment")
            if not any(column["name"] == "duration_minutes" for column in columns):
                db.session.execute(
                    text(
                        "ALTER TABLE appointment "
                        "ADD COLUMN duration_minutes INTEGER NOT NULL DEFAULT 30"
                    )
                )
                db.session.commit()
        click.echo("Appointment scheduling schema is ready.")

    @app.cli.command("migrate-medical-records")
    def migrate_medical_records_command():
        """Add structured record fields and prescription storage without data loss."""
        with app.app_context():
            db.create_all()
            columns = {
                column["name"]
                for column in inspect(db.engine).get_columns("medical_record")
            }
            for name, sql_type in (
                ("symptoms", "TEXT"),
                ("treatment_notes", "TEXT"),
                ("visit_date", "DATE"),
            ):
                if name not in columns:
                    db.session.execute(
                        text(
                            f"ALTER TABLE medical_record ADD COLUMN {name} {sql_type}"
                        )
                    )
            db.session.execute(
                text(
                    "UPDATE medical_record "
                    "SET treatment_notes = consultation_notes "
                    "WHERE treatment_notes IS NULL"
                )
            )
            db.session.execute(
                text(
                    "UPDATE medical_record "
                    "SET visit_date = DATE(created_at) "
                    "WHERE visit_date IS NULL AND created_at IS NOT NULL"
                )
            )
            db.session.commit()
        click.echo("Medical record and prescription schema is ready.")

    @app.cli.command("migrate-billing")
    def migrate_billing_command():
        """Add invoice and payment tracking while preserving existing bills."""
        from medicare.models import Payment

        with app.app_context():
            db.create_all()
            columns = {
                column["name"] for column in inspect(db.engine).get_columns("bill")
            }
            for name, sql_type in (
                ("invoice_number", "VARCHAR(40)"),
                ("consultation_fee", "NUMERIC(10, 2)"),
                ("service_charges", "NUMERIC(10, 2)"),
                ("discount_amount", "NUMERIC(10, 2)"),
            ):
                if name not in columns:
                    db.session.execute(
                        text(f"ALTER TABLE bill ADD COLUMN {name} {sql_type}")
                    )

            rows = db.session.execute(
                text(
                    "SELECT id, issued_at, amount, status, paid_at, invoice_number, "
                    "consultation_fee, service_charges, discount_amount FROM bill"
                )
            ).mappings().all()
            for row in rows:
                bill_id = row["id"]
                issued_at = row["issued_at"]
                if hasattr(issued_at, "year"):
                    year = issued_at.year
                else:
                    year = str(issued_at)[:4]

                invoice_number = row["invoice_number"]
                if invoice_number is None:
                    invoice_number = f"INV-{year}-{bill_id:06d}"

                if (
                    row["consultation_fee"] is None
                    and row["service_charges"] is None
                    and row["discount_amount"] is None
                ):
                    consultation_fee = 0
                    service_charges = row["amount"]
                    discount_amount = 0
                else:
                    consultation_fee = row["consultation_fee"] or 0
                    service_charges = row["service_charges"] or 0
                    discount_amount = row["discount_amount"] or 0

                db.session.execute(
                    text(
                        "UPDATE bill SET invoice_number = :invoice_number, "
                        "consultation_fee = :consultation_fee, "
                        "service_charges = :service_charges, "
                        "discount_amount = :discount_amount WHERE id = :bill_id"
                    ),
                    {
                        "invoice_number": invoice_number,
                        "consultation_fee": consultation_fee,
                        "service_charges": service_charges,
                        "discount_amount": discount_amount,
                        "bill_id": bill_id,
                    },
                )
                if row["status"] == "paid" and row["amount"] > 0:
                    payment_exists = db.session.execute(
                        text("SELECT id FROM payment WHERE bill_id = :bill_id LIMIT 1"),
                        {"bill_id": bill_id},
                    ).first()
                    if payment_exists is None:
                        received_at = row["paid_at"] or issued_at
                        if isinstance(received_at, str):
                            received_at = datetime.fromisoformat(received_at)
                        db.session.add(
                            Payment(
                                bill_id=bill_id,
                                amount=row["amount"],
                                payment_method=None,
                                received_at=received_at or datetime.now(),
                            )
                        )

            index_names = {
                index["name"] for index in inspect(db.engine).get_indexes("bill")
            }
            if "uq_bill_invoice_number" not in index_names:
                db.session.execute(
                    text(
                        "CREATE UNIQUE INDEX uq_bill_invoice_number "
                        "ON bill (invoice_number)"
                    )
                )
            db.session.commit()
        click.echo("Billing and payment schema is ready.")

    @app.cli.command("migrate-analytics")
    def migrate_analytics_command():
        """Add indexes used by date-filtered analytics reports."""
        with app.app_context():
            db.create_all()
            for table, column, index_name in (
                ("bill", "issued_at", "ix_bill_issued_at"),
                ("payment", "received_at", "ix_payment_received_at"),
            ):
                index_names = {
                    index["name"] for index in inspect(db.engine).get_indexes(table)
                }
                if index_name not in index_names:
                    db.session.execute(
                        text(
                            f"CREATE INDEX {index_name} ON {table} ({column})"
                        )
                    )
            db.session.commit()
        click.echo("Analytics indexes are ready.")

    @app.cli.command("seed-admin")
    def seed_admin_command():
        """Create the first administrator using ADMIN_EMAIL and ADMIN_PASSWORD."""
        from medicare.forms import LoginForm
        from medicare.models import User

        email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
        password = os.environ.get("ADMIN_PASSWORD", "")
        if not email or len(password) < 12:
            raise click.ClickException(
                "Set ADMIN_EMAIL and ADMIN_PASSWORD (at least 12 characters)."
            )
        email_form = LoginForm(data={"email": email}, meta={"csrf": False})
        if not email_form.email.validate(email_form):
            raise click.ClickException("ADMIN_EMAIL must be a valid email address.")
        if User.query.filter_by(email=email).first():
            raise click.ClickException("An account already uses that email.")
        admin_user = User(
            name=os.environ.get("ADMIN_NAME", "System Administrator"),
            email=email,
            role="admin",
        )
        admin_user.set_password(password)
        db.session.add(admin_user)
        db.session.commit()
        click.echo("Administrator account created.")

    return app
