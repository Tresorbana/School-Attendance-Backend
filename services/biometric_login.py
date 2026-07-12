"""Biometric login: match a scan → issue a JWT for the linked User account.

Only works on the desktop app (the browser cannot talk to the scanner). The
scan blob is captured by the C# fingerprint bridge and posted here as base64.
"""
import logging
from typing import Optional

from fastapi import HTTPException, status

from database import StructuredSession
from models.person import Person
from models.user import User
from pipeline.match import identify as pipeline_identify
from services.auth import create_token
from services.template_cache import template_cache

logger = logging.getLogger("biometric_login")


def biometric_login(image_bytes: bytes) -> dict:
    enrolled = template_cache.get()
    match = pipeline_identify(image_bytes, enrolled)

    if match.get("flag") == "low_confidence" or not match.get("matched"):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Fingerprint not recognised — please sign in with email and password.",
        )

    try:
        person_id = int(match["person_id"])
    except (TypeError, ValueError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid match")

    db = StructuredSession()
    try:
        person: Optional[Person] = (
            db.query(Person).filter(Person.id == person_id).first()
        )
        if not person:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Employee record missing")

        if not person.user_id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"{person.name} has no linked portal account. Ask an admin to enroll an email.",
            )

        db_user: Optional[User] = (
            db.query(User).filter(User.id == person.user_id).first()
        )
        if not db_user or not db_user.is_active:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Linked user account is inactive.",
            )

        user_dict = {
            "username": db_user.username,
            "email": db_user.email,
            "full_name": db_user.full_name,
            "role": db_user.role,
            "must_change_password": bool(db_user.must_change_password),
        }
        token = create_token(user_dict)

        logger.info(
            "Biometric login OK person_id=%s user=%s role=%s",
            person.id, db_user.username, db_user.role,
        )
        return {**user_dict, "token": token, "personId": person.id, "score": match.get("confidence_score", 0) / 100.0}
    finally:
        db.close()
