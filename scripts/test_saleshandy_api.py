"""
Saleshandy API Connection Test.

Tests authentication using SALESHANDY_API_KEY and fetches active sequences / campaign IDs.
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("SALESHANDY_API_KEY", "343e99cb89325fcf6199f20ac70ca54b")

# Test common Saleshandy API endpoints
ENDPOINTS = [
    "https://open-api.saleshandy.com/v1/sequences",
    "https://open-api.saleshandy.com/v1/prospects",
    "https://api.saleshandy.com/v1/sequences",
]

HEADERS_VARIANTS = [
    {"x-api-key": API_KEY, "Accept": "application/json"},
    {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"},
    {"Authorization": API_KEY, "Accept": "application/json"},
    {"api-key": API_KEY, "Accept": "application/json"},
]


def test_saleshandy_connection():
    print("=" * 70)
    print("TESTING SALESHANDY API AUTHENTICATION")
    print(f"API Key: {API_KEY[:6]}...{API_KEY[-4:]}")
    print("=" * 70)

    success = False
    for url in ENDPOINTS:
        for i, headers in enumerate(HEADERS_VARIANTS, 1):
            try:
                resp = requests.get(url, headers=headers, timeout=10)
                print(f"Testing URL: {url} | Header Auth #{i} -> HTTP {resp.status_code}")

                if resp.status_code in (200, 201):
                    print(f"\nSUCCESS! Authenticated with {url}")
                    data = resp.json()
                    print("Response Payload Sample:")
                    print(data)
                    success = True
                    return data
                elif resp.status_code not in (404, 401):
                    print(f"  Response ({resp.status_code}): {resp.text[:200]}")
            except Exception as e:
                print(f"  Error connecting to {url}: {e}")

    if not success:
        print("\nCould not connect to Saleshandy API with default endpoints. Checking standard API docs...")


if __name__ == "__main__":
    test_saleshandy_connection()
