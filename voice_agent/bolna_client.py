"""
Bolna API Client

This module provides a clean interface to the Bolna.ai API.
Currently supports:
- Updating agent system prompt via PATCH /v2/agent/{agent_id}

Reference: https://www.bolna.ai/docs/api-reference/agent/v2/patch_update
"""

import os
import httpx
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


class BolnaClient:
    """
    Client for Bolna.ai API operations.
    
    Usage:
        client = BolnaClient()
        success = await client.update_agent_prompt(agent_id, new_prompt)
    """
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "https://api.bolna.ai"
    ):
        """
        Initialize the Bolna client.
        
        Args:
            api_key: Bolna API key (defaults to BOLNA_API_KEY env var)
            base_url: Bolna API base URL
        """
        self.api_key = api_key or os.getenv("BOLNA_API_KEY")
        self.base_url = base_url
        
        if not self.api_key:
            raise ValueError("BOLNA_API_KEY not found. Set it in .env or pass to constructor.")
    
    def _get_headers(self) -> dict:
        """Get authorization headers."""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
    
    async def update_agent_prompt(
        self,
        agent_id: str,
        system_prompt: str,
        welcome_message: Optional[str] = None,
        task_id: str = "task_1"
    ) -> bool:
        """
        Update an agent's system prompt and optional welcome message.
        
        Args:
            agent_id: The Bolna agent ID
            system_prompt: The new system prompt to set
            welcome_message: Optional custom greeting (max 35 tokens recommended)
            task_id: Task identifier (default: "task_1")
        """
        url = f"{self.base_url}/v2/agent/{agent_id}"
        
        # Construct payload according to Bolna API spec
        # User feedback: welcome message belongs in agent_config, not agent_prompts
        payload = {
            "agent_prompts": {
                task_id: {
                    "system_prompt": system_prompt
                }
            }
        }
        
        if welcome_message:
            payload["agent_config"] = {
                "agent_welcome_message": welcome_message
            }
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.patch(
                    url,
                    json=payload,
                    headers=self._get_headers()
                )
                
            
                if response.status_code == 200:
                    print(f"[BOLNA CLIENT] ✅ Agent prompt updated successfully")
                    return True
                else:
                    print(f"[BOLNA CLIENT] ❌ Failed to update prompt: {response.status_code}")
                    print(f"[BOLNA CLIENT] Response: {response.text}")
                    return False
                    
        except httpx.TimeoutException:
            print(f"[BOLNA CLIENT] ❌ Timeout updating agent prompt")
            return False
        except Exception as e:
            print(f"[BOLNA CLIENT] ❌ Error updating prompt: {e}")
            return False
    
    async def update_agent_config(
        self,
        agent_id: str,
        agent_name: Optional[str] = None,
        welcome_message: Optional[str] = None,
        webhook_url: Optional[str] = None
    ) -> bool:
        """
        Update agent configuration (name, welcome message, webhook URL).
        
        Args:
            agent_id: The Bolna agent ID
            agent_name: New agent name (optional)
            welcome_message: New welcome message (optional)
            webhook_url: New webhook URL (optional)
            
        Returns:
            True if update was successful, False otherwise
        """
        url = f"{self.base_url}/v2/agent/{agent_id}"
        
        # Build agent_config with only provided values
        agent_config = {}
        if agent_name is not None:
            agent_config["agent_name"] = agent_name
        if welcome_message is not None:
            agent_config["agent_welcome_message"] = welcome_message
        if webhook_url is not None:
            agent_config["webhook_url"] = webhook_url
        
        if not agent_config:
            print("[BOLNA CLIENT] No config values provided to update")
            return False
        
        payload = {"agent_config": agent_config}
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.patch(
                    url,
                    json=payload,
                    headers=self._get_headers()
                )
                
                if response.status_code == 200:
                    print(f"[BOLNA CLIENT] ✅ Agent config updated")
                    return True
                else:
                    print(f"[BOLNA CLIENT] ❌ Failed: {response.status_code} - {response.text}")
                    return False
                    
        except Exception as e:
            print(f"[BOLNA CLIENT] ❌ Error: {e}")
            return False


# Singleton instance
_bolna_client: Optional[BolnaClient] = None

def get_bolna_client() -> BolnaClient:
    """Get the global Bolna client instance."""
    global _bolna_client
    if _bolna_client is None:
        _bolna_client = BolnaClient()
    return _bolna_client


async def update_agent_prompt(agent_id: str, system_prompt: str) -> bool:
    """
    Convenience function to update an agent's system prompt.
    
    Args:
        agent_id: The Bolna agent ID
        system_prompt: The new system prompt
        
    Returns:
        True if successful, False otherwise
    """
    client = get_bolna_client()
    return await client.update_agent_prompt(agent_id, system_prompt)
