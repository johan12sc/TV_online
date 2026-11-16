# Télé en ligne

Interface de télévision en direct (IPTV) à partir des playlists publiques de
[iptv-org/iptv](https://github.com/iptv-org/iptv), faite en Python avec Flask.

- Télécharge la playlist d'un pays et teste chaque chaîne
- N'affiche que les chaînes qui fonctionnent, retire celles qui échouent
- Se met à jour toutes les 6 heures

## Lancer en local

    python -m venv venv
    venv\Scripts\activate        (Windows)   ou   source venv/bin/activate
    pip install -r requirements.txt
    python app.py

Puis ouvrir http://localhost:5000

## En ligne

Hébergé sur Render grâce au fichier `render.yaml`.
