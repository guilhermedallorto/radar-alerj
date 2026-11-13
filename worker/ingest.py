#!/usr/bin/env python3
"""Coleta a Parte II da IOERJ, processa o PDF temporariamente e grava dados no Supabase.
O PDF nunca é enviado ao banco nem a qualquer serviço de armazenamento do Radar.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

try:
    import pymupdf as fitz  # PyMuPDF: API atual
except ImportError:
    import fitz  # compatibilidade com versões antigas
import pytesseract
import requests
from bs4 import BeautifulSoup
from PIL import Image
from playwright.async_api import async_playwright

PORTAL = "https://portal.ioerj.com.br/"
LAST_EDITION = "https://www.ioerj.com.br/portal/modules/conteudoonline/do_ultima_edicao.php"  # fallback
SELECT_DATE = "https://www.ioerj.com.br/portal/modules/conteudoonline/do_seleciona_data.php"
TZ = "America/Sao_Paulo"
MODEL = os.getenv("GEMINI_MODEL") or "gemini-3.1-flash-lite"
CHUNK_LIMIT = 14500

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
if not SUPABASE_KEY:
    raise RuntimeError("Defina o secret SUPABASE_SECRET_KEY (preferencial) ou SUPABASE_SERVICE_ROLE_KEY (legado).")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_KEY:
    raise RuntimeError("Defina o secret GEMINI_API_KEY no GitHub Actions.")

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; RadarLegislativoALERJ/2.0; +https://portal.ioerj.com.br/)",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.7",
})


def norm(value: Any) -> str:
    s = unicodedata.normalize("NFKD", str(value or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s).strip().lower()


def sha256(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def http_get(url: str, *, stream: bool = False) -> requests.Response:
    response = session.get(url, timeout=(20, 90), allow_redirects=True, stream=stream)
    response.raise_for_status()
    return response


def extract_date(soup: BeautifulSoup, selection_url: str) -> date:
    text = soup.get_text(" ", strip=True)
    months = {
        "janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5,
        "junho": 6, "julho": 7, "agosto": 8, "setembro": 9,
        "outubro": 10, "novembro": 11, "dezembro": 12,
    }
    match = re.search(r"(?:segunda|ter[cç]a|quarta|quinta|sexta|s[aá]bado|domingo)\s*,?\s*(\d{1,2})\s+de\s+([\wçãáâéêíóôõú]+)\s+de\s+(\d{4})", text, re.I)
    if match:
        month = months.get(norm(match.group(2)))
        if month:
            return date(int(match.group(3)), month, int(match.group(1)))

    query = parse_qs(urlparse(selection_url).query)
    encoded = query.get("data", [""])[0]
    if encoded:
        try:
            decoded = base64.b64decode(unquote(encoded) + "==").decode("utf-8", "ignore")
            found = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", decoded)
            if found:
                return date(int(found.group(1)), int(found.group(2)), int(found.group(3)))
        except Exception:
            pass
    raise RuntimeError("Não foi possível identificar a data da última edição no portal da IOERJ.")


def find_part_ii_link(html: str, base_url: str) -> str:
    """Localiza o caderno mesmo quando o portal muda a marcação do link."""
    soup = BeautifulSoup(html, "html.parser")

    for element in soup.find_all(["a", "button", "area"]):
        label_parts = [
            element.get_text(" ", strip=True),
            element.get("title", ""),
            element.get("aria-label", ""),
            element.get("data-original-title", ""),
            element.get("onclick", ""),
        ]
        label = norm(" ".join(str(x) for x in label_parts if x))
        if "parte ii" not in label:
            continue
        if "legislativo" not in label and "poder legislativo" not in label:
            # O portal também pode abreviar o rótulo visual, mas manter o destino
            # explícito no href ou no onclick.
            destination_hint = norm(str(element.get("href", "")) + " " + str(element.get("onclick", "")))
            if "legislativo" not in destination_hint and "parte_ii" not in destination_hint and "parteii" not in destination_hint:
                continue

        href = element.get("href") or element.get("data-href") or element.get("data-url")
        if href and not href.strip().lower().startswith(("javascript:", "#")):
            return urljoin(base_url, href.strip())

        # Alguns templates implementam os botões por JavaScript.
        onclick = str(element.get("onclick", ""))
        match = re.search(r"(?:location(?:\.href)?|open)\s*\(?\s*['\"]([^'\"]+)['\"]", onclick, re.I)
        if match:
            return urljoin(base_url, match.group(1))
        match = re.search(r"['\"]([^'\"]*(?:mostra_edicao|conteudoonline)[^'\"]*)['\"]", onclick, re.I)
        if match:
            return urljoin(base_url, match.group(1))

    # Diagnóstico explícito: ajuda a identificar alterações ou respostas
    # intermediárias (bloqueio, página de erro ou HTML diferente) no portal.
    visible_links = []
    for a in soup.find_all("a", href=True)[:35]:
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))[:90]
        visible_links.append(f"{text!r} -> {a.get('href', '')[:180]}")
    page_text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:500]
    diagnostic = (
        f"URL consultada: {base_url}. Título HTML: {soup.title.get_text(' ', strip=True) if soup.title else '(sem título)'}. "
        f"Texto inicial: {page_text!r}. Links encontrados: {' | '.join(visible_links) or '(nenhum)'}"
    )
    raise RuntimeError(
        "A página da edição foi localizada, mas o link da Parte II (Poder Legislativo) não foi encontrado. "
        + diagnostic
    )


def find_edition_continuation(response: requests.Response) -> str | None:
    """Encontra o passo intermediário da IOERJ sem depender da codificação do texto.

    Em algumas respostas, o portal declara/é interpretado como ISO-8859-1 embora
    entregue texto UTF-8. Nesse caso, "última edição" chega como "Ãºltima edição"
    e uma detecção baseada apenas no texto falha. O caminho do link e o parâmetro
    `data` são ASCII e continuam identificáveis.
    """
    soup = BeautifulSoup(response.text, "html.parser")

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href", "")).strip()
        if not href or href.lower().startswith(("javascript:", "#")):
            continue
        label = norm(anchor.get_text(" ", strip=True))
        parsed = urlparse(href)
        path = parsed.path.lower()
        query = parse_qs(parsed.query)

        # Forma observada no portal: do_seleciona_edicao.php?data=...&PHPSESSID=...
        if path.endswith("do_seleciona_edicao.php"):
            return urljoin(response.url, href)

        # Compatibilidade caso o portal altere o nome do endpoint, mas mantenha
        # o link textual e o parâmetro que codifica a data da edição.
        if "clique aqui" in label and "data" in query:
            return urljoin(response.url, href)

    # Fallback para redirecionamentos implementados por meta refresh.
    meta = soup.find("meta", attrs={"http-equiv": re.compile(r"refresh", re.I)})
    if meta and meta.get("content"):
        match = re.search(r"url\s*=\s*['\"]?([^'\";]+)", str(meta["content"]), re.I)
        if match:
            return urljoin(response.url, match.group(1).strip())

    return None


def date_from_calendar_link(href: str) -> date | None:
    """Decodifica a data Base64 usada nos links do calendário da IOERJ."""
    parsed = urlparse(href)
    values = parse_qs(parsed.query).get("data", [])
    for value in values:
        try:
            token = unquote(value).strip()
            token += "=" * (-len(token) % 4)
            decoded = base64.b64decode(token, validate=False).decode("ascii", "ignore").strip()
            match = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", decoded)
            if match:
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except Exception:
            continue
    return None


def discover_latest_from_calendar() -> tuple[date, str]:
    """Entra pelo calendário de datas e seleciona a edição mais recente disponível."""
    calendar = http_get(SELECT_DATE)
    soup = BeautifulSoup(calendar.text, "html.parser")
    today_local = datetime.now(ZoneInfo(TZ)).date()
    candidates: list[tuple[date, str]] = []

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href", "")).strip()
        if not href:
            continue
        target = urljoin(calendar.url, href)
        path = urlparse(target).path.lower()
        if not path.endswith("do_seleciona_edicao.php"):
            continue
        edition_day = date_from_calendar_link(target)
        if edition_day:
            candidates.append((edition_day, target))

    # O calendário pode listar vários anos. Escolhemos a maior data que não
    # ultrapasse a data local de hoje, para não selecionar um dia futuro.
    eligible = [(day, url) for day, url in candidates if day <= today_local]
    if not eligible:
        raise RuntimeError(
            "A página de seleção de datas da IOERJ foi aberta, mas nenhum dia com edição foi identificado. "
            f"URL: {calendar.url}; links de edição encontrados: {len(candidates)}."
        )
    return max(eligible, key=lambda item: item[0])


def discover_latest() -> dict[str, Any]:
    # Caminho principal: calendário de datas -> página da edição -> link
    # explicitamente rotulado Parte II (Poder Legislativo).
    try:
        calendar_date, selection_url = discover_latest_from_calendar()
    except Exception as calendar_exc:
        # Só usa o caminho legado se a própria consulta ao calendário falhar.
        # Se a página de edição existir, mas não trouxer o link da Parte II ou
        # apresentar data divergente, a coleta deve parar em vez de tentar
        # adivinhar outro arquivo.
        print(f"Falha ao localizar uma data pelo calendário ({calendar_exc}); tentando o caminho legado.")
        home = http_get(PORTAL)
        home_soup = BeautifulSoup(home.text, "html.parser")
        last_url = None
        for a in home_soup.find_all("a", href=True):
            if "ultima edicao" in norm(a.get_text(" ", strip=True)):
                last_url = urljoin(home.url, a["href"])
                break
        latest = http_get(last_url or LAST_EDITION)
        visited = {latest.url}
        for _ in range(3):
            continuation = find_edition_continuation(latest)
            if not continuation or continuation in visited:
                break
            visited.add(continuation)
            latest = http_get(continuation)
        else:
            raise RuntimeError("O portal da IOERJ apresentou redirecionamentos intermediários em excesso.")
        selection_url = latest.url
        latest_soup = BeautifulSoup(latest.text, "html.parser")
        part_url = find_part_ii_link(latest.text, selection_url)
        edition_date = extract_date(latest_soup, selection_url)
    else:
        latest = http_get(selection_url)
        latest_soup = BeautifulSoup(latest.text, "html.parser")
        # A ausência do link correto é erro fatal; não substitui a Parte II
        # por outro PDF localizado na página ou em links relacionados.
        part_url = find_part_ii_link(latest.text, latest.url)
        edition_date = extract_date(latest_soup, latest.url)
        if edition_date != calendar_date:
            raise RuntimeError(
                "A data da página da edição não corresponde à data codificada no calendário: "
                f"calendário={calendar_date.isoformat()}; página={edition_date.isoformat()}. "
                "A coleta foi interrompida para evitar selecionar outra edição."
            )

    label_match = re.search(r"Ano\s+([IVXLCDM]+).*?N[º°o]\s*(\d+)", latest_soup.get_text(" ", strip=True), re.I)
    edition_label = f"Ano {label_match.group(1).upper()} · Nº {label_match.group(2)}" if label_match else ""
    return {
        "edition_date": edition_date.isoformat(),
        "edition_label": edition_label,
        "selection_url": selection_url,
        "part_ii_url": part_url,
    }


PDFJS_EXTRACT_JS = r"""
async () => {
    const app = window.PDFViewerApplication;
    if (!app) {
        return {ok: false, motivo: "PDFViewerApplication não encontrado"};
    }
    if (!app.pdfDocument) {
        return {ok: false, motivo: "Documento ainda não carregado"};
    }

    const pdf = app.pdfDocument;
    const bytes = await pdf.getData();
    const partes = [];
    const tamanho = 49152;

    for (let i = 0; i < bytes.length; i += tamanho) {
        const bloco = bytes.subarray(i, Math.min(i + tamanho, bytes.length));
        let binario = "";
        for (let j = 0; j < bloco.length; j += 8192) {
            binario += String.fromCharCode(...bloco.subarray(j, j + 8192));
        }
        partes.push(btoa(binario));
    }

    return {
        ok: true,
        paginas: pdf.numPages,
        tamanho: bytes.length,
        base64: partes.join(""),
        url_visualizador: location.href,
        titulo: document.title
    };
}
"""


async def _extract_pdf_from_pdfjs(part_ii_url: str) -> dict[str, Any]:
    """Abre o visualizador oficial e recupera os bytes do PDF.js já carregado."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            context = await browser.new_context(
                extra_http_headers={
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                }
            )
            page = await context.new_page()

            # Impede que uma cópia antiga em cache ou service worker seja usada.
            try:
                cdp = await context.new_cdp_session(page)
                await cdp.send("Network.enable")
                await cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})
                await cdp.send("Network.setBypassServiceWorker", {"bypass": True})
            except Exception as exc:
                print(f"Aviso: não foi possível configurar o cache do Chromium: {exc}")

            print(f"Abrindo visualizador da Parte II: {part_ii_url}")
            response = await page.goto(
                part_ii_url,
                wait_until="domcontentloaded",
                timeout=120000,
            )
            if response:
                print(f"Resposta HTTP do visualizador: {response.status}")

            # Aguarda a inicialização inicial do PDF.js, que pode terminar
            # alguns segundos depois do carregamento do HTML.
            await page.wait_for_timeout(10000)
            extraction = None
            last_reasons: list[str] = []
            # O PDF.js pode estar na página principal ou em um iframe.
            for attempt in range(90):
                for frame in page.frames:
                    try:
                        result = await frame.evaluate(PDFJS_EXTRACT_JS)
                        if isinstance(result, dict) and result.get("ok"):
                            extraction = result
                            break
                        if isinstance(result, dict) and result.get("motivo"):
                            last_reasons.append(str(result["motivo"]))
                    except Exception as exc:
                        last_reasons.append(f"{frame.url}: {type(exc).__name__}")
                if extraction:
                    break
                await page.wait_for_timeout(2000)

            if not extraction:
                frame_info = []
                for frame in page.frames:
                    try:
                        info = await frame.evaluate("""() => ({
                            url: location.href,
                            titulo: document.title,
                            pdfViewerApplication: typeof window.PDFViewerApplication,
                            iframes: Array.from(document.querySelectorAll('iframe')).map(x => x.src),
                            scripts: Array.from(document.scripts).map(x => x.src).filter(Boolean)
                        })""")
                        frame_info.append(info)
                    except Exception as exc:
                        frame_info.append({"url": frame.url, "erro": str(exc)[:200]})
                raise RuntimeError(
                    "O visualizador da Parte II foi aberto, mas o PDF.js não disponibilizou "
                    "o documento dentro do prazo de espera. "
                    f"Últimos diagnósticos: {last_reasons[-8:]}. Frames: {frame_info}"
                )

            encoded = extraction.get("base64")
            if not encoded:
                raise RuntimeError("O PDF.js indicou sucesso, mas não retornou os bytes do documento.")

            pdf_bytes = base64.b64decode(encoded, validate=True)
            if not pdf_bytes.startswith(b"%PDF"):
                raise RuntimeError("Os bytes extraídos do visualizador não começam com a assinatura %PDF.")
            if len(pdf_bytes) != int(extraction.get("tamanho") or -1):
                raise RuntimeError(
                    "O tamanho dos bytes extraídos não confere com o tamanho informado pelo PDF.js."
                )

            # Confere também a contagem de páginas entre o PDF.js e o parser
            # local, evitando aceitar transferências truncadas ou inconsistentes.
            try:
                local_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
                local_page_count = len(local_doc)
                local_doc.close()
            except Exception as exc:
                raise RuntimeError(f"PyMuPDF não conseguiu abrir os bytes recebidos do PDF.js: {exc}") from exc
            if local_page_count != int(extraction.get("paginas") or -1):
                raise RuntimeError(
                    "A quantidade de páginas informada pelo PDF.js não confere com o arquivo extraído: "
                    f"PDF.js={extraction.get('paginas')}; PyMuPDF={local_page_count}."
                )

            extraction["pdf_bytes"] = pdf_bytes
            return extraction
        finally:
            await browser.close()


def find_pdf_and_html(part_url: str) -> tuple[str, str, bytes]:
    """Extrai o PDF correspondente ao link de Parte II selecionado.

    Não tenta adivinhar arquivos por links genéricos do HTML. Para os links
    mostra_edicao.php, lê o próprio documento carregado no PDF.js. Se o portal
    passar a fornecer um PDF direto, aceita-o somente quando o caminho terminar
    em .pdf; a validação do conteúdo ocorre logo depois, antes do Gemini.
    """
    parsed = urlparse(part_url)
    if parsed.path.lower().endswith(".pdf"):
        response = http_get(part_url)
        if not response.content.startswith(b"%PDF"):
            raise RuntimeError(
                f"O link direto da Parte II não retornou um PDF válido: {response.url}"
            )
        return response.url, part_url, response.content

    extraction = asyncio.run(_extract_pdf_from_pdfjs(part_url))
    pdf_bytes = extraction.pop("pdf_bytes")
    print(
        "PDF extraído do visualizador: "
        f"{extraction.get('paginas')} páginas; "
        f"{len(pdf_bytes):,} bytes; título={extraction.get('titulo')!r}; "
        f"URL={extraction.get('url_visualizador')}"
    )
    # O portal não expõe necessariamente uma URL pública do arquivo binário;
    # a URL de origem verificável é a própria página oficial do visualizador.
    return str(extraction.get("url_visualizador") or part_url), part_url, pdf_bytes

def validate_part_ii_pdf(pdf_bytes: bytes, expected_date: str | None = None) -> dict[str, Any]:
    """Confere assinatura, abertura, identificação do caderno e data da edição."""
    if not isinstance(pdf_bytes, (bytes, bytearray)) or not bytes(pdf_bytes).startswith(b"%PDF"):
        raise RuntimeError("O conteúdo recebido não é um arquivo PDF válido (assinatura %PDF ausente).")

    try:
        doc = fitz.open(stream=bytes(pdf_bytes), filetype="pdf")
    except Exception as exc:
        raise RuntimeError(f"Não foi possível abrir o PDF extraído do visualizador: {exc}") from exc

    try:
        if doc.is_encrypted:
            raise RuntimeError("O PDF da Parte II está criptografado.")
        if len(doc) < 1:
            raise RuntimeError("O PDF extraído não contém páginas.")

        first_page_text = doc[0].get_text("text", sort=False)
        first_norm = norm(first_page_text)
        required_markers = {
            "identificação Parte II": "parte ii" in first_norm,
            "identificação Poder Legislativo": "poder legislativo" in first_norm,
            "identificação Assembleia Legislativa": "assembleia legislativa" in first_norm,
        }
        missing = [label for label, found in required_markers.items() if not found]
        if missing:
            excerpt = re.sub(r"\s+", " ", first_page_text)[:900]
            raise RuntimeError(
                "O PDF localizado não passou na validação de identidade da Parte II — Poder Legislativo. "
                f"Marcadores ausentes: {', '.join(missing)}. Início da primeira página: {excerpt!r}"
            )

        months = {
            "janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5,
            "junho": 6, "julho": 7, "agosto": 8, "setembro": 9,
            "outubro": 10, "novembro": 11, "dezembro": 12,
        }
        date_match = re.search(
            r"\b(\d{1,2})\s+de\s+(janeiro|fevereiro|marco|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro)\s+de\s+(\d{4})\b",
            first_norm,
        )
        document_date = None
        if date_match:
            document_date = date(
                int(date_match.group(3)),
                months[date_match.group(2)],
                int(date_match.group(1)),
            ).isoformat()
        if expected_date and document_date and document_date != expected_date:
            raise RuntimeError(
                "A data impressa no PDF não corresponde à edição selecionada no calendário: "
                f"calendário={expected_date}; primeira página={document_date}."
            )
        if expected_date and not document_date:
            raise RuntimeError(
                "Não foi possível confirmar a data impressa na primeira página do PDF; "
                "a coleta foi interrompida para evitar processar uma edição diferente."
            )

        metadata = doc.metadata or {}
        return {
            "page_count": len(doc),
            "document_date": document_date,
            "title": metadata.get("title"),
            "author": metadata.get("author"),
            "creator": metadata.get("creator"),
        }
    finally:
        doc.close()


def extract_pdf_pages(pdf_bytes: bytes) -> list[dict[str, Any]]:
    """Extrai o texto por coluna, na ordem de leitura do Diário Oficial.

    A Parte II é diagramada em três colunas. A extração linear com sort=True
    pode intercalar trechos de colunas diferentes; por isso, os blocos são
    separados pelas mesmas faixas horizontais usadas na conferência manual.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages: list[dict[str, Any]] = []
    for index, page in enumerate(doc, start=1):
        width = page.rect.width
        limit_1 = width * 0.347
        limit_2 = width * 0.645
        columns: list[list[tuple[float, float, str]]] = [[], [], []]

        for block in page.get_text("blocks", sort=False):
            if len(block) < 5:
                continue
            x0, y0, _x1, _y1, raw_text = block[:5]
            text = str(raw_text or "").strip()
            if not text or y0 < 75:
                continue  # cabeçalho repetido; a capa é validada separadamente
            text = re.sub(r"(?<=\w)-[ \t]*\n[ \t]*(?=\w)", "", text)
            if x0 < limit_1:
                column_index = 0
            elif x0 < limit_2:
                column_index = 1
            else:
                column_index = 2
            columns[column_index].append((float(y0), float(x0), text))

        column_texts = []
        for column in columns:
            column.sort(key=lambda item: (item[0], item[1]))
            if column:
                column_texts.append("\n".join(item[2] for item in column))
        text = "\n\n".join(column_texts).strip()

        # OCR apenas como contingência para páginas sem camada textual útil.
        if len(re.sub(r"\s", "", text)) < 35:
            pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
            image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            try:
                ocr = pytesseract.image_to_string(image, lang="por+eng", config="--psm 6")
                if len(ocr.strip()) > len(text.strip()):
                    text = ocr.strip()
            except Exception as exc:
                print(f"OCR não disponível na página {index}: {exc}")

        if text:
            pages.append({"page": index, "text": text})
    doc.close()
    return pages

def html_pages(html_bytes: bytes) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html_bytes, "html.parser")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    text = soup.get_text("\n", strip=True)
    return [{"page": 1, "text": text}] if text else []


def make_chunks(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    current: list[str] = []
    size = 0
    start_page = None
    end_page = None
    for page in pages:
        page_text = f"\n\n[PÁGINA {page['page']}]\n{page['text']}"
        # Divisão especial para páginas excepcionalmente grandes.
        blocks = [page_text[i:i + CHUNK_LIMIT] for i in range(0, len(page_text), CHUNK_LIMIT)]
        for block in blocks:
            if current and size + len(block) > CHUNK_LIMIT:
                chunks.append({"start_page": start_page, "end_page": end_page, "text": "".join(current)})
                current, size, start_page, end_page = [], 0, None, None
            if start_page is None:
                start_page = page["page"]
            end_page = page["page"]
            current.append(block)
            size += len(block)
    if current:
        chunks.append({"start_page": start_page, "end_page": end_page, "text": "".join(current)})
    return chunks


def supabase_headers() -> dict[str, str]:
    headers = {"apikey": SUPABASE_KEY, "Content-Type": "application/json"}
    # As novas secret keys (sb_secret_...) não são JWTs e devem ir apenas em apikey.
    # A chave legada service_role é JWT e, por compatibilidade, também vai em Authorization.
    if not SUPABASE_KEY.startswith("sb_secret_"):
        headers["Authorization"] = f"Bearer {SUPABASE_KEY}"
    return headers


def db_get(table: str, params: dict[str, str] | None = None) -> Any:
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", params=params or {}, headers=supabase_headers(), timeout=45)
    r.raise_for_status()
    return r.json()


def db_post(table: str, rows: Any, *, on_conflict: str | None = None) -> Any:
    params = {"on_conflict": on_conflict} if on_conflict else {}
    headers = supabase_headers()
    headers["Prefer"] = "resolution=merge-duplicates,return=representation"
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}", params=params, headers=headers, json=rows, timeout=90)
    if not r.ok:
        raise RuntimeError(f"Supabase POST {table} falhou ({r.status_code}): {r.text[:1500]}")
    return r.json() if r.text.strip() else None


def db_patch(table: str, params: dict[str, str], body: dict[str, Any]) -> None:
    headers = supabase_headers()
    headers["Prefer"] = "return=minimal"
    r = requests.patch(f"{SUPABASE_URL}/rest/v1/{table}", params=params, headers=headers, json=body, timeout=60)
    if not r.ok:
        raise RuntimeError(f"Supabase PATCH {table} falhou ({r.status_code}): {r.text[:1500]}")


def db_delete(table: str, params: dict[str, str]) -> None:
    headers = supabase_headers()
    r = requests.delete(f"{SUPABASE_URL}/rest/v1/{table}", params=params, headers=headers, timeout=60)
    if not r.ok:
        raise RuntimeError(f"Supabase DELETE {table} falhou ({r.status_code}): {r.text[:1500]}")


def log_run(status: str, message: str, edition_date: str | None = None, total: int = 0, relevant: int = 0, run_id: str | None = None) -> None:
    finished = datetime.now(timezone.utc).isoformat()
    if run_id:
        db_patch("ingestion_runs", {"id": f"eq.{run_id}"}, {"finished_at": finished, "status": status, "message": message, "publications_found": total, "publications_relevant": relevant})
    else:
        db_post("ingestion_runs", {"status": status, "edition_date": edition_date, "message": message, "finished_at": finished, "publications_found": total, "publications_relevant": relevant})


def gemini_json(prompt: str, max_output_tokens: int = 8000) -> dict[str, Any] | list[Any]:
    """Solicita JSON ao Gemini com backoff para 429/503 e falhas de rede/timeout."""
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "maxOutputTokens": max_output_tokens,
        },
    }
    response = None
    max_attempts = 6
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.post(
                endpoint,
                headers={"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"},
                json=payload,
                timeout=(25, 120),
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            if attempt >= max_attempts:
                raise RuntimeError(
                    f"Gemini não respondeu após {max_attempts} tentativas; última falha de rede/timeout: {exc}"
                ) from exc
            wait_seconds = min(90, 5 * (2 ** (attempt - 1)))
            print(
                f"Gemini sem resposta ({type(exc).__name__}); tentativa {attempt}/{max_attempts}. "
                f"Nova tentativa em {wait_seconds}s."
            )
            time.sleep(wait_seconds)
            continue

        if response.ok:
            break
        if response.status_code not in (429, 500, 502, 503, 504) or attempt >= max_attempts:
            raise RuntimeError(
                f"Gemini API retornou HTTP {response.status_code} após {attempt} tentativa(s): {response.text[:1500]}"
            )

        retry_after = response.headers.get("Retry-After", "")
        try:
            wait_seconds = float(retry_after)
        except (TypeError, ValueError):
            wait_seconds = min(90, 5 * (2 ** (attempt - 1)))
        wait_seconds = max(3, min(wait_seconds, 120))
        print(
            f"Gemini temporariamente indisponível (HTTP {response.status_code}); "
            f"tentativa {attempt}/{max_attempts}, nova tentativa em {wait_seconds:.0f}s."
        )
        time.sleep(wait_seconds)
    else:
        raise RuntimeError("Gemini não respondeu após todas as tentativas configuradas.")

    data = response.json()
    candidates = data.get("candidates") or []
    parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
    output = "\n".join(
        part.get("text", "")
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str) and not part.get("thought")
    ).strip()
    if not output:
        feedback = data.get("promptFeedback", {}).get("blockReason")
        finish_reason = candidates[0].get("finishReason") if candidates else None
        raise RuntimeError(
            "Gemini não devolveu texto utilizável. "
            f"Motivo de bloqueio: {feedback or 'não informado'}; conclusão: {finish_reason or 'sem candidato'}."
        )
    # O Gemini pode respeitar o schema conceitual e ainda devolver a lista
    # diretamente, em vez do invólucro {"items": [...]}. Ambos são aceitos;
    # cada etapa valida o formato que efetivamente espera.
    candidate_json = output.strip()
    if candidate_json.startswith("```"):
        candidate_json = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate_json, flags=re.I).strip()
    try:
        parsed = json.loads(candidate_json)
        if not isinstance(parsed, (dict, list)):
            raise ValueError("A resposta JSON deve ser um objeto ou uma lista.")
        return parsed
    except Exception as exc:
        raise RuntimeError(f"O Gemini não devolveu JSON válido: {output[:800]}") from exc


def load_themes() -> list[dict[str, Any]]:
    rows = db_get("themes", {"select": "id,name,description,keywords,include_criteria,exclude_criteria,priority", "active": "eq.true", "order": "priority.asc"})
    return rows or []


def classify_chunk(chunk: dict[str, Any], edition_date: str, themes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    theme_info = themes if themes else "Nenhum tema ativo. Identifique e preserve todos os atos, mas relevant=false, matched_themes=[] e summary vazio."
    prompt = f"""Você é um analista documental do gabinete parlamentar. Analise o texto da edição do Diário Oficial do Estado do Rio de Janeiro, Parte II — Poder Legislativo.

TAREFA: segmentar o texto em publicações oficiais individuais. Não descarte atos por parecerem pouco relevantes. A abrangência é TODAS as publicações do caderno.

REGRAS:
- Preserve o conteúdo material e os dados. Não invente títulos, órgãos, pessoas, números, municípios, valores ou páginas.
- Agrupe itens somente quando a própria publicação indicar uma relação/listagem conjunta.
- Considere palavras-chave como pistas, mas classifique pelos critérios substantivos de cada tema.
- Uma publicação só é relevante se corresponder claramente a pelo menos um tema ativo. Use matched_themes com os nomes exatos dos temas.
- Para itens relevantes, escreva resumo objetivo de 2 a 4 frases e justification específica sobre a relação com o tema. Para itens não relevantes, summary deve ser vazio.
- O texto do ato deve ser fiel ao documento e suficiente para consulta posterior. Não faça paráfrase no campo text.
- Quando não souber, use null ou array vazio. Páginas são a numeração física do PDF.

DATA DA EDIÇÃO: {edition_date}
TEMAS ATIVOS (nome, descrição, palavras-chave, inclusão, exclusão e prioridade):
{json.dumps(theme_info, ensure_ascii=False)}

RETORNE SOMENTE JSON no formato {{"items":[{{"page_start":1,"page_end":1,"type":"...","title":"...","text":"...","organization":"...","people":[],"municipalities":[],"amounts":[],"relevant":false,"matched_themes":[],"confidence":0.0,"justification":"...","summary":"..."}}]}}

TEXTO:
{chunk['text']}"""
    result = gemini_json(prompt, 9000)
    if isinstance(result, list):
        # Formato observado na execução: array de publicações na raiz.
        items = result
        print("Aviso: Gemini retornou uma lista na raiz; formato normalizado para items.")
    elif isinstance(result, dict):
        items = result.get("items", [])
    else:
        raise RuntimeError("A classificação do Gemini não retornou um objeto nem uma lista.")
    if not isinstance(items, list):
        raise RuntimeError("A classificação do Gemini não contém uma lista válida em 'items'.")
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError("A lista de publicações do Gemini contém um item que não é objeto JSON.")
        if item.get("page_start") is None:
            item["page_start"] = chunk.get("start_page")
        if item.get("page_end") is None:
            item["page_end"] = chunk.get("end_page")
    return items


def clean_str(value: Any, default: str = "") -> str:
    return str(value if value is not None else default).strip()


def publication_key(edition_date: str, item: dict[str, Any]) -> str:
    body = clean_str(item.get("text"))
    title = norm(item.get("title"))
    return sha256(f"{edition_date}|{title}|{norm(body[:1200])}")


def daily_summary(edition_date: str, publications: list[dict[str, Any]]) -> str:
    summaries = []
    for p in publications[:130]:
        excerpt = re.sub(r"\s+", " ", clean_str(p.get("text")))[:360]
        summaries.append({
            "type": p.get("type"), "title": p.get("title"), "excerpt": excerpt,
            "themes": p.get("matched_themes", []), "relevant": bool(p.get("relevant")),
        })
    prompt = f"""Redija uma síntese diária para gabinete parlamentar sobre a edição da Parte II do Diário Oficial do Estado do Rio de Janeiro em {edition_date}.
Use somente as publicações listadas. Seja factual, conciso e institucional. Informe os destaques temáticos encontrados, os principais atos e lacunas relevantes. Não invente fatos ou contagens. Quando nenhum tema do gabinete aparecer, diga isso claramente. Produza um parágrafo entre 100 e 180 palavras. Retorne JSON {{\"summary\":\"...\"}}.

PUBLICAÇÕES:
{json.dumps(summaries, ensure_ascii=False)}"""
    result = gemini_json(prompt, 1200)
    if isinstance(result, dict) and clean_str(result.get("summary")):
        return clean_str(result.get("summary"))
    return (
        f"A edição de {edition_date} foi processada e contém {len(publications)} publicações. "
        "A síntese automática não foi disponibilizada nesta execução; consulte os registros individuais."
    )


def edition_exists(edition_date: str, pdf_hash: str) -> dict[str, Any] | None:
    found = db_get("editions", {"select": "id,edition_date,pdf_sha256,status", "edition_date": f"eq.{edition_date}", "limit": "1"})
    if not found:
        return None
    row = found[0]
    if row.get("pdf_sha256") == pdf_hash and row.get("status") == "processed":
        return row
    return row


def process() -> None:
    run_rows = db_post("ingestion_runs", {"status": "running", "message": "Iniciando localização da última edição."})
    run_id = run_rows[0]["id"] if run_rows else None
    edition_date = None
    existing = None
    preserve_processed_edition = False
    try:
        source = discover_latest()
        edition_date = source["edition_date"]
        print(f"Edição detectada no calendário: {edition_date} — {source['edition_label']}")
        print(f"Página de seleção da edição: {source['selection_url']}")
        print(f"Link identificado como Parte II — Poder Legislativo: {source['part_ii_url']}")

        pdf_url, part_url, content = find_pdf_and_html(source["part_ii_url"])
        pdf_info = validate_part_ii_pdf(content, expected_date=edition_date)
        pdf_hash = sha256(content)
        print(
            "Validação do PDF aprovada: Parte II — Poder Legislativo; "
            f"data impressa={pdf_info['document_date']}; páginas={pdf_info['page_count']}; "
            f"autor={pdf_info.get('author')!r}; criador={pdf_info.get('creator')!r}."
        )
        print(f"URL do visualizador/arquivo-fonte: {pdf_url}")

        existing = edition_exists(edition_date, pdf_hash)
        if existing and existing.get("pdf_sha256") == pdf_hash and existing.get("status") == "processed":
            log_run("success", "Edição já processada; nenhuma duplicidade criada.", edition_date, run_id=run_id)
            print("Edição já processada. Encerrando.")
            return
        preserve_processed_edition = bool(existing and existing.get("status") == "processed")

        pages = extract_pdf_pages(content)
        if not pages or sum(len(page["text"]) for page in pages) < 500:
            raise RuntimeError("Não foi extraído texto suficiente do PDF da Parte II. A edição não foi marcada como processada.")

        themes = load_themes()
        chunks = make_chunks(pages)
        if not chunks:
            raise RuntimeError("Não foi possível criar lotes de texto a partir do PDF da Parte II.")

        all_items: list[dict[str, Any]] = []
        for number, chunk in enumerate(chunks, start=1):
            print(f"Analisando lote {number}/{len(chunks)} (páginas {chunk['start_page']}–{chunk['end_page']}).")
            all_items.extend(classify_chunk(chunk, edition_date, themes))
            if number < len(chunks):
                time.sleep(7.0)  # Ritmo conservador para respeitar cotas do plano gratuito do Gemini.

        deduped: dict[str, dict[str, Any]] = {}
        for item in all_items:
            body = clean_str(item.get("text"))
            title = clean_str(item.get("title"), "Publicação sem título")
            if not body and title == "Publicação sem título":
                continue
            key = publication_key(edition_date, item)
            if key not in deduped or len(body) > len(clean_str(deduped[key].get("text"))):
                deduped[key] = item

        publications = list(deduped.values())
        if not publications:
            raise RuntimeError(
                "O Gemini não segmentou nenhuma publicação a partir do PDF validado. "
                "A edição anterior, caso exista, foi preservada."
            )

        rows = []
        relevant_count = 0
        for item in publications:
            relevant = bool(item.get("relevant")) and bool(item.get("matched_themes"))
            relevant_count += int(relevant)
            rows.append({
                "external_key": publication_key(edition_date, item),
                "page_start": item.get("page_start"),
                "page_end": item.get("page_end"),
                "kind": clean_str(item.get("type"), "Não identificado"),
                "title": clean_str(item.get("title"), "Publicação sem título"),
                "body_text": clean_str(item.get("text")),
                "organization": clean_str(item.get("organization")) or None,
                "people": item.get("people") if isinstance(item.get("people"), list) else [],
                "municipalities": item.get("municipalities") if isinstance(item.get("municipalities"), list) else [],
                "amounts": item.get("amounts") if isinstance(item.get("amounts"), list) else [],
                "ai_relevant": relevant,
                "matched_themes": item.get("matched_themes", []) if relevant else [],
                "confidence": item.get("confidence"),
                "justification": clean_str(item.get("justification")) if relevant else None,
                "summary": clean_str(item.get("summary")) if relevant else None,
                "source_url": part_url,
            })

        try:
            summary = daily_summary(edition_date, publications)
        except Exception as summary_exc:
            print(f"Aviso: não foi possível gerar a síntese diária; os atos serão preservados: {summary_exc}")
            summary = (
                f"A edição de {edition_date} foi processada e contém {len(publications)} publicações. "
                "A síntese automática não foi disponibilizada nesta execução; consulte os registros individuais."
            )

        # Só altera a edição no banco depois de concluir a extração e toda a
        # análise de IA. Assim, uma falha de navegador/Gemini não apaga dados
        # existentes nem marca uma edição antiga como erro.
        edition_payload = {
            "edition_date": edition_date,
            "edition_label": source["edition_label"],
            "source_page_url": source["selection_url"],
            "source_pdf_url": pdf_url,
            "pdf_sha256": pdf_hash,
            "status": "found",
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "error": None,
        }
        edition_rows = db_post("editions", edition_payload, on_conflict="edition_date")
        if edition_rows:
            edition_id = edition_rows[0]["id"]
        else:
            found = db_get("editions", {"select": "id", "edition_date": f"eq.{edition_date}", "limit": "1"})
            if not found:
                raise RuntimeError("A edição foi processada, mas não foi possível recuperar seu ID no Supabase.")
            edition_id = found[0]["id"]

        for row in rows:
            row["edition_id"] = edition_id

        # Primeiro grava todos os itens novos/atualizados. Só depois remove
        # publicações antigas que não pertençam mais ao PDF validado.
        for offset in range(0, len(rows), 60):
            db_post("publications", rows[offset:offset + 60], on_conflict="external_key")

        desired_keys = {row["external_key"] for row in rows}
        existing_publications = db_get(
            "publications",
            {"select": "id,external_key", "edition_id": f"eq.{edition_id}", "limit": "5000"},
        ) or []
        obsolete_ids = [
            row["id"] for row in existing_publications
            if row.get("external_key") not in desired_keys
        ]
        for offset in range(0, len(obsolete_ids), 100):
            id_list = ",".join(obsolete_ids[offset:offset + 100])
            db_delete("publications", {"id": f"in.({id_list})"})

        db_patch("editions", {"id": f"eq.{edition_id}"}, {
            "edition_label": source["edition_label"],
            "source_page_url": source["selection_url"],
            "source_pdf_url": pdf_url,
            "pdf_sha256": pdf_hash,
            "status": "processed",
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "processed_at": datetime.now(timezone.utc).isoformat(),
            "publication_count": len(rows),
            "relevant_count": relevant_count,
            "daily_summary": summary,
            "error": None,
        })
        log_run("success", f"Edição processada. {len(rows)} publicações; {relevant_count} relevantes. PDF não armazenado.", edition_date, len(rows), relevant_count, run_id)
        print(f"Concluído: {len(rows)} atos, {relevant_count} relevantes. PDF descartado ao final do processo.")

    except Exception as exc:
        message = f"Falha na coleta/processamento: {exc}"
        print(message, file=sys.stderr)
        try:
            # Não altera o status de uma edição que já estava processada: em
            # reprocessamentos malsucedidos, seus dados continuam válidos.
            if edition_date and not preserve_processed_edition:
                db_patch("editions", {"edition_date": f"eq.{edition_date}"}, {"status": "error", "error": message})
            log_run("error", message, edition_date, run_id=run_id)
        except Exception as log_exc:
            print(f"Também não foi possível registrar a falha no banco: {log_exc}", file=sys.stderr)
        raise


if __name__ == "__main__":
    process()
