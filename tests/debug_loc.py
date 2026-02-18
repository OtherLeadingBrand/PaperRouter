import requests
import json

url = "https://www.loc.gov/item/sn87080287/1893-04-20/ed-1?sp=1&fo=json"
print(f"Fetching {url}...")
resp = requests.get(url)
data = resp.json()

# Print top level keys
print(f"Keys: {list(data.keys())}")

# Check for fulltext
if 'fulltext_service' in data:
    print(f"Found fulltext_service: {data['fulltext_service']}")
else:
    print("fulltext_service NOT found in top level.")

# Check resources
resources = data.get('resources', [])
print(f"Number of resources: {len(resources)}")
for i, res in enumerate(resources):
    print(f"Resource {i} keys: {list(res.keys())}")
    if 'text' in res:
        print(f"  Found text: {res['text']}")
    if 'pdf' in res:
        print(f"  Found pdf: {res['pdf']}")

# Check for other potential keys
for key in ['segments', 'pages', 'issue']:
    if key in data:
        print(f"Found {key}")

# Print the whole JSON to a file for deeper inspection
with open('debug_loc_resp.json', 'w') as f:
    json.dump(data, f, indent=2)
print("Saved full response to debug_loc_resp.json")
