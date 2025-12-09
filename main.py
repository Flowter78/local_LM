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

# Préfixes de codes de départements (à étendre si besoin)
DEPT_PREFIXES = ["GE", "GM", "GCU", "GI", "MAT", "TC", "BIO"]


# ------------------------------
#  Utils
# ------------------------------

def normalize(s: str) -> str:
    if s is None:
        return ""
    return re.sub(r"\s+", " ", s).strip()


def parse_hours(line: str) -> str:
    """
    Extrait quelque chose comme "12h" depuis une ligne
    """
    if line is None:
        return ""
    m = re.search(r"(\d+\s*h)", line)
    return m.group(1).replace(" ", "") if m else ""


def guess_year_from_label(label: str) -> str:
    m = re.search(r"(20\d{2}-20\d{2})", label)
    return m.group(1) if m else ""


def guess_dept_from_label(label: str) -> str:
    d = label.lower()
    if "électrique" in d:
        return "GE"
    if "mécanique" in d:
        return "GM"
    if "civil" in d or "urbain" in d:
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


# ------------------------------
#  Extraction cours depuis un PDF (structure type GE / IF / BIO / etc.)
# ------------------------------

def extract_courses_from_pdf(label: str, path: str):
    print(f"[PDF] Extraction depuis : {os.path.basename(path)}")
    year = guess_year_from_label(label)
    dept = guess_dept_from_label(label)

    with pdfplumber.open(path) as pdf:
        text = ""
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                text += t + "\n"

    lines = [l.rstrip() for l in text.splitlines()]

    filiere = ""
    last_nonempty = ""
    current = None
    mode = None  # None, "eval", "contact"
    results = []

    STOP_SECTIONS = {
        "SUPPORTS",
        "OBJECTIFS",
        "MOTS-CLÉS",
        "MOTS-CLES",
        "PRÉREQUIS",
        "PREREQUIS",
        "CONTENU",
        "COMPÉTENCES",
        "COMPETENCES",
    }

    def push_current():
        nonlocal current
        if current is None:
            return
        code = current.get("code", "")
        if not code:
            return

        # On garde les codes qui correspondent à nos départements
        if not any(code.startswith(pref + "-") for pref in DEPT_PREFIXES):
            return

        results.append(current)
        current = None

    for raw in lines:
        line = raw.strip()
        if line:
            last_nonempty = line

        upper = line.upper()

        # Ligne filière, ex : "Ingénieur, spécialité génie électrique"
        if "INGÉNIEUR" in upper or "INGENIEUR" in upper:
            if "SPÉCIALITÉ" in upper or "SPECIALITE" in upper:
                filiere = line
                continue

        # Début d'une nouvelle fiche EC
        if line == "IDENTIFICATION":
            # Sauvegarde de la fiche précédente
            push_current()

            current = {
                "departement": dept,
                "annee": year,
                "catalogue_label": label,
                "fichier_pdf": os.path.basename(path),
                "filiere": filiere,
                "code": "",
                "titre": last_nonempty,  # la ligne juste avant "IDENTIFICATION"
                "ects": "",
                "cours_h": "",
                "td_h": "",
                "tp_h": "",
                "projet_h": "",
                "evaluation_h": "",
                "face_a_face_h": "",
                "travail_perso_h": "",
                "total_h": "",
                "evaluation_texte": "",
                "contact": "",
            }
            mode = None
            continue

        # Si on n'est pas dans une fiche, on ignore
        if current is None:
            continue

        # ----- Heures / champs simples -----

        # CODE
        if line.startswith("CODE"):
            m = re.search(r"CODE\s*:\s*(.+)", line)
            if m:
                current["code"] = normalize(m.group(1))
            continue

        # ECTS
        if line.startswith("ECTS"):
            m = re.search(r"ECTS\s*:\s*([\d\.,]+)", line)
            if m:
                current["ects"] = m.group(1).replace(",", ".").strip()
            continue

        # Horaires (HORAIRES)
        if line.startswith("Cours"):
            current["cours_h"] = parse_hours(line)
            continue
        if line.startswith("TD"):
            current["td_h"] = parse_hours(line)
            continue
        if line.startswith("TP"):
            current["tp_h"] = parse_hours(line)
            continue
        if line.startswith("Projet"):
            current["projet_h"] = parse_hours(line)
            continue
        # Attention : "Evaluation" (heure) vs rubrique "EVALUATION"
        if line.startswith("Evaluation") or line.startswith("Évaluation"):
            # Si ce n'est pas la rubrique en majuscules
            if upper != "EVALUATION":
                current["evaluation_h"] = parse_hours(line)
                continue

        if line.startswith("Face à face pédagogique") or line.startswith("Face-à-face pédagogique"):
            current["face_a_face_h"] = parse_hours(line)
            continue
        if line.startswith("Travail personnel"):
            current["travail_perso_h"] = parse_hours(line)
            continue
        if line.startswith("Total"):
            current["total_h"] = parse_hours(line)
            continue

        # ----- Rubrique EVALUATION (texte) -----
        if upper == "EVALUATION":
            mode = "eval"
            continue

        # ----- Rubrique CONTACT -----
        if upper == "CONTACT":
            mode = "contact"
            continue

        # Fin d'une rubrique (EVAL / CONTACT) dès qu'on tombe sur un nouveau bloc
        if upper in STOP_SECTIONS:
            mode = None
            continue

        # Contenu des rubriques
        if mode == "eval":
            if line:
                if current["evaluation_texte"]:
                    current["evaluation_texte"] += " "
                current["evaluation_texte"] += line
            continue

        if mode == "contact":
            if line:
                if current["contact"]:
                    current["contact"] += " "
                current["contact"] += line
            continue

    # Pousser la dernière fiche éventuelle
    push_current()

    print(f"[PDF] {len(results)} fiches extraites")
    return results


# ------------------------------
#  1) Scraper la page catalogue
# ------------------------------

resp = requests.get(CATALOGUE_URL, headers=headers)
print("Status code:", resp.status_code)
print("URL finale:", resp.url)
resp.raise_for_status()

soup = BeautifulSoup(resp.text, "html.parser")
print("Page catalogue récupérée ✅")
print("Titre:", soup.title.text)
print()

# ------------------------------
#  2) Récupérer les pages des formations ingénieur
# ------------------------------

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

# ------------------------------
#  3) Trouver les PDF "Catalogue ..."
# ------------------------------

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

# ------------------------------
#  4) Télécharger les PDF dans ./pdf_insa
# ------------------------------

os.makedirs("pdf_insa", exist_ok=True)
local_pdfs = []

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

# ------------------------------
#  5) Extraction de toutes les fiches
# ------------------------------

all_rows = []

for label, path in local_pdfs:
    rows = extract_courses_from_pdf(label, path)
    all_rows.extend(rows)

print(f"Total global de fiches extraites : {len(all_rows)}")

# ------------------------------
#  6) Sauvegarde CSV
# ------------------------------

output_csv = "ects_insa.csv"

fieldnames = [
    "departement",
    "annee",
    "catalogue_label",
    "fichier_pdf",
    "filiere",
    "code",
    "titre",
    "ects",
    "cours_h",
    "td_h",
    "tp_h",
    "projet_h",
    "evaluation_h",
    "face_a_face_h",
    "travail_perso_h",
    "total_h",
    "evaluation_texte",
    "contact",
]

with open(output_csv, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(all_rows)

print(f"✅ Écrit {output_csv} avec {len(all_rows)} lignes")
