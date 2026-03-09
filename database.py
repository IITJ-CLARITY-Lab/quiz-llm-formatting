from __future__ import annotations

import os
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from models import Base, User, UserRole, UserStatus
from security import hash_password


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///quiz_llm_studio.db")

engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, class_=Session)


def init_database(retries: int = 20, sleep_seconds: int = 2) -> None:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            Base.metadata.create_all(bind=engine)
            with SessionLocal() as session:
                run_migrations(session)
                ensure_admin_account(session)
            return
        except OperationalError as exc:
            last_error = exc
            if attempt == retries:
                break
            time.sleep(sleep_seconds)

    if last_error is not None:
        raise last_error


def run_migrations(session: Session) -> None:
    inspector = inspect(engine)
    existing_columns = {column["name"] for column in inspector.get_columns("users")}
    email_verified_added = "email_verified_at" not in existing_columns

    if "otp" not in existing_columns:
        session.execute(text("ALTER TABLE users ADD COLUMN otp VARCHAR(255)"))
    elif engine.dialect.name == "postgresql":
        session.execute(text("ALTER TABLE users ALTER COLUMN otp TYPE VARCHAR(255)"))
    if "otp_purpose" not in existing_columns:
        session.execute(text("ALTER TABLE users ADD COLUMN otp_purpose VARCHAR(32)"))
    if "otp_generated_at" not in existing_columns:
        session.execute(text("ALTER TABLE users ADD COLUMN otp_generated_at TIMESTAMP"))
    if email_verified_added:
        session.execute(text("ALTER TABLE users ADD COLUMN email_verified_at TIMESTAMP"))
    if "remember_token_hash" not in existing_columns:
        session.execute(text("ALTER TABLE users ADD COLUMN remember_token_hash VARCHAR(255)"))
    elif engine.dialect.name == "postgresql":
        session.execute(text("ALTER TABLE users ALTER COLUMN remember_token_hash TYPE VARCHAR(255)"))
    if "remember_token_expires_at" not in existing_columns:
        session.execute(text("ALTER TABLE users ADD COLUMN remember_token_expires_at TIMESTAMP"))

    if email_verified_added:
        session.execute(
            text(
                "UPDATE users SET email_verified_at = COALESCE(email_verified_at, created_at) "
                "WHERE email_verified_at IS NULL"
            )
        )
    session.commit()


def ensure_admin_account(session: Session) -> None:
    admin_email = (
        os.getenv("APP_ADMIN_EMAIL")
        or os.getenv("DEFAULT_ADMIN_EMAIL")
        or "m25ai2043@iitj.ac.in"
    ).strip().lower()

    existing_seeded_user = session.scalar(select(User).where(User.email == admin_email))
    if existing_seeded_user is not None:
        return

    raw_login_id = (
        os.getenv("APP_ADMIN_LOGIN_ID")
        or os.getenv("DEFAULT_ADMIN_USERNAME")
        or admin_email.split("@")[0]
        or "ADMIN"
    )
    admin_login_id = raw_login_id.strip().upper()
    admin_name = (os.getenv("APP_ADMIN_NAME") or raw_login_id or "Platform Admin").strip() or "Platform Admin"
    admin_password = (
        os.getenv("APP_ADMIN_PASSWORD")
        or os.getenv("DEFAULT_ADMIN_PASSWORD")
        or "1312"
    )

    conflicting_login_id = session.scalar(
        select(User.id).where((User.login_id == admin_login_id) & (User.email != admin_email))
    )
    if conflicting_login_id is not None:
        admin_login_id = f"ADMIN-{secrets.token_hex(2).upper()}"

    session.add(
        User(
            login_id=admin_login_id,
            full_name=admin_name,
            email=admin_email,
            institution="Platform Administration",
            password_hash=hash_password(admin_password),
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            approved_at=datetime.now(timezone.utc),
            email_verified_at=datetime.now(timezone.utc),
            approval_note="Bootstrap admin account.",
        )
    )
    session.commit()


@contextmanager
def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
