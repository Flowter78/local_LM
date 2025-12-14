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

def guess_semestre_from_code(code: str) -> str:
    if not code:
        return ""
    m = re.search(r"-S([12])-", code)
    return f"S{m.group(1)}" if m else ""

def guess_ue_code_from_objectifs(objectifs: str) -> str:
    if not objectifs:
        return ""
    # cherche un token du type GI-3-S1-UE-CPSI ou GE-4-S2-UE-XXXX
    m = re.search(r"\b([A-Z]{2,4}-[345]-S[12]-UE-[A-Z0-9]+)\b", objectifs)
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


def guess_niveau_from_code(code: str) -> str:
    """
    Déduit 3A / 4A / 5A à partir du code EC.
    Exemple : GE-3-S1-EC-MA1 -> 3A
    """
    if not code:
        return ""
    m = re.search(r"-([345])-", code)
    if m:
        return f"{m.group(1)}A"
    return ""


def clean_pdf_garbage(txt: str) -> str:
    """
    Nettoyage léger des caractères bizarres issus de certains PDF.
    (Tu peux enrichir si besoin.)
    """
    if not txt:
        return ""
    # caractères invisibles / remplacement basique
    txt = txt.replace("\x00", "")
    txt = txt.replace("\uFFFD", "")  # �
    txt = txt.replace("", "")       # cas rencontré parfois
    return txt.strip()


# ------------------------------
#  Extraction cours depuis un PDF (structure type GE / IF / BIO / etc.)
# ------------------------------

def extract_courses_from_pdf(label: str, path: str):
    print(f"[PDF] Extraction depuis : {os.path.basename(path)}")
    year = guess_year_from_label(label)
    dept = guess_dept_from_label(label)

    # 1) Extraction texte brut (un peu plus robuste)
    with pdfplumber.open(path) as pdf:
        text = ""
        for page in pdf.pages:
            t = page.extract_text(x_tolerance=2, y_tolerance=3)
            if t:
                text += t + "\n"

    lines = [l.rstrip() for l in text.splitlines()]

    filiere = ""
    last_nonempty = ""
    current = None
    mode = None
    results = []

    SECTION_ALIASES = {
        "EVALUATION": "eval",
        "CONTACT": "contact",
        "OBJECTIFS": "objectifs",
        "PROGRAMME": "programme",
        "BIBLIOGRAPHIE": "bibliographie",

        "PRÉ-REQUIS": "pre_requis",
        "PRÉREQUIS": "pre_requis",
        "PREREQUIS": "pre_requis",
        "PRE-REQUIS": "pre_requis",
        "PRE REQUIS": "pre_requis",
    }

    # Ces headers stopent la capture de texte (si on les rencontre)
    STOP_HEADERS = {
        "IDENTIFICATION",
        "HORAIRES",
        "SUPPORTS",
        "SUPPORTS PEDAGOGIQUES",
        "SUPPORTS PÉDAGOGIQUES",
        "LANGUE",
        "LANGUE D'ENSEIGNEMENT",
        "MOTS-CLÉS",
        "MOTS-CLES",
        "COMPÉTENCES",
        "COMPETENCES",
        "CONTENU",
    }

    def is_header(line_up: str) -> bool:
        # header connu (section ou stop)
        if line_up in SECTION_ALIASES or line_up in STOP_HEADERS:
            return True
        # parfois le PDF sort des headers en MAJUSCULES "propres"
        # (on reste prudent : seulement si c'est court et très "header-like")
        if 3 <= len(line_up) <= 30 and re.fullmatch(r"[A-ZÉÈÀÙÂÊÎÔÛÇ'\- ]+", line_up):
            # si ça ressemble à un header mais pas connu, on ne stoppe pas,
            # sinon ça coupe du contenu par erreur.
            return False
        return False

    def append_line(field_lines: str, txt: str):
        txt = clean_pdf_garbage(txt).strip()
        if txt:
            current[field_lines].append(txt)

    def finalize_blocks():
        # reconstruit les champs texte à partir des listes
        current["objectifs"] = "\n".join(current.pop("objectifs_lines", [])).strip()
        current["programme"] = "\n".join(current.pop("programme_lines", [])).strip()
        current["bibliographie"] = "\n".join(current.pop("bibliographie_lines", [])).strip()
        current["pre_requis"] = "\n".join(current.pop("pre_requis_lines", [])).strip()

    def push_current():
        nonlocal current
        if current is None:
            return

        code = current.get("code", "")
        if not code:
            return

        if not any(code.startswith(pref + "-") for pref in DEPT_PREFIXES):
            return

        # IMPORTANT: reconstruire objectifs/programme/... avant de calculer ue_code
        finalize_blocks()

        current["niveau"] = guess_niveau_from_code(code)
        current["semestre"] = guess_semestre_from_code(code)
        current["ue_code"] = guess_ue_code_from_objectifs(current.get("objectifs", ""))

        results.append(current)
        current = None

    # --- heuristique OBJECTIFS ---
    objectifs_started = False
    objectifs_nonempty_seen = 0
    prev_nonempty = ""  # pour détecter "Cet" puis "EC ..." sur la ligne suivante

    for raw in lines:
        line = raw.strip()
        if line:
            last_nonempty = line

        upper = line.upper()

        # Filière
        if line and (("INGÉNIEUR" in upper) or ("INGENIEUR" in upper)):
            if ("SPÉCIALITÉ" in upper) or ("SPECIALITE" in upper):
                filiere = line
                continue

        # Début d'une nouvelle fiche
        if line == "IDENTIFICATION":
            push_current()

            current = {
                "departement": dept,
                "annee": year,
                "catalogue_label": label,
                "fichier_pdf": os.path.basename(path),
                "filiere": filiere,
                "niveau": "",
                "semestre": "",
                "ue_code": "",
                "code": "",
                "titre": last_nonempty,

                "ects": "",
                "cours_h": "",
                "td_h": "",
                "tp_h": "",
                "projet_h": "",
                "evaluation_h": "",
                "face_a_face_h": "",
                "travail_perso_h": "",
                "total_h": "",

                "objectifs_lines": [],
                "programme_lines": [],
                "bibliographie_lines": [],
                "pre_requis_lines": [],

                "evaluation_texte": "",
                "contact": "",
            }

            mode = None
            objectifs_started = False
            objectifs_nonempty_seen = 0
            prev_nonempty = ""
            continue

        if current is None:
            continue

        # Switch de section
        if upper in SECTION_ALIASES:
            mode = SECTION_ALIASES[upper]
            if mode == "objectifs":
                objectifs_started = False
                objectifs_nonempty_seen = 0
                prev_nonempty = ""
            continue

        # Stop capture sur certains headers
        if upper in STOP_HEADERS:
            mode = None
            continue

        # Champs simples
        if line.startswith("CODE"):
            m = re.search(r"CODE\s*:\s*([A-Z]{2,4}-[345]-S[12]-EC-[A-Z0-9]+)", line)
            if m:
                current["code"] = m.group(1)
            continue

        if line.startswith("ECTS"):
            m = re.search(r"ECTS\s*:\s*([\d\.,]+)", line)
            if m:
                current["ects"] = m.group(1).replace(",", ".").strip()
            continue

        if line.startswith("Cours"):
            current["cours_h"] = parse_hours(line); continue
        if line.startswith("TD"):
            current["td_h"] = parse_hours(line); continue
        if line.startswith("TP"):
            current["tp_h"] = parse_hours(line); continue
        if line.startswith("Projet"):
            current["projet_h"] = parse_hours(line); continue

        if line.startswith("Evaluation") or line.startswith("Évaluation"):
            if upper != "EVALUATION":
                current["evaluation_h"] = parse_hours(line)
                continue

        if line.startswith("Face à face pédagogique") or line.startswith("Face-à-face pédagogique"):
            current["face_a_face_h"] = parse_hours(line); continue
        if line.startswith("Travail personnel"):
            current["travail_perso_h"] = parse_hours(line); continue
        if line.startswith("Total"):
            current["total_h"] = parse_hours(line); continue

        # Remplissage blocs
        if mode == "eval":
            if line.strip():
                current["evaluation_texte"] += ("\n" if current["evaluation_texte"] else "") + clean_pdf_garbage(line).strip()
            continue

        if mode == "contact":
            if line.strip():
                current["contact"] += ("\n" if current["contact"] else "") + clean_pdf_garbage(line).strip()
            continue

        # OBJECTIFS (heuristique améliorée)
        if mode == "objectifs":
            if not line:
                continue

            # Compte lignes non vides vues en mode objectifs
            objectifs_nonempty_seen += 1

            # Cas 1: "Cet EC" sur la même ligne (avec espaces variables)
            if not objectifs_started and re.search(r"\bCET\s+EC\b", upper):
                objectifs_started = True

            # Cas 2: "Cet" puis ligne suivante commence par "EC ..."
            if not objectifs_started and prev_nonempty.upper() == "CET" and upper.startswith("EC"):
                objectifs_started = True
                # on inclut "Cet" + "EC ..." (sinon tu perds la phrase)
                current["objectifs_lines"].append("Cet")
                current["objectifs_lines"].append(clean_pdf_garbage(line))
                prev_nonempty = line
                continue

            # Fallback: si au bout de 3 lignes on n'a toujours pas "Cet EC",
            # on démarre quand même (sinon tu perds des objectifs)
            if not objectifs_started and objectifs_nonempty_seen >= 3:
                objectifs_started = True

            if objectifs_started:
                # stop si on tombe sur un header connu par accident (rare mais arrive)
                if is_header(upper):
                    mode = None
                    continue
                append_line("objectifs_lines", line)

            if line:
                prev_nonempty = line
            continue

        if mode == "programme":
            if line and not is_header(upper):
                append_line("programme_lines", line)
            continue

        if mode == "bibliographie":
            if line and not is_header(upper):
                append_line("bibliographie_lines", line)
            continue

        if mode == "pre_requis":
            if line and not is_header(upper):
                append_line("pre_requis_lines", line)
            continue

        # update prev_nonempty pour les heuristiques
        if line:
            prev_nonempty = line

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
    "niveau",
    "semestre",
    "ue_code",
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
    "objectifs",
    "programme",
    "bibliographie",
    "pre_requis",
    "evaluation_texte",
    "contact",
]

with open(output_csv, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(all_rows)

print(f"✅ Écrit {output_csv} avec {len(all_rows)} lignes")
