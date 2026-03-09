#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import streamlit as st
import streamlit.components.v1 as components
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
from security import (
    hash_otp,
    hash_password,
    hash_session_token,
    verify_otp,
    verify_password,
    verify_session_token,
)


@st.cache_resource
def bootstrap_app() -> bool:
    init_database()
    return True


bootstrap_app()


OTP_EXPIRY_MINUTES = 10
REMEMBER_SESSION_DAYS = 14
AUTH_QUERY_PARAM = "auth"
AUTH_COOKIE_NAME = "quiz_llm_studio_auth"
AUTH_STORAGE_KEY = "quiz_llm_studio_auth"
MAX_QUESTIONS = 20
MIN_OPTIONS = 2
MAX_OPTIONS = 6
DEFAULT_OPTION_COUNT = 4
DEFAULT_RENDER_SETTINGS = {
    "width": 1400,
    "height": 920,
    "padding": 82,
    "question_font_size": 46,
    "option_font_size": 41,
    "marks_font_size": 34,
    "question_image_max_height": 280,
    "option_image_max_height": 180,
    "watermark_opacity": 46,
    "watermark_size": 30,
    "watermark_step_x": 0,
    "watermark_step_y": 0,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def init_ui_state() -> None:
    st.session_state.setdefault("auth_user_id", None)
    st.session_state.setdefault("impersonated_user_id", None)
    st.session_state.setdefault("history_preview_request_id", None)
    st.session_state.setdefault("history_preview_outputs", [])
    st.session_state.setdefault("latest_generated_request_id", None)
    st.session_state.setdefault("latest_generated_outputs", [])
    st.session_state.setdefault("signup_pending_email", "")
    st.session_state.setdefault("reset_pending_identifier", "")
    st.session_state.setdefault("workspace_drafts", {})
    st.session_state.setdefault("auth_storage_action", "idle")
    st.session_state.setdefault("auth_storage_token", "")
    st.session_state.setdefault("skip_auth_restore_once", False)


def make_empty_option_state() -> dict[str, Any]:
    return {"text": "", "image": None}


def make_empty_question_state() -> dict[str, Any]:
    return {
        "marks": "2",
        "text": "",
        "question_images": [],
        "option_count": DEFAULT_OPTION_COUNT,
        "options": [make_empty_option_state() for _ in range(MAX_OPTIONS)],
    }


def image_asset_from_bytes(image_bytes: bytes, name: str) -> dict[str, Any]:
    return {
        "name": name,
        "bytes": image_bytes,
        "signature": hashlib.sha256(image_bytes).hexdigest(),
    }


def ensure_builder_state(owner_id: str) -> dict[str, Any]:
    drafts = st.session_state["workspace_drafts"]
    builder_state = drafts.get(owner_id)
    if builder_state is None:
        builder_state = {
            "question_count": 1,
            "current_question": 1,
            "font_size": DEFAULT_RENDER_SETTINGS["question_font_size"],
            "questions": [make_empty_question_state() for _ in range(MAX_QUESTIONS)],
        }
        drafts[owner_id] = builder_state

    builder_state.setdefault("question_count", 1)
    builder_state.setdefault("current_question", 1)
    builder_state.setdefault("font_size", DEFAULT_RENDER_SETTINGS["question_font_size"])
    builder_state.setdefault("questions", [])
    while len(builder_state["questions"]) < MAX_QUESTIONS:
        builder_state["questions"].append(make_empty_question_state())

    for question_state in builder_state["questions"]:
        question_state.setdefault("marks", "2")
        question_state.setdefault("text", "")
        question_state.setdefault("question_images", [])
        question_state.setdefault("option_count", DEFAULT_OPTION_COUNT)
        question_state.setdefault("options", [])
        while len(question_state["options"]) < MAX_OPTIONS:
            question_state["options"].append(make_empty_option_state())
        for option_state in question_state["options"]:
            option_state.setdefault("text", "")
            option_state.setdefault("image", None)

    return builder_state


def append_question_images(question_state: dict[str, Any], uploaded_files: list[Any] | None) -> None:
    if not uploaded_files:
        return
    existing_signatures = {image["signature"] for image in question_state["question_images"]}
    for uploaded_file in uploaded_files:
        if uploaded_file is None:
            continue
        image_bytes = uploaded_file.getvalue()
        if not image_bytes:
            continue
        image_asset = image_asset_from_bytes(image_bytes, uploaded_file.name)
        if image_asset["signature"] in existing_signatures:
            continue
        question_state["question_images"].append(image_asset)
        existing_signatures.add(image_asset["signature"])


def assign_option_image(option_state: dict[str, Any], uploaded_file: Any | None) -> None:
    if uploaded_file is None:
        return
    image_bytes = uploaded_file.getvalue()
    if not image_bytes:
        return
    option_state["image"] = image_asset_from_bytes(image_bytes, uploaded_file.name)


def question_has_content(question_payload: dict[str, Any]) -> bool:
    if question_payload.get("text", "").strip():
        return True
    if question_payload.get("question_images_b64"):
        return True
    for option in question_payload.get("options", []):
        if (option.get("text") or "").strip() or option.get("image_b64"):
            return True
    return False


def set_auth_query_token(token: str | None) -> None:
    if token:
        st.query_params[AUTH_QUERY_PARAM] = token
    else:
        st.query_params.clear()


def get_auth_query_token() -> str:
    token = st.query_params.get(AUTH_QUERY_PARAM, "")
    if isinstance(token, list):
        return token[0] if token else ""
    return str(token)


def get_auth_cookie_token() -> str:
    token = st.context.cookies.get(AUTH_COOKIE_NAME, "")
    if isinstance(token, list):
        return token[0] if token else ""
    return str(token)


def set_auth_storage(action: str, token: str = "") -> None:
    st.session_state["auth_storage_action"] = action
    st.session_state["auth_storage_token"] = token


def render_auth_storage_bridge() -> None:
    components.html(
        f"""
        <script>
        const action = {json.dumps(st.session_state.get("auth_storage_action", "idle"))};
        const token = {json.dumps(st.session_state.get("auth_storage_token", ""))};
        const cookieName = {json.dumps(AUTH_COOKIE_NAME)};
        const storageKey = {json.dumps(AUTH_STORAGE_KEY)};
        const cookieMaxAge = {REMEMBER_SESSION_DAYS * 24 * 60 * 60};
        const migrationKey = `${{storageKey}}_cookie_migrated`;
        const parentWindow = window.parent;
        const parentDocument = parentWindow.document;

        try {{
            const readCookie = (name) => {{
                const prefix = `${{name}}=`;
                const cookies = parentDocument.cookie ? parentDocument.cookie.split("; ") : [];
                for (const entry of cookies) {{
                    if (entry.startsWith(prefix)) {{
                        return entry.slice(prefix.length);
                    }}
                }}
                return "";
            }};

            const writeCookie = (value, maxAge) => {{
                parentDocument.cookie = `${{cookieName}}=${{value}}; Path=/; Max-Age=${{maxAge}}; SameSite=Lax`;
            }};

            const clearCookie = () => {{
                parentDocument.cookie = `${{cookieName}}=; Path=/; Max-Age=0; SameSite=Lax`;
            }};

            const storedToken = parentWindow.localStorage.getItem(storageKey) || "";
            const cookieToken = readCookie(cookieName);

            if (action === "store" && token) {{
                parentWindow.localStorage.setItem(storageKey, token);
                writeCookie(token, cookieMaxAge);
                parentWindow.sessionStorage.removeItem(migrationKey);
            }} else if (action === "clear") {{
                parentWindow.localStorage.removeItem(storageKey);
                clearCookie();
                parentWindow.sessionStorage.removeItem(migrationKey);
            }} else if (!cookieToken && storedToken) {{
                writeCookie(storedToken, cookieMaxAge);
                if (!parentWindow.sessionStorage.getItem(migrationKey)) {{
                    parentWindow.sessionStorage.setItem(migrationKey, "1");
                    parentWindow.location.reload();
                }}
            }} else if (cookieToken && storedToken !== cookieToken) {{
                parentWindow.localStorage.setItem(storageKey, cookieToken);
                parentWindow.sessionStorage.removeItem(migrationKey);
            }}
        }} catch (error) {{
            console.debug("auth-storage-bridge", error);
        }}
        </script>
        """,
        height=0,
    )


def apply_theme() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@500&family=IBM+Plex+Sans:wght@400;500;600;700&family=Space+Grotesk:wght@500;700&display=swap');

        :root {
            --bg-main: #050b14;
            --bg-panel: rgba(10, 18, 31, 0.84);
            --bg-panel-strong: #0d1726;
            --bg-panel-soft: rgba(18, 29, 47, 0.82);
            --bg-sidebar: #040913;
            --border: rgba(148, 163, 184, 0.18);
            --border-strong: rgba(255, 255, 255, 0.14);
            --text-primary: #edf4ff;
            --text-muted: #9bb0cf;
            --text-subtle: #6f84a6;
            --accent: #ffb44d;
            --accent-strong: #ff8f1f;
            --accent-cool: #54c6eb;
            --shadow: 0 22px 60px rgba(0, 0, 0, 0.42);
        }

        html, body, [class*="css"] {
            font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
        }

        .stApp {
            background:
                radial-gradient(circle at top right, rgba(255, 180, 77, 0.16), transparent 22%),
                radial-gradient(circle at left 18%, rgba(84, 198, 235, 0.14), transparent 26%),
                linear-gradient(180deg, #050b14 0%, #09111d 100%);
            color: var(--text-primary);
        }
        [data-testid="stAppViewContainer"] {
            background: transparent;
            color: var(--text-primary);
        }
        [data-testid="stHeader"] {
            background: rgba(5, 11, 20, 0.72);
            backdrop-filter: blur(12px);
            min-height: 3.8rem;
        }
        .block-container {
            padding-top: 4.6rem;
            padding-bottom: 2.2rem;
        }
        @media (max-width: 768px) {
            .block-container {
                padding-top: 5rem;
            }
        }
        h1, h2, h3, h4, h5, h6 {
            font-family: "Space Grotesk", "IBM Plex Sans", sans-serif;
            color: var(--text-primary);
            letter-spacing: -0.03em;
        }
        p, label, span, li, div, small {
            color: var(--text-primary);
        }
        code, pre {
            font-family: "IBM Plex Mono", monospace;
        }
        a {
            color: var(--accent-cool) !important;
        }
        [data-testid="stSidebar"] {
            background:
                radial-gradient(circle at top, rgba(255, 180, 77, 0.10), transparent 32%),
                linear-gradient(180deg, #040913 0%, #091120 100%);
            border-right: 1px solid var(--border);
        }
        [data-testid="stSidebar"] * {
            color: var(--text-primary);
        }
        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
        [data-testid="stSidebar"] [data-testid="stCaptionContainer"] {
            color: var(--text-muted) !important;
        }
        div[data-baseweb="tab-list"] {
            gap: 0.45rem;
            margin-top: 0.35rem;
            padding: 0.35rem;
            border: 1px solid var(--border);
            border-radius: 16px;
            background: rgba(9, 16, 28, 0.72);
        }
        button[role="tab"] {
            border-radius: 12px;
            padding: 0.6rem 0.9rem;
            color: var(--text-muted);
            transition: all 120ms ease-out;
        }
        button[role="tab"][aria-selected="true"] {
            background: linear-gradient(135deg, rgba(255, 180, 77, 0.18), rgba(84, 198, 235, 0.18));
            color: var(--text-primary);
            border: 1px solid rgba(255, 180, 77, 0.24);
        }
        div[data-baseweb="input"] > div,
        div[data-baseweb="base-input"] > div,
        .stTextArea textarea,
        [data-testid="stFileUploaderDropzone"] {
            background: rgba(9, 16, 28, 0.86) !important;
            color: var(--text-primary) !important;
            border: 1px solid var(--border) !important;
            border-radius: 14px !important;
        }
        div[data-baseweb="input"] input,
        div[data-baseweb="base-input"] input,
        .stTextArea textarea {
            color: var(--text-primary) !important;
            caret-color: var(--accent);
        }
        div[data-baseweb="input"] input::placeholder,
        div[data-baseweb="base-input"] input::placeholder,
        .stTextArea textarea::placeholder {
            color: var(--text-subtle) !important;
        }
        .stButton > button,
        div[data-testid="stFormSubmitButton"] > button,
        [data-testid="stDownloadButton"] > button {
            border-radius: 13px;
            border: 1px solid var(--border);
            background: linear-gradient(180deg, rgba(18, 29, 47, 0.95), rgba(11, 19, 31, 0.95));
            color: var(--text-primary);
            box-shadow: none;
        }
        .stButton > button:hover,
        div[data-testid="stFormSubmitButton"] > button:hover,
        [data-testid="stDownloadButton"] > button:hover {
            border-color: rgba(255, 180, 77, 0.28);
            transform: translateY(-1px);
        }
        div[data-testid="stFormSubmitButton"] > button[kind="primary"],
        .stButton > button[kind="primary"] {
            background: linear-gradient(135deg, var(--accent), var(--accent-strong));
            color: #08111d;
            border-color: rgba(255, 180, 77, 0.4);
            font-weight: 700;
        }
        [data-testid="stExpander"] {
            background: rgba(7, 13, 23, 0.72);
            border: 1px solid var(--border);
            border-radius: 16px;
            overflow: hidden;
        }
        [data-testid="stExpander"] summary {
            background: rgba(11, 19, 31, 0.8);
        }
        [data-testid="stAlert"] {
            background: rgba(9, 16, 28, 0.86);
            border: 1px solid var(--border);
        }
        .hero-card, .status-card {
            padding: 1rem 1.1rem;
            border-radius: 18px;
            background: var(--bg-panel);
            border: 1px solid var(--border);
            box-shadow: var(--shadow);
            backdrop-filter: blur(14px);
        }
        .hero-card, .hero-card *, .metric-strip, .metric-strip *, .status-card, .status-card * {
            color: var(--text-primary) !important;
        }
        .status-pill {
            display: inline-block;
            padding: 0.2rem 0.55rem;
            border-radius: 999px;
            font-size: 0.82rem;
            font-weight: 600;
            background: rgba(148, 163, 184, 0.14);
            color: var(--text-primary);
        }
        .status-pill.pending { background: rgba(255, 180, 77, 0.18); color: #ffd79f; }
        .status-pill.active { background: rgba(52, 211, 153, 0.18); color: #9ff2d0; }
        .status-pill.rejected { background: rgba(248, 113, 113, 0.18); color: #ffb7b7; }
        .status-pill.disabled { background: rgba(148, 163, 184, 0.14); color: #d7e0ef; }
        .metric-strip {
            padding: 0.9rem 1rem;
            border-radius: 16px;
            background: var(--bg-panel-soft);
            border: 1px solid var(--border-strong);
        }
        .sidebar-copy {
            font-size: 0.9rem;
            color: var(--text-muted);
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def format_dt(value: datetime | None) -> str:
    if value is None:
        return "Never"
    normalized = ensure_utc(value)
    return normalized.strftime("%Y-%m-%d %H:%M UTC")


def build_default_render_settings(question_font_size: int) -> dict[str, Any]:
    preset = DEFAULT_RENDER_SETTINGS.copy()
    scale = max(0.75, min(question_font_size / DEFAULT_RENDER_SETTINGS["question_font_size"], 1.8))

    preset["question_font_size"] = int(question_font_size)
    preset["option_font_size"] = max(26, int(round(question_font_size * 0.89)))
    preset["marks_font_size"] = max(22, int(round(question_font_size * 0.74)))
    preset["width"] = int(round(DEFAULT_RENDER_SETTINGS["width"] * min(1.22, max(0.96, 0.9 + scale * 0.1))))
    preset["height"] = int(round(DEFAULT_RENDER_SETTINGS["height"] * min(1.28, max(0.96, 0.9 + scale * 0.1))))
    preset["padding"] = int(round(DEFAULT_RENDER_SETTINGS["padding"] * min(1.18, max(0.95, 0.9 + scale * 0.1))))
    preset["question_image_max_height"] = max(
        220,
        int(round(DEFAULT_RENDER_SETTINGS["question_image_max_height"] * min(1.22, max(0.9, 0.86 + scale * 0.14)))),
    )
    preset["option_image_max_height"] = max(
        150,
        int(round(DEFAULT_RENDER_SETTINGS["option_image_max_height"] * min(1.22, max(0.9, 0.86 + scale * 0.14)))),
    )
    preset["watermark_size"] = max(24, int(round(question_font_size * 0.65)))
    preset["watermark_text"] = f"{DEFAULT_EXAM_WARNING} | {DEFAULT_WATERMARK_LINE}"
    return preset


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
    otp_generated_at = ensure_utc(user.otp_generated_at)
    if otp_generated_at is None:
        return "Invalid or expired OTP."
    if (utc_now() - otp_generated_at) > timedelta(minutes=OTP_EXPIRY_MINUTES):
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
        user.remember_token_hash = None
        user.remember_token_expires_at = None
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


def clear_request_outputs(request_id: str | None = None) -> None:
    if request_id is None or st.session_state.get("history_preview_request_id") == request_id:
        st.session_state["history_preview_request_id"] = None
        st.session_state["history_preview_outputs"] = []
    if request_id is None or st.session_state.get("latest_generated_request_id") == request_id:
        st.session_state["latest_generated_request_id"] = None
        st.session_state["latest_generated_outputs"] = []


def clear_history_widget_state(request_ids: list[str] | None = None) -> None:
    prefixes = (
        "history_title_",
        "confirm_delete_history_",
        "confirm_self_history_purge_",
        "confirm_purge_history_",
    )
    request_id_set = set(request_ids or [])
    keys_to_delete: list[str] = []
    for key in list(st.session_state.keys()):
        if not key.startswith(prefixes):
            continue
        if not request_id_set:
            keys_to_delete.append(key)
            continue
        if any(request_id in key for request_id in request_id_set):
            keys_to_delete.append(key)

    for key in keys_to_delete:
        st.session_state.pop(key, None)


def clear_builder_widget_state(owner_id: str) -> None:
    prefixes = (
        f"question_count_{owner_id}",
        f"current_question_{owner_id}",
        f"question_font_size_control_{owner_id}",
        f"marks_{owner_id}_",
        f"q_text_{owner_id}_",
        f"q_img_{owner_id}_",
        f"opt_count_{owner_id}_",
        f"opt_text_{owner_id}_",
        f"opt_img_{owner_id}_",
    )
    for key in list(st.session_state.keys()):
        if key.startswith(prefixes):
            st.session_state.pop(key, None)


def decode_question_images(question: dict[str, Any]) -> list[bytes]:
    encoded_images = question.get("question_images_b64") or []
    images = [decoded for decoded in (decode_image_bytes(item) for item in encoded_images) if decoded]
    if images:
        return images

    legacy_image = decode_image_bytes(question.get("question_image_b64"))
    return [legacy_image] if legacy_image else []


def load_payload_into_builder_state(owner_id: str, payload: dict[str, Any]) -> None:
    builder_state = ensure_builder_state(owner_id)
    builder_state["questions"] = [make_empty_question_state() for _ in range(MAX_QUESTIONS)]

    questions = payload.get("questions", [])[:MAX_QUESTIONS]
    question_count = max(1, len(questions) or 1)
    builder_state["question_count"] = question_count
    builder_state["current_question"] = 1

    settings = payload.get("settings", {})
    builder_state["font_size"] = int(settings.get("question_font_size", DEFAULT_RENDER_SETTINGS["question_font_size"]))

    for q_idx, question_payload in enumerate(questions):
        question_state = builder_state["questions"][q_idx]
        question_state["marks"] = str(question_payload.get("marks", "2"))
        question_state["text"] = str(question_payload.get("text", ""))
        question_state["question_images"] = [
            image_asset_from_bytes(image_bytes, f"Question {q_idx + 1} image {image_index}")
            for image_index, image_bytes in enumerate(decode_question_images(question_payload), start=1)
        ]

        options = question_payload.get("options", [])[:MAX_OPTIONS]
        question_state["option_count"] = max(MIN_OPTIONS, len(options) or DEFAULT_OPTION_COUNT)
        question_state["options"] = [make_empty_option_state() for _ in range(MAX_OPTIONS)]
        for opt_idx, option_payload in enumerate(options):
            option_state = question_state["options"][opt_idx]
            option_state["text"] = str(option_payload.get("text", ""))
            option_image = decode_image_bytes(option_payload.get("image_b64"))
            if option_image:
                option_state["image"] = image_asset_from_bytes(option_image, f"Option {chr(65 + opt_idx)} image")

    clear_builder_widget_state(owner_id)


def clear_persistent_login(user_id: str | None) -> None:
    if not user_id:
        return
    with get_session() as session:
        user = session.get(User, user_id)
        if user is None:
            return
        user.remember_token_hash = None
        user.remember_token_expires_at = None
        session.commit()


def logout(clear_persistent: bool = True) -> None:
    auth_user_id = st.session_state.get("auth_user_id")
    if clear_persistent:
        clear_persistent_login(auth_user_id)
    st.session_state["auth_user_id"] = None
    st.session_state["impersonated_user_id"] = None
    st.session_state["skip_auth_restore_once"] = True
    clear_outputs()
    clear_history_widget_state()
    set_auth_query_token(None)
    set_auth_storage("clear")


def establish_login_session(session, user: User, remember_device: bool) -> None:
    st.session_state["auth_user_id"] = user.id
    if remember_device:
        raw_token = f"{user.id}.{secrets.token_urlsafe(32)}"
        user.remember_token_hash = hash_session_token(raw_token)
        user.remember_token_expires_at = utc_now() + timedelta(days=REMEMBER_SESSION_DAYS)
        set_auth_storage("store", raw_token)
    else:
        user.remember_token_hash = None
        user.remember_token_expires_at = None
        set_auth_query_token(None)
        set_auth_storage("clear")


def schedule_auth_storage_clear(*, clear_query: bool) -> None:
    if clear_query:
        set_auth_query_token(None)
    set_auth_storage("clear")
    st.session_state["skip_auth_restore_once"] = True


def restore_login_from_persistent_token() -> None:
    if st.session_state.get("auth_user_id"):
        return

    cookie_token = get_auth_cookie_token().strip()
    query_token = get_auth_query_token().strip()
    raw_token = cookie_token or query_token
    token_source = "cookie" if cookie_token else "query"
    if not raw_token or "." not in raw_token:
        if query_token:
            set_auth_query_token(None)
        return

    user_id, _, _ = raw_token.partition(".")
    with get_session() as session:
        user = session.get(User, user_id)
        if user is None or not user.remember_token_hash or not user.remember_token_expires_at:
            schedule_auth_storage_clear(clear_query=bool(query_token))
            st.rerun()
            return
        remember_token_expires_at = ensure_utc(user.remember_token_expires_at)
        if remember_token_expires_at is None:
            user.remember_token_hash = None
            user.remember_token_expires_at = None
            session.commit()
            schedule_auth_storage_clear(clear_query=bool(query_token))
            st.rerun()
            return
        if remember_token_expires_at < utc_now():
            user.remember_token_hash = None
            user.remember_token_expires_at = None
            session.commit()
            schedule_auth_storage_clear(clear_query=bool(query_token))
            st.rerun()
            return
        if not verify_session_token(raw_token, user.remember_token_hash):
            schedule_auth_storage_clear(clear_query=bool(query_token))
            st.rerun()
            return
        if user.role != UserRole.ADMIN and user.status != UserStatus.ACTIVE:
            user.remember_token_hash = None
            user.remember_token_expires_at = None
            session.commit()
            schedule_auth_storage_clear(clear_query=bool(query_token))
            st.rerun()
            return

        st.session_state["auth_user_id"] = user.id
        if token_source == "query":
            set_auth_query_token(None)
            set_auth_storage("store", raw_token)
            st.rerun()


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


def rename_history_item(session, actor_user: User, request: QuizRequest, new_title: str) -> tuple[bool, str]:
    cleaned_title = " ".join(new_title.split())
    if not cleaned_title:
        return False, "Title cannot be empty."
    if cleaned_title == request.title:
        return False, "Title is unchanged."

    old_title = request.title
    request.title = cleaned_title
    log_event(
        session,
        "history_item_renamed",
        actor_user_id=actor_user.id,
        target_user_id=request.owner_id,
        request_id=request.id,
        detail={"old_title": old_title, "new_title": cleaned_title},
    )
    session.commit()
    return True, f"Renamed `{old_title}` to `{cleaned_title}`."


def delete_history_item(session, actor_user: User, request: QuizRequest) -> tuple[bool, str]:
    request_id = request.id
    request_title = request.title
    owner_id = request.owner_id

    log_event(
        session,
        "history_item_deleted",
        actor_user_id=actor_user.id,
        target_user_id=owner_id,
        request_id=request_id,
        detail={"title": request_title},
    )
    session.delete(request)
    session.commit()
    clear_request_outputs(request_id)
    clear_history_widget_state([request_id])
    return True, f"Deleted `{request_title}`."


def purge_user_history(session, actor_user: User, owner_user: User) -> tuple[bool, str]:
    requests = session.scalars(
        select(QuizRequest).where(QuizRequest.owner_id == owner_user.id).order_by(QuizRequest.created_at.desc())
    ).all()
    if not requests:
        return False, "No saved history to delete."

    deleted_count = len(requests)
    deleted_request_ids = [request.id for request in requests]
    for request in requests:
        session.delete(request)

    log_event(
        session,
        "user_history_purged",
        actor_user_id=actor_user.id,
        target_user_id=owner_user.id,
        detail={"count": deleted_count},
    )
    session.commit()
    clear_request_outputs()
    clear_history_widget_state(deleted_request_ids)
    return True, f"Deleted {deleted_count} saved history item(s)."


def purge_all_history(session, actor_user: User) -> tuple[bool, str]:
    requests = session.scalars(select(QuizRequest).order_by(QuizRequest.created_at.desc())).all()
    if not requests:
        return False, "No saved history exists in the app."

    deleted_count = len(requests)
    deleted_request_ids = [request.id for request in requests]
    for request in requests:
        session.delete(request)

    log_event(
        session,
        "app_history_purged",
        actor_user_id=actor_user.id,
        detail={"count": deleted_count},
    )
    session.commit()
    clear_request_outputs()
    clear_history_widget_state(deleted_request_ids)
    return True, f"Deleted {deleted_count} history item(s) across the app."


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
    if event.event_type == "history_item_renamed":
        return f"{actor_label} renamed {event.detail.get('old_title', 'a history item')}".strip()
    if event.event_type == "history_item_deleted":
        return f"{actor_label} deleted {event.detail.get('title', 'a history item')}".strip()
    if event.event_type == "user_disabled":
        return f"{actor_label} disabled {target_label}"
    if event.event_type == "user_enabled":
        return f"{actor_label} re-enabled {target_label}"
    if event.event_type == "user_deleted":
        return f"{actor_label} deleted {target_label}"
    if event.event_type == "user_history_purged":
        if event.actor_user_id == event.target_user_id:
            return f"{actor_label} deleted their saved history"
        return f"{actor_label} deleted all history for {target_label}"
    if event.event_type == "app_history_purged":
        return f"{actor_label} deleted all saved history across the app"
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
                    Quiz LLM Studio helps teachers turn quiz questions into watermarked image sheets that are harder to
                    reuse, search, or share during online quizzes and remote assessments.
                </p>
                <div style="display:grid; gap:0.7rem;">
                    <div class="metric-strip"><strong>Watermarked quiz sheets</strong><br/>Create question images designed for online assessments where answer sharing is a concern.</div>
                    <div class="metric-strip"><strong>Text and image questions</strong><br/>Mix question text, multiple question images, and option images in one compact rendered layout.</div>
                    <div class="metric-strip"><strong>Saved history</strong><br/>Keep drafts and generated quiz items available for later reuse, review, or moderation.</div>
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
                remember_device = st.checkbox("Remember this device")
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
                        establish_login_session(session, user, remember_device=remember_device)
                        session.commit()
                        set_impersonation(None)
                        st.rerun()

        with tab_signup:
            st.caption("Request access to use the quiz image generator for secure online assessments.")

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
            st.caption("Reset your password to continue using the quiz image generator.")
            with st.form("reset_password_form", clear_on_submit=False):
                reset_identifier_confirm = st.text_input(
                    "Login ID or email",
                    value=st.session_state.get("reset_pending_identifier", ""),
                )
                reset_otp = st.text_input("6-digit OTP", max_chars=6)
                new_password = st.text_input("New password", type="password")
                confirm_new_password = st.text_input("Confirm new password", type="password")
                request_col, reset_col = st.columns(2)
                reset_requested = request_col.form_submit_button("Send reset OTP")
                reset_submitted = reset_col.form_submit_button("Reset password", type="primary")

            if reset_requested:
                ok, message = request_password_reset_code(reset_identifier_confirm)
                (st.success if ok else st.error)(message)

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
        st.markdown(
            "<div class='sidebar-copy'>Build watermarked quiz images that help reduce cheating in online quizzes.</div>",
            unsafe_allow_html=True,
        )
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
            options = ["Dashboard", "Approvals", "Users", "Workspace", "History"]
            if effective_user is not None and effective_user.id != current_user.id:
                options.extend(["Teacher Workspace", "Teacher History"])
        else:
            options = ["Workspace", "History"]

        navigation_key = f"navigation_page_{current_user.id}"
        if st.session_state.get(navigation_key) not in options:
            st.session_state[navigation_key] = options[0]
        selected = st.radio("Navigate", options, key=navigation_key)
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
                    <div style="font-size:0.82rem; color:var(--text-muted);">{label}</div>
                    <div style="font-size:1.9rem; font-weight:700; margin:0.1rem 0 0.2rem;">{value}</div>
                    <div style="font-size:0.86rem; color:var(--text-subtle);">{note}</div>
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

    st.subheader("Platform history controls")
    st.caption("This deletes saved quiz history only. Accounts and approvals remain unchanged.")
    confirm_purge_all_key = "confirm_purge_all_history"
    purge_col1, purge_col2 = st.columns([1, 2])
    purge_col1.checkbox("Confirm full purge", key=confirm_purge_all_key)
    if purge_col2.button("Delete all saved history across the app", key="purge_all_history_button", use_container_width=True):
        if not st.session_state.get(confirm_purge_all_key):
            st.error("Tick confirm full purge first.")
        else:
            ok, message = purge_all_history(session, current_user)
            (st.success if ok else st.error)(message)
            if ok:
                st.rerun()

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
                <span style="color:var(--text-subtle);">{format_dt(event.created_at)}</span>
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

    st.divider()
    history_confirm_key = f"confirm_purge_history_{teacher.id}"
    history_col1, history_col2 = st.columns([1, 2])
    history_col1.checkbox("Confirm history purge", key=history_confirm_key)
    if history_col2.button("Delete all saved history", key=f"purge_history_{teacher.id}", use_container_width=True):
        if not st.session_state.get(history_confirm_key):
            st.error("Tick confirm history purge first.")
        else:
            ok, message = purge_user_history(session, current_user, teacher)
            (st.success if ok else st.error)(message)
            if ok:
                st.rerun()


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
        question_images = decode_question_images(question)
        for image_offset in range(0, len(question_images), 3):
            row_images = question_images[image_offset:image_offset + 3]
            columns = st.columns(len(row_images))
            for local_index, (column, image_bytes) in enumerate(zip(columns, row_images), start=1):
                with column:
                    st.image(
                        image_bytes,
                        caption=f"Question {index} image {image_offset + local_index}",
                        use_container_width=True,
                    )

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

    if not read_only:
        purge_self_key = f"confirm_self_history_purge_{target_user.id}"
        purge_col1, purge_col2 = st.columns([1, 2])
        purge_col1.checkbox("Confirm purge", key=purge_self_key)
        if purge_col2.button("Delete all my saved history", key=f"purge_self_history_{target_user.id}", use_container_width=True):
            if not st.session_state.get(purge_self_key):
                st.error("Tick confirm purge first.")
            else:
                ok, message = purge_user_history(session, target_user, target_user)
                (st.success if ok else st.error)(message)
                if ok:
                    st.rerun()

    requests = session.scalars(
        select(QuizRequest).where(QuizRequest.owner_id == target_user.id).order_by(QuizRequest.created_at.desc())
    ).all()
    request_ids = [request.id for request in requests]
    if st.session_state.get("history_preview_request_id") and st.session_state.get("history_preview_request_id") not in request_ids:
        clear_request_outputs(st.session_state.get("history_preview_request_id"))

    if not requests:
        clear_request_outputs()
        clear_history_widget_state()
        st.info("No saved quiz requests yet.")
        return

    for request in requests:
        with st.expander(f"{request.title} · {request.status.value} · {format_dt(request.created_at)}", expanded=False):
            render_saved_quiz_snapshot(request)

            if not read_only:
                rename_key = f"history_title_{request.id}"
                new_title = st.text_input("History item name", value=request.title, key=rename_key)
                action_col1, action_col2, action_col3, action_col4 = st.columns([1.1, 1.1, 1, 1])
                if action_col1.button("Load into workspace", key=f"load_history_{request.id}", use_container_width=True):
                    load_payload_into_builder_state(target_user.id, request.payload)
                    st.session_state[f"navigation_page_{target_user.id}"] = "Workspace"
                    clear_request_outputs(request.id)
                    st.rerun()

                if action_col2.button("Rename item", key=f"rename_history_{request.id}", use_container_width=True):
                    ok, message = rename_history_item(session, target_user, request, new_title)
                    (st.success if ok else st.error)(message)
                    if ok:
                        st.rerun()

                delete_confirm_key = f"confirm_delete_history_{request.id}"
                action_col3.checkbox("Confirm delete", key=delete_confirm_key)
                if action_col4.button("Delete item", key=f"delete_history_{request.id}", use_container_width=True):
                    if not st.session_state.get(delete_confirm_key):
                        st.error("Tick confirm delete first.")
                    else:
                        ok, message = delete_history_item(session, target_user, request)
                        (st.success if ok else st.error)(message)
                        if ok:
                            st.rerun()

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
    builder_state = ensure_builder_state(target_user.id)

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

    st.subheader("Quiz builder")
    builder_col1, builder_col2, builder_col3 = st.columns([1, 1, 1])
    question_count = int(
        builder_col1.number_input(
            "Number of questions",
            min_value=1,
            max_value=MAX_QUESTIONS,
            value=int(builder_state["question_count"]),
            step=1,
            key=f"question_count_{target_user.id}",
            disabled=read_only,
        )
    )
    builder_state["question_count"] = question_count
    builder_state["current_question"] = min(int(builder_state["current_question"]), question_count)

    current_question_number = int(
        builder_col2.number_input(
            "Edit question",
            min_value=1,
            max_value=question_count,
            value=int(builder_state["current_question"]),
            step=1,
            key=f"current_question_{target_user.id}",
            disabled=read_only,
        )
    )
    builder_state["current_question"] = current_question_number

    question_font_size = int(
        builder_col3.number_input(
            "Font size",
            min_value=28,
            max_value=72,
            value=int(builder_state["font_size"]),
            step=2,
            key=f"question_font_size_control_{target_user.id}",
            help="Use the +/- controls or type a value directly.",
            disabled=read_only,
        )
    )
    builder_state["font_size"] = question_font_size
    render_settings = build_default_render_settings(question_font_size)

    current_question_state = builder_state["questions"][current_question_number - 1]
    st.caption(f"Editing question {current_question_number} of {question_count}")

    with st.expander(f"Question {current_question_number}", expanded=True):
        current_question_state["marks"] = st.text_input(
            "Marks",
            value=str(current_question_state["marks"]),
            key=f"marks_{target_user.id}_{current_question_number}",
            disabled=read_only,
        )
        current_question_state["text"] = st.text_area(
            "Question text",
            value=str(current_question_state["text"]),
            height=110,
            key=f"q_text_{target_user.id}_{current_question_number}",
            placeholder="Enter the question statement",
            disabled=read_only,
        )
        q_img_files = st.file_uploader(
            "Question images (optional)",
            type=["png", "jpg", "jpeg", "webp"],
            accept_multiple_files=True,
            key=f"q_img_{target_user.id}_{current_question_number}",
            disabled=read_only,
        )
        if not read_only:
            append_question_images(current_question_state, q_img_files)

        if current_question_state["question_images"]:
            st.caption("Current question images")
            for image_offset in range(0, len(current_question_state["question_images"]), 3):
                row_images = current_question_state["question_images"][image_offset:image_offset + 3]
                columns = st.columns(len(row_images))
                for local_index, (column, image_asset) in enumerate(zip(columns, row_images), start=image_offset):
                    with column:
                        st.image(image_asset["bytes"], caption=image_asset["name"], use_container_width=True)
                        if not read_only and st.button(
                            "Remove image",
                            key=f"remove_q_img_{target_user.id}_{current_question_number}_{local_index}",
                            use_container_width=True,
                        ):
                            current_question_state["question_images"].pop(local_index)
                            st.rerun()

        current_question_state["option_count"] = int(
            st.slider(
                "Number of options",
                min_value=MIN_OPTIONS,
                max_value=MAX_OPTIONS,
                value=int(current_question_state["option_count"]),
                key=f"opt_count_{target_user.id}_{current_question_number}",
                disabled=read_only,
            )
        )

        for opt_idx in range(int(current_question_state["option_count"])):
            option_state = current_question_state["options"][opt_idx]
            col1, col2 = st.columns([3, 2])
            with col1:
                option_state["text"] = st.text_input(
                    f"Option {chr(65 + opt_idx)} text",
                    value=str(option_state["text"]),
                    key=f"opt_text_{target_user.id}_{current_question_number}_{opt_idx}",
                    disabled=read_only,
                )
            with col2:
                opt_img_file = st.file_uploader(
                    f"Option {chr(65 + opt_idx)} image",
                    type=["png", "jpg", "jpeg", "webp"],
                    key=f"opt_img_{target_user.id}_{current_question_number}_{opt_idx}",
                    disabled=read_only,
                )
                if not read_only:
                    assign_option_image(option_state, opt_img_file)
                if option_state.get("image"):
                    st.image(option_state["image"]["bytes"], caption=option_state["image"]["name"], width=220)
                    if not read_only and st.button(
                        f"Remove option {chr(65 + opt_idx)} image",
                        key=f"remove_opt_img_{target_user.id}_{current_question_number}_{opt_idx}",
                        use_container_width=True,
                    ):
                        option_state["image"] = None
                        st.rerun()

    questions_payload: list[dict[str, Any]] = []
    for question_state in builder_state["questions"][:question_count]:
        options_payload: list[dict[str, Any]] = []
        for opt_idx in range(int(question_state["option_count"])):
            option_state = question_state["options"][opt_idx]
            option_image = option_state.get("image")
            options_payload.append(
                {
                    "text": str(option_state["text"]),
                    "image_b64": encode_image_bytes(option_image["bytes"]) if option_image else None,
                }
            )

        questions_payload.append(
            {
                "text": str(question_state["text"]),
                "marks": str(question_state["marks"]),
                "question_images_b64": [
                    encode_image_bytes(image_asset["bytes"])
                    for image_asset in question_state["question_images"]
                ],
                "options": options_payload,
            }
        )

    payload = {
        "title": "",
        "settings": render_settings,
        "questions": questions_payload,
    }

    if not read_only:
        col1, col2 = st.columns(2)
        save_clicked = col1.button("Save draft", use_container_width=True)
        generate_clicked = col2.button("Save and generate", type="primary", use_container_width=True)

        if save_clicked or generate_clicked:
            non_empty_questions = [question for question in questions_payload if question_has_content(question)]
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
    render_auth_storage_bridge()
    if not st.session_state.pop("skip_auth_restore_once", False):
        restore_login_from_persistent_token()

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
        elif current_page == "Workspace":
            render_teacher_workspace(session, current_user, read_only=False)
        elif current_page == "History":
            render_history_page(session, current_user, read_only=False)
        elif current_page == "Teacher Workspace":
            render_teacher_workspace(session, effective_user, read_only_teacher_view)
        elif current_page == "Teacher History":
            render_history_page(session, effective_user, read_only_teacher_view)
        else:
            st.error("Unsupported page.")


if __name__ == "__main__":
    main()
