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
                                      headers={"User-Agent": "Mozilla/5.0 TreblexWaterResearch/1.0"}) as client:
            response = await client.get(SOURCE)
            response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        found = {}
        # I documenti sono organizzati in schede: titolo zona + pulsante Download.
        # Non cercare il comune nell'intera pagina: genera falsi positivi.
        for header in soup.find_all(re.compile(r"^h[1-6]$")):
            title = header.get_text(" ", strip=True)
            if normalize(comune) not in normalize(title):
                continue
            candidates = []
            for parent in list(header.parents)[:5]:
                if parent.name in ("body", "html"):
                    break
                links = parent.select("a[href], a[data-downloadurl], a[data-url]")
                links = [a for a in links if "download" in a.get_text(" ", strip=True).lower()
                         or "pdf" in str(a.get("href", "")).lower()
                         or "download" in str(a.get("class", "")).lower()]
                if links and len(links) <= 8:
                    candidates = links
                    break
            for a in candidates:
                raw = a.get("data-downloadurl") or a.get("data-url") or a.get("href")
                if not raw:
                    continue
                url = urljoin(SOURCE, raw)
                if allowed(url) and is_download(url):
                    found[url] = {"titolo": title, "url": url}
        # Fallback: collegamenti PDF con nome del comune nel nome file o titolo.
        for a in soup.select("a[href]"):
            url = urljoin(SOURCE, a["href"])
            if allowed(url) and is_download(url):
                title = a.get("title", "") or a.get_text(" ", strip=True)
                if normalize(comune) in normalize(title + " " + url):
                    found[url] = {"titolo": title or comune, "url": url}
        return {"comune": comune, "gestore": "Acqualatina",
                "rapporti": list(found.values()), "fonte": SOURCE,
                "avviso": "Verificare zona e data del rapporto ufficiale."}
    except Exception as exc:
        raise HTTPException(502, detail="Fonte non disponibile: " + str(exc)[:180])

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
    if not allowed(url) or not is_download(url):
        raise HTTPException(400, detail="Link Acqualatina non consentito")
    try:
        async with httpx.AsyncClient(timeout=35, follow_redirects=False) as client:
            response = await client.get(url)
            for _ in range(4):
                if response.status_code not in (301, 302, 303, 307, 308):
                    break
                next_url = urljoin(str(response.url), response.headers.get("location", ""))
                if not allowed(next_url):
                    raise ValueError("Il download reindirizza a un dominio non verificato")
                response = await client.get(next_url)
            response.raise_for_status()
        if len(response.content) > 12000000 or not response.content.startswith(b"%PDF"):
            raise ValueError("Il collegamento non ha restituito un PDF: occorre un adattatore per il download")
        values = parse_pdf(response.content)
        return {"documento": url, "parametri": values,
                "righe_da_verificare": values,
                "avviso": "Valori estratti automaticamente: controllare sempre zona, data e PDF."}
    except Exception as exc:
        raise HTTPException(502, detail="Documento non leggibile: " + str(exc)[:180])
