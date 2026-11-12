#!/usr/bin/env python3
"""Coleta a Parte II da IOERJ, processa o PDF temporariamente e grava dados no Supabase.
O PDF nunca é enviado ao banco nem a qualquer serviço de armazenamento do Radar.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timezone
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

PORTAL = "https://portal.ioerj.com.br/"
LAST_EDITION = "https://www.ioerj.com.br/portal/modules/conteudoonline/do_ultima_edicao.php"
TZ = "America/Sao_Paulo"
MODEL = os.getenv("GEMINI_MODEL") or "gemini-3.8-flash"
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


def discover_latest() -> dict[str, Any]:
    home = http_get(PORTAL)
    home_soup = BeautifulSoup(home.text, "html.parser")
    last_url = None
    for a in home_soup.find_all("a", href=True):
        if "ultima edicao" in norm(a.get_text(" ", strip=True)):
            last_url = urljoin(home.url, a["href"])
            break
    last_url = last_url or LAST_EDITION

    latest = http_get(last_url)
    # A IOERJ pode apresentar uma página de redirecionamento com link "clique aqui".
    latest_soup = BeautifulSoup(latest.text, "html.parser")
    if "redirecionando para a ultima edicao" in norm(latest_soup.get_text(" ", strip=True)):
        continuation = None
        for a in latest_soup.find_all("a", href=True):
            if "clique aqui" in norm(a.get_text(" ", strip=True)):
                continuation = urljoin(latest.url, a["href"])
                break
        if continuation:
            latest = http_get(continuation)
            latest_soup = BeautifulSoup(latest.text, "html.parser")

    selection_url = latest.url
    part_url = find_part_ii_link(latest.text, selection_url)
    edition_date = extract_date(latest_soup, selection_url)
    label_match = re.search(r"Ano\s+([IVXLCDM]+).*?N[º°o]\s*(\d+)", latest_soup.get_text(" ", strip=True), re.I)
    edition_label = f"Ano {label_match.group(1).upper()} · Nº {label_match.group(2)}" if label_match else ""
    return {
        "edition_date": edition_date.isoformat(),
        "edition_label": edition_label,
        "selection_url": selection_url,
        "part_ii_url": part_url,
    }


def find_pdf_and_html(part_url: str) -> tuple[str | None, str, bytes | None]:
    response = http_get(part_url)
    content_type = response.headers.get("Content-Type", "").lower()
    if "pdf" in content_type or response.content[:4] == b"%PDF":
        return response.url, response.url, response.content

    soup = BeautifulSoup(response.text, "html.parser")
    candidates: list[str] = []
    for element in soup.find_all(["a", "iframe", "embed", "object"]):
        value = element.get("href") or element.get("src") or element.get("data")
        label = norm(element.get_text(" ", strip=True) + " " + str(element.get("title", "")))
        if value and (".pdf" in value.lower() or "pdf" in value.lower() or "baixar" in label or "download" in label or element.name in ("iframe", "embed", "object")):
            candidates.append(urljoin(response.url, value))
    # Visualizadores frequentemente escondem o link no JavaScript.
    candidates.extend(urljoin(response.url, m) for m in re.findall(r"[\"']([^\"']+\.pdf(?:\?[^\"']*)?)[\"']", response.text, re.I))

    for candidate in dict.fromkeys(candidates):
        try:
            check = http_get(candidate)
            if "pdf" in check.headers.get("Content-Type", "").lower() or check.content[:4] == b"%PDF":
                return check.url, response.url, check.content
            # um iframe pode apontar para outro leitor HTML com PDF embutido
            inner = BeautifulSoup(check.text, "html.parser")
            for element in inner.find_all(["iframe", "embed", "object", "a"]):
                value = element.get("src") or element.get("data") or element.get("href")
                if value and ".pdf" in value.lower():
                    nested_url = urljoin(check.url, value)
                    nested = http_get(nested_url)
                    if nested.content[:4] == b"%PDF":
                        return nested.url, response.url, nested.content
        except Exception:
            continue
    # Se a página HTML já contém o texto do caderno, será usado como último recurso.
    readable = soup.get_text("\n", strip=True)
    if len(re.sub(r"\s", "", readable)) > 1500:
        return None, response.url, response.content
    raise RuntimeError("O link da Parte II foi encontrado, mas não consegui localizar o PDF nem extrair texto substancial da página.")


def extract_pdf_pages(pdf_bytes: bytes) -> list[dict[str, Any]]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages: list[dict[str, Any]] = []
    for index, page in enumerate(doc, start=1):
        text = page.get_text("text", sort=True).strip()
        # OCR só nas páginas em que a camada textual está ausente ou muito curta.
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


def gemini_json(prompt: str, max_output_tokens: int = 8000) -> dict[str, Any]:
    """Solicita JSON ao Gemini, com novas tentativas para limites temporários."""
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "maxOutputTokens": max_output_tokens,
        },
    }
    response = None
    for attempt in range(5):
        response = requests.post(
            endpoint,
            headers={"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"},
            json=payload,
            timeout=(30, 180),
        )
        if response.ok:
            break
        if response.status_code not in (429, 500, 502, 503, 504) or attempt == 4:
            raise RuntimeError(
                f"Gemini API retornou HTTP {response.status_code}: {response.text[:1500]}"
            )
        retry_after = response.headers.get("Retry-After", "")
        try:
            wait_seconds = float(retry_after)
        except (TypeError, ValueError):
            wait_seconds = min(60, 5 * (2 ** attempt))
        wait_seconds = max(2, min(wait_seconds, 90))
        print(f"Gemini temporariamente indisponível (HTTP {response.status_code}); nova tentativa em {wait_seconds:.0f}s.")
        time.sleep(wait_seconds)

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
    try:
        parsed = json.loads(output)
        if not isinstance(parsed, dict):
            raise ValueError("A resposta JSON não é um objeto.")
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
    items = result.get("items", [])
    if not isinstance(items, list):
        return []
    for item in items:
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
    return clean_str(gemini_json(prompt, 1200).get("summary"))


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
    try:
        source = discover_latest()
        edition_date = source["edition_date"]
        print(f"Edição detectada: {edition_date} — {source['edition_label']}")
        pdf_url, part_url, content = find_pdf_and_html(source["part_ii_url"])

        if content and content[:4] == b"%PDF":
            pdf_hash = sha256(content)
            existing = edition_exists(edition_date, pdf_hash)
            if existing and existing.get("status") == "processed":
                log_run("success", "Edição já processada; nenhuma duplicidade criada.", edition_date, run_id=run_id)
                print("Edição já processada. Encerrando.")
                return
            pages = extract_pdf_pages(content)
            source_pdf_url = pdf_url
        else:
            pdf_hash = sha256(content or b"")
            existing = edition_exists(edition_date, pdf_hash)
            if existing and existing.get("status") == "processed":
                log_run("success", "Edição HTML já processada; nenhuma duplicidade criada.", edition_date, run_id=run_id)
                return
            pages = html_pages(content or b"")
            source_pdf_url = None

        if not pages or sum(len(p["text"]) for p in pages) < 500:
            raise RuntimeError("Não foi extraído texto suficiente do caderno. A edição não foi marcada como processada.")

        edition_payload = {
            "edition_date": edition_date,
            "edition_label": source["edition_label"],
            "source_page_url": source["part_ii_url"],
            "source_pdf_url": source_pdf_url or source["part_ii_url"],
            "pdf_sha256": pdf_hash,
            "status": "found",
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "error": None,
        }
        # Se a IOERJ substituir uma edição ou se uma tentativa anterior falhou,
        # remove os atos parciais/antigos daquela edição antes de reprocessar.
        if existing:
            db_delete("publications", {"edition_id": f"eq.{existing['id']}"})

        edition_rows = db_post("editions", edition_payload, on_conflict="edition_date")
        edition_id = edition_rows[0]["id"] if edition_rows else db_get("editions", {"select": "id", "edition_date": f"eq.{edition_date}", "limit": "1"})[0]["id"]

        themes = load_themes()
        chunks = make_chunks(pages)
        all_items: list[dict[str, Any]] = []
        for number, chunk in enumerate(chunks, start=1):
            print(f"Analisando lote {number}/{len(chunks)} (páginas {chunk['start_page']}–{chunk['end_page']}).")
            all_items.extend(classify_chunk(chunk, edition_date, themes))
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
        rows = []
        relevant_count = 0
        for item in publications:
            relevant = bool(item.get("relevant")) and bool(item.get("matched_themes"))
            relevant_count += int(relevant)
            rows.append({
                "edition_id": edition_id,
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
                "source_url": source["part_ii_url"],
            })

        # Upsert por lotes para evitar limites de payload do PostgREST.
        for offset in range(0, len(rows), 60):
            db_post("publications", rows[offset:offset + 60], on_conflict="external_key")

        summary = daily_summary(edition_date, publications) if publications else "Não foi possível identificar publicações no texto extraído."
        db_patch("editions", {"id": f"eq.{edition_id}"}, {
            "status": "processed", "processed_at": datetime.now(timezone.utc).isoformat(),
            "publication_count": len(rows), "relevant_count": relevant_count,
            "daily_summary": summary, "error": None,
        })
        log_run("success", f"Edição processada. {len(rows)} publicações; {relevant_count} relevantes. PDF não armazenado.", edition_date, len(rows), relevant_count, run_id)
        print(f"Concluído: {len(rows)} atos, {relevant_count} relevantes. PDF descartado ao final do processo.")

    except Exception as exc:
        message = f"Falha na coleta/processamento: {exc}"
        print(message, file=sys.stderr)
        try:
            if edition_date:
                db_patch("editions", {"edition_date": f"eq.{edition_date}"}, {"status": "error", "error": message})
            log_run("error", message, edition_date, run_id=run_id)
        except Exception as log_exc:
            print(f"Também não foi possível registrar a falha no banco: {log_exc}", file=sys.stderr)
        raise


if __name__ == "__main__":
    process()
