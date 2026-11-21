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
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

try:
    import pymupdf as fitz  # PyMuPDF: API atual
except ImportError:
    import fitz  # compatibilidade com versões antigas
import pytesseract
import requests
from bs4 import BeautifulSoup, Tag
from PIL import Image
from playwright.async_api import async_playwright

PORTAL = "https://portal.ioerj.com.br/"
LAST_EDITION = "https://www.ioerj.com.br/portal/modules/conteudoonline/do_ultima_edicao.php"  # fallback
SELECT_DATE = "https://www.ioerj.com.br/portal/modules/conteudoonline/do_seleciona_data.php"
TZ = "America/Sao_Paulo"
MODEL = os.getenv("GEMINI_MODEL") or "gemini-3.1-flash-lite"

# Intervalo mínimo global entre chamadas ao Gemini.
# Corresponde a, no máximo, 12 requisições por minuto.
GEMINI_INTERVAL_SECONDS = 5.0

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


def adjacent_link_label(element: Tag) -> str:
    """Recupera rótulos que o portal coloca imediatamente fora do link.

    A IOERJ, por exemplo, publica a expressão 'EDIÇÃO EXTRA' como texto
    adjacente ao <a>, e não dentro dele. A leitura é limitada aos nós vizinhos
    até o próximo link para não misturar o rótulo de cadernos diferentes.
    """
    parts: list[str] = []

    for direction in ("previous_siblings", "next_siblings"):
        for sibling in getattr(element, direction):
            if isinstance(sibling, Tag):
                if sibling.name in {"a", "button", "area"} or sibling.find(["a", "button", "area"]):
                    break
                text = sibling.get_text(" ", strip=True)
            else:
                text = str(sibling).strip()

            if text:
                parts.append(text)
                break

    # Alguns layouts envolvem o link e seu rótulo em um contêiner próprio.
    # Só usamos o texto integral do contêiner quando ele contém um único
    # elemento clicável, evitando misturar o rótulo de links irmãos.
    parent = element.parent
    if isinstance(parent, Tag):
        clickable = parent.find_all(["a", "button", "area"])
        if len(clickable) == 1:
            parent_text = parent.get_text(" ", strip=True)
            if parent_text:
                parts.append(parent_text)

    return " ".join(parts)


def find_part_ii_links(
    html: str,
    base_url: str,
    edition_type: str = "regular",
    *,
    required: bool = True,
) -> list[str]:
    """Localiza todos os links da Parte II regular ou identificados como EDIÇÃO EXTRA."""
    if edition_type not in {"regular", "extra"}:
        raise ValueError(f"Tipo de edição inválido: {edition_type}")

    soup = BeautifulSoup(html, "html.parser")
    found: list[str] = []
    for element in soup.find_all(["a", "button", "area"]):
        label_parts = [
            element.get_text(" ", strip=True),
            adjacent_link_label(element),
            element.get("title", ""),
            element.get("aria-label", ""),
            element.get("data-original-title", ""),
            element.get("onclick", ""),
        ]
        label = norm(" ".join(str(x) for x in label_parts if x))
        if "parte ii" not in label:
            continue
        if "legislativo" not in label:
            destination_hint = norm(str(element.get("href", "")) + " " + str(element.get("onclick", "")))
            if "legislativo" not in destination_hint and "parte_ii" not in destination_hint and "parteii" not in destination_hint:
                continue

        is_extra = "edicao extra" in label
        if edition_type == "extra" and not is_extra:
            continue
        if edition_type == "regular" and is_extra:
            continue

        destination = element.get("href") or element.get("data-href") or element.get("data-url")
        if destination and not str(destination).strip().lower().startswith(("javascript:", "#")):
            target = urljoin(base_url, str(destination).strip())
        else:
            onclick = str(element.get("onclick", ""))
            match = re.search(r"(?:location(?:\.href)?|open)\s*\(?\s*['\"]([^'\"]+)['\"]", onclick, re.I)
            if not match:
                match = re.search(r"['\"]([^'\"]*(?:mostra_edicao|conteudoonline)[^'\"]*)['\"]", onclick, re.I)
            if not match:
                continue
            target = urljoin(base_url, match.group(1))
        detected_type = "extra" if is_extra else "regular"
        print(
            f"Link da Parte II identificado: tipo={detected_type}; "
            f"rótulo={label!r}; destino={target}"
        )
        if target not in found:
            found.append(target)

    if found:
        return found
    if not required:
        return []

    visible_links = []
    for a in soup.find_all("a", href=True)[:45]:
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))[:90]
        visible_links.append(f"{text!r} -> {a.get('href', '')[:180]}")
    page_text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:600]
    diagnostic = (
        f"URL consultada: {base_url}. Título HTML: {soup.title.get_text(' ', strip=True) if soup.title else '(sem título)'}. "
        f"Texto inicial: {page_text!r}. Links encontrados: {' | '.join(visible_links) or '(nenhum)'}"
    )
    wanted = "Parte II — Poder Legislativo · EDIÇÃO EXTRA" if edition_type == "extra" else "Parte II — Poder Legislativo (regular)"
    raise RuntimeError(
        f"A página da edição foi localizada, mas o link da {wanted} não foi encontrado. " + diagnostic
    )


def find_part_ii_link(
    html: str,
    base_url: str,
    edition_type: str = "regular",
    *,
    required: bool = True,
) -> str | None:
    """Compatibilidade: retorna o primeiro link de uma categoria."""
    links = find_part_ii_links(html, base_url, edition_type, required=required)
    return links[0] if links else None



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


def discover_calendar_selection(target_date: date | None = None) -> tuple[date, str] | None:
    """Localiza no calendário a data solicitada ou, sem data, a edição mais recente."""
    calendar = http_get(SELECT_DATE)
    soup = BeautifulSoup(calendar.text, "html.parser")
    today_local = datetime.now(ZoneInfo(TZ)).date()
    candidates: list[tuple[date, str]] = []

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href", "")).strip()
        if not href:
            continue
        target = urljoin(calendar.url, href)
        if not urlparse(target).path.lower().endswith("do_seleciona_edicao.php"):
            continue
        edition_day = date_from_calendar_link(target)
        if edition_day:
            candidates.append((edition_day, target))

    if target_date is not None:
        exact = [(day, url) for day, url in candidates if day == target_date]
        return exact[0] if exact else None

    eligible = [(day, url) for day, url in candidates if day <= today_local]
    if not eligible:
        raise RuntimeError(
            "A página de seleção de datas da IOERJ foi aberta, mas nenhum dia com edição foi identificado. "
            f"URL: {calendar.url}; links de edição encontrados: {len(candidates)}."
        )
    return max(eligible, key=lambda item: item[0])


def discover_latest_from_calendar() -> tuple[date, str]:
    result = discover_calendar_selection()
    if result is None:
        raise RuntimeError("Não foi possível localizar uma edição no calendário da IOERJ.")
    return result


def discover_editions(
    edition_type: str = "regular",
    target_date: date | None = None,
) -> list[dict[str, Any]]:
    """Localiza a edição regular ou todas as extras da data solicitada."""
    if edition_type not in {"regular", "extra"}:
        raise ValueError("edition_type deve ser 'regular' ou 'extra'.")

    scan_date = target_date or datetime.now(ZoneInfo(TZ)).date()

    if edition_type == "extra":
        calendar_result = discover_calendar_selection(scan_date)
        if calendar_result is None:
            print(
                f"O calendário da IOERJ ainda não lista {scan_date.isoformat()}; "
                "nenhuma edição extra a conferir nessa data."
            )
            return []
    else:
        calendar_result = discover_latest_from_calendar()

    calendar_date, selection_url = calendar_result
    latest = http_get(selection_url)
    latest_soup = BeautifulSoup(latest.text, "html.parser")
    page_url = latest.url
    edition_date = extract_date(latest_soup, page_url)

    if edition_date != calendar_date:
        raise RuntimeError(
            "A data da página da edição não corresponde à data codificada no calendário: "
            f"calendário={calendar_date.isoformat()}; página={edition_date.isoformat()}. "
            "A coleta foi interrompida para evitar selecionar outra edição."
        )

    if edition_type == "extra" and edition_date != scan_date:
        raise RuntimeError(
            f"A checagem de edição extra solicitada para {scan_date.isoformat()} "
            f"retornou {edition_date.isoformat()}."
        )

    part_urls = find_part_ii_links(
        latest.text,
        page_url,
        edition_type=edition_type,
        required=(edition_type == "regular"),
    )

    if not part_urls:
        print(
            f"A página de {edition_date.isoformat()} não apresenta link "
            "'Parte II — Poder Legislativo — EDIÇÃO EXTRA'."
        )
        return []

    if edition_type == "regular" and len(part_urls) != 1:
        raise RuntimeError(
            f"A página de {edition_date.isoformat()} apresentou {len(part_urls)} links "
            "de Parte II regular; a coleta foi interrompida para evitar escolher "
            "um caderno ambíguo."
        )

    label_match = re.search(
        r"Ano\s+([IVXLCDM]+).*?N[º°o]\s*(\d+)",
        latest_soup.get_text(" ", strip=True),
        re.I,
    )
    base_label = (
        f"Ano {label_match.group(1).upper()} · Nº {label_match.group(2)}"
        if label_match else "Parte II · Poder Legislativo"
    )

    total = len(part_urls)
    results = []
    for index, part_url in enumerate(part_urls, start=1):
        if edition_type == "extra":
            ordinal = f" {index}" if total > 1 else ""
            edition_label = f"{base_label} · EDIÇÃO EXTRA{ordinal}"
        else:
            edition_label = base_label

        results.append({
            "edition_date": edition_date.isoformat(),
            "edition_type": edition_type,
            "edition_label": edition_label,
            "extra_index": index if edition_type == "extra" else None,
            "extra_total": total if edition_type == "extra" else None,
            "selection_url": page_url,
            "part_ii_url": part_url,
        })

    return results


def discover_edition(edition_type: str = "regular") -> dict[str, Any] | None:
    """Compatibilidade com chamadas antigas; retorna a primeira edição encontrada."""
    editions = discover_editions(edition_type)
    return editions[0] if editions else None


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
            "junho": 6, "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10,
            "novembro": 11, "dezembro": 12,
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


def log_run(
    status: str,
    message: str,
    edition_date: str | None = None,
    edition_type: str | None = None,
    total: int = 0,
    relevant: int = 0,
    run_id: str | None = None,
    edition_key: str | None = None,
) -> None:
    finished = datetime.now(timezone.utc).isoformat()
    payload: dict[str, Any] = {
        "finished_at": finished,
        "status": status,
        "message": message,
        "publications_found": total,
        "publications_relevant": relevant,
    }
    if edition_date is not None:
        payload["edition_date"] = edition_date
    if edition_type is not None:
        payload["edition_type"] = edition_type
    if edition_key is not None:
        payload["edition_key"] = edition_key
    if run_id:
        db_patch("ingestion_runs", {"id": f"eq.{run_id}"}, payload)
    else:
        db_post("ingestion_runs", {"status": status, **payload})


def reserve_gemini_slot(
    interval_seconds: float = GEMINI_INTERVAL_SECONDS
) -> None:
    """
    Reserva um intervalo global entre chamadas ao Gemini.

    O controle fica no PostgreSQL, e não apenas na memória deste processo.
    Assim, execuções diferentes do GitHub Actions respeitam o mesmo intervalo.
    """
    endpoint = f"{SUPABASE_URL}/rest/v1/rpc/reserve_gemini_slot"

    response = requests.post(
        endpoint,
        headers=supabase_headers(),
        json={"p_interval_seconds": interval_seconds},
        timeout=(10, 90),
    )

    if not response.ok:
        raise RuntimeError(
            "Não foi possível reservar um intervalo para a API Gemini "
            f"no Supabase ({response.status_code}): {response.text[:1000]}"
        )


def gemini_json(prompt: str, max_output_tokens: int = 8000) -> dict[str, Any] | list[Any]:
    """Solicita JSON ao Gemini, respeitando o limitador global e tratando 429/503."""
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
        # Todas as execuções que utilizam este banco compartilham o mesmo controle.
        reserve_gemini_slot(
            interval_seconds=GEMINI_INTERVAL_SECONDS
        )

        try:
            response = requests.post(
                endpoint,
                headers={
                    "x-goog-api-key": GEMINI_KEY,
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=(25, 120),
            )

        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ) as exc:
            if attempt >= max_attempts:
                raise RuntimeError(
                    f"Gemini não respondeu após {max_attempts} tentativas; "
                    f"última falha de rede/timeout: {exc}"
                ) from exc

            wait_seconds = min(90, 5 * (2 ** (attempt - 1)))

            print(
                f"Gemini sem resposta ({type(exc).__name__}); "
                f"tentativa {attempt}/{max_attempts}. "
                f"Nova tentativa em {wait_seconds}s."
            )

            time.sleep(wait_seconds)
            continue

        if response.ok:
            break

        if response.status_code not in (429, 500, 502, 503, 504) or attempt >= max_attempts:
            raise RuntimeError(
                f"Gemini API retornou HTTP {response.status_code} "
                f"após {attempt} tentativa(s): {response.text[:1500]}"
            )

        # Respeita primeiro o Retry-After do HTTP.
        retry_after = response.headers.get("Retry-After", "")

        try:
            wait_seconds = float(retry_after)
        except (TypeError, ValueError):
            wait_seconds = None

        # Algumas respostas do Google trazem o tempo apenas no corpo JSON:
        # "Please retry in 33.0073285s."
        if wait_seconds is None:
            retry_match = re.search(
                r"retry in\s+([\d.]+)\s*s",
                response.text,
                re.I,
            )

            if retry_match:
                try:
                    wait_seconds = float(retry_match.group(1))
                except ValueError:
                    wait_seconds = None

        # Backoff exponencial se o Google não informar o tempo.
        if wait_seconds is None:
            wait_seconds = min(90, 5 * (2 ** (attempt - 1)))

        wait_seconds = max(3, min(wait_seconds, 180))

        print(
            f"Gemini temporariamente indisponível (HTTP {response.status_code}); "
            f"tentativa {attempt}/{max_attempts}, "
            f"nova tentativa em {wait_seconds:.0f}s."
        )

        time.sleep(wait_seconds)

    else:
        raise RuntimeError(
            "Gemini não respondeu após todas as tentativas configuradas."
        )

    data = response.json()
    candidates = data.get("candidates") or []
    parts = candidates[0].get("content", {}).get("parts", []) if candidates else []

    output = "\n".join(
        part.get("text", "")
        for part in parts
        if isinstance(part, dict)
        and isinstance(part.get("text"), str)
        and not part.get("thought")
    ).strip()

    if not output:
        feedback = data.get("promptFeedback", {}).get("blockReason")
        finish_reason = candidates[0].get("finishReason") if candidates else None

        raise RuntimeError(
            "Gemini não devolveu texto utilizável. "
            f"Motivo de bloqueio: {feedback or 'não informado'}; "
            f"conclusão: {finish_reason or 'sem candidato'}."
        )

    candidate_json = output.strip()

    if candidate_json.startswith("```"):
        candidate_json = re.sub(
            r"^```(?:json)?\s*|\s*```$",
            "",
            candidate_json,
            flags=re.I,
        ).strip()

    try:
        parsed = json.loads(candidate_json)

        if not isinstance(parsed, (dict, list)):
            raise ValueError("A resposta JSON deve ser um objeto ou uma lista.")

        return parsed

    except Exception as exc:
        raise RuntimeError(
            f"O Gemini não devolveu JSON válido: {output[:800]}"
        ) from exc


def classify_chunk(chunk: dict[str, Any], edition_date: str) -> list[dict[str, Any]]:
    """Segmenta o conteúdo do caderno em publicações, sem classificar temas globais."""
    prompt = f"""Você é um analista documental responsável por segmentar uma edição do Diário Oficial do Estado do Rio de Janeiro, Parte II — Poder Legislativo.

TAREFA: identificar e separar TODAS as publicações oficiais individuais presentes no trecho. Esta etapa é compartilhada entre usuários: NÃO avalie relevância temática, não selecione assuntos e não gere destaques para um gabinete específico.

REGRAS:
- Não descarte publicações por parecerem pouco relevantes.
- Preserve o conteúdo material, a sequência e os dados do documento.
- Não invente títulos, órgãos, pessoas, números, municípios, valores ou páginas.
- Agrupe itens somente quando a própria publicação indicar uma relação/listagem conjunta.
- O texto em `text` deve ser fiel ao documento, sem paráfrase, e suficiente para consulta posterior.
- Quando não souber, use null ou array vazio.
- `page_start` e `page_end` são números das páginas físicas do PDF.
- Os campos `relevant`, `matched_themes`, `confidence`, `justification` e `summary` devem ser, respectivamente, false, [], null, null e null. A análise temática será feita separadamente para cada usuário autenticado.

DATA DA EDIÇÃO: {edition_date}

RETORNE SOMENTE JSON no formato {{"items":[{{"page_start":1,"page_end":1,"type":"...","title":"...","text":"...","organization":"...","people":[],"municipalities":[],"amounts":[],"relevant":false,"matched_themes":[],"confidence":null,"justification":null,"summary":null}}]}}

TEXTO:
{chunk['text']}"""
    result = gemini_json(prompt, 9000)
    if isinstance(result, list):
        items = result
        print("Aviso: Gemini retornou uma lista na raiz; formato normalizado para items.")
    elif isinstance(result, dict):
        items = result.get("items", [])
    else:
        raise RuntimeError("A segmentação do Gemini não retornou um objeto nem uma lista.")
    if not isinstance(items, list):
        raise RuntimeError("A segmentação do Gemini não contém uma lista válida em 'items'.")
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError("A lista de publicações do Gemini contém um item que não é objeto JSON.")
        if item.get("page_start") is None:
            item["page_start"] = chunk.get("start_page")
        if item.get("page_end") is None:
            item["page_end"] = chunk.get("end_page")
        item["relevant"] = False
        item["matched_themes"] = []
        item["confidence"] = None
        item["justification"] = None
        item["summary"] = None
    return items


def clean_str(value: Any, default: str = "") -> str:
    return str(value if value is not None else default).strip()


def publication_key(
    edition_date: str,
    item: dict[str, Any],
    edition_type: str = "regular",
    edition_key: str | None = None,
) -> str:
    body = clean_str(item.get("text"))
    title = norm(item.get("title"))
    # Mantém as chaves antigas das edições regulares. Cada extra usa um
    # namespace derivado do hash do PDF, permitindo múltiplas extras no mesmo dia.
    if edition_type == "regular":
        prefix = edition_date
    else:
        prefix = f"{edition_date}|{edition_key or 'extra'}"
    return sha256(f"{prefix}|{title}|{norm(body[:1200])}")


def daily_summary(edition_date: str, publications: list[dict[str, Any]], edition_type: str = "regular") -> str:
    summaries = []
    for p in publications[:130]:
        excerpt = re.sub(r"\s+", " ", clean_str(p.get("text")))[:360]
        summaries.append({
            "type": p.get("type"), "title": p.get("title"), "excerpt": excerpt,
        })
    kind = "edição extra" if edition_type == "extra" else "edição regular"
    prompt = f"""Redija uma síntese factual da {kind} da Parte II — Poder Legislativo do Diário Oficial do Estado do Rio de Janeiro em {edition_date}.
Use somente as publicações listadas. Descreva os principais assuntos e tipos de atos encontrados, sem presumir temas de interesse de um usuário específico. Seja conciso, institucional e não invente fatos ou contagens. Produza um parágrafo entre 90 e 150 palavras. Retorne JSON {{\"summary\":\"...\"}}.

PUBLICAÇÕES:
{json.dumps(summaries, ensure_ascii=False)}"""
    result = gemini_json(prompt, 1200)
    if isinstance(result, dict) and clean_str(result.get("summary")):
        return clean_str(result.get("summary"))
    return (
        f"A {kind} de {edition_date} foi processada e contém {len(publications)} publicações. "
        "Consulte os registros individuais para examinar os atos publicados."
    )


def db_get_all(table: str, params: dict[str, str], page_size: int = 1000) -> list[dict[str, Any]]:
    """Lê todas as páginas de uma tabela REST sem depender do limite padrão do Supabase."""
    collected: list[dict[str, Any]] = []
    for offset in range(0, 100_000, page_size):
        page_params = {**params, "limit": str(page_size), "offset": str(offset)}
        page = db_get(table, page_params) or []
        if not isinstance(page, list):
            raise RuntimeError(f"A consulta de {table} não retornou uma lista.")
        collected.extend(page)
        if len(page) < page_size:
            return collected
    raise RuntimeError(f"A consulta de {table} excedeu o limite de 100.000 registros.")


def user_theme_signature(themes: list[dict[str, Any]], publications: list[dict[str, Any]]) -> str:
    """Replica a assinatura usada por analyze-user-themes para permitir cache compartilhado."""
    # Ordenação por UUID torna a assinatura idêntica à calculada pela Edge
    # Function, independentemente da collation do banco ou da ordem do REST.
    active_themes = sorted(
        [theme for theme in themes if theme.get("active")],
        key=lambda t: clean_str(t.get("id")),
    )
    ordered_publications = sorted(publications, key=lambda p: clean_str(p.get("id")))
    signature_source = {
        "themes": [
            {
                "id": t.get("id"),
                "name": t.get("name"),
                "description": t.get("description") or "",
                "keywords": t.get("keywords") or [],
                "include_criteria": t.get("include_criteria") or "",
                "exclude_criteria": t.get("exclude_criteria") or "",
                "priority": int(t.get("priority") or 3),
                "updated_at": t.get("updated_at"),
            }
            for t in active_themes
        ],
        "publications": [[p.get("id"), p.get("updated_at") or ""] for p in ordered_publications],
    }
    serialized = json.dumps(signature_source, ensure_ascii=False, separators=(",", ":"))
    return sha256(serialized)


def user_theme_cache_signature(themes: list[dict[str, Any]]) -> str:
    """
    Gera uma assinatura exclusiva dos temas ativos do usuário.

    Não inclui as publicações. Assim, a entrada de uma publicação nova
    não invalida o cache das demais.
    """
    active_themes = sorted(
        [theme for theme in themes if theme.get("active")],
        key=lambda t: clean_str(t.get("id")),
    )

    signature_source = {
        "themes": [
            {
                "id": t.get("id"),
                "name": t.get("name"),
                "description": t.get("description") or "",
                "keywords": t.get("keywords") or [],
                "include_criteria": t.get("include_criteria") or "",
                "exclude_criteria": t.get("exclude_criteria") or "",
                "priority": int(t.get("priority") or 3),
                "updated_at": t.get("updated_at"),
            }
            for t in active_themes
        ]
    }

    serialized = json.dumps(
        signature_source,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    return sha256(serialized)


def compact_publication_for_analysis(
    publication: dict[str, Any]
) -> dict[str, Any]:
    """
    Prepara a representação da publicação enviada ao Gemini.

    A mesma representação é utilizada para gerar a assinatura individual
    e para compor o prompt, evitando divergências entre o cache e a análise.
    """
    return {
        "publication_id": publication.get("id"),
        "type": publication.get("kind"),
        "title": publication.get("title"),
        "organization": publication.get("organization"),
        "page_start": publication.get("page_start"),
        "page_end": publication.get("page_end"),
        "text": re.sub(
            r"\s+",
            " ",
            clean_str(publication.get("body_text")),
        )[:2600],
    }


def user_publication_cache_signature(
    theme_cache_signature: str,
    publication: dict[str, Any],
) -> str:
    """
    Gera uma assinatura individual para cada publicação.

    O resultado depende dos temas ativos e do conteúdo efetivamente
    enviado ao Gemini para classificar aquela publicação.
    """
    signature_source = {
        "theme_signature": theme_cache_signature,
        "publication": compact_publication_for_analysis(publication),
    }

    serialized = json.dumps(
        signature_source,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    return sha256(serialized)


def analyze_edition_for_users(
    edition_id: str,
    edition_date: str,
    edition_label: str,
    only_user_id: str | None = None,
) -> dict[str, int]:
    """
    Analisa os destaques de uma edição separadamente para cada usuário.

    Quando only_user_id é informado, processa somente aquela conta.
    Cada lote concluído é salvo imediatamente, permitindo retomar a análise
    sem repetir chamadas ao Gemini que já foram concluídas.
    """

    themes_all = db_get_all("themes", {
        "select": (
            "id,user_id,name,description,keywords,include_criteria,"
            "exclude_criteria,priority,active,updated_at"
        ),
        "order": "user_id.asc,priority.asc,name.asc,id.asc",
    })

    themes_by_user: dict[str, list[dict[str, Any]]] = {}

    if only_user_id:
        # Mesmo que o usuário ainda não tenha cadastrado temas,
        # criamos a entrada para que a análise seja finalizada corretamente.
        themes_by_user[only_user_id] = [
            theme for theme in themes_all
            if clean_str(theme.get("user_id")) == only_user_id
        ]
    else:
        for theme in themes_all:
            user_id = clean_str(theme.get("user_id"))
            if user_id:
                themes_by_user.setdefault(user_id, []).append(theme)

    if not themes_by_user:
        print(
            "Análise temática: não há usuários com temas cadastrados; "
            "nenhuma análise personalizada precisa ser executada."
        )
        return {"users": 0, "processed": 0, "failed": 0}

    publications = db_get_all("publications", {
        "select": (
            "id,edition_id,page_start,page_end,kind,title,body_text,"
            "organization,people,municipalities,amounts,updated_at"
        ),
        "edition_id": f"eq.{edition_id}",
        "order": "page_start.asc,id.asc",
    })

    results = {
        "users": len(themes_by_user),
        "processed": 0,
        "failed": 0,
    }

    for user_number, (user_id, user_themes_all) in enumerate(
        themes_by_user.items(),
        start=1,
    ):
        analysis_started = False
        theme_signature = ""

        try:
            active_themes = sorted(
                [theme for theme in user_themes_all if theme.get("active")],
                key=lambda t: (
                    int(t.get("priority") or 3),
                    clean_str(t.get("name")),
                    clean_str(t.get("id")),
                ),
            )

            theme_signature = user_theme_signature(
                user_themes_all,
                publications,
            )

            prior_rows = db_get("user_edition_analyses", {
                "select": (
                    "id,status,theme_signature,publication_count,"
                    "relevant_count,summary"
                ),
                "user_id": f"eq.{user_id}",
                "edition_id": f"eq.{edition_id}",
                "limit": "1",
            })

            prior = prior_rows[0] if prior_rows else None

            # Uma análise concluída com a mesma assinatura pode ser reutilizada.
            if (
                prior
                and prior.get("status") == "processed"
                and prior.get("theme_signature") == theme_signature
                and int(prior.get("publication_count") or 0) == len(publications)
            ):
                print(
                    f"Análise personalizada já atualizada: "
                    f"usuário {user_id[:8]}… "
                    f"({len(publications)} publicações)."
                )
                results["processed"] += 1
                continue

            analysis_payload = {
                "user_id": user_id,
                "edition_id": edition_id,
                "theme_signature": theme_signature,
                "status": "processing",
                "publication_count": len(publications),
                "relevant_count": 0,
                "error": None,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": None,
            }

            db_post(
                "user_edition_analyses",
                analysis_payload,
                on_conflict="user_id,edition_id",
            )

            analysis_started = True

            # Inclui theme_signature para identificar as publicações
            # que já foram classificadas na tentativa anterior.
            previous_rows = db_get_all("user_publication_matches", {
                "select": (
                    "publication_id,theme_signature,relevant,matched_themes,"
                    "confidence,justification,summary,manual_relevance,"
                    "manual_note,reviewed_at"
                ),
                "user_id": f"eq.{user_id}",
                "edition_id": f"eq.{edition_id}",
            })

            previous_by_publication = {
                row.get("publication_id"): row
                for row in previous_rows
            }

            # Usuário sem temas ativos: conclui sem chamar o Gemini.
            if not active_themes:
                db_patch(
                    "user_publication_matches",
                    {
                        "user_id": f"eq.{user_id}",
                        "edition_id": f"eq.{edition_id}",
                        "manual_relevance": "is.null",
                    },
                    {
                        "theme_signature": theme_signature,
                        "relevant": False,
                        "matched_themes": [],
                        "confidence": None,
                        "justification": None,
                        "summary": None,
                    },
                )

                summary = (
                    "Nenhum tema ativo está configurado para este usuário. "
                    "Cadastre ou ative temas para receber destaques "
                    "personalizados nas próximas edições."
                )

                db_patch(
                    "user_edition_analyses",
                    {
                        "user_id": f"eq.{user_id}",
                        "edition_id": f"eq.{edition_id}",
                    },
                    {
                        "status": "processed",
                        "summary": summary,
                        "publication_count": len(publications),
                        "relevant_count": 0,
                        "error": None,
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                    },
                )

                results["processed"] += 1
                continue

            themes_for_prompt = [
                {
                    "id": t.get("id"),
                    "name": t.get("name"),
                    "description": t.get("description") or "",
                    "keywords": t.get("keywords") or [],
                    "include_criteria": t.get("include_criteria") or "",
                    "exclude_criteria": t.get("exclude_criteria") or "",
                    "priority": int(t.get("priority") or 3),
                }
                for t in active_themes
            ]

            allowed_names = {
                theme["name"] for theme in themes_for_prompt
            }

            # A assinatura do conjunto de temas é separada da assinatura geral
            # da edição. Assim, uma publicação nova não invalida o cache das antigas.
            theme_cache_signature = user_theme_cache_signature(
                user_themes_all
            )

            # Calcula uma assinatura independente para cada publicação.
            publication_signatures = {
                clean_str(publication.get("id")):
                    user_publication_cache_signature(
                        theme_cache_signature,
                        publication,
                    )
                for publication in publications
            }

            classified_rows: list[dict[str, Any]] = []

            # Aumenta de 10 para 15 publicações por requisição.
            batch_size = 15

            total_batches = (
                (len(publications) + batch_size - 1) // batch_size
            )

            for offset in range(0, len(publications), batch_size):
                batch = publications[offset:offset + batch_size]

                cached_rows: list[dict[str, Any]] = []
                publications_to_classify: list[dict[str, Any]] = []

                # Reaproveita individualmente os resultados já gravados,
                # desde que os temas e o conteúdo da publicação não tenham mudado.
                for publication in batch:
                    publication_id = clean_str(publication.get("id"))
                    previous = previous_by_publication.get(publication_id)

                    current_publication_signature = publication_signatures.get(
                        publication_id
                    )

                    if (
                        previous
                        and current_publication_signature
                        and previous.get("theme_signature") == current_publication_signature
                    ):
                        cached_rows.append({
                            "user_id": user_id,
                            "edition_id": edition_id,
                            "publication_id": publication_id,
                            "theme_signature": current_publication_signature,
                            "relevant": bool(previous.get("relevant")),
                            "matched_themes": previous.get("matched_themes") or [],
                            "confidence": previous.get("confidence"),
                            "justification": previous.get("justification"),
                            "summary": previous.get("summary"),
                            "manual_relevance": previous.get("manual_relevance"),
                            "manual_note": previous.get("manual_note"),
                            "reviewed_at": previous.get("reviewed_at"),
                        })
                    else:
                        publications_to_classify.append(publication)

                new_rows: list[dict[str, Any]] = []

                # Só chama o Gemini para publicações que ainda não tenham
                # sido classificadas com a assinatura individual atual.
                if publications_to_classify:
                    compact = [
                        compact_publication_for_analysis(publication)
                        for publication in publications_to_classify
                    ]

                    prompt = f"""Você é um analista documental de um gabinete parlamentar. Classifique cada publicação exclusivamente em relação aos temas particulares deste usuário.

REGRAS OBRIGATÓRIAS:
- Avalie o conteúdo substantivo, não apenas coincidências de palavras.
- Considere palavras-chave como pistas; aplique os critérios de inclusão e exclusão.
- Uma publicação é relevante somente quando houver relação clara com pelo menos um tema ativo.
- Use os nomes exatos dos temas fornecidos em matched_themes.
- Não invente relações, fatos, pessoas, valores, municípios ou consequências.
- Para cada publicação, devolva exatamente uma classificação e preserve o publication_id sem alteração.
- Para itens irrelevantes, use matched_themes=[], justification="" e summary="".
- Para itens relevantes, dê uma justificativa específica e um resumo factual de 2 a 4 frases.
- Não omita nenhuma publicação do lote.

TEMAS PARTICULARES DO USUÁRIO:
{json.dumps(themes_for_prompt, ensure_ascii=False)}

PUBLICAÇÕES DO LOTE:
{json.dumps(compact, ensure_ascii=False)}

Responda somente JSON no formato {{"items":[{{"publication_id":"UUID","relevant":true,"matched_themes":["Tema exato"],"confidence":0.0,"justification":"...","summary":"..."}}]}}."""

                    parsed = gemini_json(prompt, 6000)

                    if isinstance(parsed, list):
                        items = parsed
                    elif (
                        isinstance(parsed, dict)
                        and isinstance(parsed.get("items"), list)
                    ):
                        items = parsed["items"]
                    else:
                        raise RuntimeError(
                            "O Gemini não retornou uma lista JSON de classificações."
                        )

                    by_id: dict[str, dict[str, Any]] = {}

                    valid_ids = {
                        clean_str(p.get("id"))
                        for p in publications_to_classify
                    }

                    for item in items:
                        if not isinstance(item, dict):
                            raise RuntimeError(
                                "O Gemini retornou item de classificação "
                                "em formato inválido."
                            )

                        publication_id = clean_str(item.get("publication_id"))

                        if (
                            not publication_id
                            or publication_id not in valid_ids
                            or publication_id in by_id
                        ):
                            raise RuntimeError(
                                "O Gemini retornou identificador ausente, "
                                "repetido ou estranho ao lote."
                            )

                        by_id[publication_id] = item

                    for publication in publications_to_classify:
                        publication_id = clean_str(publication.get("id"))
                        item = by_id.get(publication_id)

                        if item is None:
                            raise RuntimeError(
                                f"O Gemini não classificou a publicação "
                                f"{publication_id}."
                            )

                        matched = (
                            item.get("matched_themes")
                            if isinstance(item.get("matched_themes"), list)
                            else []
                        )

                        matched = list(dict.fromkeys(
                            str(value)
                            for value in matched
                            if str(value) in allowed_names
                        ))

                        relevant = bool(item.get("relevant")) and bool(matched)
                        previous = previous_by_publication.get(publication_id) or {}

                        try:
                            confidence = (
                                float(item.get("confidence"))
                                if item.get("confidence") is not None
                                else None
                            )

                            if confidence is not None and not 0 <= confidence <= 1:
                                confidence = max(0.0, min(1.0, confidence))

                        except (TypeError, ValueError):
                            confidence = None

                        new_rows.append({
                            "user_id": user_id,
                            "edition_id": edition_id,
                            "publication_id": publication_id,
                            "theme_signature": publication_signatures[publication_id],
                            "relevant": relevant,
                            "matched_themes": matched if relevant else [],
                            "confidence": confidence,
                            "justification": (
                                clean_str(item.get("justification"), "")[:1000] or None
                                if relevant else None
                            ),
                            "summary": (
                                clean_str(item.get("summary"), "")[:1800] or None
                                if relevant else None
                            ),
                            "manual_relevance": previous.get("manual_relevance"),
                            "manual_note": previous.get("manual_note"),
                            "reviewed_at": previous.get("reviewed_at"),
                        })

                    # Checkpoint: grava o lote assim que ele termina.
                    # Se o processo parar depois, este lote não será repetido.
                    if new_rows:
                        db_post(
                            "user_publication_matches",
                            new_rows,
                            on_conflict="user_id,publication_id",
                        )

                classified_rows.extend(cached_rows)
                classified_rows.extend(new_rows)

                batch_number = offset // batch_size + 1

                print(
                    f"Análise personalizada do usuário {user_id[:8]}…: "
                    f"lote {batch_number}/{total_batches}; "
                    f"{len(cached_rows)} reaproveitada(s), "
                    f"{len(new_rows)} nova(s)."
                )

            relevant_rows = [
                row for row in classified_rows
                if row["relevant"]
            ]

            summary = ""

            if relevant_rows:
                publication_by_id = {
                    clean_str(p.get("id")): p
                    for p in publications
                }

                highlights = []

                for row in relevant_rows[:35]:
                    publication = publication_by_id.get(
                        row["publication_id"],
                        {},
                    )

                    highlights.append({
                        "title": publication.get("title"),
                        "kind": publication.get("kind"),
                        "themes": row["matched_themes"],
                        "summary": row["summary"],
                    })

                summary_prompt = f"""Elabore uma síntese personalizada da edição {edition_date} ({edition_label}) do Diário Oficial, Parte II — Poder Legislativo, para este usuário, considerando exclusivamente os temas e correspondências abaixo. Indique os assuntos que mais aparecem, sem inventar fatos. Escreva um único parágrafo institucional de 90 a 150 palavras.

TEMAS DO USUÁRIO: {json.dumps([t['name'] for t in themes_for_prompt], ensure_ascii=False)}

DESTAQUES: {json.dumps(highlights, ensure_ascii=False)}"""

                try:
                    summary_parsed = gemini_json(summary_prompt, 1200)

                    if isinstance(summary_parsed, dict):
                        summary = clean_str(
                            summary_parsed.get("summary")
                        )[:2600]

                except Exception as summary_error:
                    print(
                        f"Síntese personalizada não gerada para usuário "
                        f"{user_id[:8]}…: {summary_error}"
                    )

            if not summary:
                summary = (
                    f"Foram identificadas {len(relevant_rows)} publicações "
                    f"relacionadas aos seus temas nesta edição, entre "
                    f"{len(publications)} publicações analisadas. "
                    "Consulte os destaques para ver os temas e resumos individuais."
                    if relevant_rows
                    else
                    f"Foram analisadas {len(publications)} publicações. "
                    "Nenhuma apresentou correspondência suficientemente clara "
                    "com os seus temas ativos nesta edição."
                )

            db_patch(
                "user_edition_analyses",
                {
                    "user_id": f"eq.{user_id}",
                    "edition_id": f"eq.{edition_id}",
                },
                {
                    "status": "processed",
                    "summary": summary,
                    "publication_count": len(publications),
                    "relevant_count": len(relevant_rows),
                    "error": None,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                },
            )

            results["processed"] += 1

            print(
                f"Análise personalizada concluída para conta "
                f"{user_number}/{len(themes_by_user)}: "
                f"{len(relevant_rows)} destaques em "
                f"{len(publications)} publicações."
            )

        except Exception as user_error:
            results["failed"] += 1
            message = clean_str(user_error)[:1800]

            print(
                f"Falha na análise personalizada para conta "
                f"{user_number}/{len(themes_by_user)}: {message}",
                file=sys.stderr,
            )

            if analysis_started and theme_signature:
                try:
                    db_patch(
                        "user_edition_analyses",
                        {
                            "user_id": f"eq.{user_id}",
                            "edition_id": f"eq.{edition_id}",
                        },
                        {
                            "status": "error",
                            "error": message,
                            "finished_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )

                except Exception as status_error:
                    print(
                        "Não foi possível registrar o erro da análise "
                        f"do usuário: {status_error}",
                        file=sys.stderr,
                    )

    return results


def process_analysis_queue() -> None:
    """
    Processa tarefas da tabela user_edition_analyses.

    Estados aceitos:
    - queued: aguardando processamento;
    - processing: em andamento;
    - processed: concluída;
    - error: falhou e aguarda nova tentativa.

    Tarefas que ficaram presas em processing por mais de 20 minutos
    são recuperadas automaticamente.
    """

    total_processed = 0
    total_failed = 0

    print("Iniciando processamento da fila de análises personalizadas.")

    while True:
        now = datetime.now(timezone.utc)
        stale_before = (
            now - timedelta(minutes=20)
        ).isoformat()

        queued_rows = db_get_all("user_edition_analyses", {
            "select": (
                "id,user_id,edition_id,status,updated_at"
            ),
            "status": "eq.queued",
            "order": "updated_at.asc",
        })

        stale_rows = db_get_all("user_edition_analyses", {
            "select": (
                "id,user_id,edition_id,status,updated_at"
            ),
            "status": "eq.processing",
            "updated_at": f"lt.{stale_before}",
            "order": "updated_at.asc",
        })

        # Evita processar a mesma tarefa duas vezes na mesma rodada.
        jobs_by_id: dict[str, dict[str, Any]] = {}

        for job in queued_rows + stale_rows:
            job_id = clean_str(job.get("id"))
            if job_id:
                jobs_by_id[job_id] = job

        jobs = sorted(
            jobs_by_id.values(),
            key=lambda job: clean_str(job.get("updated_at")),
        )

        if not jobs:
            print("Fila de análises vazia. Nenhuma tarefa pendente.")
            break

        print(
            f"Fila encontrada: {len(jobs)} tarefa(s) "
            "pendente(s) ou interrompida(s)."
        )

        for job in jobs:
            job_id = clean_str(job.get("id"))
            user_id = clean_str(job.get("user_id"))
            edition_id = clean_str(job.get("edition_id"))

            if not job_id or not user_id or not edition_id:
                print(
                    f"Tarefa com identificadores inválidos: {job_id!r}.",
                    file=sys.stderr,
                )
                total_failed += 1
                continue

            try:
                # Confere a edição antes de iniciar a análise.
                edition_rows = db_get("editions", {
                    "select": "id,edition_date,edition_label",
                    "id": f"eq.{edition_id}",
                    "limit": "1",
                })

                if not edition_rows:
                    message = "A edição associada à tarefa não foi encontrada."

                    db_patch(
                        "user_edition_analyses",
                        {"id": f"eq.{job_id}"},
                        {
                            "status": "error",
                            "error": message,
                            "finished_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )

                    print(
                        f"Falha na tarefa {job_id[:8]}…: {message}",
                        file=sys.stderr,
                    )

                    total_failed += 1
                    continue

                edition = edition_rows[0]
                edition_date = clean_str(edition.get("edition_date"))
                edition_label = clean_str(
                    edition.get("edition_label"),
                    "Parte II · Poder Legislativo",
                )

                # Atualiza a tarefa e renova seu marcador de atividade.
                db_patch(
                    "user_edition_analyses",
                    {"id": f"eq.{job_id}"},
                    {
                        "status": "processing",
                        "started_at": datetime.now(timezone.utc).isoformat(),
                        "finished_at": None,
                        "error": None,
                    },
                )

                print(
                    f"Processando tarefa: edição {edition_date}; "
                    f"usuário {user_id[:8]}…"
                )

                result = analyze_edition_for_users(
                    edition_id=edition_id,
                    edition_date=edition_date,
                    edition_label=edition_label,
                    only_user_id=user_id,
                )

                if int(result.get("failed", 0)):
                    total_failed += 1
                    print(
                        f"A análise do usuário {user_id[:8]}… "
                        "terminou com erro. Os lotes já salvos foram preservados.",
                        file=sys.stderr,
                    )
                else:
                    total_processed += 1
                    print(
                        f"Tarefa concluída: edição {edition_date}; "
                        f"usuário {user_id[:8]}…"
                    )

            except Exception as exc:
                message = clean_str(exc)[:1800]

                print(
                    f"Falha ao processar tarefa {job_id[:8]}…: {message}",
                    file=sys.stderr,
                )

                try:
                    db_patch(
                        "user_edition_analyses",
                        {"id": f"eq.{job_id}"},
                        {
                            "status": "error",
                            "error": message,
                            "finished_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                except Exception as status_error:
                    print(
                        f"Não foi possível atualizar o estado da tarefa: {status_error}",
                        file=sys.stderr,
                    )

                total_failed += 1

        # Consulta novamente a fila. Isso permite incluir tarefas que tenham
        # sido adicionadas enquanto esta execução estava em andamento.

    print(
        "Processamento da fila encerrado. "
        f"Concluídas: {total_processed}; falhas: {total_failed}."
    )


def edition_exists(edition_date: str, edition_key: str) -> dict[str, Any] | None:
    found = db_get("editions", {
        "select": "id,edition_date,edition_type,edition_key,pdf_sha256,status",
        "edition_date": f"eq.{edition_date}",
        "edition_key": f"eq.{edition_key}",
        "limit": "1",
    })
    return found[0] if found else None



def process_source(source: dict[str, Any]) -> None:
    edition_date = source["edition_date"]
    edition_type = source["edition_type"]
    run_rows = db_post("ingestion_runs", {
        "status": "running",
        "edition_date": edition_date,
        "edition_type": edition_type,
        "message": f"Iniciando coleta: {source['edition_label']}.",
    })
    run_id = run_rows[0]["id"] if run_rows else None
    existing = None
    edition_key = "regular" if edition_type == "regular" else None
    preserve_processed_edition = False
    try:
        print(f"Edição detectada: {edition_date} — {source['edition_label']} ({edition_type})")
        print(f"Página de seleção da edição: {source['selection_url']}")
        print(f"Link identificado como Parte II — Poder Legislativo: {source['part_ii_url']}")

        pdf_url, part_url, content = find_pdf_and_html(source["part_ii_url"])
        pdf_info = validate_part_ii_pdf(content, expected_date=edition_date)
        pdf_hash = sha256(content)
        # A chave do extra deriva do conteúdo do PDF: a mesma edição é
        # reconhecida na checagem seguinte, e extras diferentes do mesmo dia
        # recebem chaves distintas.
        edition_key = "regular" if edition_type == "regular" else f"extra-{pdf_hash[:20]}"
        if run_id:
            db_patch("ingestion_runs", {"id": f"eq.{run_id}"}, {"edition_key": edition_key})
        print(
            "Validação do PDF aprovada: Parte II — Poder Legislativo; "
            f"tipo={edition_type}; data impressa={pdf_info['document_date']}; "
            f"páginas={pdf_info['page_count']}; autor={pdf_info.get('author')!r}; "
            f"criador={pdf_info.get('creator')!r}."
        )
        print(f"URL do visualizador/arquivo-fonte: {pdf_url}")

        existing = edition_exists(edition_date, edition_key)
        if existing and existing.get("pdf_sha256") == pdf_hash and existing.get("status") == "processed":
            # Atualiza o rótulo caso o portal passe a listar outra quantidade de extras.
            db_patch("editions", {"id": f"eq.{existing['id']}"}, {"edition_label": source["edition_label"]})
            cached_analysis = {"users": 0, "processed": 0, "failed": 0}
            analysis_note = ""
            if edition_type == "extra":
                # Mesmo sem republicar o PDF, reavalia contas cuja análise ficou
                # em erro na tentativa anterior; contas já concluídas batem no cache.
                cached_analysis = analyze_edition_for_users(existing["id"], edition_date, source["edition_label"])
                analysis_note = (
                    f" Análise personalizada: {cached_analysis['processed']}/{cached_analysis['users']} usuário(s) "
                    f"processado(s), {cached_analysis['failed']} falha(s)."
                )
            status = "warning" if cached_analysis["failed"] else "success"
            log_run(status, "Edição já coletada; sem nova segmentação." + analysis_note, edition_date, edition_type, run_id=run_id, edition_key=edition_key)
            print("Edição já processada; verificação de análises pessoais concluída.")
            return cached_analysis
        preserve_processed_edition = bool(existing and existing.get("status") == "processed")

        pages = extract_pdf_pages(content)
        if not pages or sum(len(page["text"]) for page in pages) < 500:
            raise RuntimeError("Não foi extraído texto suficiente do PDF da Parte II. A edição não foi marcada como processada.")

        # A segmentação é comum a todo o acervo. A classificação das edições
        # extras será executada abaixo separadamente por usuário; nas regulares,
        # ela continua sob demanda pela Edge Function autenticada.
        chunks = make_chunks(pages)
        if not chunks:
            raise RuntimeError("Não foi possível criar lotes de texto a partir do PDF da Parte II.")

        all_items: list[dict[str, Any]] = []
        for number, chunk in enumerate(chunks, start=1):
            print(f"Segmentando lote {number}/{len(chunks)} (páginas {chunk['start_page']}–{chunk['end_page']}).")
            all_items.extend(classify_chunk(chunk, edition_date))
            # O intervalo entre chamadas já é controlado globalmente
            # por reserve_gemini_slot(); não é necessária outra pausa aqui.

        deduped: dict[str, dict[str, Any]] = {}
        for item in all_items:
            body = clean_str(item.get("text"))
            title = clean_str(item.get("title"), "Publicação sem título")
            if not body and title == "Publicação sem título":
                continue
            key = publication_key(edition_date, item, edition_type, edition_key)
            if key not in deduped or len(body) > len(clean_str(deduped[key].get("text"))):
                deduped[key] = item

        publications = list(deduped.values())
        if not publications:
            raise RuntimeError("O Gemini não segmentou nenhuma publicação a partir do PDF validado. A edição anterior, caso exista, foi preservada.")

        rows = []
        for item in publications:
            rows.append({
                "external_key": publication_key(edition_date, item, edition_type, edition_key),
                "page_start": item.get("page_start"),
                "page_end": item.get("page_end"),
                "kind": clean_str(item.get("type"), "Não identificado"),
                "title": clean_str(item.get("title"), "Publicação sem título"),
                "body_text": clean_str(item.get("text")),
                "organization": clean_str(item.get("organization")) or None,
                "people": item.get("people") if isinstance(item.get("people"), list) else [],
                "municipalities": item.get("municipalities") if isinstance(item.get("municipalities"), list) else [],
                "amounts": item.get("amounts") if isinstance(item.get("amounts"), list) else [],
                # Esses campos antigos permanecem por compatibilidade; a
                # relevância nova é armazenada por usuário, não globalmente.
                "ai_relevant": False,
                "matched_themes": [],
                "confidence": None,
                "justification": None,
                "summary": None,
                "source_url": part_url,
            })

        try:
            summary = daily_summary(edition_date, publications, edition_type)
        except Exception as summary_exc:
            print(f"Aviso: não foi possível gerar a síntese geral; os atos serão preservados: {summary_exc}")
            summary = (
                f"A {'edição extra' if edition_type == 'extra' else 'edição regular'} de {edition_date} "
                f"foi processada e contém {len(publications)} publicações. Consulte os registros individuais."
            )

        edition_payload = {
            "edition_date": edition_date,
            "edition_type": edition_type,
            "edition_key": edition_key,
            "edition_label": source["edition_label"],
            "source_page_url": source["selection_url"],
            "source_pdf_url": pdf_url,
            "pdf_sha256": pdf_hash,
            "status": "found",
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "error": None,
        }
        edition_rows = db_post("editions", edition_payload, on_conflict="edition_date,edition_key")
        if edition_rows:
            edition_id = edition_rows[0]["id"]
        else:
            found = db_get("editions", {
                "select": "id",
                "edition_date": f"eq.{edition_date}",
                "edition_key": f"eq.{edition_key}",
                "limit": "1",
            })
            if not found:
                raise RuntimeError("A edição foi processada, mas não foi possível recuperar seu ID no Supabase.")
            edition_id = found[0]["id"]

        for row in rows:
            row["edition_id"] = edition_id

        for offset in range(0, len(rows), 60):
            db_post("publications", rows[offset:offset + 60], on_conflict="external_key")

        desired_keys = {row["external_key"] for row in rows}
        existing_publications = db_get(
            "publications",
            {"select": "id,external_key", "edition_id": f"eq.{edition_id}", "limit": "5000"},
        ) or []
        obsolete_ids = [row["id"] for row in existing_publications if row.get("external_key") not in desired_keys]
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
            # A quantidade de destaques é por usuário, portanto não é gravada
            # como um número único na tabela compartilhada de edições.
            "relevant_count": 0,
            "daily_summary": summary,
            "error": None,
        })

        analysis_note = ""
        analysis_result = {"users": 0, "processed": 0, "failed": 0}
        if edition_type == "extra":
            # Edições extras são classificadas previamente para cada conta que
            # tenha temas cadastrados. A classificação e os resumos ficam sempre
            # vinculados ao user_id; não há resultado temático global.
            try:
                analysis = analyze_edition_for_users(edition_id, edition_date, source["edition_label"])
                analysis_result = analysis
                analysis_note = (
                    f" Análise personalizada: {analysis['processed']}/{analysis['users']} usuário(s) processado(s), "
                    f"{analysis['failed']} falha(s)."
                )
                if analysis["failed"]:
                    print("A edição foi coletada; uma ou mais análises personalizadas precisam de nova tentativa.", file=sys.stderr)
            except Exception as analysis_exc:
                analysis_result = {"users": 0, "processed": 0, "failed": 1}
                analysis_note = f" Falha ao iniciar análises personalizadas por usuário: {analysis_exc}"
                print(analysis_note, file=sys.stderr)

        log_run("success", f"{source['edition_label']} processada. {len(rows)} publicações. PDF não armazenado.{analysis_note}", edition_date, edition_type, len(rows), 0, run_id, edition_key)
        print(f"Concluído: {len(rows)} atos. Tipo da edição: {edition_type}. PDF descartado ao final do processo.")
        return analysis_result

    except Exception as exc:
        message = f"Falha na coleta/processamento: {exc}"
        print(message, file=sys.stderr)
        try:
            if edition_date and edition_key and not preserve_processed_edition:
                prior_on_error = edition_exists(edition_date, edition_key)
                if not prior_on_error or prior_on_error.get("status") != "processed":
                    db_patch("editions", {
                        "edition_date": f"eq.{edition_date}",
                        "edition_key": f"eq.{edition_key}",
                    }, {"status": "error", "error": message})
            log_run("error", message, edition_date, edition_type, run_id=run_id, edition_key=edition_key)
        except Exception as log_exc:
            print(f"Também não foi possível registrar a falha no banco: {log_exc}", file=sys.stderr)
        raise


def process() -> None:
    mode = os.getenv("EDITION_SCAN_MODE", "regular").strip().lower()

    # O modo analysis não consulta a IOERJ.
    # Apenas consome as tarefas já registradas no Supabase.
    if mode == "analysis":
        process_analysis_queue()
        return

    if mode not in {"regular", "extra", "both"}:
        raise RuntimeError(
            "EDITION_SCAN_MODE deve ser regular, extra, both ou analysis."
        )

    modes = ["regular", "extra"] if mode == "both" else [mode]
    failures = []

    for edition_type in modes:
        check_started_at = datetime.now(timezone.utc)
        today_local = datetime.now(ZoneInfo(TZ)).date()
        today = today_local.isoformat()

        # A checagem das extras verifica ontem e hoje. Isso recupera a edição
        # do dia anterior quando o agendador atrasar e a execução atravessar
        # a meia-noite. A deduplicação por hash impede reprocessamento do PDF.
        scan_dates = (
            [today_local - timedelta(days=1), today_local]
            if edition_type == "extra"
            else [None]
        )

        sources: list[dict[str, Any]] = []
        discovery_failures: list[str] = []

        for scan_date in scan_dates:
            try:
                found = discover_editions(edition_type, target_date=scan_date)
                sources.extend(found)
            except Exception as exc:
                date_label = scan_date.isoformat() if scan_date else today
                detail = f"{date_label}: {exc}"
                discovery_failures.append(detail)
                failures.append(f"{edition_type}: {detail}")
                print(
                    f"Falha ao localizar a edição {edition_type} para {date_label}: {exc}",
                    file=sys.stderr,
                )

        dates_label = ", ".join(
            item.isoformat() for item in scan_dates if item is not None
        ) or today

        if not sources:
            if edition_type == "extra":
                if discovery_failures:
                    check_status = "error"
                    check_message = (
                        f"Falha na checagem de edições extras para {dates_label}: "
                        + " | ".join(discovery_failures)
                    )
                else:
                    check_status = "success"
                    check_message = (
                        f"Checagem concluída para {dates_label}: nenhuma edição extra "
                        "da Parte II foi disponibilizada no calendário da IOERJ."
                    )

                db_post("ingestion_runs", {
                    "status": check_status,
                    "edition_date": today,
                    "edition_type": "extra",
                    "started_at": check_started_at.isoformat(),
                    "message": check_message,
                    "publications_found": 0,
                    "publications_relevant": 0,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                })
                print(check_message)
            else:
                message = (
                    "Falha ao consultar a edição regular: "
                    + " | ".join(discovery_failures)
                    if discovery_failures
                    else "O portal não disponibilizou o link obrigatório da Parte II."
                )
                db_post("ingestion_runs", {
                    "status": "error",
                    "edition_date": today,
                    "edition_type": "regular",
                    "started_at": check_started_at.isoformat(),
                    "message": message,
                    "publications_found": 0,
                    "publications_relevant": 0,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                })
                failures.append(f"regular: {message}")
            continue

        if edition_type == "extra":
            print(
                f"Foram encontradas {len(sources)} edição(ões) extra(s) da Parte II "
                f"nas datas verificadas: {dates_label}."
            )

        type_failures = []
        successful_sources = 0
        user_analysis_failures = 0

        for source in sources:
            try:
                source_result = process_source(source) or {}
                user_analysis_failures += int(source_result.get("failed", 0))
                successful_sources += 1
            except Exception as exc:
                detail = f"{source.get('edition_label', '')} ({source.get('edition_date', '')}): {exc}"
                type_failures.append(detail)
                failures.append(f"{edition_type} {detail}")
                # Continua para processar as demais edições, mesmo se uma falhar.
                continue

        if edition_type == "extra":
            if type_failures or discovery_failures:
                check_status = "error"
                all_errors = discovery_failures + type_failures
                check_message = (
                    f"Verificação de extras para {dates_label}: {len(sources)} edição(ões) "
                    f"encontrada(s), {successful_sources} processada(s), "
                    f"{len(all_errors)} falha(s). " + " | ".join(all_errors)
                )
            elif user_analysis_failures:
                check_status = "warning"
                check_message = (
                    f"{len(sources)} edição(ões) extra(s) coletada(s) para {dates_label}, "
                    f"mas houve {user_analysis_failures} falha(s) de análise personalizada. "
                    "As contas afetadas poderão tentar novamente ao abrir a edição."
                )
            else:
                check_status = "success"
                check_message = (
                    f"Verificação concluída para {dates_label}: {len(sources)} edição(ões) "
                    "extra(s) da Parte II identificada(s), processada(s) e analisada(s) "
                    "para as contas com temas cadastrados."
                )

            db_post("ingestion_runs", {
                "status": check_status,
                "edition_date": today,
                "edition_type": "extra",
                "started_at": check_started_at.isoformat(),
                "message": check_message,
                "publications_found": 0,
                "publications_relevant": 0,
                "finished_at": datetime.now(timezone.utc).isoformat(),
            })

            if user_analysis_failures:
                failures.append(
                    f"extra: {user_analysis_failures} análise(s) personalizada(s) falharam"
                )

    if failures:
        raise RuntimeError("Uma ou mais coletas falharam: " + " | ".join(failures))


if __name__ == "__main__":
    process()
