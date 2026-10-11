"""Treblex Acqua Lazio: acquisizione sperimentale dei rapporti Acqualatina."""
import io
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

@app.get("/api/rapporti")
async def reports(comune: str = Query(min_length=2, max_length=100)):
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
    pattern = re.compile(r"(?<![\\w])(?:<\\s*)?\\d+(?:[.,]\\d+)?")
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
                    value = next((x for x in cells[1:] if re.search(r"(?<!\\w)[<>≤≥]?\\s*\\d+(?:[.,]\\d+)?", x)), None)
                    if value:
                        findings.append({"parametro": names[0], "riga_originale": " | ".join(cells),
                                         "stato": "DA_VERIFICARE", "formato": "HTML"})
    return findings

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
        if response.content.startswith(b"%PDF"):
            data = parse_pdf(response.content)
            kind = "PDF"
        elif "html" in response.headers.get("content-type", "").lower() or response.content.lstrip().startswith(b"<"):
            data = parse_html(response.text)
            kind = "HTML"
        else:
            raise ValueError("Formato non ancora supportato")
        return {"documento": url, "formato": kind, "parametri": data,
                "avviso": "Estrazione preliminare: verificare valori, zona e data sulla fonte."}
    except Exception as exc:
        raise HTTPException(502, detail="Estrazione non riuscita: " + str(exc)[:180])

@app.get("/api/analisi")
async def analysis(url: str):
    if not allowed(url):
        raise HTTPException(400, detail="Fonte Acqualatina non consentita")
    try:
        content, final_url = await fetch_report(url)
        values = parse_pdf(content)
        return {"documento": final_url, "parametri": values,
                "righe_da_verificare": values,
                "avviso": "Estrazione preliminare: controllare zona, data e valori sul PDF."}
    except Exception as exc:
        raise HTTPException(502, detail="Documento non leggibile: " + str(exc)[:180])
