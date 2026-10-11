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
            if normalize(comune) not in normalize(title + " " + slug):
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
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages[:8]:
            for table in page.extract_tables() or []:
                for row in table:
                    cells = [" ".join(str(v or "").split()) for v in row]
                    if len(cells) < 3:
                        continue
                    name = normalize(cells[0])
                    for key, labels in PARAMS.items():
                        if any(name == normalize(label) or name.startswith(normalize(label) + " ") for label in labels):
                            value = cells[-1]
                            if re.match(r"^\\s*[<>≤≥]?\\s*\\d+(?:[.,]\\d+)?\\s*$", value):
                                found.append({"parametro": labels[0], "valore": value,
                                              "unita": cells[1], "stato": "DA_VERIFICARE"})
            if found:
                continue
            # Testo libero: restituiamo soltanto righe, senza indovinare colonne.
            text = page.extract_text() or ""
            for line in text.splitlines():
                for key, labels in PARAMS.items():
                    if any(normalize(line).startswith(normalize(label)) for label in labels):
                        found.append({"parametro": labels[0], "riga_originale": line[:250],
                                      "stato": "DA_VERIFICARE"})
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
