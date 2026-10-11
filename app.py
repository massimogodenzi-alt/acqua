"""Treblex Acqua Lazio: acquisizione sperimentale dei rapporti Acqualatina."""
import io
import csv
import openpyxl
import re
import unicodedata
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

import httpx
import pdfplumber
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

app = FastAPI(title="Treblex Acqua Lazio")
SOURCE = "https://www.acqualatina.it/qualita-dellacqua-per-comune/"
PARAMS = {
    "ph": ("pH",), "durezza": ("Durezza",),
    "conducibilita": ("Conducibilità elettrica", "Conducibilita elettrica"),
    "residuo": ("Residuo secco", "Residuo fisso"),
    "sodio": ("Sodio",), "nitrati": ("Nitrato", "Nitrati"),
    "arsenico": ("Arsenico",), "calcio": ("Calcio",),
    "magnesio": ("Magnesio",), "potassio": ("Potassio",),
    "cloruri": ("Cloruro",), "solfati": ("Solfato",)
}

def normalize(s):
    return "".join(c for c in unicodedata.normalize("NFKD", str(s).casefold()) if not unicodedata.combining(c))

def allowed(url):
    p = urlparse(url)
    return p.scheme == "https" and p.hostname in ("www.acqualatina.it", "acqualatina.it")

def is_download(url):
    p = urlparse(url)
    return (p.path.lower().endswith(".pdf") or "wpdmdl" in parse_qs(p.query)
            or "/download/" in p.path.lower() or "download" in p.path.lower()
            or "/file/" in p.path.lower())

def heading_name(tag):
    for key in ("h2", "h3", "h4", "h5", "strong", "b"):
        h = tag.find(key)
        if h and h.get_text(" ", strip=True):
            return h.get_text(" ", strip=True)
    return ""

@app.get("/", response_class=HTMLResponse)
def home():
    return Path(__file__).with_name("index.html").read_text(encoding="utf-8")

@app.get("/api/health")
def health():
    return {"status": "ok"}

def zone_matches(comune, title):
    """Confronta il nome del comune con l'inizio della zona, non con sottostringhe."""
    q = normalize(comune).strip()
    t = normalize(title).strip()
    if not q or not t:
        return False
    # Latina -> Latina Nord; esclude Cisterna di Latina.
    # Cisterna di Latina -> Cisterna di Latina, anche con suffissi di zona.
    return t == q or t.startswith(q + " ") or t.startswith(q + "-")

# Instradamento iniziale: non dedurre il gestore dalla sola provincia.
# I comuni non elencati richiedono una mappatura ufficiale verificata.
PROVIDER_LINKS = {
    "Acea Ato 2": "https://www.aceaato2.a-acqua.it/qualita-acqua",
    "Acea Ato 5": "https://www.aceaato5.a-acqua.it/qualita-acqua",
}
ACEA_PDF_BASE = "https://www.aceaato2.a-acqua.it/content/dam/acea-ato2/pdf/it/mappe-qualita/"
ROMA_ZONE_FILES = [
    "roma_e_fiumicino_zona_1_monteverde_vecchio.pdf",
    "roma_e_fiumicino_zona_2_peschiera_capore_%2B_vigne_nuove.pdf",
    "roma_e_fiumicino_zona_3_peschiera_capore.pdf",
    "roma_e_fiumicino_zona_4_nuovo_vergine_prevalente.pdf",
    "roma_e_fiumicino_zona_5_acqua_marcia.pdf",
    "roma_e_fiumicino_zona_6_appio_alessandrino_hc.pdf",
    "roma_e_fiumicino_zona_7_appio_alessandrino_lc.pdf",
    "roma_e_fiumicino_zona_8_acqua_marcia_%2B_peschiera_capore.pdf",
    "roma_e_fiumicino_zona_9_ostia_e_acilia.pdf"
]

KNOWN_MUNICIPALITIES = {
    "ladispoli": ("Acea Ato 2", "Roma"),
    "roma": ("Acea Ato 2", "Roma"),
    "frosinone": ("Acea Ato 5", "Frosinone"),
}

@app.get("/api/gestori")
def providers():
    return {"province": ["Roma", "Latina", "Frosinone", "Viterbo", "Rieti"],
            "gestori": [
                {"nome": "Acqualatina", "stato": "estrazione_sperimentale"},
                {"nome": "Acea Ato 2", "stato": "portale_collegato"},
                {"nome": "Acea Ato 5", "stato": "portale_collegato"},
                {"nome": "Talete", "stato": "da_integrare"},
                {"nome": "Acqua Pubblica Sabina", "stato": "da_integrare"}]}

@app.get("/api/rapporti")
async def reports(comune: str = Query(min_length=2, max_length=100)):
    key = normalize(comune).strip()
    if key == "roma":
        return {"comune": comune, "gestore": "Acea Ato 2", "provincia": "Roma",
                "rapporti": [{"titolo": "Roma e Fiumicino — Zona " + str(i+1) + " — " + filename.split("_zona_")[1].replace(".pdf","").replace("_"," ").replace("%2B","+"), "url": ACEA_PDF_BASE + filename, "tipo": "pdf"} for i, filename in enumerate(ROMA_ZONE_FILES)],
                "fonte": PROVIDER_LINKS["Acea Ato 2"], "stato": "estrazione_sperimentale",
                "avviso": "Roma ha più zone idriche: scegliere quella corrispondente all’indirizzo. Verificare il periodo di riferimento nel PDF."}
    if key in KNOWN_MUNICIPALITIES:
        gestore, provincia = KNOWN_MUNICIPALITIES[key]
        return {"comune": comune, "provincia": provincia, "gestore": gestore,
                "rapporti": [], "fonte": PROVIDER_LINKS[gestore],
                "stato": "portale_collegato",
                "avviso": "Portale ufficiale individuato; estrazione automatica non ancora disponibile."}
    # Non cercare ogni comune del Lazio su Acqualatina:
    # per i gestori non ancora mappati non attribuire una fonte sbagliata.
    acqualatina_communes = {
        "latina", "cisterna di latina", "aprilia", "formia", "gaeta",
        "fondi", "terracina", "sabaudia", "sezze", "priverno",
        "minturno", "sermoneta", "pontinia", "cori", "itri",
        "san felice circeo", "sperlonga", "lenola", "monte san biagio"
    }
    if key not in acqualatina_communes:
        return {"comune": comune, "gestore": "Da identificare",
                "rapporti": [], "fonte": None, "stato": "gestore_da_mappare",
                "avviso": "Non abbiamo ancora un connettore ufficiale verificato per questo comune. Nessun valore viene inventato."}
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                      headers={"User-Agent": "Mozilla/5.0"}) as client:
            response = await client.get(SOURCE)
            response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        found = {}
        # Acqualatina usa schede WordPress Download Manager:
        # il titolo della zona punta a /download/nome-zona/.
        for a in soup.select('a[href]'):
            title = a.get_text(" ", strip=True)
            url = urljoin(SOURCE, a.get("href", ""))
            if not allowed(url):
                continue
            path = urlparse(url).path.lower()
            if "/download/" not in path and not path.endswith(".pdf"):
                continue
            # Il nome della zona deve essere nel titolo della scheda,
            # oppure nello slug della pagina dedicata.
            slug = path.rstrip("/").split("/")[-1].replace("-", " ")
            if not zone_matches(comune, slug) and not zone_matches(comune, title):
                continue
            if title.casefold() == "download":
                continue
            found[url] = {"titolo": title or slug.title(), "url": url,
                          "tipo": "scheda" if not path.endswith(".pdf") else "pdf"}
        return {"comune": comune, "gestore": "Acqualatina",
                "rapporti": list(found.values()), "fonte": SOURCE,
                "stato": "estrazione_sperimentale" if found else "nessun_rapporto_identificato",
                "avviso": "Selezionare la zona corretta e verificare data e valori."}
    except Exception as exc:
        raise HTTPException(502, detail="Fonte non disponibile: " + str(exc)[:180])

async def fetch_report(url):
    """Segue scheda di download e link locali, senza accedere a host esterni."""
    async with httpx.AsyncClient(timeout=35, follow_redirects=False,
                                  headers={"User-Agent": "Mozilla/5.0"}) as client:
        for _ in range(6):
            if not allowed(url):
                raise ValueError("Fonte esterna non autorizzata")
            response = await client.get(url)
            if response.status_code in (301, 302, 303, 307, 308):
                url = urljoin(url, response.headers.get("location", ""))
                continue
            response.raise_for_status()
            if len(response.content) > 12000000:
                raise ValueError("Documento troppo grande")
            if response.content.startswith(b"%PDF"):
                return response.content, str(response.url)
            soup = BeautifulSoup(response.text, "html.parser")
            # Priorità ai collegamenti WordPress Download Manager.
            options = []
            for a in soup.select('a[href], a[data-downloadurl]'):
                raw = a.get("data-downloadurl") or a.get("href", "")
                candidate = urljoin(url, raw)
                if not allowed(candidate) or candidate == url:
                    continue
                label = (a.get_text(" ", strip=True) + " " + str(a.get("class", ""))).lower()
                path = urlparse(candidate).path.lower()
                query = parse_qs(urlparse(candidate).query)
                score = (10 if "wpdmdl" in query else 0) + (5 if path.endswith(".pdf") else 0) + (3 if "download" in label else 0)
                if score >= 3:
                    options.append((score, candidate))
            if not options:
                raise ValueError("Nella scheda non è stato trovato un link di download riconoscibile")
            options.sort(reverse=True)
            url = options[0][1]
        raise ValueError("Troppi passaggi nel download")

def parse_pdf(data):
    found = []
    seen = set()
    # Le righe dei rapporti Acqualatina contengono spesso:
    # parametro | unità | limite/riferimento | valore misurato.
    # Non confondere il limite con il valore.
    pattern = re.compile(r"(?<![\w])(?:<\s*)?\d+(?:[.,]\d+)?")
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages[:8]:
            text = page.extract_text() or ""
            for line in text.splitlines():
                clean = " ".join(line.split())
                norm = normalize(clean)
                for key, labels in PARAMS.items():
                    if key in seen or not any(norm.startswith(normalize(label)) for label in labels):
                        continue
                    # Elimina il nome del parametro per evitare numeri nel nome.
                    label = next(label for label in labels if norm.startswith(normalize(label)))
                    remainder = clean[len(label):].strip()
                    # Nei PDF Acqualatina il valore misurato è l'ultimo
                    # elemento numerico della riga, ma è da verificare.
                    nums = list(pattern.finditer(remainder))
                    entry = {"parametro": labels[0], "riga_originale": clean[:250],
                             "stato": "DA_VERIFICARE"}
                    if nums:
                        last = nums[-1]
                        raw = last.group().strip()
                        if remainder[max(0, last.start()-2):last.start()].strip().endswith("<"):
                            raw = "< " + raw
                        entry["valore"] = raw
                    found.append(entry)
                    seen.add(key)
                    break
    return found

def parse_html(data):
    soup = BeautifulSoup(data, "html.parser")
    findings = []
    for table in soup.select("table"):
        for tr in table.select("tr"):
            cells = [cell.get_text(" ", strip=True) for cell in tr.select("th,td")]
            if len(cells) < 2:
                continue
            label = normalize(cells[0])
            for names in PARAMS.values():
                if any(label == normalize(name) or label.startswith(normalize(name) + " ") for name in names):
                    value = next((x for x in cells[1:] if re.search(r"(?<!\w)[<>≤≥]?\s*\d+(?:[.,]\d+)?", x)), None)
                    if value:
                        findings.append({"parametro": names[0], "riga_originale": " | ".join(cells),
                                         "stato": "DA_VERIFICARE", "formato": "HTML"})
    return findings

def parse_rows(rows, kind):
    findings = []
    seen = set()
    numeric = re.compile(r"^\\s*(?:[<>≤≥]\\s*)?\\d+(?:[.,]\\d+)?\\s*$")
    for row in rows:
        cells = [str(x if x is not None else "").strip() for x in row]
        if len(cells) < 2:
            continue
        label = normalize(cells[0])
        for key, labels in PARAMS.items():
            if key in seen or not any(label == normalize(n) or label.startswith(normalize(n) + " ") for n in labels):
                continue
            values = [x for x in cells[1:] if numeric.fullmatch(x)]
            item = {"parametro": labels[0], "riga_originale": " | ".join(cells)[:300],
                    "formato": kind, "stato": "DA_VERIFICARE"}
            if values:
                item["valore"] = values[-1]
            findings.append(item)
            seen.add(key)
            break
    return findings

def parse_document(content, content_type="", url=""):
    kind = content_type.lower()
    path = urlparse(url).path.lower()
    if content.startswith(b"%PDF"):
        return "PDF", parse_pdf(content)
    if content.startswith(b"PK\\x03\\x04") or path.endswith(".xlsx"):
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        rows = (row for ws in wb.worksheets[:5] for row in ws.iter_rows(min_row=1, max_row=3000, values_only=True))
        return "Excel", parse_rows(rows, "Excel")
    if "csv" in kind or path.endswith(".csv"):
        decoded = content.decode("utf-8-sig", errors="replace")
        dialect = csv.Sniffer().sniff(decoded[:2048], delimiters=";,\\t|") if decoded.strip() else csv.excel
        return "CSV", parse_rows(csv.reader(io.StringIO(decoded), dialect), "CSV")
    if "html" in kind or content.lstrip().lower().startswith((b"<!doctype", b"<html", b"<table")):
        return "HTML", parse_html(content)
    raise ValueError("Formato non supportato: usare PDF, HTML, CSV o XLSX")

@app.get("/api/estrai")
async def extract(url: str):
    """Lettura di una fonte ufficiale HTML o PDF; mai inventare valori."""
    if not allowed(url):
        raise HTTPException(400, detail="Fonte esterna non autorizzata")
    try:
        async with httpx.AsyncClient(timeout=35, follow_redirects=False) as client:
            response = await client.get(url)
            for _ in range(4):
                if response.status_code not in (301, 302, 303, 307, 308):
                    break
                dest = urljoin(str(response.url), response.headers.get("location", ""))
                if not allowed(dest):
                    raise ValueError("Reindirizzamento esterno non consentito")
                response = await client.get(dest)
            response.raise_for_status()
        if len(response.content) > 12000000:
            raise ValueError("Documento troppo grande")
        kind, data = parse_document(response.content, response.headers.get("content-type", ""), url)
        return {"documento": url, "formato": kind, "parametri": data,
                "avviso": "Estrazione preliminare: verificare valori, zona e data sulla fonte."}
    except Exception as exc:
        raise HTTPException(502, detail="Estrazione non riuscita: " + str(exc)[:180])

@app.get("/api/analisi")
async def analysis(url: str):
    if url.startswith(ACEA_PDF_BASE) and urlparse(url).path.lower().endswith(".pdf"):
        try:
            async with httpx.AsyncClient(timeout=35, follow_redirects=True) as client:
                response = await client.get(url)
                response.raise_for_status()
            if len(response.content) > 12000000:
                raise ValueError("Documento troppo grande")
            kind, values = parse_document(response.content, response.headers.get("content-type", ""), url)
            return {"documento": url, "formato": kind, "parametri": values,
                    "righe_da_verificare": values, "avviso": "Dati da verificare sul rapporto originale."}
        except Exception as exc:
            raise HTTPException(502, detail="Lettura Acea non riuscita: " + str(exc)[:180])
    if not allowed(url):
        raise HTTPException(400, detail="Fonte non consentita")
    try:
        content, final_url = await fetch_report(url)
        values = parse_pdf(content)
        return {"documento": final_url, "parametri": values,
                "righe_da_verificare": values,
                "avviso": "Estrazione preliminare: controllare zona, data e valori sul PDF."}
    except Exception as exc:
        raise HTTPException(502, detail="Documento non leggibile: " + str(exc)[:180])

@app.get("/api/controllo-gestori")
async def controllo_gestori():
    import asyncio
    sources = [
        ("Acqualatina", "Latina / Frosinone", SOURCE, "sperimentale"),
        ("Acea Ato 2", "Roma", PROVIDER_LINKS["Acea Ato 2"], "parziale"),
        ("Acea Ato 5", "Frosinone", PROVIDER_LINKS["Acea Ato 5"], "non integrata"),
        ("Talete", "Viterbo", "https://www.taletespa.eu/", "non integrata"),
        ("Acqua Pubblica Sabina", "Rieti", "https://www.acquapubblicasabina.it/", "non integrata")
    ]
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        async def check(name, province, url, extraction):
            result = {"gestore": name, "province": province, "fonte": url, "estrazione": extraction}
            try:
                response = await client.get(url)
                response.raise_for_status()
                result["stato"] = "portale_raggiungibile"
                result["formato"] = response.headers.get("content-type", "").split(";")[0]
            except Exception:
                result["stato"] = "verifica_non_riuscita"
                result["formato"] = "non rilevato"
            return result
        checks = await asyncio.gather(*(check(*s) for s in sources))
    return {"gestori": checks, "nota": "Portale raggiungibile non significa parametri disponibili o estratti."}
