import os
import json
import pandas as pd
import requests

# --- Config LM Studio ---
LMSTUDIO_API_URL = "http://127.0.0.1:1234/v1/chat/completions" #The local server adresse
MODEL = "gpt-oss-20b"  # le nom exact du modèle dans LM Studio

CSV_PATH = "ects_insa.csv"  # ton fichier généré

df = pd.read_csv(CSV_PATH)

def build_filter_from_question(question: str):
    """
    Demande au LLM de traduire la question en filtre structuré.
    On lui demande un JSON très simple.
    """
    system_prompt = """
    Tu es un assistant qui convertit une question utilisateur en filtres pour un tableau de cours.

    Tu DOIS répondre UNIQUEMENT avec un objet JSON valide, sans texte autour.

    Les colonnes disponibles sont :
    - departement : string, ex "TC", "GE", "GM"
    - niveau : string, ex "3A", "4A", "5A"
    - ects : nombre (float)

    Tu dois renvoyer un JSON de la forme :
    {
    "departement": "...",      // ou null si pas précisé
    "niveau": "...",           // ou null si pas précisé
    "ects_min": nombre ou null // seuil minimum d'ECTS
    }

    Exemples :

    Question : "donne-moi tous les cours de 4A TC avec plus de 5 ECTS"
    Réponse :
    {"departement": "TC", "niveau": "4A", "ects_min": 5}

    Question : "liste tous les cours de 3A en génie électrique"
    Réponse :
    {"departement": "GE", "niveau": "3A", "ects_min": null}

    Question : "les cours avec au moins 2 ECTS"
    Réponse :
    {"departement": null, "niveau": null, "ects_min": 2}
    """

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question},
    ]

    resp = requests.post(
        LMSTUDIO_API_URL,
        json={
            "model": MODEL,
            "messages": messages,
            "temperature": 0.0,
        },
        timeout=60,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"].strip()

    # On suppose que le modèle respecte la consigne et renvoie du JSON pur
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        # si jamais il met du texte autour, on peut tenter un rattrapage léger
        # mais pour l'instant on lève l'erreur
        raise ValueError("Réponse du modèle non JSON:\n" + content)

    return data


def filter_df(filters: dict):
    """
    Applique le filtre sur le DataFrame pandas à partir du JSON renvoyé.
    """
    df_filtered = df.copy()

    dep = filters.get("departement")
    niv = filters.get("niveau")
    ects_min = filters.get("ects_min")

    if dep:
        df_filtered = df_filtered[df_filtered["departement"] == dep]
    if niv:
        df_filtered = df_filtered[df_filtered["niveau"] == niv]
    if ects_min is not None:
        # parfois ects est string -> on convertit au vol
        df_filtered = df_filtered.copy()
        df_filtered["ects_num"] = pd.to_numeric(df_filtered["ects"], errors="coerce")
        df_filtered = df_filtered[df_filtered["ects_num"] >= float(ects_min)]

    return df_filtered


def ask(question: str):
    print(f"Question : {question}\n")

    filters = build_filter_from_question(question)
    print("Filtres déduits par le LLM :", filters)

    df_res = filter_df(filters)

    if df_res.empty:
        print("\nAucun cours trouvé avec ces critères.")
        return

    # On affiche juste quelques colonnes utiles
    cols = ["departement", "niveau", "code", "titre", "ects"]
    cols = [c for c in cols if c in df_res.columns]

    print("\nCours trouvés :\n")
    print(df_res[cols].to_string(index=False))


if __name__ == "__main__":
    while True:
        q = input("\n Question (enter pour quitter) : ")
        if not q.strip():
            break
        ask(q)
