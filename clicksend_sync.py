#!/usr/bin/env python3
"""
ClickSend Contact List Sync Script for Hey Alex
Usage: 
  python clicksend_sync.py sync
  python clicksend_sync.py lists
  python clicksend_sync.py broadcast <list_id> "Your message"
"""

import requests
import sys
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
BASE_URL = os.getenv("APP_URL", "https://your-app.onrender.com")
API_KEY = os.getenv("BROADCAST_API_KEY", "your-secret-key-here")

def get_contact_lists():
    """Get all ClickSend contact lists"""
    print("📋 Getting ClickSend contact lists...")
    
    response = requests.get(
        f"{BASE_URL}/clicksend/lists",
        headers={"X-API-Key": API_KEY}
    )
    
    if response.status_code == 200:
        data = response.json()
        lists = data.get("lists", [])
        
        print(f"📊 Found {len(lists)} contact lists:")
        for contact_list in lists:
            print(f"  📋 {contact_list['list_name']} (ID: {contact_list['list_id']}) - {contact_list.get('contact_count', 0)} contacts")
        
        return lists
    else:
        print(f"❌ Failed to get lists: {response.status_code} - {response.text}")
        return []

def sync_whitelist():
    """Sync whitelist to ClickSend"""
    print("🔄 Syncing whitelist to ClickSend...")
    
    response = requests.post(
        f"{BASE_URL}/clicksend/sync",
        headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
        json={
            "list_name": "Hey Alex Subscribers"
        }
    )
    
    if response.status_code == 200:
        data = response.json()
        print(f"✅ Sync completed!")
        print(f"📊 List: {data['list_name']} (ID: {data['list_id']})")
        print(f"📤 Synced: {data['total_contacts']} contacts")
        print(f"📈 Successful batches: {data['successful_batches']}/{len(data['batches'])}")
        
        if data['successful_batches'] < len(data['batches']):
            print("⚠️ Some batches failed. Check logs for details.")
        
        return data['list_id']
    else:
        print(f"❌ Sync failed: {response.status_code} - {response.text}")
        return None

def send_clicksend_broadcast(list_id, message):
    """Send broadcast via ClickSend contact list"""
    print(f"📢 Sending broadcast to ClickSend list {list_id}...")
    print(f"📝 Message: {message}")
    
    response = requests.post(
        f"{BASE_URL}/clicksend/broadcast",
        headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
        json={
            "list_id": int(list_id),
            "message": message
        }
    )
    
    if response.status_code == 200:
        data = response.json()
        print(f"✅ Broadcast sent via ClickSend!")
        return True
    else:
        print(f"❌ Broadcast failed: {response.status_code} - {response.text}")
        return False

def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python clicksend_sync.py sync")
        print("  python clicksend_sync.py lists") 
        print("  python clicksend_sync.py broadcast <list_id> \"Your message\"")
        return
    
    command = sys.argv[1].lower()
    
    if command == "lists":
        get_contact_lists()
        
    elif command == "sync":
        # First show current lists
        print("📋 Current ClickSend lists:")
        existing_lists = get_contact_lists()
        
        # Ask for confirmation
        confirm = input(f"\n❓ Sync whitelist to ClickSend? This will update/create 'Hey Alex Subscribers' list. (y/N): ")
        
        if confirm.lower() in ['y', 'yes']:
            list_id = sync_whitelist()
            if list_id:
                print(f"🎉 Success! Your whitelist is now synced to ClickSend list ID: {list_id}")
                print(f"💡 You can now send broadcasts via ClickSend dashboard or API")
        else:
            print("📝 Sync cancelled")
            
    elif command == "broadcast":
        if len(sys.argv) < 4:
            print("Usage: python clicksend_sync.py broadcast <list_id> \"Your message\"")
            print("\nAvailable lists:")
            get_contact_lists()
            return
        
        list_id = sys.argv[2]
        message = " ".join(sys.argv[3:])
        
        # Confirm broadcast
        confirm = input(f"\n❓ Send '{message}' via ClickSend list {list_id}? (y/N): ")
        
        if confirm.lower() in ['y', 'yes']:
            send_clicksend_broadcast(list_id, message)
        else:
            print("📝 Broadcast cancelled")
    
    else:
        print(f"❌ Unknown command: {command}")
        print("Available commands: sync, lists, broadcast")

if __name__ == "__main__":
    main()
