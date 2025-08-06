import os
import requests
import openai
from dotenv import load_dotenv

# Load credentials from .env
load_dotenv()

# ClickSend credentials
clicksend_user = os.getenv("CLICKSEND_USERNAME")
clicksend_key = os.getenv("CLICKSEND_API_KEY")
to_number = os.getenv("TO_NUMBER")

# OpenAI key for legacy SDK
openai.api_key = os.getenv("OPENAI_API_KEY")

# Ask the user a question
question = input("📨 What do you want to ask ChatGPT via SMS?\n> ")

# Legacy-style GPT call (OpenAI v0.28)
def ask_gpt(message):
    response = openai.ChatCompletion.create(
        model="gpt-4",  # or "gpt-3.5-turbo"
        messages=[{"role": "user", "content": message}],
        max_tokens=300
    )
    return response.choices[0].message.content.strip()

# Get response from GPT
gpt_response = ask_gpt(question)

# Prepare SMS payload
sms_payload = {
    "messages": [
        {
            "source": "python",
            "body": gpt_response[:1600],
            "to": to_number,
            "custom_string": "gpt_response"
        }
    ]
}

# Send SMS
response = requests.post(
    "https://rest.clicksend.com/v3/sms/send",
    auth=(clicksend_user, clicksend_key),
    headers={"Content-Type": "application/json"},
    json=sms_payload
)

# Debug
print("\n✅ SMS Sent!")
print("📤 Question:", question)
print("🤖 GPT Response:\n", gpt_response)
print("📦 SMS Status:", response.status_code)
print("📦 SMS API Response:", response.text)
