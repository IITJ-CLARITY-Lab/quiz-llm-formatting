#!/usr/bin/env python3
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import streamlit as st
from sqlalchemy import func, select

from database import get_session, init_database
from email_utils import send_email
from models import AuditEvent, QuizRequest, QuizRequestStatus, User, UserRole, UserStatus
from rendering import (
    DEFAULT_EXAM_WARNING,
    DEFAULT_WATERMARK_LINE,
    build_request_summary,
    build_zip,
    decode_image_bytes,
    encode_image_bytes,
    render_payload,
)
from security import hash_otp, hash_password, verify_otp, verify_password


@st.cache_resource
def bootstrap_app() -> bool:
    init_database()
    return True


bootstrap_app()


OTP_EXPIRY_MINUTES = 10


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def init_ui_state() -> None:
    st.session_state.setdefault("auth_user_id", None)
    st.session_state.setdefault("impersonated_user_id", None)
    st.session_state.setdefault("history_preview_request_id", None)
    st.session_state.setdefault("history_preview_outputs", [])
    st.session_state.setdefault("latest_generated_request_id", None)
    st.session_state.setdefault("latest_generated_outputs", [])
    st.session_state.setdefault("signup_pending_email", "")
    st.session_state.setdefault("reset_pending_identifier", "")


def apply_theme() -> None:
    st.markdown(
        """
        <style>
        .stApp {
            background:
                radial-gradient(circle at top right, rgba(255, 190, 92, 0.16), transparent 22%),
                radial-gradient(circle at left 20%, rgba(24, 119, 242, 0.12), transparent 24%),
                linear-gradient(180deg, #f7f6f2 0%, #edf2f7 100%);
        }
        .hero-card, .status-card {
            padding: 1rem 1.1rem;
            border-radius: 18px;
            background: rgba(255, 255, 255, 0.82);
            border: 1px solid rgba(15, 23, 42, 0.08);
            box-shadow: 0 18px 50px rgba(15, 23, 42, 0.08);
        }
        .status-pill {
            display: inline-block;
            padding: 0.2rem 0.55rem;
            border-radius: 999px;
            font-size: 0.82rem;
            font-weight: 600;
            background: #e2e8f0;
            color: #1f2937;
        }
        .status-pill.pending { background: #fff3cd; color: #7c5a00; }
        .status-pill.active { background: #d1fae5; color: #065f46; }
        .status-pill.rejected { background: #fee2e2; color: #991b1b; }
        .status-pill.disabled { background: #e5e7eb; color: #374151; }
        .metric-strip {
            padding: 0.9rem 1rem;
            border-radius: 16px;
            background: rgba(255, 255, 255, 0.86);
            border: 1px solid rgba(15, 23, 42, 0.06);
        }
        .sidebar-copy {
            font-size: 0.9rem;
            color: #475569;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def format_dt(value: datetime | None) -> str:
    if value is None:
        return "Never"
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def status_badge(status: UserStatus) -> str:
    return f"<span class='status-pill {status.value}'>{status.value.title()}</span>"


def sanitize_email(email: str) -> str:
    return email.strip().lower()


def sanitize_identifier(identifier: str) -> str:
    return identifier.strip().upper()


def validate_email(email: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email))


def generate_otp_code() -> str:
    return f"{secrets.randbelow(900000) + 100000:06d}"


def clear_user_otp(user: User) -> None:
    user.otp = None
    user.otp_purpose = None
    user.otp_generated_at = None


def get_otp_error(user: User, purpose: str, otp_attempt: str) -> str | None:
    if not user.otp or not user.otp_generated_at or user.otp_purpose != purpose:
        return "Invalid or expired OTP."
    if not verify_otp(otp_attempt.strip(), user.otp):
        return "Invalid OTP."
    if (utc_now() - user.otp_generated_at) > timedelta(minutes=OTP_EXPIRY_MINUTES):
        return "OTP expired."
    return None


def send_one_time_code(session, user: User, purpose: str, subject: str, body: str) -> bool:
    plain_otp = generate_otp_code()
    user.otp = hash_otp(plain_otp)
    user.otp_purpose = purpose
    user.otp_generated_at = utc_now()

    sent = send_email(
        user.email,
        subject,
        body.format(
            name=user.full_name,
            login_id=user.login_id,
            otp=plain_otp,
            minutes=OTP_EXPIRY_MINUTES,
        ),
    )
    if not sent:
        session.rollback()
        return False

    session.commit()
    return True


def request_signup_verification(
    full_name: str,
    email: str,
    institution: str,
    password: str,
) -> tuple[bool, str]:
    cleaned_email = sanitize_email(email)
    with get_session() as session:
        existing = session.scalar(select(User).where(User.email == cleaned_email))
        teacher: User | None = None

        if existing is None:
            teacher = User(
                login_id=generate_teacher_login_id(session),
                full_name=full_name.strip(),
                email=cleaned_email,
                institution=institution.strip(),
                password_hash=hash_password(password),
                role=UserRole.TEACHER,
                status=UserStatus.PENDING,
                approval_note="Verify your email to finish the access request.",
            )
            session.add(teacher)
            session.flush()
        elif (
            existing.role == UserRole.TEACHER
            and existing.status == UserStatus.PENDING
            and existing.email_verified_at is None
        ):
            teacher = existing
            teacher.full_name = full_name.strip()
            teacher.institution = institution.strip()
            teacher.password_hash = hash_password(password)
            teacher.approval_note = "Verify your email to finish the access request."
        else:
            return False, "An account with this email already exists."

        sent = send_one_time_code(
            session,
            teacher,
            "signup",
            "Quiz LLM Studio verification code",
            (
                "Hi {name},\n\n"
                "Your Quiz LLM Studio verification code is: {otp}\n"
                "Login ID: {login_id}\n"
                "It expires in {minutes} minutes.\n\n"
                "After verification, your request will move to admin approval."
            ),
        )
        if not sent:
            return False, "OTP email could not be sent. Check the ClassCam mail credentials."

        st.session_state["signup_pending_email"] = teacher.email
        return True, f"Verification code sent to `{teacher.email}`. Your login ID is `{teacher.login_id}`."


def verify_signup_code(email: str, otp_attempt: str) -> tuple[bool, str]:
    cleaned_email = sanitize_email(email)
    with get_session() as session:
        teacher = session.scalar(
            select(User).where((User.email == cleaned_email) & (User.role == UserRole.TEACHER))
        )
        if teacher is None:
            return False, "No pending signup found for this email."

        otp_error = get_otp_error(teacher, "signup", otp_attempt)
        if otp_error:
            if otp_error == "OTP expired.":
                clear_user_otp(teacher)
                session.commit()
            return False, otp_error

        clear_user_otp(teacher)
        teacher.email_verified_at = utc_now()
        teacher.approval_note = "Awaiting admin approval."
        log_event(
            session,
            "access_requested",
            actor_user_id=teacher.id,
            target_user_id=teacher.id,
            detail={"email": teacher.email, "institution": teacher.institution},
        )
        session.commit()
        st.session_state["signup_pending_email"] = teacher.email
        return True, (
            f"Email verified. Your request is now waiting for admin approval. "
            f"Use `{teacher.login_id}` or `{teacher.email}` after approval."
        )


def resend_signup_code(email: str) -> tuple[bool, str]:
    cleaned_email = sanitize_email(email)
    with get_session() as session:
        teacher = session.scalar(
            select(User).where((User.email == cleaned_email) & (User.role == UserRole.TEACHER))
        )
        if teacher is None or teacher.email_verified_at is not None:
            return False, "No unverified signup was found for this email."

        sent = send_one_time_code(
            session,
            teacher,
            "signup",
            "Quiz LLM Studio verification code",
            (
                "Hi {name},\n\n"
                "Your new Quiz LLM Studio verification code is: {otp}\n"
                "Login ID: {login_id}\n"
                "It expires in {minutes} minutes."
            ),
        )
        if not sent:
            return False, "OTP email could not be sent. Check the ClassCam mail credentials."

        st.session_state["signup_pending_email"] = teacher.email
        return True, f"A fresh OTP was sent to `{teacher.email}`."


def request_password_reset_code(identifier: str) -> tuple[bool, str]:
    with get_session() as session:
        user = lookup_user_by_identifier(session, identifier)
        if user is None:
            return True, "If the account exists, a reset code has been sent."

        sent = send_one_time_code(
            session,
            user,
            "password_reset",
            "Quiz LLM Studio password reset code",
            (
                "Hi {name},\n\n"
                "Your Quiz LLM Studio password reset code is: {otp}\n"
                "It expires in {minutes} minutes."
            ),
        )
        if not sent:
            return False, "Reset OTP email could not be sent. Check the ClassCam mail credentials."

        st.session_state["reset_pending_identifier"] = user.email
        return True, f"Reset code sent to `{user.email}`."


def reset_password_with_otp(identifier: str, otp_attempt: str, new_password: str) -> tuple[bool, str]:
    with get_session() as session:
        user = lookup_user_by_identifier(session, identifier)
        if user is None:
            return False, "Invalid account or OTP."

        otp_error = get_otp_error(user, "password_reset", otp_attempt)
        if otp_error:
            if otp_error == "OTP expired.":
                clear_user_otp(user)
                session.commit()
            return False, otp_error

        user.password_hash = hash_password(new_password)
        clear_user_otp(user)
        log_event(
            session,
            "password_reset",
            actor_user_id=user.id,
            target_user_id=user.id,
            detail={"email": user.email},
        )
        session.commit()
        st.session_state["reset_pending_identifier"] = user.email
        return True, "Password updated. You can log in with the new password now."


def lookup_user_by_identifier(session, identifier: str) -> User | None:
    cleaned = identifier.strip()
    if not cleaned:
        return None

    email = sanitize_email(cleaned)
    login_id = sanitize_identifier(cleaned)
    return session.scalar(
        select(User).where((User.email == email) | (User.login_id == login_id))
    )


def generate_teacher_login_id(session) -> str:
    while True:
        candidate = f"TCH-{secrets.token_hex(3).upper()}"
        exists = session.scalar(select(User.id).where(User.login_id == candidate))
        if not exists:
            return candidate


def log_event(
    session,
    event_type: str,
    *,
    actor_user_id: str | None = None,
    target_user_id: str | None = None,
    request_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            target_user_id=target_user_id,
            request_id=request_id,
            event_type=event_type,
            detail=detail or {},
        )
    )


def clear_outputs() -> None:
    st.session_state["history_preview_request_id"] = None
    st.session_state["history_preview_outputs"] = []
    st.session_state["latest_generated_request_id"] = None
    st.session_state["latest_generated_outputs"] = []


def logout() -> None:
    st.session_state["auth_user_id"] = None
    st.session_state["impersonated_user_id"] = None
    clear_outputs()


def set_impersonation(user_id: str | None) -> None:
    st.session_state["impersonated_user_id"] = user_id
    clear_outputs()


def save_quiz_request(session, owner: User, payload: dict[str, Any], generated: bool) -> QuizRequest:
    title = (payload.get("title") or "").strip() or f"{owner.login_id} Quiz {utc_now().strftime('%Y-%m-%d %H:%M')}"
    request = QuizRequest(
        owner_id=owner.id,
        title=title,
        summary=build_request_summary(payload),
        payload=payload,
        question_count=len(payload.get("questions", [])),
        status=QuizRequestStatus.GENERATED if generated else QuizRequestStatus.DRAFT,
        generation_count=1 if generated else 0,
        last_generated_at=utc_now() if generated else None,
    )
    session.add(request)
    session.flush()
    log_event(
        session,
        "quiz_generated" if generated else "quiz_saved",
        actor_user_id=owner.id,
        target_user_id=owner.id,
        request_id=request.id,
        detail={"title": title, "question_count": request.question_count},
    )
    session.commit()
    session.refresh(request)
    return request


def describe_event(event: AuditEvent, people: dict[str, User]) -> str:
    actor = people.get(event.actor_user_id) if event.actor_user_id else None
    target = people.get(event.target_user_id) if event.target_user_id else None
    actor_label = actor.login_id if actor else "System"
    target_label = target.login_id if target else "n/a"

    if event.event_type == "access_requested":
        return f"{actor_label} requested access ({target_label})"
    if event.event_type == "user_approved":
        return f"{actor_label} approved {target_label}"
    if event.event_type == "user_rejected":
        return f"{actor_label} rejected {target_label}"
    if event.event_type == "login_success":
        return f"{actor_label} signed in"
    if event.event_type == "password_reset":
        return f"{actor_label} reset their password"
    if event.event_type == "quiz_saved":
        return f"{actor_label} saved draft {event.detail.get('title', '')}".strip()
    if event.event_type == "quiz_generated":
        return f"{actor_label} generated {event.detail.get('title', '')}".strip()
    if event.event_type == "user_disabled":
        return f"{actor_label} disabled {target_label}"
    if event.event_type == "user_enabled":
        return f"{actor_label} re-enabled {target_label}"
    if event.event_type == "user_deleted":
        return f"{actor_label} deleted {target_label}"
    if event.event_type == "impersonation_started":
        return f"{actor_label} opened {target_label}'s workspace"
    if event.event_type == "impersonation_stopped":
        return f"{actor_label} returned to admin view"
    return f"{actor_label} triggered {event.event_type}"


def render_public_landing() -> None:
    col_left, col_right = st.columns([1.25, 0.95], gap="large")
    with col_left:
        st.markdown(
            """
            <div class="hero-card">
                <h1 style="margin-bottom:0.35rem;">Quiz LLM Studio</h1>
                <p style="font-size:1.05rem; margin-bottom:1rem;">
                    Teacher requests now pass through email OTP verification first, then admin approval.
                    Every generated quiz stays in platform memory so teachers can revisit it later and the admin can audit usage.
                </p>
                <div style="display:grid; gap:0.7rem;">
                    <div class="metric-strip"><strong>Teacher onboarding</strong><br/>Teachers verify email with OTP, then wait for admin approval.</div>
                    <div class="metric-strip"><strong>Persistent quiz history</strong><br/>Generated quizzes and drafts are saved for later viewing.</div>
                    <div class="metric-strip"><strong>Admin oversight</strong><br/>Approval queue, user IDs, usage metrics, user workspace impersonation, and OTP-backed password reset.</div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with col_right:
        tab_login, tab_signup, tab_reset = st.tabs(["Login", "Request Access", "Reset Password"])

        with tab_login:
            with st.form("login_form", clear_on_submit=False):
                identifier = st.text_input("Login ID or email")
                password = st.text_input("Password", type="password")
                submitted = st.form_submit_button("Log in", type="primary")

            if submitted:
                with get_session() as session:
                    user = lookup_user_by_identifier(session, identifier)
                    if user is None or not verify_password(password, user.password_hash):
                        st.error("Invalid credentials.")
                    elif user.role == UserRole.TEACHER and user.email_verified_at is None:
                        st.warning("Verify your email with the OTP first, then wait for admin approval.")
                    elif user.status == UserStatus.PENDING:
                        st.warning("Your account is still pending admin approval.")
                    elif user.status == UserStatus.REJECTED:
                        st.error("This access request was rejected. Ask the admin to reopen it.")
                    elif user.status == UserStatus.DISABLED:
                        st.error("This account is disabled.")
                    else:
                        user.last_login_at = utc_now()
                        log_event(session, "login_success", actor_user_id=user.id, target_user_id=user.id)
                        session.commit()
                        st.session_state["auth_user_id"] = user.id
                        set_impersonation(None)
                        st.rerun()

        with tab_signup:
            st.caption("Step 1: request an OTP at your email. Step 2: verify the OTP to place the request into admin approval.")

            with st.form("signup_form", clear_on_submit=False):
                full_name = st.text_input("Full name")
                email = st.text_input("Email")
                institution = st.text_input("School / institution")
                password = st.text_input("Create password", type="password")
                confirm_password = st.text_input("Confirm password", type="password")
                requested = st.form_submit_button("Send signup OTP", type="primary")

            if requested:
                if not full_name.strip():
                    st.error("Full name is required.")
                elif not validate_email(sanitize_email(email)):
                    st.error("Enter a valid email address.")
                elif len(password) < 8:
                    st.error("Password must be at least 8 characters.")
                elif password != confirm_password:
                    st.error("Passwords do not match.")
                else:
                    ok, message = request_signup_verification(full_name, email, institution, password)
                    (st.success if ok else st.error)(message)

            st.divider()
            with st.form("signup_verify_form", clear_on_submit=False):
                verify_email = st.text_input(
                    "Email used for signup",
                    value=st.session_state.get("signup_pending_email", ""),
                )
                signup_otp = st.text_input("6-digit OTP", max_chars=6)
                verify_requested = st.form_submit_button("Verify signup OTP", type="primary")

            if verify_requested:
                ok, message = verify_signup_code(verify_email, signup_otp)
                (st.success if ok else st.error)(message)

            pending_signup_email = st.session_state.get("signup_pending_email", "")
            if pending_signup_email:
                st.caption(f"Need a fresh code for `{pending_signup_email}`?")
                if st.button("Resend signup OTP", key="resend_signup_otp"):
                    ok, message = resend_signup_code(pending_signup_email)
                    (st.success if ok else st.error)(message)

        with tab_reset:
            st.caption("Request a reset OTP, then submit the OTP and your new password.")
            with st.form("reset_request_form", clear_on_submit=False):
                reset_identifier = st.text_input("Login ID or email")
                reset_requested = st.form_submit_button("Send reset OTP", type="primary")

            if reset_requested:
                ok, message = request_password_reset_code(reset_identifier)
                (st.success if ok else st.error)(message)

            st.divider()
            with st.form("reset_confirm_form", clear_on_submit=False):
                reset_identifier_confirm = st.text_input(
                    "Login ID or email",
                    value=st.session_state.get("reset_pending_identifier", ""),
                )
                reset_otp = st.text_input("6-digit OTP", max_chars=6)
                new_password = st.text_input("New password", type="password")
                confirm_new_password = st.text_input("Confirm new password", type="password")
                reset_submitted = st.form_submit_button("Reset password", type="primary")

            if reset_submitted:
                if len(new_password) < 8:
                    st.error("Password must be at least 8 characters.")
                elif new_password != confirm_new_password:
                    st.error("Passwords do not match.")
                else:
                    ok, message = reset_password_with_otp(reset_identifier_confirm, reset_otp, new_password)
                    (st.success if ok else st.error)(message)


def render_sidebar(current_user: User, effective_user: User | None) -> str:
    with st.sidebar:
        st.title("Quiz LLM Studio")
        st.markdown("<div class='sidebar-copy'>Teacher product workspace with admin gating.</div>", unsafe_allow_html=True)
        st.write(f"Signed in as **{current_user.full_name}**")
        st.caption(f"Role: {current_user.role.value} | Login ID: {current_user.login_id}")

        if current_user.role == UserRole.ADMIN and effective_user is not None and effective_user.id != current_user.id:
            st.info(f"Viewing as {effective_user.full_name} ({effective_user.login_id}) in read-only mode.")
            if st.button("Exit user view", use_container_width=True):
                with get_session() as session:
                    log_event(
                        session,
                        "impersonation_stopped",
                        actor_user_id=current_user.id,
                        target_user_id=effective_user.id,
                    )
                    session.commit()
                set_impersonation(None)
                st.rerun()

        if current_user.role == UserRole.ADMIN:
            options = ["Dashboard", "Approvals", "Users"]
            if effective_user is not None and effective_user.id != current_user.id:
                options.extend(["Teacher Workspace", "Teacher History"])
        else:
            options = ["Workspace", "History"]

        selected = st.radio("Navigate", options, use_container_width=True)
        if st.button("Log out", use_container_width=True):
            logout()
            st.rerun()
        return selected


def render_metric_cards(cards: list[tuple[str, str, str]]) -> None:
    columns = st.columns(len(cards))
    for column, (label, value, note) in zip(columns, cards):
        with column:
            st.markdown(
                f"""
                <div class="status-card">
                    <div style="font-size:0.82rem; color:#475569;">{label}</div>
                    <div style="font-size:1.9rem; font-weight:700; margin:0.1rem 0 0.2rem;">{value}</div>
                    <div style="font-size:0.86rem; color:#64748b;">{note}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )


def render_admin_dashboard(session, current_user: User) -> None:
    st.title("Admin Dashboard")
    teacher_count = session.scalar(
        select(func.count()).select_from(User).where(User.role == UserRole.TEACHER)
    ) or 0
    active_count = session.scalar(
        select(func.count()).select_from(User).where(
            (User.role == UserRole.TEACHER) & (User.status == UserStatus.ACTIVE)
        )
    ) or 0
    pending_count = session.scalar(
        select(func.count()).select_from(User).where(
            (User.role == UserRole.TEACHER)
            & (User.status == UserStatus.PENDING)
            & (User.email_verified_at.is_not(None))
        )
    ) or 0
    verification_count = session.scalar(
        select(func.count()).select_from(User).where(
            (User.role == UserRole.TEACHER)
            & (User.status == UserStatus.PENDING)
            & (User.email_verified_at.is_(None))
        )
    ) or 0
    request_count = session.scalar(select(func.count()).select_from(QuizRequest)) or 0
    generation_total = session.scalar(select(func.coalesce(func.sum(QuizRequest.generation_count), 0))) or 0

    render_metric_cards(
        [
            ("Teachers", str(teacher_count), "All teacher accounts"),
            ("Active", str(active_count), "Approved and enabled"),
            ("Pending approval", str(pending_count), "Verified teachers waiting for your decision"),
            ("Awaiting OTP", str(verification_count), "Signup started but not yet verified"),
            ("Saved quizzes", str(request_count), "Drafts and generated runs"),
            ("Total generations", str(generation_total), "Completed output exports"),
        ]
    )

    st.subheader("Recent platform activity")
    events = session.scalars(
        select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(15)
    ).all()
    if not events:
        st.info("No activity yet.")
        return

    user_ids = {
        event.actor_user_id for event in events if event.actor_user_id
    } | {event.target_user_id for event in events if event.target_user_id}
    people = {
        user.id: user
        for user in session.scalars(select(User).where(User.id.in_(user_ids))).all()
    } if user_ids else {}

    for event in events:
        st.markdown(
            f"""
            <div class="metric-strip">
                <strong>{describe_event(event, people)}</strong><br/>
                <span style="color:#64748b;">{format_dt(event.created_at)}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )


def render_admin_approvals(session, current_user: User) -> None:
    st.title("Approvals")
    pending_users = session.scalars(
        select(User).where(
            (User.role == UserRole.TEACHER)
            & (User.status == UserStatus.PENDING)
            & (User.email_verified_at.is_not(None))
        ).order_by(User.created_at.asc())
    ).all()
    unverified_count = session.scalar(
        select(func.count()).select_from(User).where(
            (User.role == UserRole.TEACHER)
            & (User.status == UserStatus.PENDING)
            & (User.email_verified_at.is_(None))
        )
    ) or 0

    if not pending_users:
        if unverified_count:
            st.info(f"No verified approvals yet. {unverified_count} signup(s) are still waiting on OTP verification.")
        else:
            st.success("No pending approvals.")
        return

    st.caption("Approve or reject each teacher request. Login IDs are assigned when they register.")

    for teacher in pending_users:
        with st.expander(f"{teacher.full_name} · {teacher.login_id}", expanded=False):
            st.write(f"Email: `{teacher.email}`")
            st.write(f"Institution: {teacher.institution or 'Not provided'}")
            st.write(f"Requested: {format_dt(teacher.created_at)}")
            st.write(f"Email verified: {format_dt(teacher.email_verified_at)}")
            note_key = f"approval_note_{teacher.id}"
            st.text_area("Admin note", key=note_key, placeholder="Optional note shown in the account record")
            col1, col2 = st.columns(2)

            if col1.button("Approve", key=f"approve_{teacher.id}", type="primary", use_container_width=True):
                teacher.status = UserStatus.ACTIVE
                teacher.approved_at = utc_now()
                teacher.approved_by_id = current_user.id
                teacher.approval_note = st.session_state.get(note_key, "").strip() or "Approved for platform access."
                log_event(
                    session,
                    "user_approved",
                    actor_user_id=current_user.id,
                    target_user_id=teacher.id,
                    detail={"note": teacher.approval_note},
                )
                session.commit()
                st.rerun()

            if col2.button("Reject", key=f"reject_{teacher.id}", use_container_width=True):
                teacher.status = UserStatus.REJECTED
                teacher.approved_by_id = current_user.id
                teacher.approval_note = st.session_state.get(note_key, "").strip() or "Rejected by admin."
                log_event(
                    session,
                    "user_rejected",
                    actor_user_id=current_user.id,
                    target_user_id=teacher.id,
                    detail={"note": teacher.approval_note},
                )
                session.commit()
                st.rerun()


def render_user_actions(session, current_user: User, teacher: User) -> None:
    request_count = session.scalar(
        select(func.count()).select_from(QuizRequest).where(QuizRequest.owner_id == teacher.id)
    ) or 0
    generation_total = session.scalar(
        select(func.coalesce(func.sum(QuizRequest.generation_count), 0)).where(QuizRequest.owner_id == teacher.id)
    ) or 0

    st.markdown(status_badge(teacher.status), unsafe_allow_html=True)
    st.write(f"Email: `{teacher.email}`")
    st.write(f"Institution: {teacher.institution or 'Not provided'}")
    st.write(f"Created: {format_dt(teacher.created_at)}")
    st.write(f"Email verified: {format_dt(teacher.email_verified_at)}")
    st.write(f"Last login: {format_dt(teacher.last_login_at)}")
    st.write(f"Saved quizzes: **{request_count}** | Total generations: **{generation_total}**")
    if teacher.approval_note:
        st.caption(f"Admin note: {teacher.approval_note}")

    col1, col2, col3 = st.columns(3)
    if col1.button("View as user", key=f"impersonate_{teacher.id}", use_container_width=True):
        log_event(
            session,
            "impersonation_started",
            actor_user_id=current_user.id,
            target_user_id=teacher.id,
        )
        session.commit()
        set_impersonation(teacher.id)
        st.rerun()

    if teacher.status == UserStatus.DISABLED:
        if col2.button("Enable", key=f"enable_{teacher.id}", use_container_width=True):
            teacher.status = UserStatus.ACTIVE
            teacher.approval_note = "Re-enabled by admin."
            log_event(
                session,
                "user_enabled",
                actor_user_id=current_user.id,
                target_user_id=teacher.id,
            )
            session.commit()
            st.rerun()
    else:
        if col2.button("Disable", key=f"disable_{teacher.id}", use_container_width=True):
            teacher.status = UserStatus.DISABLED
            teacher.approval_note = "Disabled by admin."
            log_event(
                session,
                "user_disabled",
                actor_user_id=current_user.id,
                target_user_id=teacher.id,
            )
            session.commit()
            if st.session_state.get("impersonated_user_id") == teacher.id:
                set_impersonation(None)
            st.rerun()

    confirm_key = f"confirm_delete_{teacher.id}"
    col3.checkbox("Confirm delete", key=confirm_key)
    if st.button("Delete user permanently", key=f"delete_{teacher.id}", use_container_width=True):
        if not st.session_state.get(confirm_key):
            st.error("Tick confirm delete first.")
        else:
            teacher_label = teacher.login_id
            log_event(
                session,
                "user_deleted",
                actor_user_id=current_user.id,
                target_user_id=teacher.id,
                detail={"login_id": teacher_label},
            )
            session.delete(teacher)
            session.commit()
            if st.session_state.get("impersonated_user_id") == teacher.id:
                set_impersonation(None)
            st.rerun()

    recent_requests = session.scalars(
        select(QuizRequest).where(QuizRequest.owner_id == teacher.id).order_by(QuizRequest.created_at.desc()).limit(3)
    ).all()
    if recent_requests:
        st.caption("Recent saved quizzes")
        for request in recent_requests:
            st.write(
                f"- {request.title} · {request.status.value} · {request.question_count} questions · {format_dt(request.created_at)}"
            )


def render_admin_users(session, current_user: User) -> None:
    st.title("Users")
    st.caption("Teacher IDs, moderation controls, and quick workspace access.")
    teachers = session.scalars(
        select(User).where(User.role == UserRole.TEACHER).order_by(User.created_at.desc())
    ).all()

    if not teachers:
        st.info("No teacher accounts yet.")
        return

    for teacher in teachers:
        with st.expander(f"{teacher.login_id} · {teacher.full_name}", expanded=False):
            render_user_actions(session, current_user, teacher)


def render_saved_quiz_snapshot(request: QuizRequest) -> None:
    payload = request.payload or {}
    st.caption(
        f"Request ID: {request.id} | Status: {request.status.value} | "
        f"Questions: {request.question_count} | Generations: {request.generation_count}"
    )
    st.write(request.summary)

    for index, question in enumerate(payload.get("questions", []), start=1):
        st.markdown(f"**Q{index}. {question.get('text') or 'Untitled question'}**")
        marks = question.get("marks") or "-"
        st.caption(f"Marks: {marks}")
        question_image = decode_image_bytes(question.get("question_image_b64"))
        if question_image:
            st.image(question_image, caption=f"Question {index} image", use_container_width=False, width=420)

        for option_index, option in enumerate(question.get("options", []), start=0):
            option_prefix = chr(65 + option_index)
            option_text = option.get("text") or "(image-only option)"
            st.write(f"{option_prefix}) {option_text}")
            option_image = decode_image_bytes(option.get("image_b64"))
            if option_image:
                st.image(option_image, caption=f"Option {option_prefix} image", width=320)


def render_history_page(session, target_user: User, read_only: bool) -> None:
    title = "Teacher History" if read_only else "History"
    st.title(title)
    st.caption("Saved drafts and generated quizzes remain available here.")

    requests = session.scalars(
        select(QuizRequest).where(QuizRequest.owner_id == target_user.id).order_by(QuizRequest.created_at.desc())
    ).all()

    if not requests:
        st.info("No saved quiz requests yet.")
        return

    for request in requests:
        with st.expander(f"{request.title} · {request.status.value} · {format_dt(request.created_at)}", expanded=False):
            render_saved_quiz_snapshot(request)

            if st.button("Render preview and ZIP", key=f"prepare_preview_{request.id}", type="primary"):
                outputs = render_payload(request.payload)
                st.session_state["history_preview_request_id"] = request.id
                st.session_state["history_preview_outputs"] = outputs
                st.rerun()

            if st.session_state.get("history_preview_request_id") == request.id:
                outputs = st.session_state.get("history_preview_outputs", [])
                if outputs:
                    zip_bytes = build_zip(outputs)
                    st.download_button(
                        "Download this request as ZIP",
                        data=zip_bytes,
                        file_name=f"{request.title.replace(' ', '_').lower()}_images.zip",
                        mime="application/zip",
                        key=f"download_zip_{request.id}",
                    )
                    for file_name, png_bytes in outputs:
                        st.image(png_bytes, caption=file_name)


def render_teacher_workspace(session, target_user: User, read_only: bool) -> None:
    title = "Teacher Workspace" if read_only else "Workspace"
    st.title(title)

    request_count = session.scalar(
        select(func.count()).select_from(QuizRequest).where(QuizRequest.owner_id == target_user.id)
    ) or 0
    generation_total = session.scalar(
        select(func.coalesce(func.sum(QuizRequest.generation_count), 0)).where(QuizRequest.owner_id == target_user.id)
    ) or 0
    last_request = session.scalar(
        select(QuizRequest).where(QuizRequest.owner_id == target_user.id).order_by(QuizRequest.created_at.desc()).limit(1)
    )

    render_metric_cards(
        [
            ("Login ID", target_user.login_id, "Teacher account identifier"),
            ("Saved quizzes", str(request_count), "Drafts plus generated history"),
            ("Generations", str(generation_total), "Total exported output runs"),
            ("Last activity", format_dt(last_request.created_at if last_request else None), "Most recent saved request"),
        ]
    )

    if read_only:
        st.info("Admin view is read-only. Use History to inspect saved requests in detail.")

    with st.expander("Rendering and watermark settings", expanded=not read_only):
        width = st.number_input("Canvas width", min_value=900, max_value=2600, value=1600, step=50, key="canvas_width", disabled=read_only)
        height = st.number_input("Canvas height", min_value=700, max_value=2200, value=900, step=50, key="canvas_height", disabled=read_only)
        padding = st.number_input("Padding", min_value=40, max_value=180, value=90, step=5, key="canvas_padding", disabled=read_only)
        question_font_size = st.number_input("Question font size", min_value=24, max_value=56, value=34, step=1, key="question_font_size", disabled=read_only)
        option_font_size = st.number_input("Option font size", min_value=22, max_value=52, value=34, step=1, key="option_font_size", disabled=read_only)
        marks_font_size = st.number_input("Marks font size", min_value=20, max_value=42, value=28, step=1, key="marks_font_size", disabled=read_only)
        question_image_max_height = st.number_input(
            "Question image max height",
            min_value=120,
            max_value=700,
            value=260,
            step=10,
            key="question_image_max_height",
            disabled=read_only,
        )
        option_image_max_height = st.number_input(
            "Option image max height",
            min_value=100,
            max_value=500,
            value=170,
            step=10,
            key="option_image_max_height",
            disabled=read_only,
        )
        exam_warning = st.text_input("Exam warning", value=DEFAULT_EXAM_WARNING, key="exam_warning", disabled=read_only)
        llm_line = st.text_input("LLM line", value=DEFAULT_WATERMARK_LINE, key="llm_line", disabled=read_only)
        exam_tag = st.text_input("Exam tag", value="", key="exam_tag", disabled=read_only)
        candidate_tag = st.text_input("Candidate tag", value="", key="candidate_tag", disabled=read_only)
        watermark_opacity = st.slider("Opacity", min_value=4, max_value=40, value=10, key="watermark_opacity", disabled=read_only)
        watermark_size = st.slider("Font size", min_value=14, max_value=36, value=22, key="watermark_size", disabled=read_only)
        watermark_step_x = st.number_input("Step X", min_value=0, max_value=1200, value=0, step=10, key="watermark_step_x", disabled=read_only)
        watermark_step_y = st.number_input("Step Y", min_value=0, max_value=800, value=0, step=10, key="watermark_step_y", disabled=read_only)

    st.subheader("Quiz builder")
    request_title = st.text_input(
        "Request title",
        value="",
        placeholder="Class 10 Physics - Chapter 4 - Set A",
        key="request_title",
        disabled=read_only,
    )
    question_count = int(
        st.number_input(
            "Number of questions",
            min_value=1,
            max_value=20,
            value=1,
            step=1,
            key="question_count",
            disabled=read_only,
        )
    )

    questions_payload: list[dict[str, Any]] = []
    for q_idx in range(question_count):
        with st.expander(f"Question {q_idx + 1}", expanded=(q_idx == 0)):
            marks = st.text_input("Marks", value="2", key=f"marks_{q_idx}", disabled=read_only)
            q_text = st.text_area(
                "Question text",
                value="",
                height=110,
                key=f"q_text_{q_idx}",
                placeholder="Enter the question statement",
                disabled=read_only,
            )
            q_img_file = st.file_uploader(
                "Question image (optional)",
                type=["png", "jpg", "jpeg", "webp"],
                key=f"q_img_{q_idx}",
                disabled=read_only,
            )
            option_count = st.slider(
                "Number of options",
                min_value=2,
                max_value=6,
                value=4,
                key=f"opt_count_{q_idx}",
                disabled=read_only,
            )

            options_payload: list[dict[str, Any]] = []
            for opt_idx in range(option_count):
                col1, col2 = st.columns([3, 2])
                with col1:
                    opt_text = st.text_input(
                        f"Option {chr(65 + opt_idx)} text",
                        value="",
                        key=f"opt_text_{q_idx}_{opt_idx}",
                        disabled=read_only,
                    )
                with col2:
                    opt_img_file = st.file_uploader(
                        f"Option {chr(65 + opt_idx)} image",
                        type=["png", "jpg", "jpeg", "webp"],
                        key=f"opt_img_{q_idx}_{opt_idx}",
                        disabled=read_only,
                    )

                options_payload.append(
                    {
                        "text": opt_text,
                        "image_b64": encode_image_bytes(opt_img_file.getvalue()) if opt_img_file else None,
                    }
                )

            questions_payload.append(
                {
                    "text": q_text,
                    "marks": marks,
                    "question_image_b64": encode_image_bytes(q_img_file.getvalue()) if q_img_file else None,
                    "options": options_payload,
                }
            )

    combined_watermark_parts = [exam_warning.strip(), llm_line.strip()]
    if exam_tag.strip():
        combined_watermark_parts.append(exam_tag.strip())
    if candidate_tag.strip():
        combined_watermark_parts.append(candidate_tag.strip())

    payload = {
        "title": request_title.strip(),
        "settings": {
            "width": int(width),
            "height": int(height),
            "padding": int(padding),
            "question_font_size": int(question_font_size),
            "option_font_size": int(option_font_size),
            "marks_font_size": int(marks_font_size),
            "question_image_max_height": int(question_image_max_height),
            "option_image_max_height": int(option_image_max_height),
            "watermark_text": " | ".join([part for part in combined_watermark_parts if part]),
            "watermark_opacity": int(watermark_opacity),
            "watermark_size": int(watermark_size),
            "watermark_step_x": int(watermark_step_x),
            "watermark_step_y": int(watermark_step_y),
        },
        "questions": questions_payload,
    }

    if not read_only:
        col1, col2 = st.columns(2)
        save_clicked = col1.button("Save draft", use_container_width=True)
        generate_clicked = col2.button("Save and generate", type="primary", use_container_width=True)

        if save_clicked or generate_clicked:
            non_empty_questions = [question for question in questions_payload if question.get("text", "").strip()]
            if not non_empty_questions:
                st.error("Add at least one question before saving.")
            else:
                payload["questions"] = questions_payload
                request = save_quiz_request(session, target_user, payload, generated=generate_clicked)
                st.success(
                    f"{'Generated' if generate_clicked else 'Saved'} request `{request.title}` "
                    f"with ID `{request.id}`."
                )
                if generate_clicked:
                    outputs = render_payload(payload)
                    st.session_state["latest_generated_request_id"] = request.id
                    st.session_state["latest_generated_outputs"] = outputs
                else:
                    clear_outputs()

    if st.session_state.get("latest_generated_outputs"):
        st.subheader("Latest generated output")
        outputs = st.session_state["latest_generated_outputs"]
        for index, (file_name, png_bytes) in enumerate(outputs):
            st.image(png_bytes, caption=file_name)
            st.download_button(
                label=f"Download {file_name}",
                data=png_bytes,
                file_name=file_name,
                mime="image/png",
                key=f"download_latest_{index}_{file_name}",
            )
        if len(outputs) > 1:
            zip_bytes = build_zip(outputs)
            st.download_button(
                "Download all as ZIP",
                data=zip_bytes,
                file_name="quiz_images.zip",
                mime="application/zip",
                key="download_latest_zip",
            )

    recent_requests = session.scalars(
        select(QuizRequest).where(QuizRequest.owner_id == target_user.id).order_by(QuizRequest.created_at.desc()).limit(5)
    ).all()
    if recent_requests:
        st.subheader("Recent saved requests")
        for request in recent_requests:
            st.markdown(
                f"""
                <div class="metric-strip">
                    <strong>{request.title}</strong><br/>
                    ID: <code>{request.id}</code> · {request.status.value} · {request.question_count} questions ·
                    {format_dt(request.created_at)}
                </div>
                """,
                unsafe_allow_html=True,
            )


def main() -> None:
    st.set_page_config(page_title="Quiz LLM Studio", layout="wide")
    init_ui_state()
    apply_theme()

    auth_user_id = st.session_state.get("auth_user_id")
    if not auth_user_id:
        render_public_landing()
        return

    with get_session() as session:
        current_user = session.get(User, auth_user_id)
        if current_user is None:
            logout()
            st.rerun()

        if current_user.role != UserRole.ADMIN and current_user.status != UserStatus.ACTIVE:
            logout()
            st.rerun()

        effective_user = current_user
        impersonated_user_id = st.session_state.get("impersonated_user_id")
        if current_user.role == UserRole.ADMIN and impersonated_user_id:
            candidate = session.get(User, impersonated_user_id)
            if candidate is not None and candidate.role == UserRole.TEACHER:
                effective_user = candidate
            else:
                set_impersonation(None)

        current_page = render_sidebar(current_user, effective_user)
        read_only_teacher_view = current_user.role == UserRole.ADMIN and effective_user.id != current_user.id

        if current_user.role == UserRole.ADMIN and current_page == "Dashboard":
            render_admin_dashboard(session, current_user)
        elif current_user.role == UserRole.ADMIN and current_page == "Approvals":
            render_admin_approvals(session, current_user)
        elif current_user.role == UserRole.ADMIN and current_page == "Users":
            render_admin_users(session, current_user)
        elif current_page in {"Teacher Workspace", "Workspace"}:
            render_teacher_workspace(session, effective_user, read_only_teacher_view)
        elif current_page in {"Teacher History", "History"}:
            render_history_page(session, effective_user, read_only_teacher_view)
        else:
            st.error("Unsupported page.")


if __name__ == "__main__":
    main()
