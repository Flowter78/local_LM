import os
import re
import csv
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import pdfplumber

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
# ==========================
formation_urls = []

for a in soup.find_all("a", href=True):
    href = a["href"]
    text = a.get_text(strip=True)

    if not href.startswith("/fr/formation/"):
        continue
    if href == "/fr/formation/catalogue":
        continue

    if "Formation Ingénieur" in text:
        full_url = urljoin(BASE_URL, href)
        formation_urls.append(full_url)

formation_urls = sorted(set(formation_urls))

print("Formations Ingénieur détectées :")
for url in formation_urls:
    print(" -", url)
print()

# ==========================
# 3) Parcourir chaque page de formation et récupérer les PDF
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

        if href.lower().endswith(".pdf") and "catalogue" in text.lower():
            pdf_url = urljoin(BASE_URL, href)
            catalogue_pdfs.append((text, pdf_url))
            print(f"  -> trouvé : {text}  =>  {pdf_url}")

    print()

print("==== RÉSUMÉ DES CATALOGUES PDF TROUVÉS ====")
for text, pdf_url in catalogue_pdfs:
    print(f"- {text} : {pdf_url}")
print(f"\nTotal PDF trouvés : {len(catalogue_pdfs)}")
print()

# ==========================
# 4) Téléchargement des PDF dans ./pdf_insa
# ==========================
os.makedirs("pdf_insa", exist_ok=True)

local_pdfs = []  # (label, local_path)

for label, url in catalogue_pdfs:
    filename = url.split("/")[-1]
    local_path = os.path.join("pdf_insa", filename)

    if not os.path.exists(local_path):
        print(f"Téléchargement de {label} ...")
        r = requests.get(url, headers=headers)
        r.raise_for_status()
        with open(local_path, "wb") as f:
            f.write(r.content)
    else:
        print(f"Déjà présent : {filename}")

    local_pdfs.append((label, local_path))

print("\nTous les PDF sont téléchargés ✅\n")


# ==========================
# 5) Extraction des ECTS avec pdfplumber
# ==========================

def normalize(s):
    if s is None:
        return ""
    return re.sub(r"\s+", " ", s).strip()

def guess_year_from_label(label: str) -> str:
    m = re.search(r"(20\d{2}-20\d{2})", label)
    return m.group(1) if m else ""

def guess_dept_from_label(label: str) -> str:
    # ultra simple, tu peux affiner au besoin
    d = label.lower()
    if "électrique" in d:
        return "GE"
    if "mécanique" in d:
        return "GM"
    if "civil" in d:
        return "GCU"
    if "industriel" in d:
        return "GI"
    if "matériaux" in d:
        return "MAT"
    if "télécommunications" in d or "tc" in d:
        return "TC"
    if "biotechnologies" in d:
        return "BIO"
    return ""


def extract_ects_from_pdf(label, path):
    print(f"Extraction depuis : {path}")
    results = []
    year = guess_year_from_label(label)
    dept = guess_dept_from_label(label)

    with pdfplumber.open(path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            try:
                tables = page.extract_tables()
            except Exception as e:
                print(f"  [!] Erreur extraction tables page {page_num}: {e}")
                continue

            if not tables:
                continue

            for table in tables:
                if not table or len(table) < 2:
                    continue

                header = [normalize(c) for c in table[0]]
                # Chercher les colonnes qui nous intéressent
                idx_ects = None
                idx_code = None
                idx_title = None

                for i, col in enumerate(header):
                    low = col.lower()
                    if "ects" in low:
                        idx_ects = i
                    if "code" in low or "ec" == low.lower():
                        idx_code = i
                    if ("intitulé" in low) or ("titre" in low) or ("libellé" in low):
                        idx_title = i

                # Si pas de colonne ECTS, cette table ne nous intéresse pas
                if idx_ects is None:
                    continue

                # fallback au cas où
                if idx_title is None and len(header) >= 2:
                    idx_title = 1

                for row in table[1:]:
                    if row is None or all(c is None for c in row):
                        continue

                    # sécuriser les indices
                    ects_val = normalize(row[idx_ects]) if idx_ects < len(row) else ""
                    if ects_val == "":
                        continue

                    code_val = normalize(row[idx_code]) if idx_code is not None and idx_code < len(row) else ""
                    title_val = normalize(row[idx_title]) if idx_title is not None and idx_title < len(row) else ""

                    # Nettoyage ECTS -> float ou texte brut
                    ects_clean = ects_val.replace(",", ".")
                    try:
                        ects_float = float(re.findall(r"[\d\.]+", ects_clean)[0])
                    except Exception:
                        ects_float = None

                    results.append({
                        "departement": dept,
                        "annee": year,
                        "catalogue_label": label,
                        "fichier_pdf": os.path.basename(path),
                        "page": page_num,
                        "code": code_val,
                        "titre": title_val,
                        "ects": ects_float if ects_float is not None else ects_val,
                    })

    print(f"  -> {len(results)} lignes extraites\n")
    return results


all_rows = []
for label, path in local_pdfs:
    rows = extract_ects_from_pdf(label, path)
    all_rows.extend(rows)

# ==========================
# 6) Sauvegarde dans ects_insa.csv
# ==========================
output_csv = "ects_insa.csv"
fieldnames = [
    "departement",
    "annee",
    "catalogue_label",
    "fichier_pdf",
    "page",
    "code",
    "titre",
    "ects",
]

with open(output_csv, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(all_rows)

print(f"✅ Écrit {output_csv} avec {len(all_rows)} lignes")
