import requests
import json

urls = [
    "https://www.loc.gov/item/sn87080287/1893-01-02/ed-1?sp=1&fo=json",
    "https://www.loc.gov/resource/sn87080287/1893-01-02/ed-1/?sp=1&fo=json"
]

for url in urls:
    print(f"\nFetching {url}")
    resp = requests.get(url)
    data = resp.json()

    print("KEYS:", data.keys())
    print("fulltext_service:", data.get('fulltext_service'))
    resources = data.get('resources', [])
    if resources:
        print("RESOURCES[0] KEYS:", resources[0].keys())
        print("word_coordinates:", resources[0].get('word_coordinates'))
    else:
        print("NO RESOURCES FOUND")
