import os
import uuid
import asyncio
import hashlib
import json
from typing import Optional, Dict, Any
from datetime import datetime, timedelta

from dotenv import load_dotenv
load_dotenv() # Load env vars before imports

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel  
from live_transcript import LiveTranscriptHandler
from data_provider import get_business_info, get_user_preference
from prompt_builder import build_dynamic_prompt, generate_welcome_message
from bolna_client import get_bolna_client
import threading

# Optional: Real-time transcription service (may not be available)
try:
    from realtime_transcription import RealTimeTranscriptionService
except ImportError:
    RealTimeTranscriptionService = None
    print("[IMPORT] realtime_transcription module not found - real-time transcription will be disabled")  


load_dotenv()  # load .env automatically

import os
from pathlib import Path
from dotenv import load_dotenv, find_dotenv

# ========= ENV LOADING =========
print("=== DEBUG ENV LOADING ===")

# Find and load .env (same folder as main.py)
env_path = Path(__file__).resolve().parent / ".env"
print("CWD:", os.getcwd())
print("__file__:", __file__)
print("find_dotenv() ->", repr(find_dotenv()))
print(".env next to main.py exists:", env_path.exists(), "at", env_path)

loaded = load_dotenv(env_path, override=True)
print("load_dotenv(...) returned:", loaded)

# Read env vars ONCE into module-level constants
BOLNA_API_KEY = os.getenv("BOLNA_API_KEY")
BOLNA_ASSISTANT_ID = os.getenv("BOLNA_ASSISTANT_ID")
BOLNA_PHONE_NUMBER_ID = os.getenv("BOLNA_PHONE_NUMBER_ID")

# print("API KEY:", BOLNA_API_KEY)
# print("ASSISTANT:", BOLNA_ASSISTANT_ID)
# print("PHONE:", BOLNA_PHONE_NUMBER_ID)

# (optional) define base URL if you haven't already
BOLNA_BASE_URL = "https://api.bolna.ai"

# ========= FASTAPI APP =========
app = FastAPI(title="Voice Call Agent with Bolna.ai Integration")




class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.bolna_to_app_call: Dict[str, str] = {}
        self.app_call_control_url: Dict[str, str] = {}
        # Track how many messages we have fully processed for each call
        self.call_message_cursors: Dict[str, int] = {} 
        # Track the content hash of each message by index to detect duplicates
        self.message_content_hashes: Dict[str, Dict[int, str]] = {}  # app_call_id -> {index: content_hash}
        # Track current partial transcript index for each speaker (for streaming updates)
        self.partial_transcript_indices: Dict[str, Dict[str, int]] = {}  # app_call_id -> {speaker: current_index}

    async def connect(self, call_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[call_id] = websocket
        self.call_message_cursors[call_id] = 0
        self.message_content_hashes[call_id] = {}

    def disconnect(self, call_id: str):
        self.active_connections.pop(call_id, None)
        self.app_call_control_url.pop(call_id, None)
        self.call_message_cursors.pop(call_id, None)
        self.message_content_hashes.pop(call_id, None)
        self.partial_transcript_indices.pop(call_id, None)

    async def send_to_app_call(self, call_id: str, message: dict):
        ws = self.active_connections.get(call_id)
        if ws:
            try:
                await ws.send_json(message)
                print(f"[WEBSOCKET] ✅ Sent message to {call_id}: type={message.get('type')}, speaker={message.get('speaker')}")
            except RuntimeError as e:
                # WebSocket was closed or response already completed
                if "websocket.close" in str(e) or "response already completed" in str(e) or "Unexpected ASGI message" in str(e):
                    print(f"[WEBSOCKET] ⚠️ WebSocket was closed for {call_id}, removing from active connections")
                    self.disconnect(call_id)
                else:
                    print(f"[WEBSOCKET] ❌ ERROR sending to {call_id}: {e}")
                    import traceback
                    traceback.print_exc()
            except Exception as e:
                # Check if it's a connection error
                error_str = str(e).lower()
                if "closed" in error_str or "disconnect" in error_str or "connection" in error_str:
                    print(f"[WEBSOCKET] ⚠️ WebSocket connection error for {call_id}, removing from active connections")
                    self.disconnect(call_id)
                else:
                    print(f"[WEBSOCKET] ❌ ERROR sending to {call_id}: {e}")
                    import traceback
                    traceback.print_exc()
        else:
            print(f"[WEBSOCKET] ⚠️ WARNING: No WebSocket connection found for call_id: {call_id}")
            print(f"[WEBSOCKET] Active connections: {list(self.active_connections.keys())}")
            print(f"[WEBSOCKET] Message that failed to send: {message}")

    def link_bolna_call(self, app_call_id: str, bolna_call_id: str, control_url: Optional[str] = None):
        self.bolna_to_app_call[bolna_call_id] = app_call_id
        if control_url:
            self.app_call_control_url[app_call_id] = control_url

    def get_app_call_id_from_bolna(self, bolna_call_id: str) -> Optional[str]:
        return self.bolna_to_app_call.get(bolna_call_id)
    
    def get_bolna_call_id_from_app_call(self, app_call_id: str) -> Optional[str]:
        """Reverse lookup: find bolna_call_id from app_call_id"""
        for bolna_id, app_id in self.bolna_to_app_call.items():
            if app_id == app_call_id:
                return bolna_id
        return None

    def get_control_url_for_app_call(self, app_call_id: str) -> Optional[str]:
        return self.app_call_control_url.get(app_call_id)

    # Get and Set Cursor
    def get_cursor(self, app_call_id: str) -> int:
        return self.call_message_cursors.get(app_call_id, 0)

    def update_cursor(self, app_call_id: str, new_index: int):
        self.call_message_cursors[app_call_id] = new_index

    # Check if message content has changed
    def has_message_changed(self, app_call_id: str, index: int, content: str) -> bool:
        """Returns True if message content is new or has changed"""
        content_hash = hashlib.md5(content.encode()).hexdigest()
        stored_hash = self.message_content_hashes.get(app_call_id, {}).get(index)
        
        if stored_hash != content_hash:
            # Update stored hash
            if app_call_id not in self.message_content_hashes:
                self.message_content_hashes[app_call_id] = {}
            self.message_content_hashes[app_call_id][index] = content_hash
            return True
        return False

manager = ConnectionManager()

# In-memory store for transcripts
conversation_store: Dict[str, list] = {}      # app_call_id -> list of transcript entries
conversation_summaries: Dict[str, str] = {}   # app_call_id -> summary text

# Initialize live transcript handler
transcript_handler = LiveTranscriptHandler(
    connection_manager=manager,
    conversation_store=conversation_store,
    bolna_to_app_call=manager.bolna_to_app_call,
    bolna_api_key=BOLNA_API_KEY,
    bolna_base_url=BOLNA_BASE_URL
)
# Mapping: Twilio Call SID -> app_call_id (for Media Streams)
twilio_call_sid_to_app_call: Dict[str, str] = {}

# Approval tracking (will be initialized after CallPreferences is defined)
pending_approvals: Dict[str, dict] = {}       # app_call_id -> approval details
call_approval_status: Dict[str, dict] = {}    # app_call_id -> {"status": "approved/denied/timeout", "negotiated_value": "...", "original_value": "..."}
active_approvals: Dict[str, dict] = {}       # app_call_id -> active approval (during call)
user_phone_numbers: Dict[str, str] = {}       # app_call_id -> user phone number
call_preferences: Dict[str, dict] = {}  # app_call_id -> call preferences (stored as dict)


# ---------- Pydantic model for preferences ----------

class CallPreferences(BaseModel):
    business_id: Optional[str] = None        # Optional business identifier
    business_name: Optional[str] = None     # Optional business name
    user_name: Optional[str] = None
    user_phone: Optional[str] = None         # User's phone number for sharing with business owner if needed
    requirement: Optional[str] = None         # User's requirement/service needed (e.g. "dental consultation", "gym membership", "room booking")
    preferred_date: Optional[str] = None     # "dd-mm-yyyy" or flexible
    preferred_call_time: Optional[str] = "Any"
    budget: Optional[float] = None
    negotiation_type: Optional[str] = "normal"
    notes: Optional[str] = None             # Additional requirements or preferences
    special_requests: Optional[list] = []   # Special service requests (e.g., urgent service, weekend availability)
    call_type: str = "info"                 # info, negotiation, auto, or booking
    service_type: Optional[str] = None      # Specific service details
    urgency: Optional[str] = None           # e.g., "Immediate", "Within 24 hours"
    service_address: Optional[str] = None   # Address for service delivery
    location: Optional[str] = None          # User location
    custom_data: Optional[Dict[str, Any]] = {} # Capture any extra form fields from dynamic UI
    service: Optional[str] = None            # Main service requested (e.g. from 'service' field)

    business_owner_phone: str                # "+91..." – Business owner's phone number


class ApprovalRequest(BaseModel):
    call_id: str
    approval_type: str                        # e.g., "price_negotiation", "terms_change"
    description: str                          # What needs approval
    original_value: Optional[str] = None     # Original price/term
    negotiated_value: Optional[str] = None   # Negotiated price/term
    user_budget: Optional[float] = None     # User's original budget/preference

class ApprovalResponse(BaseModel):
    call_id: str
    approved: bool
    user_phone: Optional[str] = None         # User phone to share if approved
    


# ---------- Health check ----------

@app.get("/health")
async def health_check():
    return {"status": "ok"}

@app.get("/bolna/server")
async def bolna_server_get():
    """Test endpoint to verify webhook URL is accessible"""
    return {
        "status": "ok",
        "message": "Webhook endpoint is accessible. Configure this URL in Bolna.ai Assistant settings.",
        "webhook_url": "/bolna/server",
        "method": "POST"
    }

# ---------- Dynamic Form Generation ----------

# Global state for the current schema (for LLM-generated forms)
current_schema: Optional[dict] = None
schema_lock = threading.Lock()

# OpenAI client for schema generation (optional)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

def get_fallback_schema(query: str = "") -> dict:
    """
    Return a fallback schema when LLM is not available or fails.
    Uses data from the data folder to provide sensible defaults.
    """
    # Try to get context from data provider for better defaults
    try:
        from data_provider import get_business_info, get_user_preference
        business = get_business_info()
        pref = get_user_preference()
        
        title = f"Call {business.name}" if business.name else "Voice Agent Call Form"
        description = f"Help us call the business on your behalf"
        if query:
            description += f": {query}"
        elif business.name:
            description = f"Let our voice agent call {business.name} to check availability and pricing for you"
        
        # Build service options from business info
        service_options = business.services if business.services else ["General Inquiry"]
        
        # Get default values from user preferences
        default_service = pref.service if pref.service else ""
        default_budget = str(int(pref.budget)) if pref.budget else ""
        default_location = pref.location if pref.location else business.location
        
        return {
            "title": title,
            "description": description,
            "fields": [
                {
                    "id": "service",
                    "label": "Service Required",
                    "type": "select" if len(service_options) > 1 else "text",
                    "required": True,
                    "placeholder": default_service or "Select the service you need",
                    "options": service_options if len(service_options) > 1 else None
                },
                {
                    "id": "provider_name",
                    "label": "Provider Name",
                    "type": "text",
                    "required": False,
                    "placeholder": business.name or "Business Name"
                },
                {
                    "id": "budget",
                    "label": "Budget (₹)",
                    "type": "number",
                    "required": False,
                    "placeholder": default_budget or f"e.g., {business.price_range}" if business.price_range else "Enter your budget"
                },
                {
                    "id": "preferred_date",
                    "label": "Preferred Date",
                    "type": "date",
                    "required": False
                },
                {
                    "id": "preferred_time",
                    "label": "Preferred Time",
                    "type": "select",
                    "required": False,
                    "options": ["Any", "Morning", "Afternoon", "Evening"]
                },
                {
                    "id": "location",
                    "label": "Location",
                    "type": "text",
                    "required": False,
                    "placeholder": default_location or "Preferred location"
                },
                {
                    "id": "notes",
                    "label": "Additional Requirements",
                    "type": "textarea",
                    "required": False,
                    "placeholder": "Any specific requirements or preferences..."
                }
            ],
            "service_category": "Service Details",
            "service_label": "Service Type",
            "urgency_label": "Urgency"
        }
    except Exception as e:
        print(f"[SCHEMA] ⚠️ Error building fallback schema from data: {e}")
        # Ultimate fallback if data provider fails
        return {
            "title": "Voice Agent Call Form",
            "description": f"Help us call the business on your behalf{': ' + query if query else ''}",
            "fields": [
                {
                    "id": "requirement",
                    "label": "Your Requirement",
                    "type": "text",
                    "required": True,
                    "placeholder": "What do you need?"
                },
                {
                    "id": "budget",
                    "label": "Budget",
                    "type": "number",
                    "required": False,
                    "placeholder": "Enter your budget (optional)"
                },
                {
                    "id": "preferred_date",
                    "label": "Preferred Date",
                    "type": "date",
                    "required": False
                },
                {
                    "id": "notes",
                    "label": "Additional Notes",
                    "type": "textarea",
                    "required": False,
                    "placeholder": "Any other requirements..."
                }
            ]
        }

# def generate_schema_from_query(query: str) -> dict:
#     """
#     Generate a form schema using ChatGPT with business data context.
    
#     This function combines:
#     1. Business information from data/business.txt
#     2. User preferences from data/user_preference.json
#     3. The user's query
    
#     The data is fetched via DataProvider, which can be replaced
#     with a chatbot integration in the future.
#     """
#     if not OPENAI_API_KEY:
#         return get_fallback_schema(query)
    
#     try:
#         from openai import OpenAI
#         client = OpenAI(api_key=OPENAI_API_KEY)
        
#         # Get context from data provider (business.txt + user_preference.json)
#         from data_provider import get_business_info, get_user_preference
#         business = get_business_info()
#         pref = get_user_preference()
#         data_context = f"""
# BUSINESS INFO:
# Name: {business.name}
# Services: {', '.join(business.services)}
# Price Range: {business.price_range}
# Location: {business.location}
# Additional Info: {business.additional_info}
# feedback: {business.feedback}
# availability: {business.availability}
# business_overview: {business.business_overview}

# USER PREFERENCES (Defaults):
# Service: {pref.service}
# Budget: {pref.budget}
# Notes: {pref.special_requests}
# Location: {pref.location}

# USER QUERY: "{query}"
# """
        
#         print(f"\n{'='*60}")
#         print("[SCHEMA GENERATION] 📊 Context loaded from data folder:")
#         print(f"{'='*60}")
#         print(data_context)
#         print(f"{'='*60}\n")
        
#         # System prompt with clear instructions
#         system_prompt = """You are an expert at understanding business contexts and generating form schemas for voice agent calls.

# Your task: Generate a JSON schema for a UI form that collects information from a user who wants to contact a business via voice agent.

# IMPORTANT CONTEXT:
# - You will receive business information (name, services, pricing, location)
# - You will receive user preferences (what they're looking for, budget, etc.)
# - You will receive the user's query

# RULES:
# 1. Analyze ALL provided context (business info + user preferences + query)
# 2. Generate form fields relevant to the specific business and user needs
# 3. Pre-fill field placeholders with relevant information from the context
# 4. If user has a preferred service, include it as a default option
# 5. If business has specific services, offer them as select options
# 6. Keep fields minimal but sufficient (max 8 fields)
# 7. Use appropriate field types: text, number, boolean, select, date, textarea
# 8. Always include: budget (number), preferred_date (date), notes (textarea), provider_name (text)
# 9. Make important fields required, optional ones not required
# 10. Title should reference the business name if available
# 11. CRITICAL: For 'service_label' and 'urgency_label', DO NOT use generic terms like 'Service Type' or 'Urgency'. Use specific terms like 'Cuisine', 'Dish Type', 'Treatment', 'Repair Type', 'Reservation Time', 'Appointment Slot', etc. based on the business context."""

#         # User prompt with full context
#         user_prompt = f"""CONTEXT INFORMATION:
# {data_context}

# Based on the above context, generate a JSON form schema for the voice agent to collect user requirements.
# while generating the "special_request_options", it should be related to the business and user query, and 
# user preferences. you can use own resoning to decide those options but it must be related 
# to {data_context}
# Output ONLY valid JSON with this structure:
# {{
#   "title": "string (include business name if available)",
#   "description": "string (describe what the form is for)",
#   "fields": [
#     {{
#       "id": "string (snake_case)",
#       "label": "string",
#       "type": "text|number|boolean|select|date|textarea|tel",
#       "required": true|false,
#       "placeholder": "string (use context to provide relevant default)",
#       "options": ["array of options for select type"]
#     }}
#   ],
#   "service_category": "Short category name",
#   "service_label": "Context-aware label (e.g. 'Cuisine')",
#   "urgency_label": "Context-aware label (e.g. 'Reservation Time')",
#   "special_request_options": ["Option 1", "Option 2", "Option 3", "Option 4" ]
# }}"""

#         response = client.chat.completions.create(
#             model="gpt-4o-mini",
#             messages=[
#                 {"role": "system", "content": system_prompt},
#                 {"role": "user", "content": user_prompt}
#             ],
#             response_format={"type": "json_object"},
#             temperature=0.7,
#             max_tokens=1500
#         )
        
#         schema = json.loads(response.choices[0].message.content)
        
#         # Debug logging for dynamic labels
#         print(f"[SCHEMA DEBUG] Top-level keys: {list(schema.keys())}")
#         print(f"[SCHEMA DEBUG] Service Category: {schema.get('service_category')}")
#         print(f"[SCHEMA DEBUG] Service Label: {schema.get('service_label')}")
#         print(f"[SCHEMA DEBUG] Urgency Label: {schema.get('urgency_label')}")
        
#         # Validate schema has required fields
#         if "title" in schema and "fields" in schema:
#             print(f"[SCHEMA] ✅ Generated schema: {schema['title']} with {len(schema['fields'])} fields")
#             return schema
#         return get_fallback_schema(query)
        
#     except Exception as e:
#         print(f"[SCHEMA] ❌ Error generating schema with LLM: {e}")
#         return get_fallback_schema(query)


# TESTING DIFFERENT SCHEMA GENERATION




def generate_schema_from_query(query: str) -> dict:
    """
    Generate a form schema using ChatGPT with business data context.

    This function combines:
    1. Business information from data/business.txt
    2. User preferences from data/user_preference.json
    3. The user's query
    """

    if not OPENAI_API_KEY:
        return get_fallback_schema(query)

    try:
        import json
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)

        # Load business + user preference context
        from data_provider import get_business_info, get_user_preference
        business = get_business_info()
        pref = get_user_preference()

        data_context = f"""
BUSINESS INFO:
Name: {business.name}
Services: {', '.join(business.services)}
Price Range: {business.price_range}
Location: {business.location}
Additional Info: {business.additional_info}
Feedback: {business.feedback}
Availability: {business.availability}
Business Overview: {business.business_overview}

USER PREFERENCES (Defaults):
Preferred Service: {pref.service}
Budget: {pref.budget}
Notes: {pref.special_requests}
Preferred Location: {pref.location}

USER QUERY:
"{query}"
"""

        print(f"\n{'='*60}")
        print("[SCHEMA GENERATION] 📊 Context loaded from data folder:")
        print(f"{'='*60}")
        print(data_context)
        print(f"{'='*60}\n")

        # 🔥 REWRITTEN SYSTEM PROMPT (BUSINESS-DRIVEN, NON-GENERIC)
        system_prompt = """
You are a senior product designer and prompt engineer specializing in dynamic, context-aware UI schema generation for voice agents.

Your job is to generate a JSON form schema that feels CUSTOM-BUILT for THIS business and THIS user request.

CRITICAL MINDSET:
If the generated title, description, service_category, or service_label could reasonably be reused for a different business, the output is WRONG.

An urgency concept is OPTIONAL and must only appear if the business naturally discusses time, priority, or readiness with customers.

You must deeply analyze:
- The nature of the business (what it ACTUALLY does)
- The services it offers
- The user's intent and wording
- The user's default preferences

Before generating the schema, you MUST internally reason about:
1. What is this business fundamentally selling or providing?
2. What is the user trying to accomplish right now?
3. What information would a HUMAN receptionist for THIS business ask first?
4. What terminology would THIS industry naturally use?

STRICT RULES (NON-NEGOTIABLE):
- Titles and descriptions MUST reference the actual business domain (not generic booking/contact language)
- service_category MUST be a domain-specific category (e.g. "Dental Care", "Home Appliance Repair", "Fine Dining Reservation")
- service_label MUST reflect the real-world choice users make in THIS business (e.g. "Treatment Type", "Repair Issue", "Cuisine Preference")
- urgency_label MUST be expressed ONLY through business-native time or priority concepts.
- You MUST NOT create a field literally labeled "Urgency" or conceptually equivalent generic urgency.
- If timing matters, encode it using how THIS business naturally talks about time, priority, or readiness
  (e.g. "Appointment Window", "Preferred Visit Time", "Reservation Time", "Issue Severity", "Service Priority").
- If the business does NOT typically ask about urgency, DO NOT add an urgency-related field at all.

- must NOT use generic labels like:
  - Service Type
  - Urgency
  - Request Details
  - Booking Info
- Field labels, placeholders, and options MUST reuse language from:
  - business.services
  - business_overview
  - user query
  - user preferences
- Max 8 fields total
- Always include:
  - provider_name (text)
  - budget (number)
  - preferred_date (date)
  - notes (textarea)

FAIL CONDITIONS:
- Generic titles
- Reused schema structure across businesses
- Labels that do not sound industry-specific
- Options unrelated to the business or user intent

OUTPUT:
Return ONLY valid JSON. No explanations. No markdown.
"""

        # User prompt with full context
        user_prompt = f"""
CONTEXT:
{data_context}

TASK:
Generate a highly specific, business-context-aware JSON form schema for a voice agent UI.

# SPECIAL INSTRUCTIONS:
# - The "special_request_options" MUST be realistic requests a customer would make to THIS business
# - Use the business language, not platform language
# - Infer intelligently if the user query is vague, but stay grounded in business reality

SPECIAL INSTRUCTIONS FOR "special_request_options":

You must generate "special_request_options" using the following logic:

1. These options are NOT limited to services explicitly listed in the business data.
2. They represent COMMON, REALISTIC, and INDUSTRY-TYPICAL requests that customers frequently ask for when dealing with THIS TYPE of business.
3. Think in terms of:
   - Preferences
   - Add-ons
   - Constraints
   - Quality expectations
   - Customizations
4. The options MUST:
   - Be something a real customer would naturally request
   - Be compatible with the business offerings
   - Use industry language, not platform or system language
5. The options MUST NOT:
   - Introduce entirely new services unrelated to the business
   - Sound technical, internal, or operational
   - Be generic across industries

Mental test before including an option:
"If I were calling this business in real life, would I reasonably ask for this?"

Generate 4 concise, customer-facing options.

JSON STRUCTURE (STRICT):
{{
  "title": "string",
  "description": "string",
  "fields": [
    {{
      "id": "string (snake_case)",
      "label": "string",
      "type": "text|number|boolean|select|date|textarea|tel",
      "required": true|false,
      "placeholder": "string",
      "options": ["string"]
    }}
  ],
  "service_category": "string",
  "service_label": "string",
  "special_request_options": ["string", "string", "string", "string"]
}}
"""

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.75,
            max_tokens=1500
        )

        schema = json.loads(response.choices[0].message.content)

        print(f"[SCHEMA DEBUG] Keys: {list(schema.keys())}")
        print(f"[SCHEMA DEBUG] Service Category: {schema.get('service_category')}")
        print(f"[SCHEMA DEBUG] Service Label: {schema.get('service_label')}")
        print(f"[SCHEMA DEBUG] Urgency Label: {schema.get('urgency_label')}")

        if "title" in schema and "fields" in schema:
            print(f"[SCHEMA] ✅ Generated schema: {schema['title']} ({len(schema['fields'])} fields)")
            return schema

        return get_fallback_schema(query)

    except Exception as e:
        print(f"[SCHEMA] ❌ Error generating schema with LLM: {e}")
        return get_fallback_schema(query)



@app.get("/")
async def root():
    """Serve the unified frontend page at root."""
    return FileResponse("static/index.html")

@app.get("/app")
async def app_page():
    """Serve the same unified frontend (for backward compatibility with redirects)."""
    return FileResponse("static/index.html")

@app.get("/api/schema")
async def get_schema():
    """Get the current generated schema for dynamic form."""
    global current_schema
    
    with schema_lock:
        if current_schema is None:
            # Return default fallback schema agent instead of waiting
            schema = get_fallback_schema()
        else:
            schema = current_schema
        
        # Add prefill values from user_preference.json
        try:
            from data_provider import get_user_preference
            pref = get_user_preference()
            from data_provider import get_business_info
            business = get_business_info()
            
            # Add top-level fields for easy access in frontend
            schema["business_name"] = business.name
            schema["business_price_range"] = business.price_range
            schema["user_budget"] = pref.budget
            
            # Ensure provider_name is prefilled if it exists in fields
            # Check if provider_name is in schema fields, if not and we want to force it, we might need to add it here
            # But simpler to just prefill it if it exists
            
            if "prefillValues" not in schema:
                schema["prefillValues"] = {}
            
            prefillValues = schema["prefillValues"]
            if business.name:
                prefillValues['provider_name'] = business.name
            
            if "prefillValues" not in schema:
                schema["prefillValues"] = {}
            
            prefillValues = schema["prefillValues"] # Use the existing or newly created dict
            if pref.service:
                prefillValues['service'] = pref.service
            if pref.budget:
                prefillValues['budget'] = pref.budget
            if pref.location:
                prefillValues['location'] = pref.location
            if pref.preferred_date:
                prefillValues['preferred_date'] = pref.preferred_date
            if pref.preferred_time:
                prefillValues['preferred_time'] = pref.preferred_time
            if pref.duration:
                prefillValues['duration'] = pref.duration
            if pref.special_requests:
                prefillValues['special_requests'] = pref.special_requests
            
            # Add to schema
            schema['prefillValues'] = prefillValues
            print(f"[SCHEMA] ✅ Added prefill values: {list(prefillValues.keys())}")
        except Exception as e:
            print(f"[SCHEMA] ⚠️ Could not load prefill values: {e}")
        
        return schema

@app.post("/api/schema")
async def set_schema(request: Request):
    """Set a new schema (can be called by terminal or API)."""
    global current_schema
    
    try:
        body = await request.json()
        query = body.get("query", "")
        
        if query:
            schema = generate_schema_from_query(query)
        else:
            schema = body.get("schema", get_fallback_schema())
        
        with schema_lock:
            current_schema = schema
        
        return {"status": "ok", "schema": schema}
    except Exception as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})

# ---------- Data Provider API Endpoints ----------
# These endpoints allow external systems (like chatbots) to interact with the data

@app.get("/api/data/context")
async def get_data_context():
    """Get the current data context (business info + user preferences)."""
    try:
        from data_provider import get_business_info, get_user_preference
        business = get_business_info()
        pref = get_user_preference()
        
        return {
            "status": "ok",
            "business": business.to_dict(),
            "user_preference": pref.to_dict()
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.post("/api/data/refresh")
async def refresh_data():
    """Refresh data from files."""
    try:
        from data_provider import get_business_info, get_user_preference
        # Force reload both
        business = get_business_info(force_reload=True)
        pref = get_user_preference(force_reload=True)
        
        return {
            "status": "ok",
            "message": "Data refreshed successfully",
            "business_name": business.name,
            "user_service": pref.service
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

# Note: update_data endpoint removed as it's not supported in simplified mode


@app.post("/api/submit")
async def submit_dynamic_form(request: Request):
    """Handle dynamic form submission and start the call."""
    try:
        body = await request.json()
        form_data = body.get("data", {})
        
        print(f"\n{'='*60}")
        print(f"[DYNAMIC FORM] 📬 FORM SUBMISSION RECEIVED")
        print(f"{'='*60}")
        for key, value in form_data.items():
            print(f"   {key}: {value}")
        print(f"{'='*60}\n")
        
        # Map form data to CallPreferences format
        # INTELLIGENT SERVICE EXTRACTION: Use OpenAI + business context to get specific service name
        raw_service = form_data.get("service") or form_data.get("requirement") or ""
        
        # Get business and user preference for intelligent extraction
        try:
            from data_provider import get_business_info, get_user_preference
            business_info = get_business_info()
            user_pref = get_user_preference()
        except Exception as e:
            print(f"[FORM] ⚠️ Could not load business/user data: {e}")
            business_info = None
            user_pref = None
        
        # Extract service intelligently using OpenAI + context
        if raw_service and raw_service not in ["", "Not specified"]:
            # User provided some input - use intelligent extraction
            from prompt_builder import extract_service_intelligently
            service_or_requirement = extract_service_intelligently(
                user_input=raw_service,
                business_info=business_info,
                user_pref=user_pref
            )
        elif user_pref and hasattr(user_pref, 'service') and user_pref.service:
            # No user input, use preference
            service_or_requirement = user_pref.service
            print(f"[FORM] Using user preference service: {service_or_requirement}")
        elif business_info and business_info.services:
            # No input or preference, use first business service
            services = [s.strip() for s in business_info.services.split(",")]
            service_or_requirement = services[0] if services else "Not specified"
            print(f"[FORM] Using business default service: {service_or_requirement}")
        else:
            service_or_requirement = "your service"
        
        # Handle special_requests - can be list or string
        special_requests_raw = form_data.get("special_requests", [])
        if isinstance(special_requests_raw, str):
            # If it's a string, convert to list (split by comma if multiple)
            special_requests = [s.strip() for s in special_requests_raw.split(",")] if special_requests_raw else []
        elif isinstance(special_requests_raw, list):
            special_requests = special_requests_raw
        else:
            special_requests = []
        
        # Capture all other unknown fields as custom_data
        known_fields = [
            "owner_phone", "requirement", "service", "budget", "preferred_date", 
            "preferred_time", "notes", "user_phone", "user_name", "business_name", 
            "call_type", "urgent", "special_requests",
            "service_type", "urgency", "service_address", "location"
        ]
        
        custom_data = {}
        for key, value in form_data.items():
            if key not in known_fields and value:
                custom_data[key] = value
                print(f"[FORM] ➕ Captured custom field: {key}={value}")

        prefs = CallPreferences(
            business_owner_phone=form_data.get("owner_phone", ""),
            requirement=form_data.get("notes", ""),
            budget=float(form_data.get("budget")) if form_data.get("budget") else None,
            preferred_date=form_data.get("preferred_date", "Flexible"),
            preferred_call_time=form_data.get("preferred_time", "Any"),
            service=service_or_requirement,
            user_phone=form_data.get("user_phone", ""),
            business_name=form_data.get("business_name", "Business owner"),
            special_requests=special_requests,
            call_type=form_data.get("call_type", "info"),
            service_type=form_data.get("service_type"),
            urgency=form_data.get("urgency"),
            service_address=form_data.get("service_address"),
            location=form_data.get("location"),
            custom_data=custom_data  # Pass captured custom fields
        )

        
        # Validate required field
        if not prefs.business_owner_phone:
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "Business owner phone number is required"}
            )
        
        # Start the call using existing logic
        result = await start_call(prefs)
        
        if result.get("call_id"):
            return {
                "status": "success",
                "message": "Call started successfully!",
                "call_id": result["call_id"],
                "bolna_call_id": result.get("bolna_call_id")
            }
        else:
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": result.get("message", "Failed to start call")}
            )
            
    except Exception as e:
        print(f"[DYNAMIC FORM] ❌ Error processing submission: {e}")
        import traceback
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": str(e)}
        )


@app.post("/start-call")
async def start_call(prefs: CallPreferences):
    """
    Called when user clicks 'Start Call on my behalf'.
    
    Flow:
    1. Build dynamic system prompt from business data + form data
    2. Update agent's system prompt via Bolna PATCH API
    3. Initiate the call
    """

    # 1. Check config first
    if not (BOLNA_API_KEY and BOLNA_ASSISTANT_ID and BOLNA_PHONE_NUMBER_ID):
        print("Bolna.ai config missing:",
              "API_KEY:", bool(BOLNA_API_KEY),
              "ASSISTANT_ID:", bool(BOLNA_ASSISTANT_ID),
              "PHONE_NUMBER_ID:", bool(BOLNA_PHONE_NUMBER_ID))
        return {
            "message": "Bolna.ai configuration missing. Check BOLNA_API_KEY / BOLNA_ASSISTANT_ID / BOLNA_PHONE_NUMBER_ID.",
            "call_id": None,
            "bolna_call_id": None,
        }

    # 2. Create our internal call ID for WebSocket routing
    app_call_id = str(uuid.uuid4())
    
    # Store user phone number and preferences for sharing with business owner if needed
    if prefs.user_phone:
        user_phone_numbers[app_call_id] = prefs.user_phone
    
    # Store preferences as dict for later use
    call_preferences[app_call_id] = {
        "budget": prefs.budget,
        "user_phone": prefs.user_phone,
        "business_owner_phone": prefs.business_owner_phone,
        "requirement": prefs.requirement,
        "preferred_date": prefs.preferred_date,
        "notes": prefs.notes,
        "special_requests": prefs.special_requests,
        "service_type": prefs.service_type,
        "urgency": prefs.urgency,
        "service_address": prefs.service_address,
        "location": prefs.location,
        "business_name": prefs.business_name,
        "call_type": prefs.call_type,
        "business_id": prefs.business_id,
        "user_name": prefs.user_name,
        "service": prefs.service,
        "preferred_call_time": prefs.preferred_call_time,
        "custom_data": prefs.custom_data
    }

    # 3. Build dynamic system prompt from business data + form data
    form_data = {
        "budget": prefs.budget,
        "user_phone": prefs.user_phone,
        "requirement": prefs.requirement,
        "preferred_date": prefs.preferred_date,
        "notes": prefs.notes,
        "special_requests": prefs.special_requests,
        "service_type": prefs.service_type,
        "urgency": prefs.urgency,
        "service_address": prefs.service_address,
        "location": prefs.location,
        "business_name": prefs.business_name,
        "call_type": prefs.call_type,
        "service": prefs.service,
        "preferred_call_time": prefs.preferred_call_time,
        "custom_data": prefs.custom_data
    }

    # Merge custom data if present
    if prefs.custom_data:
        form_data.update(prefs.custom_data)

    
    # Get call type from preferences (default to info)
    selected_call_type = getattr(prefs, "call_type", "info")
    
    business_info = get_business_info()
    dynamic_prompt = build_dynamic_prompt(form_data, business_info, selected_call_type)
    print(f"\n{'='*60}")
    print("[DYNAMIC PROMPT] Generated system prompt for this call:")
    print(f"{'='*60}")
    print(dynamic_prompt[:500] + "..." if len(dynamic_prompt) > 500 else dynamic_prompt)
    print(f"{'='*60}\n")

    # 4. Update agent's system prompt via Bolna PATCH API
    # Reference: https://www.bolna.ai/docs/api-reference/agent/v2/patch_update
    try:
        # Generate dynamic welcome message using business context
        # Generate dynamic welcome message using business context
        business_info = get_business_info()
        welcome_service = prefs.service or "your services"
        welcome_msg = generate_welcome_message(welcome_service, business_info.name)
        
        bolna_client = get_bolna_client()
        prompt_updated = await bolna_client.update_agent_prompt(
            agent_id=BOLNA_ASSISTANT_ID,
            system_prompt=dynamic_prompt,
            welcome_message=welcome_msg
        )
        if prompt_updated:
            print("[START-CALL] ✅ Agent prompt updated before call")
        else:
            print("[START-CALL] ⚠️ Failed to update agent prompt, proceeding with existing prompt")
    except Exception as e:
        print(f"[START-CALL] ⚠️ Error updating prompt: {e}, proceeding with existing prompt")

    # 5. Build payload for Bolna.ai call initiation
    payload = {
        "agent_id": BOLNA_ASSISTANT_ID,
        "phone_number_id": BOLNA_PHONE_NUMBER_ID,
        "recipient_phone_number": prefs.business_owner_phone,
    }
    
    # Add user_data for context (referenced in agent prompts via {variable_name})
    user_data = {
        "requirement": prefs.requirement or "Not specified",
        "budget": str(prefs.budget) if prefs.budget else "Not specified",
        "preferred_date": prefs.preferred_date or "Flexible",
        "service": prefs.service or "None", 
        "special_requests": ", ".join(prefs.special_requests) if prefs.special_requests else "None",
        "service_type": prefs.service_type or "Not specified",
        "urgency": prefs.urgency or "Not specified",
        "service_address": prefs.service_address or "Not specified",
        "user_name": prefs.user_name or "User",
        "user_phone": prefs.user_phone or "",
        "business_name": prefs.business_name or "Business owner"
    }
    payload["user_data"] = user_data
    
    # Add metadata for webhook mapping
    payload["metadata"] = {
        "appCallId": app_call_id  # CRITICAL: For webhook mapping
    }
    if prefs.business_id:
        payload["metadata"]["businessId"] = prefs.business_id
    if prefs.business_name:
        payload["metadata"]["businessName"] = prefs.business_name

    headers = {
        "Authorization": f"Bearer {BOLNA_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(base_url=BOLNA_BASE_URL, timeout=30) as client:
            print(f"[BOLNA API] Sending request to {BOLNA_BASE_URL}/call")
            print(f"[BOLNA API] Payload: {json.dumps(payload, indent=2)}")
            resp = await client.post("/call", json=payload, headers=headers)
            print(f"[BOLNA API] Response status: {resp.status_code}")
            print(f"[BOLNA API] Response body: {resp.text}")

            if resp.status_code >= 400:
                return {
                    "message": f"Bolna.ai error: {resp.text}",
                    "call_id": None,
                    "bolna_call_id": None,
                }

            data = resp.json()
    except Exception as e:
        print("Error while calling Bolna.ai:", repr(e))
        return {
            "message": "Error calling Bolna.ai. Check server logs for details.",
            "call_id": None,
            "bolna_call_id": None,
        }
    # 4. Map Bolna.ai call ID -> our app_call_id (and save control url if present)
    bolna_call_id = data.get("id")
    print(f"[BOLNA API] 🔍 Extracted bolna_call_id from response: {bolna_call_id}")
    print(f"[BOLNA API] 🔍 Response data keys: {list(data.keys())}")
    control_url = None
    try:
        # Try multiple possible locations for control_url
        control_url = (
            data.get("monitor", {}).get("controlUrl") or
            data.get("monitor", {}).get("control_url") or
            data.get("control_url") or
            data.get("controlUrl") or
            None
        )
        if control_url:
            print(f"[BOLNA API] ✅ Found control_url: {control_url}")
        else:
            print(f"[BOLNA API] ⚠️ No control_url found in response. Available keys: {list(data.keys())}")
            if "monitor" in data:
                print(f"[BOLNA API] Monitor object keys: {list(data.get('monitor', {}).keys())}")
    except Exception as e:
        print(f"[BOLNA API] ⚠️ Error extracting control_url: {e}")
        control_url = None

    if bolna_call_id:
        # pass control_url optionally so manager can call it later (stop-call)
        manager.link_bolna_call(app_call_id, bolna_call_id, control_url)
        print(f"[BOLNA API] ✅ Created mapping: bolna_call_id={bolna_call_id} -> app_call_id={app_call_id}")
        print(f"[BOLNA API] Control URL stored: {control_url is not None}")
        print(f"[BOLNA API] Current mappings: {list(manager.bolna_to_app_call.keys())}")
        
        # Polling logic removed as per user request (relying on webhooks only)

    else:
        print(f"[BOLNA API] ⚠️ WARNING: No bolna_call_id in response! Response data: {data}")
        print(f"[BOLNA API] ⚠️ This means polling will NOT start - transcripts will only come via webhook at call end")

    # Don't send initial message immediately - wait for WebSocket to connect
    # The frontend will connect WebSocket after receiving the call_id
    # We'll send the initial message after a small delay to ensure WebSocket is connected
    async def send_initial_message():
        await asyncio.sleep(0.5)  # Small delay to allow WebSocket to connect
    await manager.send_to_app_call(
        app_call_id,
            {"speaker": "SYSTEM", "text": "Dialing business owner via Bolna.ai…"}
    )
    
    # Send initial message in background (non-blocking)
    asyncio.create_task(send_initial_message())

    return {
        "message": "Call started via Bolna.ai",
        "call_id": app_call_id,
        "bolna_call_id": bolna_call_id,
    }

@app.post("/stop-call")
async def stop_call(request: Request):
    body = await request.json()
    app_call_id = body.get("call_id")
    if not app_call_id:
        print(f"[STOP CALL] ❌ Missing call_id in request")
        return {"status": "error", "message": "missing call_id"}, 400

    print(f"[STOP CALL] 🛑 Received stop call request for app_call_id: {app_call_id}")
    headers = {"Authorization": f"Bearer {BOLNA_API_KEY}", "Content-Type": "application/json"}
    
    # Try to get control_url first
    control_url = manager.get_control_url_for_app_call(app_call_id)
    bolna_call_id = manager.get_bolna_call_id_from_app_call(app_call_id)
    
    print(f"[STOP CALL] Control URL available: {control_url is not None}")
    print(f"[STOP CALL] Bolna call ID available: {bolna_call_id is not None}")
    if bolna_call_id:
        print(f"[STOP CALL] Bolna call ID: {bolna_call_id}")
    
    success = False
    error_message = None
    
    # Method 1: Try using control_url if available
    if control_url:
        try:
            print(f"[STOP CALL] Attempting to end call via control_url for {app_call_id}")
            async with httpx.AsyncClient(timeout=10) as client:
                # Most control endpoints accept action { "action": "hangup" }
                resp = await client.post(control_url, json={"action": "hangup"}, headers=headers)
                if resp.status_code < 400:
                    success = True
                    print(f"[STOP CALL] ✅ Successfully ended call via control_url")
                else:
                    error_message = f"control API error: {resp.status_code} - {resp.text}"
                    print(f"[STOP CALL] ❌ Control URL failed: {error_message}")
        except Exception as e:
            error_message = f"control URL error: {repr(e)}"
            print(f"[STOP CALL] ❌ Exception using control_url: {error_message}")
    
    # Method 2: If control_url failed or not available, try using execution ID
    if not success and bolna_call_id:
        try:
            print(f"[STOP CALL] Attempting to end call via execution ID {bolna_call_id} for {app_call_id}")
            endpoint = f"/executions/{bolna_call_id}"
            async with httpx.AsyncClient(base_url=BOLNA_BASE_URL, timeout=10) as client:
                # Try POST /executions/{execution_id}/hangup first (most common endpoint)
                try:
                    resp = await client.post(f"{endpoint}/hangup", json={}, headers=headers)
                    if resp.status_code < 400:
                        success = True
                        print(f"[STOP CALL] ✅ Successfully ended call via /hangup endpoint")
                    else:
                        print(f"[STOP CALL] ⚠️ /hangup returned {resp.status_code}: {resp.text[:200]}")
                except Exception as e:
                    print(f"[STOP CALL] ⚠️ /hangup endpoint failed: {e}")
                
                # If /hangup doesn't work, try PATCH with status update
                if not success:
                    try:
                        resp = await client.patch(endpoint, json={"status": "canceled"}, headers=headers)
                        if resp.status_code < 400:
                            success = True
                            print(f"[STOP CALL] ✅ Successfully ended call via PATCH status")
                        else:
                            print(f"[STOP CALL] ⚠️ PATCH returned {resp.status_code}: {resp.text[:200]}")
                    except Exception as e:
                        print(f"[STOP CALL] ⚠️ PATCH endpoint failed: {e}")
                
                # If still not successful, try DELETE
                if not success:
                    try:
                        resp = await client.delete(endpoint, headers=headers)
                        if resp.status_code < 400:
                            success = True
                            print(f"[STOP CALL] ✅ Successfully ended call via DELETE")
                        else:
                            if not error_message:
                                error_message = f"All execution ID methods failed. Last: DELETE {resp.status_code} - {resp.text[:200]}"
                            print(f"[STOP CALL] ❌ DELETE returned {resp.status_code}: {resp.text[:200]}")
                    except Exception as e:
                        if not error_message:
                            error_message = f"All execution ID methods failed: {repr(e)}"
                        print(f"[STOP CALL] ❌ DELETE endpoint failed: {e}")
        except Exception as e:
            if not error_message:
                error_message = f"execution ID error: {repr(e)}"
            print(f"[STOP CALL] ❌ Exception using execution ID: {error_message}")
    
    # If all methods failed, try one more approach: POST to /call endpoint with hangup action
    if not success and bolna_call_id:
        try:
            print(f"[STOP CALL] Attempting to end call via /call endpoint with execution ID {bolna_call_id}")
            async with httpx.AsyncClient(base_url=BOLNA_BASE_URL, timeout=10) as client:
                # Try POST /call with action to hangup
                resp = await client.post("/call", json={"execution_id": bolna_call_id, "action": "hangup"}, headers=headers)
                if resp.status_code < 400:
                    success = True
                    print(f"[STOP CALL] ✅ Successfully ended call via /call endpoint")
                else:
                    print(f"[STOP CALL] ⚠️ /call endpoint returned {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            print(f"[STOP CALL] ⚠️ /call endpoint failed: {e}")
    
    if success:
        # inform UI
        await manager.send_to_app_call(app_call_id, {"speaker": "SYSTEM", "text": "Call hangup requested."})
        print(f"[STOP CALL] ✅ Successfully ended call for {app_call_id}")
        return {"status": "ok", "message": "Call hangup requested successfully"}
    else:
        # Even if we couldn't end it via API, inform the user
        error_msg = error_message or "Could not find control URL or execution ID. Call may already be ended."
        print(f"[STOP CALL] ❌ Failed to end call: {error_msg}")
        await manager.send_to_app_call(app_call_id, {
            "speaker": "SYSTEM", 
            "text": f"Attempted to end call. {error_msg}"
        })
        return {
            "status": "warning", 
            "message": error_msg
        }

# ---------- WebSocket endpoint ----------

@app.websocket("/ws/call/{call_id}")
async def call_websocket(websocket: WebSocket, call_id: str):
    await manager.connect(call_id, websocket)
    print(f"[WEBSOCKET] ✅ Connected for call_id: {call_id}")
    try:
        # Keep connection alive by receiving messages
        while True:
            try:
                # Wait for any message to keep connection alive
                # Client sends ping messages periodically
                data = await websocket.receive_text()
                # Handle ping messages
                try:
                    msg = json.loads(data)
                    if msg.get("type") == "ping":
                        # Respond to ping to keep connection alive
                        await websocket.send_json({"type": "pong"})
                        continue
                except:
                    pass
                # Log other messages
                print(f"[WEBSOCKET] Received from client {call_id}: {data}")
            except Exception as e:
                print(f"[WEBSOCKET] Error in receive loop for {call_id}: {e}")
                break
    except WebSocketDisconnect:
        print(f"[WEBSOCKET] ❌ Disconnected: {call_id}")
        manager.disconnect(call_id)
    except Exception as e:
        print(f"[WEBSOCKET] ❌ Error: {call_id}, {e}")
        manager.disconnect(call_id)



# last message sent per app_call_id
last_sent_text: Dict[str, str] = {}
# last full role-specific text if you want separate tracking (optional)
last_role_text: Dict[str, str] = {}

def normalize_text(t: str) -> str:
    # simple normalization to make dedupe resilient
    if t is None:
        return ""
    return " ".join(t.strip().split())

def is_system_prompt_text(text: str) -> bool:
    """
    Heuristic to detect and filter out the long system prompt text.
    """
    if not text:
        return True
    keywords = ["You are an AI caller", "Objective", "User Preferences", "Conversation Flow"]
    # if this looks like a template/system prompt, treat as system prompt
    if len(text) > 400 and any(k in text for k in keywords):
        return True
    # also filter obvious handlebars/templates
    if "{{" in text and "}}" in text and len(text) > 50:
        return True
    return False

async def store_and_forward_filtered(app_call_id: str, speaker: str, text: str, entry_type: str = "transcript"):
    """
    Store message and forward to UI, but:
      - drop system prompt clones
      - dedupe exact duplicates
      - avoid re-sending long prefixes repeatedly when Bolna.ai streams incremental updates
    """
    if not text:
        return

    text = normalize_text(text)

    # filter system prompt clones (but allow explicit SYSTEM messages through)
    if is_system_prompt_text(text) and speaker != "SYSTEM":
        print(f"[bolna_server] filtered system prompt for {app_call_id} (len={len(text)})")
        return

    # dedupe exact equal messages
    last = last_sent_text.get(app_call_id)
    if last and last == text:
        # exact duplicate - skip
        print(f"[bolna_server] dedupe exact for {app_call_id}: {text[:80]!r}")
        return

    # handle progressive streaming: if new text is basically "old + small suffix", only send suffix or defer
    if last:
        # If text is strictly longer and starts with last (Bolna.ai appended more), send only the new suffix
        if text.startswith(last):
            suffix = text[len(last):].strip()
            # If suffix is too small/garbled (like punctuation or a word fragment), skip to avoid UI spam
            if len(suffix) < 8:
                print(f"[bolna_server] skipped small streaming delta for {app_call_id}: {suffix!r}")
                # but still update last_sent_text to the longer text to avoid further small deltas
                last_sent_text[app_call_id] = text
                return
            # Otherwise send the suffix as one message from same speaker (keeps UI readable)
            send_text = suffix
            # optionally you can send the full text instead of suffix if you want full utterance always:
            # send_text = text
        else:
            # If not a prefix relation, it's a different sentence -> send full text
            send_text = text
    else:
        send_text = text

    # store full text in conversation_store (we store the full text, not just suffix)
    entry = {"speaker": speaker, "text": text, "ts": datetime.utcnow().isoformat() + "Z", "type": entry_type}
    conversation_store.setdefault(app_call_id, []).append(entry)

    # update last_sent_text to the full text (not the suffix) to handle future streaming deltas
    last_sent_text[app_call_id] = text

    # forward what we decided to send (suffix or full)
    await manager.send_to_app_call(app_call_id, {"speaker": speaker, "text": send_text})







# =============================================================================
# Shared helper for report_call_info processing
# =============================================================================
async def _handle_report_call_info(body: dict) -> dict:
    """
    Shared logic for processing report_call_info function calls.
    Called by both /report-call-info endpoint and /bolna/server webhook handler.
    """
    print(f"\n{'='*80}")
    print(f"[REPORT CALL INFO] 📊 PROCESSING REAL-TIME INFO FROM AI AGENT")
    print(f"[REPORT CALL INFO] Full body: {json.dumps(body, indent=2)}")
    print(f"{'='*80}")
    
    # Extract call_id (Bolna execution ID)
    execution_id = (
        body.get("call_id") or
        body.get("execution_id") or
        body.get("callId") or
        ""
    )
    
    # Map execution_id to app_call_id
    app_call_id = manager.bolna_to_app_call.get(execution_id)
    
    if not app_call_id:
        # Try to find by active connections
        if len(manager.active_connections) == 1:
            app_call_id = list(manager.active_connections.keys())[0]
            print(f"[REPORT CALL INFO] 🔄 FALLBACK: Using single active WebSocket: {app_call_id}")
        else:
            print(f"[REPORT CALL INFO] ❌ Cannot map execution_id {execution_id} to app_call_id")
            return {"status": "error", "message": "Cannot find active call"}
    
    # Extract info details
    info_type = body.get("info_type", "status").lower()
    value = body.get("value", "")
    status = body.get("status", "checking").lower()
    message = body.get("message", "")
    
    # Map info_type to emoji and label
    type_config = {
        "price": {"emoji": "💰", "label": "Price"},
        "date": {"emoji": "📅", "label": "Date"},
        "availability": {"emoji": "✅", "label": "Availability"},
        "requirement": {"emoji": "📋", "label": "Requirement"},
        "status": {"emoji": "ℹ️", "label": "Status"},
        "negotiation": {"emoji": "🤝", "label": "Negotiation"}
    }
    
    config = type_config.get(info_type, {"emoji": "ℹ️", "label": info_type.title()})
    label = config["label"]
    
    # Map status to visual indicator
    status_emoji = {
        "confirmed": "✅",
        "not_available": "❌",
        "checking": "🔍",
        "negotiating": "🤝",
        "waiting": "⏳"
    }.get(status, "ℹ️")
    
    # Build the text message to display in chat
    if message:
        display_text = f"{status_emoji} {label}: {value} - {message}"
    else:
        display_text = f"{status_emoji} {label}: {value}"
    
    # Send as chat text message (conversation_update)
    chat_message = {
        "type": "conversation_update",
        "speaker": "SYSTEM",
        "text": display_text,
        "is_info_update": True,
        "info_type": info_type,
        "info_status": status
    }
    
    try:
        await manager.send_to_app_call(app_call_id, chat_message)
        print(f"[REPORT CALL INFO] ✅ Sent to frontend: {display_text}")
        
        return {
            "status": "ok",
            "message": f"Info displayed: {display_text}",
            "success": True
        }
    except Exception as e:
        print(f"[REPORT CALL INFO] ❌ Error sending to WebSocket: {e}")
        return {"status": "error", "message": str(e)}


@app.post("/report-call-info")
async def report_call_info(request: Request):
    """
    Receive real-time call info from Bolna.ai AI agent.
    
    This is called by the AI agent using the 'report_call_info' custom function.
    It displays information as TEXT messages in the chat.
    """
    try:
        body = await request.json()
    except Exception as e:
        print(f"[REPORT CALL INFO] ❌ ERROR: Failed to parse JSON body: {e}")
        return {"status": "error", "message": "Invalid JSON"}, 400
    
    return await _handle_report_call_info(body)



@app.post("/bolna/server")
async def bolna_server(request: Request):
    """Handle webhook requests from Bolna.ai AND direct function calls"""
    try:
        body = await request.json()
    except Exception as e:
        print(f"[BOLNA WEBHOOK] ❌ ERROR: Failed to parse JSON body: {e}")
        print(f"[BOLNA WEBHOOK] Raw request body: {await request.body()}")
        return {"status": "error", "message": "Invalid JSON"}, 400
    
    # Log ALL incoming requests for debugging
    print(f"\n{'='*80}")
    print(f"[BOLNA WEBHOOK] 📥 INCOMING REQUEST")
    print(f"[BOLNA WEBHOOK] Full body: {json.dumps(body, indent=2)}")
    print(f"{'='*80}")
    
    # --- CHECK FOR DIRECT FUNCTION CALL FIRST ---
    # When Bolna.ai calls a custom function, it sends the function parameters directly in the body
    # Check if this looks like a direct function call (has function parameters, not webhook structure)
    is_direct_function_call = (
        body.get("approval_type") is not None or
        body.get("original_value") is not None or
        body.get("negotiated_value") is not None or
        body.get("description") is not None
    ) and (
        body.get("id") is None and  # Not a webhook (webhooks have "id")
        body.get("type") is None and  # Not a webhook event
        body.get("status") is None  # Not a status webhook
    )
    
    if is_direct_function_call:
        print(f"[FUNCTION CALL] 🎯 DIRECT FUNCTION CALL DETECTED!")
        print(f"[FUNCTION CALL] This is a direct function call from Bolna.ai, not a webhook")
        
        # Extract function parameters
        approval_type = body.get("approval_type", "price_negotiation")
        description = body.get("description", "Approval needed")
        original_value = str(body.get("original_value", "")).strip()
        negotiated_value = str(body.get("negotiated_value", "")).strip()
        
        print(f"[FUNCTION CALL] approval_type: {approval_type}")
        print(f"[FUNCTION CALL] description: {description}")
        print(f"[FUNCTION CALL] original_value: {original_value}")
        print(f"[FUNCTION CALL] negotiated_value: {negotiated_value}")
        
        # Try to find app_call_id from various sources
        # Bolna.ai might include execution_id or call_id in the request
        execution_id = (
            body.get("execution_id") or
            body.get("call_id") or
            body.get("executionId") or
            body.get("callId") or
            body.get("id") or
            ""
        )
        
        app_call_id = None
        if execution_id:
            app_call_id = manager.get_app_call_id_from_bolna(execution_id)
            print(f"[FUNCTION CALL] Found app_call_id from execution_id: {app_call_id}")
        
        # Fallback: try to get from active WebSocket connections
        if not app_call_id:
            active_connections = list(manager.active_connections.keys())
            if len(active_connections) == 1:
                app_call_id = active_connections[0]
                print(f"[FUNCTION CALL] Using single active WebSocket: {app_call_id}")
            elif len(active_connections) > 1:
                # Use most recently connected
                most_recent = max(active_connections, key=lambda x: manager.last_connected_time.get(x, datetime.min))
                app_call_id = most_recent
                print(f"[FUNCTION CALL] Using most recent WebSocket: {app_call_id}")
        
        if not app_call_id:
            print(f"[FUNCTION CALL] ⚠️ WARNING: No app_call_id found for function call")
            print(f"[FUNCTION CALL] Available connections: {list(manager.active_connections.keys())}")
            # Still process it, but we can't send to frontend
            return {
                "status": "ok",
                "message": "Function call received but no active call found"
            }
        
        # Process the approval request (same logic as webhook handler)
        print(f"[APPROVAL] Processing approval request for app_call_id: {app_call_id}")
        
        # Get call preferences for user budget
        prefs = call_preferences.get(app_call_id, {})
        user_budget = prefs.get("budget", "0")
        requirement = prefs.get("requirement", "service")
        
        # Create approval data
        approval_id = str(uuid.uuid4())
        approval_data = {
            "approval_id": approval_id,
            "call_id": app_call_id,
            "approval_type": approval_type,
            "description": description,
            "original_value": original_value,
            "negotiated_value": negotiated_value,
            "user_budget": str(user_budget),
            "requirement": requirement,
            "timestamp": datetime.utcnow().isoformat(),
            "status": "pending",
            "expires_at": (datetime.utcnow() + timedelta(seconds=10)).isoformat()
        }
        
        # Store approval
        active_approvals[app_call_id] = approval_data
        
        # Send to frontend
        await manager.send_to_app_call(app_call_id, {
            "type": "approval_request",
            "approval": approval_data
        })
        
        print(f"[APPROVAL] ✅ Sent approval request to frontend for app_call_id: {app_call_id}")
        
        # Initialize approval status tracking (will be updated when user responds)
        call_approval_status[app_call_id] = {
            "status": "waiting",
            "negotiated_value": negotiated_value,
            "original_value": original_value,
            "description": description
        }
        
        # Wait for user response
        approval_result = await wait_for_approval_response(app_call_id, approval_id, timeout=10)
        
        print(f"[APPROVAL] User response: {approval_result}")
        
        # Track approval status for end-of-call summary
        call_approval_status[app_call_id] = {
            "status": approval_result,
            "negotiated_value": negotiated_value,
            "original_value": original_value,
            "description": description
        }
        
        # Return response to Bolna.ai
        # Bolna.ai expects a response that the LLM can use
        if approval_result == "approved":
            # Format response for LLM to speak
            # CRITICAL: This message is spoken to the BUSINESS OWNER, not the user
            response_message = f"Great news! The user has approved the price of {negotiated_value} for {requirement}. Let's proceed with the booking. Could you please confirm the details and terms? The user's contact number is {prefs.get('user_phone', 'not provided')}."
        elif approval_result == "denied":
            response_message = "I'm sorry, but the user has decided not to proceed with this option. Thank you for your time and understanding. Have a good day."
        else:  # timeout
            response_message = "I'll confirm with the user and call you back with their decision. Thank you for your time."
        
        print(f"[APPROVAL] Returning response to Bolna.ai: {response_message}")
        
        return {
            "status": "ok",
            "result": response_message
        }
    
    
    # --- CHECK FOR report_call_info DIRECT FUNCTION CALL ---
    # This detects specific real-time info updates from the AI (prices, availability, etc.)
    # Used when AI calls the report_call_info function
    is_report_call_info = (
        body.get("info_type") is not None or
        (body.get("value") is not None and body.get("status") is not None)
    ) and (
        body.get("id") is None and  # Not a webhook (webhooks have "id")
        body.get("transcript") is None and  # Not a transcript webhook
        body.get("approval_type") is None  # Not an approval function
    )
    
    if is_report_call_info:
        # Use shared helper function to avoid code duplication
        return await _handle_report_call_info(body)

    # Try multiple ways to extract event type (Bolna.ai might use different field names)
    message_field = body.get("message", {})
    
    msg_type = (
        body.get("type") or 
        body.get("event") or 
        body.get("event_type") or
        ""
    )
    
    # Only try to extract from message if it's a dict
    if isinstance(message_field, dict):
        msg_type = msg_type or message_field.get("type") or message_field.get("event") or ""

    
    print(f"[BOLNA WEBHOOK] Detected event type: {msg_type}")
    print(f"[BOLNA WEBHOOK] Body keys: {list(body.keys())}")
    
    # Try multiple ways to extract call information
    # CRITICAL: message_field might be string, only extract from it if it's dict
    call_from_message = message_field.get("call") if isinstance(message_field, dict) else {}
    call = body.get("call") or call_from_message or {}
    bolna_call_id = (
        body.get("id") or  # Most common: top-level "id" field
        call.get("id") or 
        body.get("call_id") or 
        body.get("callId") or
        body.get("execution_id") or
        body.get("executionId") or
        ""
    )
    
    print(f"[BOLNA WEBHOOK] Bolna call ID: {bolna_call_id}")
    
    # Try to find app_call_id from mapping or metadata
    app_call_id = None
    if bolna_call_id:
        app_call_id = manager.get_app_call_id_from_bolna(bolna_call_id)
        print(f"[BOLNA WEBHOOK] Looked up app_call_id from mapping: {app_call_id}")
    
    if not app_call_id:
        # Try metadata in various locations
        metadata = (
            call.get("metadata") or 
            body.get("metadata") or 
            body.get("message", {}).get("metadata") or
            body.get("context_details", {}).get("metadata") or
            {}
        )
        app_call_id = (
            metadata.get("appCallId") or 
            metadata.get("app_call_id") or
            body.get("appCallId") or
            body.get("app_call_id") or
            ""
        )
        print(f"[BOLNA WEBHOOK] Looked up app_call_id from metadata: {app_call_id}")

    if not app_call_id:
        print(f"[BOLNA WEBHOOK] ⚠️ WARNING: No app_call_id found in mapping or metadata.")
        print(f"[BOLNA WEBHOOK] Bolna call ID was: {bolna_call_id}")
        print(f"[BOLNA WEBHOOK] Call object: {call}")
        print(f"[BOLNA WEBHOOK] Body metadata: {body.get('metadata')}")
        print(f"[BOLNA WEBHOOK] Available mappings: {list(manager.bolna_to_app_call.keys())}")
        print(f"[BOLNA WEBHOOK] Active WebSocket connections: {list(manager.active_connections.keys())}")
        
        # FALLBACK: Try to match webhook to active WebSocket connection
        # Strategy 1: If only one connection, use it
        # Strategy 2: If multiple connections, try to match by phone number or use most recent
        if len(manager.active_connections) > 0 and (body.get("transcript") or body.get("status")):
            # Try to match by phone number first
            recipient_phone = body.get("recipient_phone_number") or body.get("user_number") or ""
            user_phone_from_context = body.get("context_details", {}).get("recipient_data", {}).get("user_phone") or ""
            
            matched_call_id = None    
            if recipient_phone or user_phone_from_context:
                # Try to find matching call by checking call_preferences
                for call_id in manager.active_connections.keys():
                    prefs = call_preferences.get(call_id, {})
                    if isinstance(prefs, dict):
                        # Check if phone numbers match
                        if (prefs.get("business_owner_phone") == recipient_phone or 
                            prefs.get("user_phone") == user_phone_from_context):
                            matched_call_id = call_id
                            print(f"[BOLNA WEBHOOK] 🔄 FALLBACK: Matched by phone number: {call_id}")
                            break
            
            # If no phone match, use the most recently created connection (last in dict)
            # Note: Python 3.7+ dicts maintain insertion order
            if not matched_call_id and len(manager.active_connections) == 1:
                matched_call_id = list(manager.active_connections.keys())[0]
                print(f"[BOLNA WEBHOOK] 🔄 FALLBACK: Using single active WebSocket connection: {matched_call_id}")
            elif not matched_call_id and len(manager.active_connections) > 1:
                # Use the last (most recent) connection
                matched_call_id = list(manager.active_connections.keys())[-1]
                print(f"[BOLNA WEBHOOK] 🔄 FALLBACK: Multiple connections found, using most recent: {matched_call_id}")
                print(f"[BOLNA WEBHOOK] All active connections: {list(manager.active_connections.keys())}")
            
            if matched_call_id:
                app_call_id = matched_call_id
                # Recreate the mapping for future webhooks
                if bolna_call_id:
                    manager.link_bolna_call(app_call_id, bolna_call_id, None)
                    print(f"[BOLNA WEBHOOK] ✅ Recreated mapping: {bolna_call_id} -> {app_call_id}")
            else:
                print(f"[BOLNA WEBHOOK] This might be a test webhook or webhook for a different call")
                if body.get("transcript") and isinstance(body.get("transcript"), str):
                    print(f"[BOLNA WEBHOOK] Found transcript but no app_call_id - transcript preview: {body.get('transcript')[:100]}...")
                return {"status": "ok", "message": "No app_call_id found, might be test webhook"}
        else:
            print(f"[BOLNA WEBHOOK] This might be a test webhook or webhook for a different call")
            # Don't return error - just log it, as Bolna.ai might send test webhooks
            # But still try to process transcript if it exists (for logging/debugging)
            if body.get("transcript") and isinstance(body.get("transcript"), str):
                print(f"[BOLNA WEBHOOK] Found transcript but no app_call_id - transcript preview: {body.get('transcript')[:100]}...")
            return {"status": "ok", "message": "No app_call_id found, might be test webhook"}
    
    print(f"[BOLNA WEBHOOK] ✅ Processing for app_call_id: {app_call_id}, msg_type: {msg_type}")
    print(f"[BOLNA WEBHOOK] WebSocket connection exists: {app_call_id in manager.active_connections}")
    print(f"[BOLNA WEBHOOK] Active WebSocket connections: {list(manager.active_connections.keys())}")

    # --- 1) CONVERSATION UPDATE / TRANSCRIPT / MESSAGE ---
    # Handle multiple possible event types for conversation updates
    conversation_event_types = [
        "conversation-update",
        "conversation_update",
        "transcript",
        "transcription",
        "message",
        "utterance",
        "user_message",
        "assistant_message",
        "agent_message",
        "transcription_update",
        "transcription-update"
    ]
    
    # Check if this is a conversation/transcript/message event
    # Also check if body has transcript field (even if event type is missing)
    # CRITICAL: Process transcript even if webhook has status field (Bolna.ai sends both)
    has_transcript = bool(body.get("transcript") or body.get("transcription"))
    is_conversation_event = (
        msg_type in conversation_event_types or 
        "transcript" in str(msg_type).lower() or 
        "message" in str(msg_type).lower() or
        "utterance" in str(msg_type).lower() or
        has_transcript  # Process if transcript field exists, even without event type or status
    )
    
    if is_conversation_event:
        print(f"[CONVERSATION UPDATE] Received for {app_call_id}")
        # Use the new LiveTranscriptHandler module to process webhook transcript
        await transcript_handler.process_webhook_transcript(body, app_call_id)
        # Don't return yet - continue to check for status updates below
        # (Bolna.ai webhooks can contain both transcript and status)

    # --- 2) STATUS UPDATE ---
    # Check for status field even if event type is missing (Bolna.ai sends status in webhook body)
    status = (
        body.get("status") or
        body.get("call_status") or
        body.get("message", {}).get("status") or
        body.get("data", {}).get("status") or
        ""
    )
    
    status_event_types = ["status-update", "status_update", "status", "call_status", "call-status"]
    is_status_event = msg_type in status_event_types or bool(status)
    
    if is_status_event and status:
        print(f"[STATUS UPDATE] Status: {status} for app_call_id: {app_call_id}")
        # Handle various status values from Bolna.ai
        if status in ["ended", "completed", "finished", "call-disconnected", "disconnected"]:
            # Stop polling when call ends
            # Polling stopped (feature removed)

            # Check if summary is already available in this webhook or has been sent before
            summary_in_webhook = (
                body.get("analysis", {}).get("summary") or
                body.get("summary") or
                body.get("message", {}).get("analysis", {}).get("summary") or
                body.get("message", {}).get("summary") or
                body.get("data", {}).get("summary") or
                ""
            )
            summary_already_sent = app_call_id in conversation_summaries and conversation_summaries[app_call_id]
            
            # Only send "Waiting for summary" if we don't have a summary yet
            if not summary_in_webhook.strip() and not summary_already_sent:
             await manager.send_to_app_call(app_call_id, {
                "type": "status",
                    "speaker": "SYSTEM",
                "text": "Call ended. Waiting for summary..."
            })
            elif summary_in_webhook.strip() or summary_already_sent:
                # Summary is available or already sent, don't show "Waiting for summary"
                print(f"[STATUS UPDATE] Summary available or already sent, skipping 'Waiting for summary' message")
        elif status in ["ringing", "initiated", "connecting"]:
            await manager.send_to_app_call(app_call_id, {
                "type": "status",
                "speaker": "SYSTEM",
                "text": "Call connecting..."
            })
        elif status in ["in-progress", "active", "ongoing"]:
            await manager.send_to_app_call(app_call_id, {
                "type": "status",
                "speaker": "SYSTEM",
                "text": "Call in progress..."
            })
        # Continue processing - don't return early (transcript/summary might be in same webhook)

    # --- 3) SUMMARY ---
    # Extract summary when call ends (status is completed) OR when msg_type matches summary event types
    summary_event_types = ["end-of-call-report", "end_of_call_report", "summary", "call_summary", "call-summary"]
    is_call_ended = status in ["ended", "completed", "finished", "call-disconnected", "disconnected"]
    should_extract_summary = msg_type in summary_event_types or is_call_ended
    
    if should_extract_summary and app_call_id:
        summary = (
            body.get("analysis", {}).get("summary") or
            body.get("summary") or
            body.get("message", {}).get("analysis", {}).get("summary") or
            body.get("message", {}).get("summary") or
            body.get("data", {}).get("summary") or
            ""
        )
        
        # Only send summary if we actually found one AND haven't sent it before
        if summary and summary.strip() and summary != "No summary available.":
            # Check if we've already sent this summary (prevent duplicates)
            existing_summary = conversation_summaries.get(app_call_id)
            if existing_summary == summary:
                print(f"[SUMMARY] Summary already sent for app_call_id: {app_call_id}, skipping duplicate")
            else:
                # Store summary for later retrieval
                conversation_summaries[app_call_id] = summary
                # Only send if it's a new summary
                await manager.send_to_app_call(app_call_id, {
                    "type": "summary",
                    "speaker": "SYSTEM",
                    "text": f"Summary: {summary}"
                })
        else:
            # Store empty summary to prevent retries
            if app_call_id not in conversation_summaries:
                conversation_summaries[app_call_id] = ""
            approval = active_approvals[app_call_id]
            approval["status"] = "pending"
            approval["call_ended"] = True
            pending_approvals[app_call_id] = approval
            del active_approvals[app_call_id]
            
            # Don't send pending_approval dashboard - just track it silently
            # The user can't act on it after the call ends anyway
        
        # Store Twilio Call SID mapping for Media Streams (if available)
        telephony_data = body.get("telephony_data") or {}
        provider_call_id = telephony_data.get("provider_call_id")
        if provider_call_id:
            twilio_call_sid_to_app_call[provider_call_id] = app_call_id
            print(f"[TWILIO MEDIA STREAM] ✅ Stored mapping: {provider_call_id} -> {app_call_id}")
            
            # Optionally enable Media Streams automatically (requires Twilio SDK)
            # Uncomment if you want to auto-enable Media Streams:
            # if realtime_transcription_service:
            #     asyncio.create_task(enable_twilio_media_streams(provider_call_id, app_call_id))
        
        # Send approval status summary if there was an approval during the call
        if app_call_id in call_approval_status:
            approval_info = call_approval_status[app_call_id]
            status = approval_info.get("status", "")
            negotiated_value = approval_info.get("negotiated_value", "")
            original_value = approval_info.get("original_value", "")
            
            if status == "approved":
                status_text = f"✅ User approved the final negotiated price of {negotiated_value} (original: {original_value})"
            elif status == "denied":
                status_text = f"❌ User denied the negotiated price of {negotiated_value} (original: {original_value})"
            elif status == "timeout":
                status_text = f"⏱️ Approval request timed out. Negotiated price: {negotiated_value} (original: {original_value})"
            else:
                status_text = f"ℹ️ Approval status: {status}. Negotiated price: {negotiated_value} (original: {original_value})"
            
            # Send simple text message instead of dashboard
            await manager.send_to_app_call(app_call_id, {
                "type": "conversation_update",
                "speaker": "SYSTEM",
                "text": status_text
            })
            print(f"[APPROVAL SUMMARY] Sent approval status summary: {status_text}")
        
        return {"status": "ok"}

    # --- 4) FUNCTION CALL - Approval Request ---
    # Check multiple possible message types and formats that Bolna.ai might use
    is_function_call = (
        msg_type == "function-call" or 
        msg_type == "tool-calls" or 
        msg_type == "function-calls" or
        msg_type == "tool-call" or
        msg_type == "tool-call-start" or
        msg_type == "tool-call-complete" or
        msg_type == "tool-call-result" or
        "function" in str(msg_type).lower() or
        "tool" in str(msg_type).lower()
    )
    
    # Also check if function call data exists anywhere in the body
    message_obj = body.get("message", {})
    has_function_data = (
        message_obj.get("functionCall") or 
        message_obj.get("toolCalls") or
        message_obj.get("function") or
        message_obj.get("toolCall") or
        body.get("functionCall") or
        body.get("toolCalls") or
        body.get("function") or
        body.get("toolCall") or
        body.get("tool") or
        message_obj.get("tool")
    )
    
    print(f"[FUNCTION CHECK] is_function_call: {is_function_call}, has_function_data: {bool(has_function_data)}")
    
    if is_function_call or has_function_data:
        print(f"[FUNCTION CALL] Processing function call for app_call_id: {app_call_id}")
        # Try multiple ways to extract function calls (Bolna.ai may send in different formats)
        message_obj = body.get("message", {})
        function_calls = (
            message_obj.get("functionCall") or 
            message_obj.get("toolCalls") or
            message_obj.get("toolCall") or
            message_obj.get("function") or
            message_obj.get("tool") or
            body.get("functionCall") or
            body.get("toolCalls") or
            body.get("toolCall") or
            body.get("function") or
            body.get("tool") or
            []
        )
        
        print(f"[FUNCTION CALL] Extracted function_calls: {function_calls}")
        
        if not isinstance(function_calls, list):
            function_calls = [function_calls]
        
        print(f"[FUNCTION CALL] Processing {len(function_calls)} function call(s)")
        
        for func_call in function_calls:
            if not func_call:
                continue
            
            print(f"[FUNCTION CALL] Processing call: {func_call}")
            
            # Extract tool call ID (needed for response)
            tool_call_id = (
                func_call.get("id") or
                func_call.get("toolCallId") or
                func_call.get("callId") or
                ""
            )
            print(f"[FUNCTION CALL] Tool call ID: '{tool_call_id}'")
                
            # Extract function name - Bolna.ai sends it nested in 'function' object
            func_obj = func_call.get("function") or {}
            func_name = (
                func_obj.get("name") or  # Most common: function.name
                func_call.get("name") or  # Fallback: direct name
                func_call.get("functionName") or
                func_call.get("function_name") or
                func_call.get("toolName") or
                func_call.get("tool_name") or
                ""
            )
            
            print(f"[FUNCTION CALL] Function name extracted: '{func_name}'")
            print(f"[FUNCTION CALL] Function object: {func_obj}")
            
            if func_name == "request_user_approval":
                print(f"[APPROVAL] request_user_approval function detected!")
                
                # Extract approval details - arguments are in function.arguments
                args = (
                    func_obj.get("arguments") or  # Most common: function.arguments
                    func_call.get("arguments") or
                    func_call.get("parameters") or
                    func_call.get("args") or
                    {}
                )
                
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except:
                        args = {}
                
                print(f"[APPROVAL] Extracted arguments: {args}")
                
                approval_type = args.get("approval_type", "general")
                description = args.get("description", "Approval needed")
                original_value = args.get("original_value") or args.get("originalValue") or ""
                negotiated_value = args.get("negotiated_value") or args.get("negotiatedValue") or ""
                
                # Convert to string and clean up
                if original_value:
                    original_value = str(original_value).strip()
                if negotiated_value:
                    negotiated_value = str(negotiated_value).strip()
                
                # Fallback: Try to extract values from description if missing
                import re
                if not original_value or not negotiated_value:
                    # Try to extract from description like "reduce price from ₹6000 to ₹5500"
                    price_pattern = r'₹?\s*(\d+(?:,\d+)*(?:\.\d+)?)'
                    prices = re.findall(price_pattern, description)
                    if len(prices) >= 2:
                        if not original_value:
                            original_value = f"₹{prices[0]}"
                        if not negotiated_value:
                            negotiated_value = f"₹{prices[1]}"
                        print(f"[APPROVAL] Extracted prices from description: original={original_value}, negotiated={negotiated_value}")
                    elif len(prices) == 1:
                        if not negotiated_value:
                            negotiated_value = f"₹{prices[0]}"
                        print(f"[APPROVAL] Extracted negotiated price from description: {negotiated_value}")
                
                print(f"[APPROVAL] Final extracted values - original_value: '{original_value}', negotiated_value: '{negotiated_value}'")
                
                # Get call preferences for budget info and requirement
                prefs_dict = call_preferences.get(app_call_id, {})
                user_budget = prefs_dict.get("budget") if isinstance(prefs_dict, dict) else None
                requirement = prefs_dict.get("requirement", "") if isinstance(prefs_dict, dict) else ""
                
                # Ensure user_budget is a number (float) if it exists, or None if missing/0
                # Also check for 0 as a valid budget value
                if user_budget is not None and user_budget != "":
                    try:
                        user_budget = float(user_budget)
                        # Keep 0 as a valid budget (user might have 0 budget)
                    except (ValueError, TypeError):
                        user_budget = None
                else:
                    user_budget = None
                
                print(f"[APPROVAL] Extracted user_budget: {user_budget} (type: {type(user_budget)}), prefs_dict: {prefs_dict}")
                
                # Warn if critical values are missing
                if not original_value or not negotiated_value:
                    print(f"[APPROVAL] ⚠️ WARNING: Missing values! original_value: '{original_value}', negotiated_value: '{negotiated_value}'")
                    print(f"[APPROVAL] Full args received: {args}")
                
                # Enhance description to include requirement and show negotiation flow
                if requirement and original_value and negotiated_value:
                    # Build a natural description showing the negotiation flow
                    if "for" not in description.lower() and requirement not in description:
                        description = f"Owner agreed to reduce the price from {original_value} to {negotiated_value} for {requirement}."
                    elif requirement not in description:
                        # Add requirement to existing description
                        description = f"{description} for {requirement}."
                elif original_value and negotiated_value:
                    # Build description without requirement if not available
                    if "reduce" not in description.lower() and "from" not in description.lower():
                        description = f"Owner agreed to reduce the price from {original_value} to {negotiated_value}."
            
                # Create approval request
                # Ensure user_budget is always included (even if None) so frontend can display it
                approval_data = {
                    "approval_id": str(uuid.uuid4()),
                    "call_id": app_call_id,
                    "approval_type": approval_type,
                    "description": description,
                    "original_value": original_value,
                    "negotiated_value": negotiated_value,
                    "user_budget": user_budget,  # This will be None, a number, or 0 - frontend handles all cases
                    "requirement": requirement,  # Add requirement for UI display
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                    "status": "waiting",
                    "expires_at": (datetime.utcnow() + timedelta(seconds=10)).isoformat() + "Z"
                }
                
                # Log approval data to verify user_budget is included
                print(f"[APPROVAL] Created approval_data with user_budget: {approval_data.get('user_budget')} (type: {type(approval_data.get('user_budget'))})")
                print(f"[APPROVAL] Full approval_data keys: {list(approval_data.keys())}")
                
                # CRITICAL: Clear any previous approval for this call_id to prevent stale data
                if app_call_id in active_approvals:
                    old_approval = active_approvals[app_call_id]
                    old_approval_id = old_approval.get("approval_id")
                    print(f"[APPROVAL] ⚠️ Found existing approval {old_approval_id} for call {app_call_id}, removing it before creating new one")
                    # Move old approval to pending if it exists
                    if old_approval_id:
                        pending_approvals[app_call_id] = old_approval
                    del active_approvals[app_call_id]
                
                active_approvals[app_call_id] = approval_data
                
                print(f"[APPROVAL] Created approval_data: {approval_data}")
                print(f"[APPROVAL] Approval details - original_value: {original_value}, negotiated_value: {negotiated_value}, user_budget: {user_budget}, requirement: {requirement}")
                print(f"[APPROVAL] Sending to WebSocket for app_call_id: {app_call_id}")
                
                # Send approval request to frontend via WebSocket
                try:
                    await manager.send_to_app_call(app_call_id, {
                        "type": "approval_request",
                        "approval": approval_data
                    })
                    print(f"[APPROVAL] ✅ Successfully sent approval request to WebSocket with data: {approval_data}")
                except Exception as e:
                    print(f"[APPROVAL] ERROR sending to WebSocket: {e}")
                
                # Wait for user approval (with 10 second timeout)
                print(f"[APPROVAL] Waiting for user approval (10 second timeout)...")
                approval_result = await wait_for_approval_response(app_call_id, approval_data["approval_id"], timeout=10)
                
                print(f"[APPROVAL] Approval result: {approval_result}")
                
                # Clean up active_approvals - only remove if it's the same approval_id
                if app_call_id in active_approvals:
                    final_approval = active_approvals[app_call_id]
                    if final_approval.get("approval_id") == approval_data["approval_id"]:
                        print(f"[APPROVAL] Moving approval {approval_data['approval_id']} to pending_approvals")
                        pending_approvals[app_call_id] = final_approval
                        del active_approvals[app_call_id]
                        print(f"[APPROVAL] Cleaned up active_approvals for call {app_call_id}")
                    else:
                        print(f"[APPROVAL] ⚠️ Approval ID mismatch during cleanup! Expected {approval_data['approval_id']}, found {final_approval.get('approval_id')}")
                else:
                    print(f"[APPROVAL] ⚠️ No active approval found during cleanup for call {app_call_id}")
                
                # Return the actual approval result to Bolna.ai in the correct format
                # Bolna.ai expects: {"results": [{"toolCallId": "...", "result": "..."}]}
                # tool_call_id was already extracted above
                
                # Track approval status for end-of-call summary
                call_approval_status[app_call_id] = {
                    "status": approval_result,
                    "negotiated_value": approval_data.get("negotiated_value", ""),
                    "original_value": approval_data.get("original_value", ""),
                    "description": approval_data.get("description", "")
                }
                
                if approval_result == "approved":
                    user_phone = user_phone_numbers.get(app_call_id, "")
                    # Get call preferences for context
                    prefs_dict = call_preferences.get(app_call_id, {})
                    requirement = prefs_dict.get("requirement", "the service")
                    negotiated_price = approval_data.get("negotiated_value", "")
                    
                    # CRITICAL: Format ALL values in natural language to prevent digit-by-digit reading
                    # Convert price to words (e.g., "4000" -> "four thousand")
                    price_words = number_to_words(str(negotiated_price))
                    requirement_clean = requirement.lower() if requirement else "the service"
                    
                    # Format date naturally if present (convert to "January 1st, 2026" format)
                    preferred_date = prefs_dict.get("preferred_date", "")
                    date_text = ""
                    if preferred_date and preferred_date != "Flexible" and preferred_date.strip():
                        formatted_date = format_date_naturally(preferred_date)
                        if formatted_date:
                            date_text = f" The user's preferred date is {formatted_date}."
                    
                    # Format phone number naturally (group digits with spaces)
                    phone_text = ""
                    if user_phone:
                        formatted_phone = format_phone_naturally(user_phone)
                        phone_text = f" If you need the user's contact number, it's {formatted_phone}."
                    
                    # Create response message - DO NOT include "Thank you for waiting" 
                    # (Bolna.ai will say that automatically BEFORE this message)
                    # Write price in words to ensure natural pronunciation
                    # CRITICAL: This message is spoken to the BUSINESS OWNER, not the user
                    response_msg = f"Great news! The user has approved the price of {price_words} rupees for {requirement_clean}. "
                    response_msg += "Let's proceed with the booking. Could you please confirm the details and terms?"
                    if date_text:
                        response_msg += date_text
                    if phone_text:
                        response_msg += phone_text
                    response_msg += " Please confirm these details so we can finalize."
                    
                    print(f"[APPROVAL] Returning approved response to Bolna.ai: {response_msg}")
                    
                    # Return function result first (required by Bolna.ai)
                    result_response = {
                        "results": [
                            {
                                "toolCallId": tool_call_id,
                                "result": response_msg
                            }
                        ]
                    }
                    
                    # Use control API to inject the function result using "say" action
                    # This makes Bolna.ai speak the message immediately
                    message_sent = await send_message_to_bolna(app_call_id, response_msg, message_type="say")
                    if message_sent:
                        print(f"[APPROVAL] Successfully injected approval message via control API using 'say' action")
                    else:
                        print(f"[APPROVAL] Control API injection failed, relying on function result only")
                    
                    return result_response
                elif approval_result == "denied":
                    # Track approval status for end-of-call summary
                    call_approval_status[app_call_id] = {
                        "status": "denied",
                        "negotiated_value": approval_data.get("negotiated_value", ""),
                        "original_value": approval_data.get("original_value", ""),
                        "description": approval_data.get("description", "")
                    }
                    
                    # For denial, provide a clear message that will end the call
                    # IMPORTANT: Bolna.ai will say "Thank you for waiting. I have the user's response." BEFORE this message
                    # So the flow is: "Thank you for waiting..." → [This denial message] → End call
                    response_msg = "I'm sorry, but the user has decided not to proceed with this option. Thank you for your time and understanding. Have a good day."
                    print(f"[APPROVAL] Returning denied response to Bolna.ai: {response_msg}")
                    
                    # Return function result - Bolna.ai will speak this after "Thank you for waiting"
                    # DO NOT use control API "say" for denial as it might cause message ordering issues
                    result_response = {
                        "results": [
                            {
                                "toolCallId": tool_call_id,
                                "result": response_msg
                            }
                        ]
                    }
                    
                    return result_response
                else:  # timeout
                    # Track approval status for end-of-call summary
                    call_approval_status[app_call_id] = {
                        "status": "timeout",
                        "negotiated_value": approval_data.get("negotiated_value", ""),
                        "original_value": approval_data.get("original_value", ""),
                        "description": approval_data.get("description", "")
                    }
                    
                    response_msg = "I'll confirm with the user and call you back with their decision. Thank you for your time."
                    print(f"[APPROVAL] Returning timeout response to Bolna.ai: {response_msg}")
                    return {
                        "results": [
                            {
                                "toolCallId": tool_call_id,
                                "result": response_msg
                            }
                        ]
                    }
        
        print(f"[FUNCTION CALL] No matching function found. Processed {len(function_calls)} calls.")

    return {"status": "ok"}

# ---------- Wait for approval response ----------

async def wait_for_approval_response(app_call_id: str, approval_id: str, timeout: int = 10) -> str:
    """Wait for user approval response, return 'approved', 'denied', or 'timeout'"""
    start_time = datetime.utcnow()
    check_interval = 0.05  # Check every 50ms for faster response
    
    print(f"[APPROVAL WAIT] Starting wait for approval {approval_id} in call {app_call_id}")
    print(f"[APPROVAL WAIT] Current active_approvals keys: {list(active_approvals.keys())}")
    
    while True:
        # Check if approval was responded to
        if app_call_id in active_approvals:
            approval = active_approvals[app_call_id]
            stored_approval_id = approval.get("approval_id")
            print(f"[APPROVAL WAIT] Found approval in active_approvals: stored_id={stored_approval_id}, expected_id={approval_id}")
            
            # CRITICAL: Only check status if approval_id matches
            if stored_approval_id == approval_id:
                status = approval.get("status")
                print(f"[APPROVAL WAIT] Status match! Current status: {status}")
                if status == "approved":
                    print(f"[APPROVAL WAIT] ✅ User approved!")
                    return "approved"
                elif status == "denied":
                    print(f"[APPROVAL WAIT] ❌ User denied!")
                    return "denied"
                # If status is "waiting", continue waiting
                elif status == "waiting":
                    pass  # Continue waiting
                else:
                    print(f"[APPROVAL WAIT] ⚠️ Unexpected status: {status}, continuing to wait...")
            else:
                print(f"[APPROVAL WAIT] ⚠️ Approval ID mismatch! Expected {approval_id}, found {stored_approval_id}. This might be a previous approval.")
        
        # Check timeout
        elapsed = (datetime.utcnow() - start_time).total_seconds()
        if elapsed >= timeout:
            print(f"[APPROVAL WAIT] ⏱️ Timeout after {timeout} seconds (elapsed: {elapsed:.2f}s)")
            # Check one more time right before timeout
            if app_call_id in active_approvals:
                approval = active_approvals[app_call_id]
                stored_approval_id = approval.get("approval_id")
                if stored_approval_id == approval_id:
                    status = approval.get("status")
                    print(f"[APPROVAL WAIT] Final check before timeout: status={status}")
                    if status == "approved":
                        print(f"[APPROVAL WAIT] ✅ User approved just before timeout!")
                        return "approved"
                    elif status == "denied":
                        print(f"[APPROVAL WAIT] ❌ User denied just before timeout!")
                        return "denied"
                    elif status == "waiting":
                        # Still waiting - this is a timeout
                        print(f"[APPROVAL WAIT] Still waiting after timeout - treating as timeout")
                    else:
                        print(f"[APPROVAL WAIT] ⚠️ Unexpected status before timeout: {status}, treating as timeout")
                else:
                    print(f"[APPROVAL WAIT] ⚠️ Approval ID mismatch at timeout! Expected {approval_id}, found {stored_approval_id}. Treating as timeout.")
            
            # Move to pending if still active and waiting
            if app_call_id in active_approvals:
                approval = active_approvals[app_call_id]
                if approval.get("approval_id") == approval_id and approval.get("status") == "waiting":
                    approval["status"] = "timeout"
                    pending_approvals[app_call_id] = approval
                    del active_approvals[app_call_id]
                    print(f"[APPROVAL WAIT] Moved approval {approval_id} to pending_approvals with status 'timeout'")
            else:
                print(f"[APPROVAL WAIT] ⚠️ No active approval found at timeout for call {app_call_id}")
            
            return "timeout"
        
        # Wait a bit before checking again (faster polling)
        await asyncio.sleep(check_interval)

# ---------- Approval timeout handler (for frontend notification) ----------

async def approval_timeout_handler(app_call_id: str, approval_id: str):
    """Handle approval timeout after 10 seconds"""
    print(f"[APPROVAL TIMEOUT] Starting 10-second countdown for approval {approval_id} in call {app_call_id}")
    await asyncio.sleep(10)
    print(f"[APPROVAL TIMEOUT] 10 seconds elapsed, checking approval status...")
    
    # Check if approval is still active and not responded to
    if app_call_id in active_approvals:
        approval = active_approvals[app_call_id]
        if approval.get("approval_id") == approval_id and approval.get("status") == "waiting":
            print(f"[APPROVAL TIMEOUT] Approval {approval_id} timed out - moving to pending")
            # Timeout - move to pending and end call
            approval["status"] = "timeout"
            approval["call_ended"] = False
            pending_approvals[app_call_id] = approval
            del active_approvals[app_call_id]
            
            # Notify frontend
            print(f"[APPROVAL TIMEOUT] Sending timeout notification to frontend for call {app_call_id}")
            await manager.send_to_app_call(app_call_id, {
                "type": "approval_timeout",
                "approval": approval
            })
            
            # Send message to Bolna.ai to end call gracefully
            await send_message_to_bolna(app_call_id, "I'll confirm with the user and call you back with their decision. Thank you for your time.")
        else:
            print(f"[APPROVAL TIMEOUT] Approval {approval_id} was already responded to (status: {approval.get('status')})")
    else:
        print(f"[APPROVAL TIMEOUT] No active approval found for call {app_call_id}")

async def send_message_to_bolna(app_call_id: str, message: str, message_type: str = "say"):
    """
    Send a message to Bolna.ai using the control API.
    
    Args:
        app_call_id: The application call ID
        message: The message content to send
        message_type: "say" to speak the message, or "add-message" to add to context
    """
    bolna_call_id = None
    for bolna_id, app_id in manager.bolna_to_app_call.items():
        if app_id == app_call_id:
            bolna_call_id = bolna_id
            break
    
    if not bolna_call_id:
        print(f"[BOLNA MESSAGE] No bolna_call_id found for app_call_id: {app_call_id}")
        return False
    
    # Get control URL for this call
    control_url = manager.app_call_control_url.get(app_call_id)
    if not control_url:
        print(f"[BOLNA MESSAGE] No control URL found for app_call_id: {app_call_id}")
        return False
    
    headers = {
        "Authorization": f"Bearer {BOLNA_API_KEY}",
        "Content-Type": "application/json"
    }
    
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            if message_type == "say":
                # Use "say" action to inject a message to be spoken
                payload = {
                    "type": "say",
                    "message": message
                }
            else:
                # Use "add-message" to add to conversation context
                payload = {
                    "type": "add-message",
                    "message": {
                        "role": "assistant",
                        "content": message
                    },
                    "triggerResponseEnabled": False
                }
            
            resp = await client.post(control_url, json=payload, headers=headers)
            print(f"[BOLNA MESSAGE] Sent {message_type} message to call {bolna_call_id}: {message}")
            print(f"[BOLNA MESSAGE] Response status: {resp.status_code}, body: {resp.text}")
            if resp.status_code >= 400:
                print(f"[BOLNA MESSAGE] Error sending message: {resp.text}")
                return False
            return True
    except Exception as e:
        print(f"[BOLNA MESSAGE] Error sending message to Bolna.ai: {e}")
        return False

# ---------- Approval endpoints ----------

@app.post("/approve")
async def approve_request(response: ApprovalResponse):
    """Handle user approval"""
    app_call_id = response.call_id
    
    if app_call_id not in active_approvals:
        print(f"[APPROVAL ENDPOINT] No active approval found for call {app_call_id}")
        print(f"[APPROVAL ENDPOINT] Current active_approvals keys: {list(active_approvals.keys())}")
        return {"status": "error", "message": "No active approval found"}, 404
    
    approval = active_approvals[app_call_id]
    approval_id = approval.get("approval_id")
    
    # Update status immediately
    approval["status"] = "approved"
    approval["user_phone"] = response.user_phone or user_phone_numbers.get(app_call_id)
    
    print(f"[APPROVAL ENDPOINT] ✅ User approved! Status updated for call {app_call_id}, approval_id: {approval_id}")
    print(f"[APPROVAL ENDPOINT] Approval object: {approval}")
    
    # Keep in active_approvals so wait_for_approval_response can detect it
    # It will be removed when the function call completes
    
    # Notify frontend
    await manager.send_to_app_call(app_call_id, {
        "type": "approval_response",
        "approved": True,
        "approval": approval
    })
    
    print(f"[APPROVAL ENDPOINT] Approval processed for call {app_call_id}")
    
    return {"status": "ok", "message": "Approval sent to AI"}

@app.post("/deny")
async def deny_request(response: ApprovalResponse):
    """Handle user denial"""
    app_call_id = response.call_id
    
    if app_call_id not in active_approvals:
        print(f"[APPROVAL ENDPOINT] No active approval found for call {app_call_id}")
        print(f"[APPROVAL ENDPOINT] Current active_approvals keys: {list(active_approvals.keys())}")
        return {"status": "error", "message": "No active approval found"}, 404
    
    approval = active_approvals[app_call_id]
    approval_id = approval.get("approval_id")
    
    # Update status immediately
    approval["status"] = "denied"
    
    print(f"[APPROVAL ENDPOINT] ❌ User denied! Status updated for call {app_call_id}, approval_id: {approval_id}")
    print(f"[APPROVAL ENDPOINT] Approval object: {approval}")
    
    # Keep in active_approvals so wait_for_approval_response can detect it
    # It will be removed when the function call completes
    
    # Notify frontend
    await manager.send_to_app_call(app_call_id, {
        "type": "approval_response",
        "approved": False,
        "approval": approval
    })
    
    print(f"[APPROVAL ENDPOINT] Denial processed for call {app_call_id}")
    
    return {"status": "ok", "message": "Denial sent to AI"}

@app.get("/call/{call_id}/approvals")
async def get_approvals(call_id: str):
    """Get all approvals for a call (active and pending)"""
    active = active_approvals.get(call_id)
    pending = pending_approvals.get(call_id)
    return {
        "active": active,
        "pending": pending
    }

@app.get("/call/{call_id}/transcript")
async def get_transcript(call_id: str):
    transcript = conversation_store.get(call_id, [])
    summary = conversation_summaries.get(call_id)
    approvals = pending_approvals.get(call_id)
    return {
        "transcript": transcript,
        "summary": summary,
        "pending_approvals": approvals
    }



# ---------- Serve static frontend ----------

# The /app path will serve static/index.html and related assets
app.mount("/app", StaticFiles(directory="static", html=True), name="static")


# ========= TERMINAL INPUT LOOP =========

def terminal_input_loop():
    """Background thread for terminal input to generate form schemas."""
    global current_schema
    
    print("\n" + "="*60)
    print("🎯 DYNAMIC UI FORM GENERATOR")
    print("="*60)
    print("\nEnter a query to generate a dynamic form schema.")
    print("Examples:")
    print("  • Find a gym near me")
    print("  • Book a hotel room")
    print("  • Order food from a restaurant")
    print("  • Schedule a doctor appointment")
    print("\nType 'quit' to exit.\n")
    
    while True:
        try:
            query = input("\n📝 Enter your query: ").strip()
            
            if query.lower() in ['quit', 'exit', 'q']:
                print("👋 Goodbye!")
                os._exit(0)
            
            if not query:
                print("⚠️  Please enter a valid query.")
                continue
            
            print(f"\n🔄 Generating form schema for: \"{query}\"...")
            print("   (Using data from data/ folder + ChatGPT)\n")
            
            schema = generate_schema_from_query(query)
            
            with schema_lock:
                current_schema = schema
            
            print("✅ Schema generated successfully!\n")
            print("-" * 40)
            print(f"📋 Title: {schema['title']}")
            print(f"📝 Description: {schema['description']}")
            print(f"📊 Fields: {len(schema['fields'])}")
            for field in schema['fields']:
                req = "✓" if field.get('required') else " "
                print(f"   [{req}] {field['label']} ({field['type']})")
            print("-" * 40)
            print("\n🌐 Open http://localhost:8000 in your browser")
            print("   to see the generated form!\n")
            
        except EOFError:
            break
        except KeyboardInterrupt:
            print("\n👋 Goodbye!")
            break


@app.on_event("startup")
async def startup_event():
    """Start the terminal input loop when the server starts."""
    # Check for OpenAI API key
    if not OPENAI_API_KEY:
        print("\n" + "="*60)
        print("⚠️  OPENAI_API_KEY not found in environment!")
        print("="*60)
        print("Schema generation will use fallback schema from data/ folder.")
        print("Set OPENAI_API_KEY in .env for AI-generated schemas.\n")
    
    # Start terminal input thread
    input_thread = threading.Thread(target=terminal_input_loop, daemon=True)
    input_thread.start()
