from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.orm import Session
from typing import Dict, Optional
import os
import firebase_admin
from firebase_admin import credentials, auth, exceptions as firebase_exceptions

from database import get_db
from models import User
from schemas import UserCreate, UserResponse
from config import get_settings

# Initialize Firebase Admin SDK using centralized settings
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    firebase_credentials_path = get_settings().FIREBASE_CREDENTIALS_PATH
    if firebase_credentials_path and not firebase_admin._apps:
        resolved_credentials_path = (
            firebase_credentials_path
            if os.path.exists(firebase_credentials_path)
            else os.path.join(_BACKEND_DIR, firebase_credentials_path)
        )
        if os.path.exists(resolved_credentials_path):
            cred = credentials.Certificate(resolved_credentials_path)
            firebase_admin.initialize_app(cred)
            print("Firebase Admin SDK initialized successfully")
        else:
            print(f"Firebase credentials file not found at: {firebase_credentials_path}")
            print("Warning: Firebase Admin SDK is not initialized; authenticated requests will fail with HTTP 503")
except Exception as e:
    print(f"Firebase initialization error: {e}")
    print("Warning: Firebase Admin SDK is not initialized; authenticated requests will fail with HTTP 503")

router = APIRouter()


def _extract_bearer_token(authorization: str | None) -> str:
    """Validate the Authorization header and extract the non-empty Bearer token."""
    if not authorization or not isinstance(authorization, str) or not authorization.strip():
        raise HTTPException(status_code=401, detail="Authorization header missing")

    scheme, separator, token_part = authorization.strip().partition(" ")
    if scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid authorization header format")

    if not separator or not token_part.strip():
        raise HTTPException(status_code=401, detail="Bearer token missing")

    token = token_part.strip()
    if any(ch.isspace() for ch in token):
        raise HTTPException(status_code=401, detail="Invalid authorization header format")

    return token


# Dependency to verify Firebase token
async def verify_firebase_token(authorization: str = Header(None)) -> Dict:
    token = _extract_bearer_token(authorization)

    # Reject securely when Firebase Admin SDK is not initialized
    if not firebase_admin._apps:
        raise HTTPException(
            status_code=503,
            detail="Authentication service is not configured or unavailable",
        )

    try:
        decoded_token = auth.verify_id_token(token)
    except auth.ExpiredIdTokenError:
        raise HTTPException(status_code=401, detail="Authentication token has expired")
    except auth.RevokedIdTokenError:
        raise HTTPException(status_code=401, detail="Authentication token has been revoked")
    except auth.UserDisabledError:
        raise HTTPException(status_code=401, detail="User account is disabled")
    except auth.InvalidIdTokenError:
        raise HTTPException(status_code=401, detail="Invalid authentication token")
    except (
        auth.CertificateFetchError,
        auth.ConfigurationNotFoundError,
        auth.UnexpectedResponseError,
        firebase_exceptions.UnavailableError,
        firebase_exceptions.InternalError,
    ):
        raise HTTPException(
            status_code=503,
            detail="Authentication verification service is unavailable",
        )
    except ValueError as exc:
        err_msg = str(exc).lower()
        if "project id" in err_msg or "default firebase app" in err_msg:
            raise HTTPException(
                status_code=503,
                detail="Authentication service is not configured or unavailable",
            )
        raise HTTPException(status_code=401, detail="Invalid authentication token")
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Authentication verification service is unavailable",
        )

    if not isinstance(decoded_token, dict) or not decoded_token.get("uid"):
        raise HTTPException(status_code=401, detail="Invalid authentication token")

    return decoded_token


def get_or_create_user(token_data: dict, db: Session, preferred_name: str | None = None) -> User:
    """
    Look up the DB user by firebase_uid.
    Auto-creates the record from the verified Firebase token if the user hasn't registered yet,
    so that any authenticated route works immediately after Firebase login.
    """
    firebase_uid = token_data.get('uid')
    if not firebase_uid:
        raise HTTPException(
            status_code=400,
            detail="Verified identity is missing required claims (uid)",
        )

    user = db.query(User).filter(User.firebase_uid == firebase_uid).first()
    if user:
        return user

    email = token_data.get('email')
    if not email:
        raise HTTPException(
            status_code=400,
            detail="Verified identity is missing required claims (email)",
        )

    conflicting_user = db.query(User).filter(User.email == email).first()
    if conflicting_user and conflicting_user.firebase_uid != firebase_uid:
        raise HTTPException(
            status_code=409,
            detail="An account with this email already exists under a different identity",
        )

    display_name = (
        preferred_name
        or token_data.get('name')
        or email.split('@')[0]
    )

    user = User(
        firebase_uid=firebase_uid,
        email=email,
        name=display_name,
        password_hash='firebase_auth',
        preferred_language='en',
        role='user',
    )
    try:
        db.add(user)
        db.commit()
        db.refresh(user)
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="Failed to provision user account",
        )
    return user


@router.post("/register", response_model=UserResponse)
async def register_user(
    user_data: Optional[UserCreate] = None,
    token_data: Dict = Depends(verify_firebase_token),
    db: Session = Depends(get_db),
):
    """
    Provision or return a user account based on verified Firebase token claims.
    Accepts optional display name profile information; never trusts client-supplied credentials.
    """
    preferred_name = None
    if user_data:
        preferred_name = user_data.display_name or user_data.name

    return get_or_create_user(token_data, db, preferred_name=preferred_name)

def get_current_user(
    token_data: Dict = Depends(verify_firebase_token),
    db: Session = Depends(get_db),
) -> User:
    """
    Resolve the registered database User from verified Firebase token claims.
    Rejects unregistered identities with HTTP 404.
    """
    firebase_uid = token_data.get("uid") if isinstance(token_data, dict) else None
    if not firebase_uid:
        raise HTTPException(
            status_code=400,
            detail="Verified identity is missing required claims (uid)",
        )

    user = db.query(User).filter(User.firebase_uid == firebase_uid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return user


@router.get("/me", response_model=UserResponse)
async def get_current_user_profile(
    user: User = Depends(get_current_user),
):
    """Get current user information"""
    return user

@router.put("/language")
async def update_language(
    language_data: dict,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update user's preferred language"""
    language = language_data.get('language')
    
    if not language:
        raise HTTPException(status_code=400, detail="Language is required")
    
    user.preferred_language = language
    db.commit()
    
    return {"message": "Language updated"}


def require_admin_user(
    token_data: Dict = Depends(verify_firebase_token),
    db: Session = Depends(get_db),
) -> User:
    """
    Ensure the caller is authenticated and holds the 'admin' role in the database.
    Can be used as a FastAPI dependency or called directly: check_admin_role(token_data, db).
    """
    user = get_current_user(token_data=token_data, db=db)

    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    return user


check_admin_role = require_admin_user

