"""
Dynamic System Prompt Builder for Bolna.ai Voice Agent

Handles context-aware system prompts and service recognition.
Simplified implementation relying on data_provider for context.
"""

import os
from typing import Dict, Any, Optional
from openai import OpenAI
from data_provider import BusinessInfo

# Initialize OpenAI client for service extraction
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# ==============================================================================
# 1. SERVICE EXTRACTION (Critical for accurate service names)
# ==============================================================================

def extract_service_intelligently(
    user_input: str,
    business_info: Any,
    user_pref: Optional[Any] = None
) -> str:
    """Use OpenAI to match user input to business services."""
    if not openai_client:
        return _fallback_service_extraction(user_input, business_info, user_pref)
    
    try:
        business_services = business_info.services or "general services"
        user_pref_service = getattr(user_pref, 'service', None) if user_pref else None
        
        prompt = f"""You are a service extraction assistant. Find the SPECIFIC service name.
Business: {business_info.name}
Services: {business_services}
User input: "{user_input}"
{f'Previous pref: {user_pref_service}' if user_pref_service else ''}

Extract specific service matching user wants AND business offers.
Rules: Return ONLY the service name. If vague, pick best match.
Input: "massage" -> Output: "Authentic Thai Massage" (if offered)
"""
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1, max_tokens=50
        )
        extracted = response.choices[0].message.content.strip()
        print(f"[SERVICE EXTRACTION] ✅ '{user_input}' → '{extracted}'")
        return extracted
    except Exception as e:
        print(f"[SERVICE EXTRACTION] ⚠️ Error: {e}, using fallback")
        return _fallback_service_extraction(user_input, business_info, user_pref)

def _fallback_service_extraction(user_input, business_info, user_pref) -> str:
    """Fallback if OpenAI unavailable."""
    if user_input and user_input.lower() not in ["not specified", "service"]: return user_input
    if user_pref and getattr(user_pref, 'service', None): return user_pref.service
    return user_input or "Not specified"

def generate_welcome_message(requirement: str, business_name: str) -> str:
    """Generate a short (<35 tokens) welcome message for the AI."""
    if not openai_client:
        return f"Hello, I'm calling about {requirement}. Do you have a moment?"
        
    try:
        prompt = f"""Generate a concise welcome message for a voice agent calling {business_name} about "{requirement}".
Rules:
1. MAX 35 tokens.
2. Be polite but direct.
3. Start immediately (no "Sure!").
4. Example: "Hi, I'm calling to inquire about {requirement}. Is now a good time?"
"""
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7, max_tokens=40
        )
        msg = response.choices[0].message.content.strip()
        print(f"[WELCOME MSG] ✅ Generated: '{msg}'")
        return msg
    except Exception as e:
        print(f"[WELCOME MSG] ⚠️ Error: {e}")
        return f"Hello, I'm calling to ask about {requirement}. Do you have a few minutes?"

# ==============================================================================
# 2. PROMPT BUILDING
# ==============================================================================

def build_dynamic_prompt(
    form_data: dict,
    business_info: "BusinessInfo",
    call_type: str = "info"
) -> str:
    """
    Builds a comprehensive system prompt for bolna.ai voice agents.

    Call Types:
    - info: Information gathering only. No booking, no approval.
    - negotiation: Price negotiation only. No approval.
    - auto: Auto-book if price <= budget. Approval required if price > budget.
    - booking: Always aim to book, but approval required if price > budget.
    """

    # -------------------------
    # 1. Core User Context
    # -------------------------
    # Fix: BusinessInfo is a dataclass, access attributes directly
    # STRICT SEPARATION: Business Services vs User Requirement
    
    # Core user info
    user_name = form_data.get("user_name", "Valued Customer")
    user_phone = form_data.get("user_phone", "")
    user_location = form_data.get("location", "Unknown Location")
    service_address = form_data.get("service_address", "")

    # Service & requirement
    service = form_data.get("service") or "General Inquiry"
    requirement = form_data.get("requirement") or service
    service_type = form_data.get("service_type", "")
    business_name = form_data.get("business_name", "") or (business_info.name if business_info else "Business")
    business_location = business_info.location if business_info else "Unknown Location"

    # Budget
    budget = form_data.get("budget")


    # Scheduling
    preferred_date = form_data.get("preferred_date", "")
    preferred_call_time = form_data.get("preferred_call_time", "")

    # Call & urgency
    call_type = form_data.get("call_type", "outbound")
    urgency = form_data.get("urgency", "normal")

    # Notes & extras
    notes = form_data.get("notes", "")
    special_requests = form_data.get("special_requests") or []
    custom_data = form_data.get("custom_data") or {}

    
    goal_instruction = ""
    if call_type == "info":
         goal_instruction = f"""
         GOAL:
- Collect complete information only
- Ask about availability, pricing, timelines, and terms
- DO NOT negotiate aggressively
- DO NOT request approval
- DO NOT book anything

ENDING:
- Politely thank the owner
- End the call with:
  "Thank you for your time. Have a great day."
  """
    elif call_type == "negotiation":
        goal_instruction = f"""
        GOAL:
- Ask for the service price
- If quoted price > {budget}:
  - Politely request a reduction
  - Negotiate gently and respectfully
- Reach a final negotiated price

ENDING:
- Thank the owner politely
- End the call gracefully after negotiation
- DO NOT book
- DO NOT request approval
        """
    elif call_type == "auto":
        goal_instruction = f"""

GOAL:
- Ask for the price
- If price is greater than {budget}:
  - First negotiate to reduce the price
- AFTER negotiation:
  call to the function @request_user_approval for price approval 
  once user approve or deny then proceed to the next step
AFTER APPROVAL:
- If user APPROVES:
  - Inform the owner that the user approved the price
  - Share the user phone number: {user_phone}
  - Proceed toward booking
- If user DENIES:
  - Apologize politely
  - Thank the owner
  - End the call
- If user TIMES OUT:
  - Inform owner you will reconnect later
  - End the call politely
        """
    elif call_type == "booking":
        goal_instruction = f"""
        GOAL:
- Ask for price
- If price is greater than {budget}:
  - First negotiate from owner to reduce the price
- AFTER negotiation:
  call to the function @request_user_approval for price approval 
  once user approve or deny or timeout then proceed to the next step
- after user response:
  - Confirm booking details
  - Schedule appointment using:
    - Date: {preferred_date}
    - Time: {preferred_call_time}
  - Share user phone number: {user_phone}
  ENDING:
- Confirm next steps clearly
- Thank the owner and close politely
        """  

    system_prompt = f"""
    SYSTEM ROLE (GENERAL INSTRUCTIONS):

You are an AI voice agent calling a business owner on behalf of a user.
You always represent the USER’S SIDE.

Your role is to:
- Speak politely, professionally, and naturally like a real human
- Sound calm, respectful, and confident
- Never sound robotic or rushed
- Clearly explain why you are calling
- Gather accurate information and act strictly according to the call type

If asked your name, you MUST say: "Alex".

You are calling the business owner of:
- Business Name: {business_name}
- Business Location: {business_location}

You are calling on behalf of:
- User Name: {user_name}
- User Location: {user_location}
- User Phone: {user_phone}

The user’s main requirement is:
- Service / Requirement: {requirement}
- Service Type: {service_type}
- Urgency Level: {urgency}
- Preferred Date: {preferred_date}
- Preferred Call Time: {preferred_call_time}
- User Budget: {budget}

--------------------------------------------------
TOOLS (MANDATORY USAGE RULES – VERY IMPORTANT)
--------------------------------------------------

1. @report_call_info  (MANDATORY)

You MUST call @report_call_info IMMEDIATELY whenever you observe, confirm, or update ANY of the following:

- Any price quote (initial or revised)
- Availability details (dates, time slots)
- Negotiation progress or outcome
- Any change in terms or conditions

Additional rules:
- You MUST call @report_call_info after EACH COMPLETE TURN of the business owner
  IF any of the above information was mentioned or changed.
- Do NOT wait until the end of the call.
- Failure to report updates is a HARD FAILURE.

--------------------------------------------------

2. @request_user_approval

This tool:
- Is the ONLY way to get user consent
- Is ONLY allowed in call types: auto, booking
- Can be used ONLY AFTER negotiation is complete
- Can be used ONLY when final price > user budget

If you say:
"Let me check with the user"
You MUST immediately call @request_user_approval.

--------------------------------------------------
STRICT BEHAVIOR RULES (NON-NEGOTIABLE)
--------------------------------------------------

- Always speak politely and naturally
- Never imply user approval without calling @request_user_approval
- Never skip negotiation when price exceeds budget
- Never finalize booking without required approval
- Never hallucinate prices, availability, or confirmations
- If something is unclear, ASK the business owner

--------------------------------------------------
STANDARD CONVERSATION FLOW (ALWAYS FOLLOW)
--------------------------------------------------

1. Introduction:
- Introduce yourself as Alex
- Say you are calling on behalf of a client
- Clearly state the service / requirement

2. Availability & Capability:
- Ask if the business provides {service_type}
- Ask if they can deliver the service within the user’s urgency: {urgency}
- Ask about availability for {preferred_date} and {preferred_call_time}

3. Location & Coverage:
- Ask if the service is available at {user_location}

4. Special Requests:
- Ask whether the business can accommodate:
  {special_requests}
- Ask if there are any additional charges for these requests

5. Custom User Data (if provided):
- If additional custom details exist, ask about them clearly:
  {custom_data}

{goal_instruction}


--------------------------------------------------

FAILURE TO FOLLOW THESE INSTRUCTIONS IS A HARD FAILURE.

 """

    return system_prompt.strip()
