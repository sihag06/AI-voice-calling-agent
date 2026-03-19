"""
Data Provider - Simple File Reader
Reads business.txt and user_preference.json to provide context for the AI.
"""

import os
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Dict, Any, Optional
from openai import OpenAI


# Load env vars to ensure client works even if imported standalone
from dotenv import load_dotenv
load_dotenv()

DATA_FOLDER = Path("data")

# Initialize OpenAI client
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    print("[DATA] ⚠️ OPENAI_API_KEY not found in environment!")
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# ========== DATA STRUCTURES ==========

from dataclasses import dataclass, field
from typing import List, Dict, Any

@dataclass
class BusinessInfo:
    """Structured business information extracted from text"""
    name: str = ""
    location: str = ""
    business_overview: str = ""
    services: List[str] = field(default_factory=list)
    price_range: str = ""
    availability: str = ""
    phone: str = ""
    feedback: str = ""
    additional_info: Dict[str, Any] = field(default_factory=dict)

@dataclass
class UserPreference:
    """Holds defaults from user_preference.json"""
    service: str = ""
    duration: str = ""
    budget: Optional[float] = None
    location: str = ""
    preferred_date: str = ""
    preferred_time: str = ""
    special_requests: List[str] = None
    additional_preferences: Dict[str, Any] = None
    
    def __post_init__(self):
        self.special_requests = self.special_requests or []
        self.additional_preferences = self.additional_preferences or {}

# ========== PUBLIC FUNCTIONS ==========

def get_business_info(force_reload: bool = False) -> BusinessInfo:
    """Read data/business.txt and return BusinessInfo object."""
    # Caching enabled for performance
    if not force_reload and hasattr(get_business_info, "_cache"):
        return get_business_info._cache

    file_path = DATA_FOLDER / "business.txt"
    business = BusinessInfo()
    
    if file_path.exists():
        try:
            content = file_path.read_text(encoding="utf-8")
            print(f"[DATA DEBUG] Raw business.txt content (first 50 chars): {content[:50]}...")
            # Use intelligent parser for unstructured text
            business = _parse_business_intelligent(content)
            print(f"[DATA] ✅ Loaded business: {business.name}")
        except Exception as e:
            print(f"[DATA] ⚠️ Error reading business.txt: {e}")
            import traceback
            traceback.print_exc()
    
    get_business_info._cache = business
    return business

def get_user_preference(force_reload: bool = False) -> UserPreference:
    """Read data/user_preference.json and return UserPreference object."""
    # Caching enabled for performance
    if not force_reload and hasattr(get_user_preference, "_cache"):
        return get_user_preference._cache

    file_path = DATA_FOLDER / "user_preference.json"
    pref = UserPreference()
    
    if file_path.exists():
        try:
            content = file_path.read_text(encoding="utf-8")
            data = json.loads(content)
            pref = _parse_user_json(data)
            print(f"[DATA] ✅ Loaded preferences")
        except Exception as e:
            print(f"[DATA] ⚠️ Error reading user_preference.json: {e}")
            
    get_user_preference._cache = pref
    return pref

# ========== INTERNAL PARSING HELPERS ==========
def _parse_business_intelligent(content: str) -> BusinessInfo:
    """Use OpenAI to extract structured info from unstructured text."""
    if not openai_client:
        print("[DATA] ⚠️ OpenAI client not available, falling back to simple parser")
        return _parse_business_txt(content)

    try:
        prompt = f"""
You are a data extraction assistant.

Extract structured business information from the text below.

Return JSON ONLY with these keys:
- name
- location
- business_overview
- services (list of strings)
- price_range (string, ONLY the numerical range e.g. "₹600 - ₹1200", not the full text)
- availability
- phone
- feedback
- additional_info (any other relevant information not covered above)

Text:
{content}
"""

        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.1
        )

        data = json.loads(response.choices[0].message.content)

        # Validate additional_info is a dict
        add_info = data.get("additional_info", {})
        if isinstance(add_info, str):
            add_info = {"info": add_info}
            
        return BusinessInfo(
            name=data.get("name", ""),
            location=data.get("location", ""),
            business_overview=data.get("business_overview", ""),
            services=data.get("services", []) or [],
            price_range=data.get("price_range", ""),
            availability=data.get("availability", ""),
            phone=data.get("phone", ""),
            feedback=data.get("feedback", ""),
            additional_info={
                **add_info,
                "raw_text_preview": content[:200] + "..."
            }
        )

    except Exception as e:
        print(f"[DATA] ⚠️ Intelligent extraction failed: {e}, falling back")
        return _parse_business_txt(content)

def _parse_business_txt(content: str) -> BusinessInfo:
    """Fallback simple parser for key:value formatted business text."""
    b = BusinessInfo()
    lines = [l.strip() for l in content.split("\n") if l.strip()]
    
    current_key = None
    
    # Map of lowercase keys to BusinessInfo attributes
    key_map = {
        "name": "name", "business name": "name", "provider name": "name",
        "location": "location", "address": "location",
        "overview": "business_overview", "business overview": "business_overview", "description": "business_overview",
        "services": "services", "service": "services",
        "price": "price_range", "price range": "price_range", "pricing": "price_range",
        "availability": "availability", "hours": "availability", "timing": "availability",
        "phone": "phone", "mobile": "phone", "contact": "phone", "phone number": "phone",
        "feedback": "feedback", "reviews": "feedback", "rating": "feedback"
    }

    for line in lines:
        # Check if line looks like a header (e.g., "Pricing:")
        is_header = line.endswith(":") or (":" in line and len(line.split(":")[0]) < 20)
        
        if is_header:
            parts = line.split(":", 1)
            raw_key = parts[0].strip().lower()
            val = parts[1].strip() if len(parts) > 1 else ""
            
            # Identify which attribute this touches
            matched_attr = None
            for k, attr in key_map.items():
                if raw_key == k:
                    matched_attr = attr
                    break
            
            if matched_attr:
                current_key = matched_attr
                if val:
                    # set value immediately if present on same line
                    if matched_attr == "services":
                        b.services = [s.strip() for s in val.split(",") if s.strip()]
                    else:
                        setattr(b, matched_attr, val)
            else:
                # Unknown header, put in additional_info or ignore
                current_key = "additional_info"
                b.additional_info[raw_key] = val
                
        elif current_key:
            # It's a continuation value for the current key
            val = line
            if current_key == "services":
                # Add to services list
                new_services = [s.strip() for s in val.split(",") if s.strip()]
                b.services.extend(new_services)
            elif current_key == "additional_info":
                pass # Complex to append to arbitrary dict key without tracking strict sub-key
            else:
                # Append to existing string value (space separated)
                current_val = getattr(b, current_key)
                new_val = f"{current_val} {val}".strip()
                setattr(b, current_key, new_val)
        else:
            # First line without header is usually the name
            if not b.name:
                b.name = line

    if not b.additional_info:
        b.additional_info["raw_text_preview"] = content[:200] + "..."

    return b
 
def _parse_user_json(data: dict) -> UserPreference:
    """Parse user_preference.json handling nested structure."""
    # Check if nested under "user_preferences" (new format)
    target = data.get("user_preferences", data)
    
    return UserPreference(
        service=target.get("service_type") or target.get("service", ""),
        duration=str(target.get("session_duration_minutes") or target.get("duration", "")),
        budget=target.get("budget") or (target.get("price_range", {}).get("max") if isinstance(target.get("price_range"), dict) else None),
        location=target.get("location", ""),
        preferred_date=target.get("preferred_date", target.get("date", "")),
        preferred_time=target.get("preferred_time", target.get("time", "")),
        special_requests=target.get("special_requests", []),
        additional_preferences={k: v for k, v in target.items() if k not in [
            "service_type", "service", "session_duration_minutes", "duration", 
            "budget", "price_range", "location", "preferred_date", "date", 
            "preferred_time", "time", "special_requests"
        ]}
    )

if __name__ == "__main__":
    b_info = get_business_info(force_reload=True)
    print("\nBusiness Info:")
    print(f"Name: {b_info.name}")
    print(f"Services: {b_info.services}")
    
    u_pref = get_user_preference(force_reload=True)
    print("\nUser Preferences:")
    print(f"Service: {u_pref.service}")
    print(f"Budget: {u_pref.budget}")
