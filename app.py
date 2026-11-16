"""
Télé en ligne - mini serveur IPTV
---------------------------------
1. Télécharge une playlist M3U depuis le dépôt GitHub iptv-org/iptv
2. Analyse (parse) le fichier pour en extraire les chaînes
3. TESTE chaque flux et ne garde que les chaînes qui répondent vraiment
4. Sert une page web avec un lecteur vidéo

Pour durer dans le temps :
- la playlist est retéléchargée régulièrement (iptv-org la met à jour tous les
  jours : les liens morts y sont remplacés), puis revérifiée ;
- une copie est gardée sur le disque (dossier cache/) si GitHub est injoignable ;
- une chaîne qui ne passe pas dans le navigateur est signalée et masquée.

Lancer en local :  python app.py   puis ouvrir http://localhost:5000
En ligne (Render) : gunicorn app:app --workers 1 --threads 8 --timeout 120
"""

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin

import requests
from flask import Flask, jsonify, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
# Sur Render, le site est derrière un relais : on récupère la vraie adresse (https)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Adresse des playlists publiées par iptv-org (une par pays)
URL_PAYS = "https://iptv-org.github.io/iptv/countries/{code}.m3u"
URL_THEME = "https://iptv-org.github.io/iptv/categories/{code}.m3u"

# Thèmes proposés dans le menu : chaînes du monde entier, triées par sujet
THEMES = {
    "animation": "Animation & animé",
    "kids": "Enfants",
}

# Mots qui reconnaissent une chaîne d'animé japonais (pour le filtre « Animé »)
MOTS_ANIME = ["anime", "animé", "manga", "otaku", "crunchyroll", "hidive", "naruto",
              "one piece", "conan", "pokemon", "pokémon", "dragon ball", "sailor moon",
              "yu-gi-oh", "inazuma", "beyblade", "boruto", "captain tsubasa"]

# Pays proposés dans le menu (code ISO -> nom affiché)
PAYS = {
    "fr": "France",
    "be": "Belgique",
    "ch": "Suisse",
    "ca": "Canada",
    "ma": "Maroc",
    "dz": "Algérie",
    "tn": "Tunisie",
    "sn": "Sénégal",
    "ci": "Côte d'Ivoire",
    "cm": "Cameroun",
    "cd": "RD Congo",
    "ht": "Haïti",
    "us": "États-Unis",
    "uk": "Royaume-Uni",
    "es": "Espagne",
    "de": "Allemagne",
    "it": "Italie",
}

# ---------- Réglages ----------
DOSSIER_CACHE = Path(__file__).parent / "cache"
DOSSIER_CACHE.mkdir(exist_ok=True)
FICHIER_MASQUEES = DOSSIER_CACHE / "masquees.json"

DUREE_VERIFICATION = 6 * 3600      # on revérifie les chaînes toutes les 6 heures
DUREE_MASQUAGE = 3 * 24 * 3600     # une chaîne signalée reste masquée 3 jours
NB_TESTS_EN_PARALLELE = 40         # nombre de chaînes testées en même temps
DELAI = (5, 8)                     # secondes : (connexion, réponse)

# On se présente comme un navigateur, depuis notre page web
# Adresse de notre site : Render la fournit automatiquement, sinon on est en local
ORIGINE = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:5000")
ENTETES = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Origin": ORIGINE,
}

# Avancement des vérifications en cours : {code: {"fait": 12, "total": 208}}
EN_COURS = {}
VERROU = threading.Lock()


# ---------- 1. Parser la playlist ----------
def parser_m3u(texte):
    """Transforme le texte d'une playlist M3U en liste de chaînes.

    Une chaîne est décrite par deux lignes :
        #EXTINF:-1 tvg-logo="..." group-title="News",Nom de la chaîne
        https://exemple.com/flux.m3u8
    """
    chaines = []
    infos = None  # infos de la ligne #EXTINF en attente de son lien

    for ligne in texte.splitlines():
        ligne = ligne.strip()

        if ligne.startswith("#EXTINF"):
            # Les attributs sont de la forme  cle="valeur"
            attributs = dict(re.findall(r'([\w-]+)="([^"]*)"', ligne))
            # Le nom est tout ce qui suit la dernière virgule
            nom = ligne.rsplit(",", 1)[-1].strip()
            infos = {
                "nom": nom,
                "logo": attributs.get("tvg-logo", ""),
                "categorie": attributs.get("group-title", "") or "Autres",
            }
            # On ajoute la catégorie « Animé » aux chaînes reconnues par leur nom
            if any(mot in nom.lower() for mot in MOTS_ANIME):
                infos["categorie"] += ";Animé"

        elif ligne and not ligne.startswith("#") and infos:
            # Première ligne non commentée après #EXTINF = le lien du flux
            infos["url"] = ligne
            chaines.append(infos)
            infos = None

    return chaines


def telecharger_playlist(code):
    """Télécharge la playlist ; si GitHub ne répond pas, reprend la copie locale."""
    copie = DOSSIER_CACHE / f"{code}.m3u"
    try:
        modele = URL_THEME if code in THEMES else URL_PAYS
        reponse = requests.get(modele.format(code=code), timeout=20)
        reponse.raise_for_status()
        reponse.encoding = "utf-8"
        copie.write_text(reponse.text, encoding="utf-8")
        return reponse.text
    except requests.RequestException:
        if copie.exists():
            return copie.read_text(encoding="utf-8")
        raise


# ---------- 2. Tester un flux ----------
def cors_ok(reponse):
    """Vrai si le serveur autorise notre page à lire ce fichier (règle CORS du navigateur)."""
    return reponse.headers.get("Access-Control-Allow-Origin") in ("*", ORIGINE)


def lire_playlist_hls(url):
    """Télécharge un fichier .m3u8.

    Renvoie (lignes, adresse finale après redirection, CORS autorisé ?)
    ou None si ce n'est pas une playlist HLS valide.
    """
    reponse = requests.get(url, headers=ENTETES, timeout=DELAI, stream=True)
    try:
        if reponse.status_code != 200:
            return None
        debut = reponse.raw.read(200_000, decode_content=True).decode("utf-8", "ignore")
        if not debut.lstrip().startswith("#EXTM3U"):
            return None  # ce n'est pas un flux HLS : le lecteur ne saura pas le lire
        lignes = [ligne.strip() for ligne in debut.splitlines() if ligne.strip()]
        return lignes, reponse.url, cors_ok(reponse)
    finally:
        reponse.close()


def flux_fonctionne(url):
    """Vrai si la chaîne diffuse vraiment : on va jusqu'à un morceau de vidéo.

    Un flux HLS a souvent 2 niveaux :
      playlist principale -> playlist d'une qualité -> morceaux de vidéo (segments)
    À chaque niveau, le serveur doit autoriser notre page à lire le fichier (CORS).
    """
    try:
        for _ in range(3):  # on descend de niveau en niveau
            resultat = lire_playlist_hls(url)
            if resultat is None:
                return False
            lignes, url, autorise = resultat
            if not autorise:
                return False
            liens = [urljoin(url, l) for l in lignes if not l.startswith("#")]
            if not liens:
                return False
            if any(l.startswith("#EXTINF") for l in lignes):
                # Dernier niveau : on teste le dernier morceau de vidéo
                segment = requests.get(liens[-1], headers=ENTETES, timeout=DELAI, stream=True)
                segment.close()
                return segment.status_code in (200, 206) and cors_ok(segment)
            url = liens[0]  # playlist principale : on suit la première qualité
        return False
    except (requests.RequestException, OSError, ValueError):
        return False


# ---------- 3. Chaînes masquées (signalées par le navigateur) ----------
def lire_masquees():
    """Renvoie {url: date} en oubliant les signalements trop anciens."""
    try:
        masquees = json.loads(FICHIER_MASQUEES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    limite = time.time() - DUREE_MASQUAGE
    return {url: date for url, date in masquees.items() if date > limite}


# ---------- 4. Vérifier toutes les chaînes d'un pays ----------
def fichier_resultat(code):
    return DOSSIER_CACHE / f"{code}.json"


def lire_resultat(code):
    try:
        return json.loads(fichier_resultat(code).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def verifier_pays(code):
    """Tâche de fond : télécharge, teste chaque chaîne, enregistre celles qui marchent."""
    try:
        chaines = parser_m3u(telecharger_playlist(code))
        # Une même adresse peut apparaître deux fois : on garde la première
        chaines = list({c["url"]: c for c in chaines}.values())
        EN_COURS[code]["total"] = len(chaines)

        def tester(chaine):
            ok = flux_fonctionne(chaine["url"])
            with VERROU:
                EN_COURS[code]["fait"] += 1
            return ok

        with ThreadPoolExecutor(NB_TESTS_EN_PARALLELE) as groupe:
            resultats = list(groupe.map(tester, chaines))

        valides = [c for c, ok in zip(chaines, resultats) if ok]
        fichier_resultat(code).write_text(json.dumps({
            "date": time.time(),
            "total": len(chaines),
            "chaines": valides,
        }, ensure_ascii=False), encoding="utf-8")
    except Exception as erreur:  # on note l'erreur pour l'afficher dans la page
        EN_COURS[code]["erreur"] = str(erreur)
        time.sleep(5)
    finally:
        with VERROU:
            if "erreur" not in EN_COURS[code]:
                del EN_COURS[code]


# ---------- 5. Les routes web ----------
@app.route("/")
def accueil():
    return render_template("index.html", pays=PAYS, themes=THEMES)


@app.route("/api/chaines")
def api_chaines():
    code = request.args.get("pays", "fr").lower()
    if code not in PAYS and code not in THEMES:
        return jsonify({"erreur": "Choix inconnu"}), 400
    forcer = request.args.get("forcer") == "1"

    with VERROU:
        avancement = EN_COURS.get(code)
        if avancement and "erreur" in avancement:
            del EN_COURS[code]
            return jsonify({"erreur": "Impossible de charger la playlist : " + avancement["erreur"]}), 502

        resultat = lire_resultat(code)
        a_jour = resultat and time.time() - resultat["date"] < DUREE_VERIFICATION

        if not avancement and (forcer or not a_jour):
            # On lance la vérification en arrière-plan
            avancement = EN_COURS[code] = {"fait": 0, "total": 0}
            threading.Thread(target=verifier_pays, args=(code,), daemon=True).start()

        if avancement and (forcer or not resultat):
            return jsonify({"etat": "verification", **avancement})

    # Liste prête (si une revérification tourne, on montre l'ancienne en attendant)
    masquees = lire_masquees()
    chaines = [c for c in resultat["chaines"] if c["url"] not in masquees]
    if request.scheme == "https":
        # Une page en https n'a pas le droit de lire un flux en http (contenu mixte)
        chaines = [c for c in chaines if c["url"].startswith("https://")]
    return jsonify({
        "etat": "pret",
        "chaines": chaines,
        "total": resultat["total"],
        "date": resultat["date"],
    })


@app.route("/api/signaler", methods=["POST"])
def api_signaler():
    """Le navigateur prévient qu'une chaîne n'a pas pu être lue : on la masque."""
    url = (request.get_json(silent=True) or {}).get("url")
    if not url:
        return jsonify({"erreur": "url manquante"}), 400
    with VERROU:
        masquees = lire_masquees()
        masquees[url] = time.time()
        FICHIER_MASQUEES.write_text(json.dumps(masquees), encoding="utf-8")
    return jsonify({"ok": True})


if __name__ == "__main__":
    # threaded=True : la page reste réactive pendant les vérifications
    app.run(port=int(os.environ.get("PORT", 5000)), threaded=True)
