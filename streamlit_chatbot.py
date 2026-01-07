import os
import json
import re
import pandas as pd
import requests
import streamlit as st

# -----------------------------
# Config LM Studio (local)
# -----------------------------
LMSTUDIO_API_URL = os.getenv("LMSTUDIO_API_URL", "http://127.0.0.1:1234/v1/chat/completions")
MODEL = os.getenv("LMSTUDIO_MODEL", "gpt-oss-20b")

CSV_PATH = os.getenv("ECTS_CSV_PATH", "ects_insa.csv")

# -----------------------------
# Helpers
# -----------------------------
def call_llm(messages, temperature=0.2, max_tokens=900):
    resp = requests.post(
        LMSTUDIO_API_URL,
        json={
            "model": MODEL,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def load_df(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        raise FileNotFoundError(f"CSV introuvable: {path}")
    df = pd.read_csv(path)
    # petites sécurités de types
    if "ects" in df.columns:
        df["ects_num"] = pd.to_numeric(df["ects"], errors="coerce")
    return df


def extract_json_safely(txt: str):
    """
    Essaie d'extraire un JSON même si le modèle a ajouté du texte autour.
    """
    txt = txt.strip()
    try:
        return json.loads(txt)
    except Exception:
        pass

    # rattrapage simple: prend le premier {...}
    m = re.search(r"\{.*\}", txt, flags=re.DOTALL)
    if not m:
        raise ValueError("Réponse non-JSON du modèle:\n" + txt)
    return json.loads(m.group(0))


def llm_build_filters(question: str):
    system = """
Tu es un assistant qui transforme une question utilisateur en filtres structurés pour un tableau de cours INSA.

Tu DOIS répondre UNIQUEMENT avec un JSON valide, sans texte autour.

Colonnes possibles (si pas sûr, mets null) :
- departement (ex: "GE", "TC", "GM", "GI", "BIO", "GCU", "MAT")
- niveau (ex: "3A", "4A", "5A")
- semestre (ex: "S1", "S2")
- ects_min (nombre) / ects_max (nombre)
- keywords (liste de mots-clés) : pour chercher dans titre/objectifs/programme/pre_requis
- limit (nombre) : max de cours à retourner (ex 12)

Format EXACT attendu :
{
  "departement": "... ou null",
  "niveau": "... ou null",
  "semestre": "... ou null",
  "ects_min": nombre ou null,
  "ects_max": nombre ou null,
  "keywords": ["..."] ,
  "limit": nombre
}

Exemples:
Question: "cours 4A GE avec au moins 4 ects sur électronique de puissance"
=> {"departement":"GE","niveau":"4A","semestre":null,"ects_min":4,"ects_max":null,"keywords":["électronique","puissance"],"limit":12}

Question: "donne moi des cours de S2 en TC"
=> {"departement":"TC","niveau":null,"semestre":"S2","ects_min":null,"ects_max":null,"keywords":[],"limit":12}
"""
    out = call_llm(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": question},
        ],
        temperature=0.0,
        max_tokens=250,
    )
    data = extract_json_safely(out)

    # defaults propres
    data.setdefault("departement", None)
    data.setdefault("niveau", None)
    data.setdefault("semestre", None)
    data.setdefault("ects_min", None)
    data.setdefault("ects_max", None)
    data.setdefault("keywords", [])
    data.setdefault("limit", 12)

    # normalisation
    if isinstance(data["keywords"], str):
        data["keywords"] = [data["keywords"]]
    if data["limit"] is None:
        data["limit"] = 12

    return data


def apply_filters(df: pd.DataFrame, f: dict) -> pd.DataFrame:
    res = df.copy()

    dep = f.get("departement")
    niv = f.get("niveau")
    sem = f.get("semestre")
    ects_min = f.get("ects_min")
    ects_max = f.get("ects_max")
    keywords = [k.strip() for k in (f.get("keywords") or []) if str(k).strip()]
    limit = int(f.get("limit") or 12)

    if dep:
        res = res[res.get("departement", "") == dep]
    if niv:
        res = res[res.get("niveau", "") == niv]
    if sem:
        res = res[res.get("semestre", "") == sem]

    if ects_min is not None and "ects_num" in res.columns:
        res = res[res["ects_num"].fillna(-1) >= float(ects_min)]
    if ects_max is not None and "ects_num" in res.columns:
        res = res[res["ects_num"].fillna(10**9) <= float(ects_max)]

    # recherche mots-clés (simple mais efficace)
    if keywords:
        hay_cols = [c for c in ["titre", "objectifs", "programme", "pre_requis"] if c in res.columns]
        if hay_cols:
            hay = res[hay_cols].fillna("").agg(" ".join, axis=1).str.lower()
            mask = True
            for kw in keywords:
                kw_l = kw.lower()
                mask = mask & hay.str.contains(re.escape(kw_l), regex=True)
            res = res[mask]

    # tri: plus d'ECTS en premier si dispo
    if "ects_num" in res.columns:
        res = res.sort_values(by="ects_num", ascending=False, na_position="last")

    return res.head(limit)


def dataframe_to_context(df: pd.DataFrame) -> str:
    """
    On envoie au LLM une version compacte des cours, pour éviter de lui balancer 5000 lignes.
    """
    cols = [c for c in [
        "departement", "niveau", "semestre", "code", "titre", "ects",
        "pre_requis", "objectifs", "programme", "evaluation_texte", "contact"
    ] if c in df.columns]

    lines = []
    for _, row in df[cols].iterrows():
        item = {c: ("" if pd.isna(row[c]) else str(row[c])) for c in cols}
        # coupe un peu les longs champs
        for k in ["objectifs", "programme", "pre_requis", "evaluation_texte"]:
            if k in item and len(item[k]) > 700:
                item[k] = item[k][:700] + " …"
        lines.append(item)

    return json.dumps(lines, ensure_ascii=False, indent=2)


def llm_answer(question: str, courses_df: pd.DataFrame, chat_history):
    courses_context = dataframe_to_context(courses_df)

    system = """
Tu es un assistant type "chatbot" pour interroger un catalogue de cours INSA (données issues d'un CSV).
Règles:
- Tu réponds en français, style clair et un peu étudiant.
- Tu DOIS te baser UNIQUEMENT sur les cours fournis dans CONTEXTE_COURS.
- Si le contexte est vide: dis que tu ne trouves rien et propose 2-3 pistes pour reformuler (département, niveau, semestre, mots-clés).
- Si l'utilisateur demande "tous les cours" -> tu refuses de lister tout, tu proposes de filtrer.
- Tu peux faire une réponse courte + une mini-liste (max 8 cours) avec: code — titre — ects — (semestre si dispo).
"""

    # on garde un peu d'historique (pas besoin de tout)
    history_msgs = []
    for m in chat_history[-8:]:
        history_msgs.append({"role": m["role"], "content": m["content"]})

    user = f"""
QUESTION_UTILISATEUR:
{question}

CONTEXTE_COURS (liste JSON):
{courses_context}
"""

    return call_llm(
        [{"role": "system", "content": system}]
        + history_msgs
        + [{"role": "user", "content": user}],
        temperature=0.2,
        max_tokens=900,
    )


# -----------------------------
# Streamlit UI
# -----------------------------
st.set_page_config(page_title="Chatbot Cours INSA (local)", page_icon="🤖", layout="centered")
st.title("🤖 Chatbot Cours INSA (local)")
st.caption("Pose une question sur les cours. Réponse basée sur ton `ects_insa.csv` + LLM local (LM Studio).")

with st.sidebar:
    st.subheader("⚙️ Config")
    st.write("API:", LMSTUDIO_API_URL)
    st.write("Modèle:", MODEL)
    st.write("CSV:", CSV_PATH)
    st.divider()
    debug = st.checkbox("Afficher debug (filtres + table)", value=False)

# init state
if "df" not in st.session_state:
    try:
        st.session_state.df = load_df(CSV_PATH)
    except Exception as e:
        st.error(f"Impossible de charger le CSV: {e}")
        st.stop()

if "messages" not in st.session_state:
    st.session_state.messages = [
        {"role": "assistant", "content": "Salut 👋 Pose-moi une question du style : « cours de 4A GE en S2 », ou « cours sur FPGA en 5A »."}
    ]

# display messages
for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])

# input
question = st.chat_input("Ta question…")

if question:
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    # step 1: filtres
    try:
        filters = llm_build_filters(question)
    except Exception as e:
        filters = {"departement": None, "niveau": None, "semestre": None, "ects_min": None, "ects_max": None, "keywords": [], "limit": 12}
        if debug:
            st.warning(f"Filtres LLM impossibles, fallback. Détail: {e}")

    # step 2: apply filters
    df_hit = apply_filters(st.session_state.df, filters)

    # step 3: answer
    try:
        answer = llm_answer(question, df_hit, st.session_state.messages)
    except Exception as e:
        answer = f"J’ai eu une erreur en appelant le LLM : {e}\n\nVérifie que LM Studio tourne et que l’API est bien sur `{LMSTUDIO_API_URL}`."

    st.session_state.messages.append({"role": "assistant", "content": answer})
    with st.chat_message("assistant"):
        st.markdown(answer)

        if debug:
            st.divider()
            st.write("**Filtres déduits :**", filters)
            st.write("**Cours retournés :**", len(df_hit))
            st.dataframe(df_hit)
