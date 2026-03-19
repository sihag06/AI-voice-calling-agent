"""
Live Transcript Module for Bolna.ai Integration

This module handles near-live transcript streaming using two methods:
1. Webhook-based: Receives transcript updates via webhooks from Bolna.ai
2. Polling-based: Periodically polls Bolna.ai API for transcript updates

Both methods process transcripts and send them to the frontend via WebSocket in real-time.
"""

import asyncio
import hashlib
import json
import re
from typing import Dict, Optional, List, Any, Set
from datetime import datetime
import httpx
import traceback


class LiveTranscriptHandler:
    """
    Handles live transcript streaming from Bolna.ai using webhooks and polling.
    """
    
    def __init__(
        self,
        connection_manager,
        conversation_store: Dict[str, list],
        bolna_to_app_call: Dict[str, str],
        bolna_api_key: str,
        bolna_base_url: str = "https://api.bolna.ai"
    ):
        """
        Initialize the Live Transcript Handler.
        
        Args:
            connection_manager: WebSocket connection manager instance
            conversation_store: Dictionary to store final transcripts (app_call_id -> list)
            bolna_to_app_call: Mapping from Bolna execution_id to app_call_id
            bolna_api_key: Bolna.ai API key
            bolna_base_url: Bolna.ai API base URL
        """
        self.manager = connection_manager
        self.conversation_store = conversation_store
        self.bolna_to_app_call = bolna_to_app_call
        self.bolna_api_key = bolna_api_key
        self.bolna_base_url = bolna_base_url

        
        # Track extracted information per call to avoid duplicates
        self.extracted_info: Dict[str, Dict[str, Set[str]]] = {}
        # Structure: {app_call_id: {"prices": set(), "available_facilities": set(), "unavailable_facilities": set()}}
        
        # Track during-call AI responses separately from post-call transcript
        # This prevents duplicate detection from blocking legitimate during-call messages
        self.during_call_ai_responses: Dict[str, Set[str]] = {}  # app_call_id -> set of content hashes
        
        # Track current post-call batch to avoid duplicates within the same final transcript
        self._current_postcall_batch: Dict[str, Set[str]] = {}  # app_call_id -> set of content hashes in current batch
        
        # Track which calls have already had their final transcript sent (prevent duplicates)
        self._final_transcript_sent: Set[str] = set()  # app_call_id -> True if final transcript already sent
        
        # Track which AI messages we've sent during call by position and content
        # This helps catch all AI responses even if transcript is processed multiple times
        self._sent_ai_messages: Dict[str, Set[str]] = {}  # app_call_id -> set of (position:content_hash) strings
        
    # ========== WEBHOOK-BASED TRANSCRIPT PROCESSING ==========
    
    async def process_webhook_transcript(
        self,
        body: dict,
        app_call_id: str
    ) -> bool:
        """
        Process transcript data from a webhook event.
        
        DURING CALL: Only extracts AI/assistant responses (for real-time display)
        POST-CALL: Extracts ALL messages (AI + Owner) from final transcript
        
        Args:
            body: Webhook request body from Bolna.ai
            app_call_id: Application call ID
            
        Returns:
            True if transcript was processed, False otherwise
        """
        try:
            # Check if this is a final transcript (call ended)
            status = (
                body.get("status") or
                body.get("call_status") or
                body.get("message", {}).get("status") or
                ""
            ).lower()
            
            is_call_ended = status in ["ended", "completed", "finished", "call-disconnected", "disconnected"]
            final_transcript_already_sent = app_call_id in self._final_transcript_sent
            
            # CRITICAL: ALWAYS try to extract AI responses from transcript FIRST (for during-call display)
            # This works even if status says "completed" - we extract AI responses before sending final transcript
            raw_transcript = body.get("transcript") or body.get("transcription") or ""
            raw_conversation = (
                body.get("conversation") or
                body.get("messages") or
                body.get("message", {}).get("conversation") or
                body.get("message", {}).get("messages") or
                body.get("data", {}).get("conversation") or
                []
            )
            
            # Parse string transcript format: "role: message\nrole: message"
            if isinstance(raw_transcript, str) and raw_transcript.strip():
                parsed_messages = self._parse_string_transcript(raw_transcript)
                if parsed_messages:
                    raw_conversation = parsed_messages
            
            # Normalize to list format
            if isinstance(raw_conversation, dict):
                raw_conversation = [raw_conversation]
            elif not isinstance(raw_conversation, list):
                raw_conversation = []
            
            # NOTE: Intermediate/Live transcript updates (previously STEP 1) 
            # have been removed as per request to disable "live transcript" flow.
            # We now only process the FINAL transcript when the call ends.

            
            # STEP 2: If call ended and final transcript not sent, send full transcript
            if is_call_ended and not final_transcript_already_sent:
                if not raw_conversation:
                    # If we didn't get conversation data, return (we already tried to extract AI responses above)
                    return False
                
                print(f"[FINAL TRANSCRIPT] 📋 Call ended, processing FULL transcript (AI + Owner)...")
                
                # CRITICAL: Clear previous messages (AI-only from during call) before sending final transcript
                # This prevents duplicate AI messages from appearing
                clear_message = {
                    "type": "clear_conversation",
                    "reason": "final_transcript"
                }
                try:
                    await self.manager.send_to_app_call(app_call_id, clear_message)
                    print(f"[FINAL TRANSCRIPT] ✅ Sent clear_conversation message to frontend")
                except Exception as e:
                    print(f"[FINAL TRANSCRIPT] ⚠️ Error sending clear message: {e}")
                
                # Process and send ALL messages (AI + Owner) from final transcript
                # Clear any previous batch tracking for this call to ensure all messages are sent
                if hasattr(self, '_current_postcall_batch'):
                    self._current_postcall_batch.pop(app_call_id, None)
                await self.process_conversation_update(app_call_id, raw_conversation, ai_only=False, is_during_call=False)
                
                # Mark final transcript as sent
                self._final_transcript_sent.add(app_call_id)
                print(f"[FINAL TRANSCRIPT] ✅ Marked final transcript as sent for {app_call_id}")
                return True
            elif is_call_ended and final_transcript_already_sent:
                print(f"[FINAL TRANSCRIPT] ⏭️ Final transcript already sent for {app_call_id}, skipping duplicate")
                return False
            
            # If we processed AI responses (even if call not ended), return True
            if raw_conversation:
                return True
            
            return False
            
        except Exception as e:
            print(f"[TRANSCRIPT] ❌ ERROR processing webhook transcript: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def _parse_string_transcript(self, transcript: str) -> List[dict]:
        """Parse transcript string in format 'role: message\\nrole: message'"""
        parsed_messages = []
        lines = transcript.strip().split('\n')
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if ':' in line:
                parts = line.split(':', 1)
                if len(parts) == 2:
                    role = parts[0].strip().lower()
                    text = parts[1].strip()
                    if text:
                        parsed_messages.append({
                            "role": role,
                            "content": text,
                            "text": text
                        })
        return parsed_messages
    
    
    async def process_conversation_update(
        self,
        app_call_id: str,
        raw_conversation: List[dict],
        ai_only: bool = True,
        is_during_call: bool = False
    ):
        """
        Process conversation updates and send messages to frontend.
        """
        
        # 1. Filter out tool calls to get a stable list
        canonical_messages = []
        for item in raw_conversation:
            if not isinstance(item, dict): continue
            
            role = (item.get("role") or item.get("speaker") or item.get("type") or item.get("sender") or "").lower()
            text = (item.get("content") or item.get("text") or item.get("message") or item.get("transcript") or item.get("utterance") or "")
            text = str(text).strip() if text else ""
            
            # Skip hidden roles
            if role in ["tool_calls", "tool", "function", "system_prompt", "system"]:
                continue
            if not text:
                continue
                
            canonical_messages.append({
                "role": role,
                "text": text
            })
            
        if not canonical_messages:
            return

        messages_sent = 0
        
        # 2. Iterate using STABLE indices
        # This index 'i' corresponds to the message's position in the full conversation history (excluding tools).
        for i, item in enumerate(canonical_messages):
            role = item["role"]
            text = item["text"]
            
            # If in AI-only mode, we ONLY send AI messages, but we MUST respect the canonical index 'i'
            # to ensures that if we later send the full transcript, the AI message IDs remain consistent.
            if ai_only:
                if role not in ["assistant", "agent", "ai", "bot"]:
                    continue

            normalized_text = self._normalize_text(text)
            speaker = self._map_role_to_speaker(role)
            
            # 3. Check for updates using index-based tracking
            # This handles streaming updates (e.g. "He" -> "Hello") by overwriting the message at index 'i'
            if self.manager.has_message_changed(app_call_id, i, normalized_text):
                
                message_payload = {
                    "type": "conversation_update",
                    "speaker": speaker,
                    "text": normalized_text,
                    "index": i,         # <--- Critical for frontend to identify which bubble to update
                    "partial": True     # <--- tells frontend to look for msg-{index}
                }
                
                try:
                    await self.manager.send_to_app_call(app_call_id, message_payload)
                    messages_sent += 1
                    # Log removed to reduce noise
                except Exception as e:
                    print(f"[TRANSCRIPT] ❌ Error sending message {i}: {e}")
            
        if messages_sent > 0:
            print(f"[{'AI' if ai_only else 'TRANSCRIPT'}] ✅ Sync complete. Updated {messages_sent} message(s).")
        
        # Store messages in conversation store
        if app_call_id not in self.conversation_store:
            self.conversation_store[app_call_id] = []
        
        for item in clean_messages:
            self.conversation_store[app_call_id].append({
                "speaker": self._map_role_to_speaker(item.get("role", "")),
                "text": self._normalize_text(item.get("text", "")),
                "timestamp": datetime.utcnow().isoformat()
            })
    
    # Polling-based transcript fetching removed (webhook only)

    

    
    # ========== HELPER METHODS ==========
    
    def _map_role_to_speaker(self, role: str) -> str:
        """Map role to speaker name for UI"""
        role_lower = role.lower()
        if role_lower in ["assistant", "agent", "ai", "bot"]:
            return "AI"
        elif role_lower in ["user", "customer", "caller", "owner", "human"]:
            return "Owner"
        else:
            return "Owner"  # Default
    
    def _normalize_text(self, text: str) -> str:
        """Normalize text for display"""
        if not text:
            return ""
        # Remove extra whitespace
        text = " ".join(text.split())
        return text.strip()
    


