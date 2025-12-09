import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

BASE_URL = "https://www.insa-lyon.fr"
CATALOGUE_URL = f"{BASE_URL}/fr/formation/catalogue"

headers = {
    "User-Agent": "Mozilla/5.0 (compatible; INSA-ECTS-Scraper/1.0)"
}

# ==========================
# 1) Récupération de la page catalogue
# ==========================
resp = requests.get(CATALOGUE_URL, headers=headers)
print("Status code:", resp.status_code)
print("URL finale:", resp.url)
resp.raise_for_status()

soup = BeautifulSoup(resp.text, "html.parser")
print("Page catalogue récupérée ✅")
print("Titre:", soup.title.text)
print()

# ==========================
# 2) Récupérer les pages des formations ingénieur
#    (Génie électrique, génie méca, info, etc.)
# ==========================
formation_urls = []

for a in soup.find_all("a", href=True):
    href = a["href"]
    text = a.get_text(strip=True)

    # On ne garde que les liens "formation/..." mais pas le catalogue lui-même
    if not href.startswith("/fr/formation/"):
        continue
    if href == "/fr/formation/catalogue":
        continue

    # On filtre sur le texte pour ne garder que les formations ingénieur
    # ex : "Formation Ingénieur Génie Électrique", "Formation Ingénieur Informatique", etc.
    if "Formation Ingénieur" in text:
        full_url = urljoin(BASE_URL, href)
        formation_urls.append(full_url)

# On enlève les doublons au cas où
formation_urls = sorted(set(formation_urls))

print("Formations Ingénieur détectées :")
for url in formation_urls:
    print(" -", url)
print()

# ==========================
# 3) Parcourir chaque page de formation
#    et récupérer les PDF de type "Catalogue ..."
# ==========================
catalogue_pdfs = []

for url in formation_urls:
    print(f"Analyse de la page : {url}")
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    s = BeautifulSoup(r.text, "html.parser")

    for a in s.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)

        # On cherche des liens vers des PDF avec "catalogue" dans le texte
        if href.lower().endswith(".pdf") and "catalogue" in text.lower():
            pdf_url = urljoin(BASE_URL, href)
            catalogue_pdfs.append((text, pdf_url))
            print(f"  -> trouvé : {text}  =>  {pdf_url}")

    print()

# ==========================
# 4) Résumé
# ==========================
print("==== RÉSUMÉ DES CATALOGUES PDF TROUVÉS ====")
for text, pdf_url in catalogue_pdfs:
    print(f"- {text} : {pdf_url}")

print(f"\nTotal PDF trouvés : {len(catalogue_pdfs)}")
