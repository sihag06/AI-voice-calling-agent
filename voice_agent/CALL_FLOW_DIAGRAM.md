# 🔄 Complete Call Flow Diagram

> **Visual guide showing every step from call initiation to completion**

---

## 📊 High-Level Overview

```mermaid
flowchart TB
    subgraph Frontend["Frontend - index.html"]
        A[User fills form] --> B[Click Call button]
        B --> C[POST start-call]
        W[WebSocket connection] --> D[Display updates]
    end
    
    subgraph Backend["Backend - main.py"]
        C --> E[start_call endpoint]
        E --> F[build_system_prompt]
        F --> G[Create payload]
        G --> H[POST to Bolna API]
    end
    
    subgraph Bolna["Bolna.ai Platform"]
        H --> I[Start call via Twilio]
        I --> J[AI talks to owner]
        J --> K[AI calls report_call_info]
        J --> L[Call ends]
        L --> M[Send webhook with transcript]
    end
    
    subgraph Webhooks["Webhook Handling"]
        K --> N[report-call-info endpoint]
        M --> O[bolna-webhook endpoint]
        N --> P[Send to WebSocket]
        O --> Q[Process transcript]
        Q --> R[live_transcript.py]
        R --> P
    end
    
    P --> W
```

---

## 📋 Step-by-Step Flow

### Phase 1: Call Initiation

```mermaid
sequenceDiagram
    participant U as 👤 User
    participant F as 🖥️ Frontend
    participant B as ⚙️ Backend
    participant API as ☁️ Bolna.ai API
    
    U->>F: Fill form (phone, requirement, budget, date, notes)
    U->>F: Click "Call on my behalf"
    F->>F: Generate app_call_id (UUID)
    F->>B: WebSocket connect ws/call/{app_call_id}
    F->>B: POST /start-call with CallPreferences
    
    B->>B: build_system_prompt(prefs)
    B->>B: Create payload with user_data dictionary
    B->>API: POST /call with agent_id, phone, user_data, metadata
    
    API-->>B: Response with execution_id, control_url
    B->>B: Store mapping: bolna_call_id → app_call_id
    B-->>F: Return success with call_id
    F->>F: Show "Call connecting..."
```

### Phase 2: During Call (Real-time Updates)

```mermaid
sequenceDiagram
    participant O as 📞 Business Owner
    participant AI as 🤖 Bolna AI Agent
    participant W as 📡 Webhook
    participant B as ⚙️ Backend
    participant F as 🖥️ Frontend
    
    AI->>O: "Hello, I'm calling about a library..."
    O->>AI: "Yes, it's available"
    
    Note over AI: AI MUST call report_call_info FIRST
    
    AI->>W: POST /report-call-info
    Note right of AI: info_type: "availability"<br/>value: "library"<br/>status: "confirmed"
    
    W->>B: report_call_info endpoint
    B->>B: Format message with emoji
    B->>F: WebSocket: conversation_update
    F->>F: Display "✅ Availability: library - confirmed"
    
    AI->>O: "What is the pricing?"
    O->>AI: "1500 rupees"
    
    AI->>W: POST /report-call-info
    Note right of AI: info_type: "price"<br/>value: "₹1500"<br/>status: "checking"
    
    W->>B: report_call_info endpoint
    B->>F: WebSocket: conversation_update
    F->>F: Display "💰 Price: ₹1500 - checking"
```

### Phase 3: Call Ends (Final Transcript)

```mermaid
sequenceDiagram
    participant AI as 🤖 Bolna AI
    participant BL as ☁️ Bolna Platform
    participant W as 📡 Webhook
    participant B as ⚙️ Backend
    participant LT as 📝 live_transcript.py
    participant F as 🖥️ Frontend
    
    AI->>AI: Call ends (hangup)
    BL->>W: POST /bolna-webhook
    Note right of BL: status: "completed"<br/>transcript: "assistant: Hello...<br/>user: Yes..."<br/>summary: "..."
    
    W->>B: bolna_webhook endpoint
    B->>B: Look up app_call_id from bolna_call_id
    B->>LT: process_webhook_transcript(body, app_call_id)
    
    LT->>LT: _parse_string_transcript()
    LT->>LT: _process_conversation_updates()
    
    loop For each message
        LT->>F: WebSocket: conversation_update (AI/Owner)
    end
    
    B->>F: WebSocket: summary
    F->>F: Display full conversation + summary
```

---

## 🗂️ File Responsibilities

| File | Responsibility |
|------|----------------|
| `main.py` | API endpoints, webhook handling, system prompt |
| `live_transcript.py` | Parse and send final transcript to frontend |
| `static/index.html` | UI, WebSocket connection, display updates |

---

## 📡 API Endpoints

| Endpoint | Method | Purpose | Called By |
|----------|--------|---------|-----------|
| `/start-call` | POST | Start a new call | Frontend |
| `/stop-call` | POST | End an active call | Frontend |
| `/report-call-info` | POST | Receive real-time info | Bolna AI |
| `/bolna-webhook` | POST | Receive call events | Bolna Platform |
| `/ws/call/{id}` | WS | Real-time updates | Frontend |

---

## 🔧 Key Functions

### `build_system_prompt(prefs)` - Lines 448-710
Creates the AI agent's instructions including:
- Real-time reporting requirements
- Conversation flow (availability → price → negotiate)
- Pronunciation rules
- Identity and behavior rules

### `start_call()` - Lines 705-835
1. Generates `app_call_id`
2. Builds `user_data` dictionary with form values + system_prompt
3. Calls Bolna.ai `/call` API
4. Stores call mappings
5. Returns call_id to frontend

### `report_call_info()` - Lines 1191-1290
1. Receives info from AI agent
2. Formats with emoji based on `info_type`
3. Sends to frontend via WebSocket

### `bolna_webhook()` - Lines 1530-1720
1. Receives all Bolna.ai events
2. Routes to appropriate handler
3. Processes final transcript when call ends
4. Sends summary to frontend

### `process_webhook_transcript()` - live_transcript.py
1. Parses transcript string
2. Converts to structured messages
3. Sends each message to frontend via WebSocket

---

## 📊 Data Flow Diagram

```mermaid
flowchart LR
    subgraph Input["📥 Input"]
        Form[Form Data]
    end
    
    subgraph Processing["⚙️ Processing"]
        SP[System Prompt]
        UD[user_data dict]
    end
    
    subgraph External["☁️ External"]
        Bolna[Bolna.ai API]
        Twilio[Twilio Call]
    end
    
    subgraph Output["📤 Output"]
        WS[WebSocket]
        UI[Frontend UI]
    end
    
    Form --> SP
    Form --> UD
    SP --> UD
    UD --> Bolna
    Bolna --> Twilio
    Twilio --> Bolna
    Bolna -->|report_call_info| WS
    Bolna -->|bolna_webhook| WS
    WS --> UI
```

---

## ✅ Summary

1. **User fills form** → Frontend sends to `/start-call`
2. **Backend builds prompt** → Calls Bolna.ai API with `user_data`
3. **Bolna.ai calls business** → AI agent talks via Twilio
4. **AI reports updates** → `report_call_info` → WebSocket → Frontend
5. **Call ends** → `bolna_webhook` → `live_transcript.py` → WebSocket → Frontend
6. **User sees everything** → Real-time updates + final transcript + summary
