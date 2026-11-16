// ---------- Éléments de la page ----------
const video = document.getElementById("video");
const message = document.getElementById("message");
const listeEl = document.getElementById("chaines");
const categoriesEl = document.getElementById("categories");
const compteurEl = document.getElementById("compteur");
const rechercheEl = document.getElementById("recherche");
const paysEl = document.getElementById("pays");
const reverifierEl = document.getElementById("reverifier");
const nomEnCours = document.getElementById("nom-en-cours");
const categorieEnCours = document.getElementById("categorie-en-cours");
const logoEnCours = document.getElementById("logo-en-cours");

// ---------- État ----------
let chaines = [];          // chaînes vérifiées du pays choisi
let totalPlaylist = 0;     // nombre de chaînes dans la playlist d'origine
let categorie = "Toutes";  // filtre de catégorie actif
let urlActive = null;      // flux en cours de lecture
let hls = null;            // instance hls.js
let aDemarre = false;      // la chaîne en cours a-t-elle affiché de l'image ?
let chargement = 0;        // numéro du chargement en cours (pour ignorer les anciens)

const attendre = ms => new Promise(r => setTimeout(r, ms));

// ---------- Chargement des chaînes depuis notre serveur Python ----------
// Le serveur teste les flux ; tant qu'il n'a pas fini, on affiche l'avancement.
async function chargerPays(code, forcer = false) {
  const numero = ++chargement;
  chaines = [];
  listeEl.innerHTML = "";
  categoriesEl.innerHTML = "";
  compteurEl.textContent = "Chargement…";
  reverifierEl.disabled = true;

  try {
    while (true) {
      const reponse = await fetch(`/api/chaines?pays=${code}${forcer ? "&forcer=1" : ""}`);
      const donnees = await reponse.json();
      if (numero !== chargement) return;  // l'utilisateur a changé de pays entre-temps
      if (!reponse.ok) throw new Error(donnees.erreur || "Erreur");

      if (donnees.etat === "pret") {
        chaines = donnees.chaines;
        totalPlaylist = donnees.total;
        break;
      }
      forcer = false;  // la vérification est lancée, on ne fait plus que suivre
      compteurEl.textContent = donnees.total
        ? `Test des chaînes… ${donnees.fait} / ${donnees.total}`
        : "Téléchargement de la playlist…";
      await attendre(1000);
    }
  } catch (erreur) {
    if (numero === chargement) compteurEl.textContent = erreur.message;
    reverifierEl.disabled = false;
    return;
  }

  reverifierEl.disabled = false;
  categorie = "Toutes";
  afficherCategories();
  afficherChaines();
}

// ---------- Boutons de catégories ----------
function afficherCategories() {
  // Une chaîne peut avoir plusieurs catégories séparées par ";"
  const noms = new Set();
  chaines.forEach(c => c.categorie.split(";").forEach(n => noms.add(n)));
  const liste = ["Toutes", ...[...noms].sort()];

  categoriesEl.innerHTML = "";
  liste.forEach(nom => {
    const bouton = document.createElement("button");
    bouton.textContent = nom;
    bouton.classList.toggle("actif", nom === categorie);
    bouton.onclick = () => {
      categorie = nom;
      afficherCategories();
      afficherChaines();
    };
    categoriesEl.appendChild(bouton);
  });
}

// ---------- Liste des chaînes (avec recherche + filtre) ----------
function afficherChaines() {
  const texte = rechercheEl.value.trim().toLowerCase();
  const visibles = chaines.filter(c =>
    (categorie === "Toutes" || c.categorie.split(";").includes(categorie)) &&
    c.nom.toLowerCase().includes(texte)
  );

  compteurEl.textContent =
    `${visibles.length} chaîne(s) · ${chaines.length} en ligne sur ${totalPlaylist}`;
  listeEl.innerHTML = "";

  visibles.forEach(chaine => {
    const li = document.createElement("li");
    li.classList.toggle("actif", chaine.url === urlActive);

    const vignette = document.createElement("div");
    vignette.className = "vignette";
    const lettre = chaine.nom.charAt(0).toUpperCase();
    if (chaine.logo) {
      const img = document.createElement("img");
      img.src = chaine.logo;
      img.alt = "";
      img.loading = "lazy";
      img.referrerPolicy = "no-referrer";
      img.onerror = () => { vignette.textContent = lettre; };  // logo cassé -> on affiche la lettre
      vignette.appendChild(img);
    } else {
      vignette.textContent = lettre;
    }

    const bloc = document.createElement("div");
    bloc.className = "texte";
    const nom = document.createElement("div");
    nom.className = "nom";
    nom.textContent = chaine.nom;
    const cat = document.createElement("div");
    cat.className = "discret";
    cat.textContent = chaine.categorie.replaceAll(";", " · ");
    bloc.append(nom, cat);

    li.append(vignette, bloc);
    li.onclick = () => lire(chaine);
    listeEl.appendChild(li);
  });
}

// ---------- Lecture d'une chaîne ----------
function afficherMessage(texte) {
  message.textContent = texte;
  message.hidden = !texte;
}

// Dès que l'image s'affiche, on sait que la chaîne fonctionne
video.addEventListener("playing", () => {
  aDemarre = true;
  afficherMessage("");
});

function lire(chaine) {
  urlActive = chaine.url;
  aDemarre = false;
  nomEnCours.textContent = chaine.nom;
  categorieEnCours.textContent = chaine.categorie.replaceAll(";", " · ");
  logoEnCours.hidden = !chaine.logo;
  if (chaine.logo) logoEnCours.src = chaine.logo;
  afficherChaines();
  afficherMessage("Connexion au flux…");

  // On arrête proprement le flux précédent
  if (hls) { hls.destroy(); hls = null; }

  if (window.Hls && Hls.isSupported()) {
    // Cas général (Chrome, Firefox, Edge) : hls.js lit le .m3u8
    hls = new Hls();
    hls.loadSource(chaine.url);
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {}));
    hls.on(Hls.Events.ERROR, (evenement, donnees) => {
      if (donnees.fatal) {
        hls.destroy();
        hls = null;
        enPanne(chaine);
      }
    });
  } else {
    // Safari / iPhone : le navigateur lit le HLS tout seul
    video.src = chaine.url;
    video.onerror = () => enPanne(chaine);
    video.play().catch(() => {});
  }
}

// ---------- Une chaîne ne passe pas ----------
function enPanne(chaine) {
  if (chaine.url !== urlActive) return;

  if (aDemarre) {
    // Elle marchait puis s'est coupée : simple coupure, on la garde dans la liste
    afficherMessage("Le flux s'est interrompu. Clique à nouveau sur la chaîne pour relancer.");
    return;
  }

  // Elle n'a jamais démarré : on la retire de la liste et on prévient le serveur
  chaines = chaines.filter(c => c.url !== chaine.url);
  urlActive = null;
  afficherCategories();
  afficherChaines();
  afficherMessage(`« ${chaine.nom} » ne répond pas : elle a été retirée de la liste.`);
  fetch("/api/signaler", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url: chaine.url }),
  }).catch(() => {});
}

// ---------- Événements ----------
rechercheEl.addEventListener("input", afficherChaines);
paysEl.addEventListener("change", () => chargerPays(paysEl.value));
reverifierEl.addEventListener("click", () => chargerPays(paysEl.value, true));

chargerPays(paysEl.value);
