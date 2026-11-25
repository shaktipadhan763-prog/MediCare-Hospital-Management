from urllib.parse import urlparse

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_user, logout_user

from medicare.extensions import db
from medicare.forms import LoginForm, RegistrationForm
from medicare.models import Patient, User

bp = Blueprint("auth", __name__, url_prefix="/auth")


def _safe_next_url(target):
    if not target:
        return None
    parsed = urlparse(target)
    if (
        parsed.scheme
        or parsed.netloc
        or not target.startswith("/")
        or target.startswith("//")
        or "\\" in target
        or any(ord(character) < 32 for character in target)
    ):
        return None
    return target


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("views.dashboard"))
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data.strip().lower()).first()
        if user and user.is_active and user.check_password(form.password.data):
            session.clear()
            login_user(user, fresh=True)
            session.permanent = True
            flash("Welcome back.", "success")
            return redirect(
                _safe_next_url(request.args.get("next"))
                or url_for("views.dashboard")
            )
        flash("The email or password is incorrect, or the account is inactive.", "danger")
    return render_template("auth/login.html", form=form)


@bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("views.dashboard"))
    form = RegistrationForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        if User.query.filter_by(email=email).first():
            flash("An account already uses this email address.", "danger")
        else:
            user = User(name=form.name.data.strip(), email=email, role="patient")
            user.set_password(form.password.data)
            user.patient_profile = Patient(phone=form.phone.data.strip() or None)
            db.session.add(user)
            db.session.commit()
            flash("Your patient account is ready. Please sign in.", "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/register.html", form=form)


@bp.post("/logout")
def logout():
    if current_user.is_authenticated:
        logout_user()
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("views.index"))
