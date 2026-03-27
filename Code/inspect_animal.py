#!/usr/bin/env python3
"""
Inspect a single animal from RescueGroups API by ID.
Usage: python Code/inspect_animal.py <animal_id>
Example: python Code/inspect_animal.py 15572972
"""

import os
import sys
import json
import requests

RESCUEGROUPS_API_KEY = os.environ["RESCUE_GROUP_API_KEY"]
animal_id = sys.argv[1] if len(sys.argv) > 1 else "15572972"

headers = {
    "Authorization": RESCUEGROUPS_API_KEY,
    "Content-Type": "application/vnd.api+json"
}

response = requests.get(
    f"https://api.rescuegroups.org/v5/public/animals/{animal_id}?include[]=pictures&include[]=orgs&include[]=locations",
    headers=headers,
    timeout=30
)
print(f"Status: {response.status_code}")
data = response.json()

# Print animal attributes
animal = data.get("data", [])
if isinstance(animal, list):
    animal = animal[0] if animal else {}

print("\n--- Animal Attributes ---")
for key, value in animal.get("attributes", {}).items():
    print(f"{key}: {value}")

# Print included data
print("\n--- Included ---")
for item in data.get("included", []):
    print(f"\nType: {item.get('type')} | ID: {item.get('id')}")
    for key, value in item.get("attributes", {}).items():
        print(f"  {key}: {value}")
