import re
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

PASSWORD_REGEX = re.compile(
    r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[!@#$%^&*()_+\-=\[\]{};':\"\\|,.<>/?]).{8,}$"
)


def validate_strong_password(v: str) -> str:
    if not isinstance(v, str) or not PASSWORD_REGEX.match(v):
        raise ValueError(
            "Password must be at least 8 characters long and contain at least one uppercase letter, "
            "one lowercase letter, one number, and one special character."
        )
    return v


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)
