import requests
from bs4 import BeautifulSoup

CATALOGUE_URL = "https://www.insa-lyon.fr/fr/formation"

headers = {
    "User-Agent": "Mozilla/5.0 (compatible; INSA-ECTS-Scraper/1.0)"
}

resp = requests.get(CATALOGUE_URL, headers=headers)
resp.raise_for_status()

soup = BeautifulSoup(resp.text, "html.parser")

print("Page récupérée ✅")
print("Titre:", soup.title.text)
