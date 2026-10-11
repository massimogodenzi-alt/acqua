"""Treblex Acqua Lazio: ricerca sperimentale dei rapporti Acqualatina."""
import io, re, unicodedata
from urllib.parse import urljoin, urlparse
import httpx, pdfplumber
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pathlib import Path

app = FastAPI(title="Treblex Acqua Lazio")
SOURCE = "https://www.acqualatina.it/qualita-dellacqua-per-comune/"
PARAMS = ["Durezza", "Residuo fisso", "Conducibilità", "Nitrati", "Sodio", "Arsenico", "pH"]

def normalize(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s.casefold()) if not unicodedata.combining(c))

@app.get("/", response_class=HTMLResponse)
def home():
    return Path(__file__).with_name("index.html").read_text(encoding="utf-8")

@app.get("/api/health")
def health():
    return {"status": "ok", "nota": "Non garantisce la disponibilità delle fonti esterne"}

@app.get("/api/rapporti")
async def reports(comune: str = Query(min_length=2, max_length=100)):
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.get(SOURCE)
            response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        matches = []
        for a in soup.select("a[href]"):
            url = urljoin(SOURCE, a["href"])
            if ".pdf" not in urlparse(url).path.lower() or not (urlparse(url).hostname or "").endswith("acqualatina.it"):
                continue
            if not a.get("href", "").lower().split("?")[0].endswith(".pdf"):
                continue
            parent = a.find_parent(["div", "li", "section", "article"])
            context = parent.get_text(" ", strip=True)[:300] if parent else ""
            title = a.get_text(" ", strip=True)
            haystack = " ".join([title, context, url])
            if normalize(comune) in normalize(title + " " + url):
                matches.append({"titolo": title or "Rapporto PDF", "url": url})
        return {"comune": comune, "gestore": "Acqualatina", "rapporti": matches,
                "fonte": SOURCE, "avviso": "Ricerca sperimentale: verificare comune e zona nel documento."}
    except Exception as exc:
        raise HTTPException(502, detail="Impossibile consultare la fonte: " + str(exc)[:180])

@app.get("/api/analisi")
async def analysis(url: str):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"acqualatina.it", "www.acqualatina.it"} or not parsed.path.lower().endswith(".pdf"):
        raise HTTPException(400, detail="Sono consentiti soltanto PDF Acqualatina")
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            response = await client.get(url)
            response.raise_for_status()
            if len(response.content) > 12000000 or not response.content.startswith(b"%PDF"):
                raise ValueError("Documento non PDF o troppo grande")
        with pdfplumber.open(io.BytesIO(response.content)) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages[:8])
        lines = text.splitlines()
        findings = []
        for name in PARAMS:
            for line in lines:
                if re.search(r"\b" + re.escape(name) + r"\b", line, re.I):
                    findings.append({"parametro": name, "riga_originale": line.strip()[:250],
                                     "stato": "DA_VERIFICARE"})
                    break
        return {"documento": url, "righe_da_verificare": findings,
                "avviso": "Nessun valore viene certificato automaticamente. Verificare le righe nel PDF originale."}
    except Exception as exc:
        raise HTTPException(502, detail="PDF non leggibile: " + str(exc)[:180])
