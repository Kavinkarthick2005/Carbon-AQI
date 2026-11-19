from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import Dict, Any, List
import uuid

from database import get_db
from models import User, Translation
from routers.auth import verify_firebase_token, check_admin_role, require_admin_user

router = APIRouter()

def seed_translations(db: Session):
    """Seed basic translations for English and Tamil"""
    
    # Check if translations already exist
    existing_count = db.query(Translation).count()
    if existing_count > 0:
        return  # Already seeded
    
    # Basic translations
    translations_data = [
        # English translations
        {"key_name": "nav_dashboard", "language_code": "en", "translated_text": "Dashboard"},
        {"key_name": "nav_aqi", "language_code": "en", "translated_text": "Air Quality"},
        {"key_name": "nav_carbon", "language_code": "en", "translated_text": "Carbon Footprint"},
        {"key_name": "nav_compare", "language_code": "en", "translated_text": "Compare Cities"},
        {"key_name": "nav_chatbot", "language_code": "en", "translated_text": "Chatbot"},
        {"key_name": "aqi_good", "language_code": "en", "translated_text": "Good (0-50)"},
        {"key_name": "aqi_moderate", "language_code": "en", "translated_text": "Moderate (51-100)"},
        {"key_name": "aqi_high", "language_code": "en", "translated_text": "High (101-150)"},
        {"key_name": "btn_calculate", "language_code": "en", "translated_text": "Calculate"},
        {"key_name": "btn_submit", "language_code": "en", "translated_text": "Submit"},
        {"key_name": "msg_loading", "language_code": "en", "translated_text": "Loading..."},
        {"key_name": "msg_error", "language_code": "en", "translated_text": "Error occurred"},
        
        # Tamil translations
        {"key_name": "nav_dashboard", "language_code": "ta", "translated_text": "டாஷ்போர்டு"},
        {"key_name": "nav_aqi", "language_code": "ta", "translated_text": "காற்று தரம்"},
        {"key_name": "nav_carbon", "language_code": "ta", "translated_text": "கார்பன் தடம்"},
        {"key_name": "nav_compare", "language_code": "ta", "translated_text": "நகரங்களை ஒப்பிடு"},
        {"key_name": "nav_chatbot", "language_code": "ta", "translated_text": "அரட்டைத் துண்டு"},
        {"key_name": "aqi_good", "language_code": "ta", "translated_text": "நல்லது (0-50)"},
        {"key_name": "aqi_moderate", "language_code": "ta", "translated_text": "மிதமானது (51-100)"},
        {"key_name": "aqi_high", "language_code": "ta", "translated_text": "அதிகம் (101-150)"},
        {"key_name": "btn_calculate", "language_code": "ta", "translated_text": "கணக்கிடு"},
        {"key_name": "btn_submit", "language_code": "ta", "translated_text": "சமர்ப்பிக்கவும்"},
        {"key_name": "msg_loading", "language_code": "ta", "translated_text": "ஏற்றுகிறது..."},
        {"key_name": "msg_error", "language_code": "ta", "translated_text": "பிழை ஏற்பட்டது"},
    ]
    
    # Insert translations
    for trans_data in translations_data:
        translation = Translation(**trans_data)
        db.add(translation)
    
    db.commit()
    print(f"Seeded {len(translations_data)} translations")

@router.get("/keys")
async def get_translation_keys(db: Session = Depends(get_db)):
    """Return all unique key_names from translation table"""
    
    # Seed translations if needed
    seed_translations(db)
    
    # Get unique keys
    keys = db.query(Translation.key_name).distinct().all()
    
    return {"keys": [key[0] for key in keys]}

@router.get("/{language_code}")
async def get_translations_by_language(
    language_code: str,
    db: Session = Depends(get_db)
):
    """Return all translations for a given language code as a dict"""
    
    # Seed translations if needed
    seed_translations(db)
    
    # Get translations for language
    translations = db.query(Translation)\
        .filter(Translation.language_code == language_code)\
        .all()
    
    # Convert to dict
    result = {}
    for trans in translations:
        result[trans.key_name] = trans.translated_text
    
    return result

@router.post("/add")
async def add_translation(
    translation_data: dict,
    token_data: dict = Depends(verify_firebase_token),
    db: Session = Depends(get_db)
):
    """Add new translation (admin only)"""
    
    # Check admin role
    check_admin_role(token_data, db)
    
    # Validate required fields
    required_fields = ['key_name', 'language_code', 'translated_text']
    for field in required_fields:
        if field not in translation_data:
            raise HTTPException(status_code=400, detail=f"Missing required field: {field}")
    
    # Check if translation already exists
    existing = db.query(Translation)\
        .filter(
            Translation.key_name == translation_data['key_name'],
            Translation.language_code == translation_data['language_code']
        )\
        .first()
    
    if existing:
        # Update existing translation
        existing.translated_text = translation_data['translated_text']
        db.commit()
        return {"message": "Translation updated successfully"}
    else:
        # Create new translation
        new_translation = Translation(
            key_name=translation_data['key_name'],
            language_code=translation_data['language_code'],
            translated_text=translation_data['translated_text']
        )
        db.add(new_translation)
        db.commit()
        return {"message": "Translation added successfully"}
