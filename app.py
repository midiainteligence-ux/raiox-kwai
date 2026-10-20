# -*- coding: utf-8 -*-
"""
RAIO-X KWAI — site público
==========================
Qualquer pessoa abre o site, cola o link de um perfil do Kwai e clica em
Analisar. O servidor puxa os links dos últimos vídeos (mesma coleta do robô
de links, em modo celular), lê views/curtidas/comentários/data de cada um e
a página monta o laudo na hora.

Proteções pra uso público:
  - FILA: no máximo COLETAS_SIMULTANEAS navegadores ao mesmo tempo; quem
    chega depois vê "você é o nº X da fila".
  - CACHE: o mesmo perfil analisado há menos de CACHE_HORAS devolve o
    resultado pronto, sem abrir o navegador de novo.
  - LIMITE POR PESSOA: no máximo MAX_POR_IP análises em andamento por IP.

Rodar local:  pip install -r requirements.txt && python app.py
Publicar:     veja o README.md (Hugging Face Spaces, grátis).
"""

import os
import random
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, Response, jsonify, request

COLETAS_SIMULTANEAS = int(os.environ.get("COLETAS_SIMULTANEAS", "2"))
CACHE_HORAS = float(os.environ.get("CACHE_HORAS", "6"))
MAX_POR_IP = int(os.environ.get("MAX_POR_IP", "2"))
MAX_VIDEOS_TETO = 80
TRABALHADORES_PAGINA = 4
FUSO_BR = timezone(timedelta(hours=-3))
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Referer": "https://www.kwai.com/",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}


def normalizar_perfil(texto):
    """Aceita 'https://www.kwai.com/@conta', 'k.kwai.com/@conta', '@conta' ou
    'conta' (com lixo tipo '$0' no fim) e devolve (url_perfil, conta)."""
    texto = (texto or "").strip().strip('"').strip("'")
    texto = re.sub(r"\$\d+$", "", texto)
    m = re.search(r"@([\w.\-]+)", texto)
    if m:
        conta = m.group(1)
    elif re.fullmatch(r"[\w.\-]+", texto):
        conta = texto
    else:
        return None, None
    conta = conta.rstrip(".")
    return f"https://www.kwai.com/@{conta}", conta


def contagem_para_int(texto):
    """'208' → 208 | '2.8K' → 2800 | '1,2 mil' → 1200 | '3.4M' → 3400000."""
    if texto is None:
        return None
    t = str(texto).strip().lower().replace(" ", "").replace(" ", "")
    m = re.fullmatch(r"(\d+(?:[.,]\d+)*)(k|m|b|mil|mi|bi)?", t)
    if not m:
        return None
    numero, sufixo = m.groups()
    if sufixo:
        mult = {"k": 1e3, "mil": 1e3, "m": 1e6, "mi": 1e6, "b": 1e9, "bi": 1e9}[sufixo]
        return int(round(float(numero.replace(",", ".")) * mult))
    return int(numero.replace(".", "").replace(",", ""))


def _ts_para_data(valor):
    try:
        v = int(valor)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if v > 10**12:
        v = v / 1000
    try:
        return datetime.fromtimestamp(v, tz=FUSO_BR).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return None


def _meta(html, nome):
    padroes = [
        r'<meta[^>]*?(?:name|property)=["\']' + re.escape(nome) + r'["\'][^>]*?content=["\']([^"\']*)["\']',
        r'<meta[^>]*?content=["\']([^"\']*)["\'][^>]*?(?:name|property)=["\']' + re.escape(nome) + r'["\']',
    ]
    for p in padroes:
        m = re.search(p, html, re.IGNORECASE | re.DOTALL)
        if m:
            return m.group(1)
    return None


# ----------------------------------------------------------------------
# 1) GRADE DO PERFIL (Selenium): links + visualizações de cada card
# ----------------------------------------------------------------------

# Para cada <a href=".../video/ID">, sobe na árvore até o "card" (o maior
# bloco que ainda contém um único vídeo) e lê o texto dele: a linha que é
# só um número ("2.8K") é a visualização; a linha mais longa é o título.
JS_LER_CARDS = r"""
const conta = (arguments[0] || '').toLowerCase();
const reNum = /^\d+(?:[.,]\d+)*\s*(?:[KkMmBb]|mil|mi|bi)?$/;
const saida = [];
const vistos = new Set();
const ancoras = Array.from(document.querySelectorAll('a[href*="/video/"]'));
for (const a of ancoras) {
  const href = a.getAttribute('href') || '';
  const mId = href.match(/\/video\/(\d{8,})/);
  if (!mId) continue;
  const mConta = href.match(/@([\w.\-]+)\/video\//);
  if (mConta && mConta[1].toLowerCase() !== conta) continue;   // vídeo de outra conta
  const id = mId[1];
  if (vistos.has(id)) continue;
  vistos.add(id);

  let card = a;
  for (let i = 0; i < 6 && card.parentElement; i++) {
    const pai = card.parentElement;
    const nVideos = new Set(Array.from(pai.querySelectorAll('a[href*="/video/"]'))
      .map(x => ((x.getAttribute('href') || '').match(/\/video\/(\d{8,})/) || [])[1])
      .filter(Boolean)).size;
    if (nVideos > 1) break;
    card = pai;
  }
  const linhas = (card.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
  const numeros = linhas.filter(l => reNum.test(l));
  const textos = linhas.filter(l => !reNum.test(l));
  const img = card.querySelector('img');
  let titulo = a.getAttribute('title') || '';
  if (!titulo) titulo = textos.sort((x, y) => y.length - x.length)[0] || '';
  if (!titulo && img) titulo = img.getAttribute('alt') || '';
  saida.push({
    id: id,
    href: href,
    views_texto: numeros[0] || '',
    titulo_grade: titulo,
    miniatura: img ? (img.currentSrc || img.src || '') : ''
  });
}
return saida;
"""


def montar_driver(desktop=False):
    """Modo CELULAR (430x900 + Android) = mesmo do robô de links, que acha os IDs.
    desktop=True abre como computador, layout em que a grade mostra ▷ views."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    opcoes = Options()
    opcoes.add_argument("--headless=new")
    opcoes.add_argument("--no-sandbox")
    opcoes.add_argument("--disable-dev-shm-usage")
    opcoes.add_argument("--disable-gpu")
    opcoes.add_argument("--window-size=1400,1000" if desktop else "--window-size=430,900")
    opcoes.add_argument(f"--user-data-dir=/tmp/chrome-perfil-{random.randint(1, 999999)}")
    opcoes.add_argument("user-agent=" + (HEADERS["User-Agent"] if desktop else
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"))
    for caminho in ("/usr/bin/google-chrome-stable", "/usr/bin/google-chrome",
                    "/usr/bin/chromium-browser", "/usr/bin/chromium"):
        if os.path.exists(caminho):
            opcoes.binary_location = caminho
            break
    if os.path.exists("/usr/bin/chromedriver"):
        return webdriver.Chrome(service=Service("/usr/bin/chromedriver"), options=opcoes)
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opcoes)
    except ImportError:
        return webdriver.Chrome(options=opcoes)


ROTULOS_PERFIL = {
    "seguidores": ("seguidores", "seguidor", "followers", "follower", "fãs", "fas", "fans"),
    "seguindo": ("seguindo", "following"),
    "curtidas_total": ("curtidas", "likes", "like"),
}
_NUM = r"(\d+(?:[.,]\d+)*\s*(?:k|m|b|mil|mi|bi)?)"


def extrair_info_perfil(texto, html):
    """Lê seguidores / seguindo / curtidas totais do cabeçalho do perfil.
    Aceita '1.2M Seguidores', 'Seguidores 1.2M' ou número e rótulo em linhas
    separadas. Se não achar no texto visível, tenta campos JSON do HTML."""
    info = {}
    linhas = [l.strip().lower() for l in texto.split("\n") if l.strip()][:120]  # só o topo da página
    for i, linha in enumerate(linhas):
        for chave, nomes in ROTULOS_PERFIL.items():
            if chave in info:
                continue
            for nome in nomes:
                n = re.escape(nome)
                m = re.fullmatch(_NUM + r"\s*" + n, linha) or re.fullmatch(n + r"\s*:?\s*" + _NUM, linha)
                valor = m.group(1) if m else None
                if valor is None and linha == nome:
                    for vizinha in (linhas[i - 1] if i else "", linhas[i + 1] if i + 1 < len(linhas) else ""):
                        if re.fullmatch(_NUM, vizinha):
                            valor = vizinha
                            break
                if valor is not None and contagem_para_int(valor) is not None:
                    info[chave] = contagem_para_int(valor)
                    break
    if "seguidores" not in info:
        for p in (r'"?fans?_count"?\s*:\s*"?(\d+)', r'"?fansCount"?\s*:\s*"?(\d+)',
                  r'"?follower_?[cC]ount"?\s*:\s*"?(\d+)', r'"?fan"?\s*:\s*"?(\d+)'):
            m = re.search(p, html)
            if m:
                info["seguidores"] = int(m.group(1))
                info["fonte_seguidores"] = "html"
                break
    elif "fonte_seguidores" not in info:
        info["fonte_seguidores"] = "cabeçalho"
    return info


def _rolar(driver):
    altura = driver.execute_script("return window.innerHeight;") or 900
    for _ in range(6):
        driver.execute_script(f"window.scrollBy(0, {altura // 2});")
        driver.execute_script("window.dispatchEvent(new Event('scroll'));")
        time.sleep(0.5)
    time.sleep(3)


def ler_grade_do_perfil(url_perfil, conta, max_videos, avisar):
    """IDs: mesma lógica do robô de links (modo celular; links na tela + /video/ID
    e photoId no código-fonte). Devolve (cards, info)."""
    avisar("Abrindo o navegador…")
    driver = montar_driver()
    ids, vistos, info = [], set(), {}
    sem_novidade = 0
    try:
        avisar(f"Carregando o perfil @{conta}…")
        driver.get(url_perfil)
        time.sleep(3)
        try:
            texto = driver.execute_script("return document.body.innerText;") or ""
            info.update(extrair_info_perfil(texto, driver.page_source or ""))
        except Exception:
            pass

        for i in range(100):
            hrefs = driver.execute_script(
                "return Array.from(document.querySelectorAll('a[href*=\"/video/\"]'))"
                ".map(a => a.getAttribute('href'));"
            ) or []
            ids_dom = re.findall(r"/video/(\d{8,})", " ".join(h for h in hrefs if h))
            html = driver.page_source
            ids_html = re.findall(r"/video/(\d{8,})", html)
            ids_html += re.findall(r'"photoId"\s*:\s*"?(\d{8,})"?', html)

            antes = len(ids)
            for vid in ids_dom + ids_html:
                if vid not in vistos and len(ids) < max_videos:
                    vistos.add(vid)
                    ids.append(vid)
            avisar(f"Rolando a grade: {len(ids)}/{max_videos} vídeo(s)")
            if len(ids) >= max_videos:
                break
            sem_novidade = sem_novidade + 1 if len(ids) == antes else 0
            if sem_novidade >= 8:
                break
            _rolar(driver)

        if "seguidores" not in info:
            try:
                driver.execute_script("window.scrollTo(0, 0);")
                time.sleep(1)
                texto = driver.execute_script("return document.body.innerText;") or ""
                info.update(extrair_info_perfil(texto, driver.page_source or ""))
            except Exception:
                pass
    finally:
        driver.quit()

    cards = [{"id": v, "href": "", "views_texto": "", "titulo_grade": "", "miniatura": ""}
             for v in ids[:max_videos]]
    return cards, info


# ----------------------------------------------------------------------
# 2) PÁGINA DO VÍDEO (requests): curtidas, comentários, legenda, data
# ----------------------------------------------------------------------

PADROES_URL_VIDEO = [
    r'"photoUrl"\s*:\s*"([^"]+\.mp4[^"]*)"',
    r'"srcNoMark"\s*:\s*"([^"]+)"',
    r'"mainMvUrl"\s*:\s*"([^"]+)"',
    r'"playUrl"\s*:\s*"([^"]+)"',
    r'https?:[^\s"\'<>]+\.mp4[^\s"\'<>]*',
]

PADROES_CAMPOS = {
    "curtidas": [r'"?like_count"?\s*:\s*(\d+)', r'"?likeCount"?\s*:\s*(\d+)', r'"?realLikeCount"?\s*:\s*(\d+)'],
    "comentarios": [r'"?comment_count"?\s*:\s*(\d+)', r'"?commentCount"?\s*:\s*(\d+)'],
    "compartilhamentos": [r'"?forward_count"?\s*:\s*(\d+)', r'"?share_count"?\s*:\s*(\d+)', r'"?shareCount"?\s*:\s*(\d+)'],
    "views_pagina": [r'"?view_count"?\s*:\s*(\d+)', r'"?play_count"?\s*:\s*(\d+)', r'"?viewCount"?\s*:\s*(\d+)',
                     r'"?playCount"?\s*:\s*(\d+)', r'"?photoViewCount"?\s*:\s*(\d+)'],
}


def _url_video(html):
    for p in PADROES_URL_VIDEO:
        for m in re.findall(p, html):
            url = m.replace("\\u002F", "/").replace("\\/", "/")
            if url.startswith("http") and (".mp4" in url or ".m3u8" in url):
                return url
    return None


def _data_publicacao(html, video_id):
    """Devolve (data AAAA-MM-DD, hora 0-23 em Brasília ou None, fonte).
    Procura o horário mais próximo do ID do vídeo (a página traz outros vídeos).
    A hora só vem de timestamp (fuso conhecido)."""
    candidatos = []
    p_hora = re.compile(r'\btime"?\s*:\s*"(\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}:\d{2}"')
    p_ts = re.compile(r'\b(?:timestamp|createTime|create_time|publishTime)"?\s*:\s*"?(\d{10,13})\b')
    for m_id in re.finditer(re.escape(video_id), html):
        ini = max(0, m_id.start() - 800)
        janela = html[ini:m_id.end() + 800]
        pos = m_id.start() - ini
        for m in p_hora.finditer(janela):
            candidatos.append((abs(m.start() - pos), m.group(1), None))
        for m in p_ts.finditer(janela):
            v = int(m.group(1))
            v = v / 1000 if v > 10**12 else v
            try:
                dt = datetime.fromtimestamp(v, tz=FUSO_BR)
            except (ValueError, OSError, OverflowError):
                continue
            if 2015 <= dt.year <= 2100:
                candidatos.append((abs(m.start() - pos) - 0.5, dt.strftime("%Y-%m-%d"), dt.hour))
    if candidatos:
        _, data, hora = min(candidatos, key=lambda c: c[0])
        if hora is None:
            com_hora = [c for c in candidatos if c[2] is not None and c[1] == data]
            if com_hora:
                hora = min(com_hora, key=lambda c: c[0])[2]
        return data, hora, "html"
    url = _url_video(html)
    if url:
        m = re.search(r"/(\d{4})/(\d{2})/(\d{2})/\d{2}/", url)
        if m:
            return "-".join(m.groups()), None, "cdn"
    return "", None, ""


def metricas_da_pagina(link, video_id):
    r = requests.get(link, headers=HEADERS, timeout=25)
    html = r.text
    out = {"curtidas": None, "comentarios": None, "compartilhamentos": None,
           "views_pagina": None, "legenda": "", "data_publicacao": "", "hora_publicacao": None,
           "fonte_data": ""}

    legenda = _meta(html, "og:description") or ""
    out["legenda"] = legenda.strip()

    desc = _meta(html, "description") or ""
    padroes_lc = [
        r'(\d[\d.,]*\s*[KkMm]?)\s*Like\(s\).*?(\d[\d.,]*\s*[KkMm]?)\s*Comment\(s\)',
        r'(\d[\d.,]*\s*[KkMm]?)\s*Curtida\(s\).*?(\d[\d.,]*\s*[KkMm]?)\s*Coment[aá]rio\(s\)',
        r'(\d[\d.,]*\s*[KkMm]?)\s*curtidas?.*?(\d[\d.,]*\s*[KkMm]?)\s*coment[aá]rios?',
    ]
    for fonte in (desc, html):
        for p in padroes_lc:
            m = re.search(p, fonte, re.IGNORECASE)
            if m:
                out["curtidas"] = contagem_para_int(m.group(1))
                out["comentarios"] = contagem_para_int(m.group(2))
                break
        if out["curtidas"] is not None:
            break

    for campo, padroes in PADROES_CAMPOS.items():
        if out.get(campo) is not None:
            continue
        for p in padroes:
            m = re.search(p, html)
            if m:
                out[campo] = int(m.group(1))
                break

    out["data_publicacao"], out["hora_publicacao"], out["fonte_data"] = _data_publicacao(html, video_id)
    return out






# ----------------------------------------------------------------------
# TAREFAS, FILA E CACHE
# ----------------------------------------------------------------------

app = Flask(__name__)
JOBS = {}
CACHE = {}                       # (conta, max) -> (hora, job_id)
TRAVA = threading.Lock()
NAVEGADORES = threading.Semaphore(COLETAS_SIMULTANEAS)
FILA = []                        # job_ids esperando navegador


def _atualizar(job_id, **campos):
    with TRAVA:
        JOBS[job_id].update(campos)


def executar(job_id, url_perfil, conta, max_videos):
    with TRAVA:
        FILA.append(job_id)
    try:
        while True:                                   # espera a vez na fila
            with TRAVA:
                pos = FILA.index(job_id) + 1
            if pos <= COLETAS_SIMULTANEAS and NAVEGADORES.acquire(timeout=1):
                break
            _atualizar(job_id, mensagem=f"Muita gente analisando agora — você é o nº {pos} da fila. Já já começa…")
            time.sleep(1)
        with TRAVA:
            FILA.remove(job_id)
        try:
            cards, info = ler_grade_do_perfil(url_perfil, conta, max_videos,
                                              lambda m: _atualizar(job_id, mensagem=m))
        finally:
            NAVEGADORES.release()
    except Exception as e:
        with TRAVA:
            if job_id in FILA:
                FILA.remove(job_id)
        _atualizar(job_id, status="erro", mensagem=f"Não consegui abrir o perfil: {e}")
        return

    if not cards:
        _atualizar(job_id, status="erro",
                   mensagem="Nenhum vídeo apareceu nesse perfil. Confira o link — perfis privados ou inexistentes não funcionam.")
        return

    itens = []
    for pos, c in enumerate(cards, start=1):
        views = contagem_para_int(c["views_texto"])
        itens.append({"posicao": pos, "id_video": c["id"],
                      "link": f"https://www.kwai.com/@{conta}/video/{c['id']}",
                      "titulo": c["titulo_grade"], "visualizacoes": views,
                      "fonte_views": "grade" if views is not None else "",
                      "curtidas": None, "comentarios": None, "compartilhamentos": None,
                      "data_publicacao": "", "hora_publicacao": None, "legenda": "", "estado": "pendente"})
    _atualizar(job_id, itens=itens, perfil_info=info,
               mensagem=f"{len(itens)} vídeo(s) encontrados. Lendo os dados de cada um…")

    def um(item):
        try:
            return item, metricas_da_pagina(item["link"], item["id_video"]), None
        except Exception as e:
            return item, None, str(e)

    feitos = 0
    with ThreadPoolExecutor(max_workers=TRABALHADORES_PAGINA) as ex:
        for fut in as_completed([ex.submit(um, it) for it in itens]):
            item, m, erro = fut.result()
            with TRAVA:
                if erro:
                    item["estado"] = "erro"
                else:
                    for k in ("curtidas", "comentarios", "compartilhamentos", "data_publicacao",
                              "hora_publicacao", "legenda"):
                        item[k] = m[k]
                    if not item["titulo"]:
                        item["titulo"] = (m["legenda"] or "")[:120]
                    item["estado"] = "ok"
                feitos += 1
                JOBS[job_id]["mensagem"] = f"Lendo os vídeos: {feitos}/{len(itens)}"

    _atualizar(job_id, status="concluido", mensagem=f"Pronto: {len(itens)} vídeo(s) de @{conta}.")
    with TRAVA:
        CACHE[(conta.lower(), max_videos)] = (time.time(), job_id)


def _limpar_antigos():
    limite = time.time() - CACHE_HORAS * 3600
    with TRAVA:
        for chave, (hora, jid) in list(CACHE.items()):
            if hora < limite:
                CACHE.pop(chave, None)
        vivos = {jid for _, jid in CACHE.values()}
        for jid in list(JOBS):
            j = JOBS[jid]
            if j["status"] != "rodando" and jid not in vivos and j["criado"] < limite:
                JOBS.pop(jid, None)


# ----------------------------------------------------------------------
# ROTAS
# ----------------------------------------------------------------------

@app.get("/")
def pagina():
    return Response(HTML, mimetype="text/html")


@app.get("/api/ping")
def ping():
    return jsonify({"ok": True})


@app.post("/api/coletar")
def coletar():
    _limpar_antigos()
    dados = request.get_json(silent=True) or {}
    url_perfil, conta = normalizar_perfil(dados.get("perfil", ""))
    if not conta:
        return jsonify({"erro": "Não reconheci esse link. Use algo como https://www.kwai.com/@conta"}), 400
    try:
        max_videos = max(5, min(int(dados.get("max", 40)), MAX_VIDEOS_TETO))
    except (TypeError, ValueError):
        max_videos = 40

    with TRAVA:
        pronto = CACHE.get((conta.lower(), max_videos))
        if pronto and time.time() - pronto[0] < CACHE_HORAS * 3600 and pronto[1] in JOBS:
            return jsonify({"job": pronto[1], "conta": conta, "cache": True})
        for j in JOBS.values():                       # alguém já está analisando esse perfil agora
            if j["status"] == "rodando" and j["conta"].lower() == conta.lower() and j["max"] == max_videos:
                return jsonify({"job": j["id"], "conta": conta})
        ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip()
        if sum(1 for j in JOBS.values() if j["status"] == "rodando" and j.get("ip") == ip) >= MAX_POR_IP:
            return jsonify({"erro": "Você já tem análises em andamento. Espere terminar para pedir outra."}), 429

        job_id = uuid.uuid4().hex[:12]
        JOBS[job_id] = {"id": job_id, "conta": conta, "max": max_videos, "status": "rodando",
                        "mensagem": "Iniciando…", "itens": [], "ip": ip, "criado": time.time(),
                        "inicio": datetime.now(FUSO_BR).strftime("%Y-%m-%d %H:%M")}
    threading.Thread(target=executar, args=(job_id, url_perfil, conta, max_videos), daemon=True).start()
    return jsonify({"job": job_id, "conta": conta})


@app.get("/api/job/<job_id>")
def andamento(job_id):
    with TRAVA:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"erro": "Essa análise expirou. Clique em Analisar de novo."}), 404
        publico = {k: v for k, v in job.items() if k not in ("ip", "criado")}
        return jsonify(publico)


HTML = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body>
<title>Raio-X Kwai</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Familjen+Grotesk:wght@500;600;700&family=Instrument+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
  :root {
    --bg: #fbf4ee; --surface: #ffffff; --sunken: #f6eae1;
    --ink: #24170f; --ink2: #5b4638; --muted: #8a7466; --line: #eedccf; --grid: #f5e9e0;
    --accent: #c8420b; --accent-soft: #fde2d2; --accent-ink: #ffffff;
    --hot: #8a2c00; --warn: #a33a06; --warn-soft: #fde2d2; --err: #a33a06;
    --display: "Familjen Grotesk", "Helvetica Neue", Arial, sans-serif;
    --body: "Instrument Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
    --mono: "IBM Plex Mono", ui-monospace, "SF Mono", Menlo, monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --bg: #16100c; --surface: #1f1712; --sunken: #2a1f18;
      --ink: #f6ece5; --ink2: #d5c2b4; --muted: #a28b7b; --line: #3b2d24; --grid: #2d221b;
      --accent: #ff7a33; --accent-soft: #4a2211; --accent-ink: #1b0b02;
      --hot: #ffb380; --warn: #ff9a5c; --warn-soft: #4a2211; --err: #ff9a5c;
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --bg: #16100c; --surface: #1f1712; --sunken: #2a1f18;
    --ink: #f6ece5; --ink2: #d5c2b4; --muted: #a28b7b; --line: #3b2d24; --grid: #2d221b;
    --accent: #ff7a33; --accent-soft: #4a2211; --accent-ink: #1b0b02;
    --hot: #ffb380; --warn: #ff9a5c; --warn-soft: #4a2211; --err: #ff9a5c;
  }
  * { box-sizing: border-box; }
  body { background: var(--bg); color: var(--ink); font: 15px/1.5 var(--body); }
  .wrap { max-width: 1120px; margin: 0 auto; padding-inline: 16px; padding-block: 28px 64px; display: grid; gap: 28px; }
  .num, td.n, .kpi b, .mono { font-family: var(--mono); font-variant-numeric: tabular-nums; }

  header.top { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap;
                background: var(--accent); color: var(--accent-ink); border-radius: 16px; padding: 18px 20px; }
  .marca { font: 700 24px/1 var(--display); letter-spacing: -0.02em; display: flex; align-items: center; gap: 10px; }
  .marca i { width: 13px; height: 13px; border-radius: 3px; background: var(--accent-ink); display: inline-block; transform: rotate(45deg); }
  .top small { color: var(--accent-ink); opacity: .85; font-size: 14px; }
  .marca-bloco { display: grid; gap: 6px; }
  .autoria { font-size: 13px; color: var(--accent-ink); opacity: .9; letter-spacing: .01em; }
  .autoria b { font-weight: 600; opacity: 1; }

  /* entrada */
  .entrada { background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: 18px; display: grid; gap: 14px; }
  .linha-form { display: flex; gap: 8px; flex-wrap: wrap; }
  .linha-form input[type=text] { flex: 1 1 320px; min-width: 0; }
  input[type=text], input[type=number], select { font: inherit; color: var(--ink); background: var(--bg); border: 1px solid var(--line);
         border-radius: 10px; padding: 11px 12px; }
  input[type=number] { width: 150px; }
  .btn { font: 600 14px/1 var(--body); padding: 12px 16px; border-radius: 10px; border: 1px solid var(--line);
         background: var(--surface); color: var(--ink); cursor: pointer; display: inline-flex; align-items: center; gap: 8px; }
  .btn.pri { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); }
  .btn:hover { filter: brightness(1.06); }
  input:focus-visible, select:focus-visible, .btn:focus-visible, th:focus-visible, summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
            font: 12.5px/1.5 var(--mono); color: var(--ink); overflow-x: auto; white-space: pre; }
  [hidden] { display: none !important; }
  body { margin: 0; }
  .acao { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .status-txt { font-size: 14px; color: var(--ink2); display: inline-flex; align-items: center; gap: 8px; }
  .spin { width: 14px; height: 14px; border: 2px solid var(--line); border-top-color: var(--accent); border-radius: 50%; animation: gira .8s linear infinite; }
  @keyframes gira { to { transform: rotate(360deg); } }
  @media (prefers-reduced-motion: reduce) { .spin { animation-duration: 3s; } }
  .barra { height: 4px; border-radius: 4px; background: var(--grid); overflow: hidden; }
  .barra i { display: block; height: 100%; width: 0; background: var(--accent); transition: width .3s; }
          gap: 12px; flex-wrap: wrap; color: var(--ink2); font-size: 14px; }
  .aviso { font-size: 13.5px; padding: 9px 12px; border-radius: 8px; background: var(--warn-soft); color: var(--ink); }
  .aviso b { color: var(--warn); }
  .erro { color: var(--err); font-size: 14px; }

  /* laudo */
  .laudo { display: grid; gap: 14px; }
  .eyebrow { font: 500 12px/1 var(--mono); letter-spacing: .08em; text-transform: uppercase; color: var(--muted); display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
  .tag { font: 600 11px/1 var(--mono); letter-spacing: .06em; padding: 5px 8px; border-radius: 999px; background: var(--warn-soft); color: var(--warn); text-transform: uppercase; }
  .laudo h1 { font: 600 clamp(24px, 3.4vw, 36px)/1.18 var(--display); letter-spacing: -0.02em; margin: 0; text-wrap: balance; max-width: 30ch; }
  .laudo h1 em { font-style: normal; color: var(--accent); }
  .laudo p.resumo { margin: 0; color: var(--ink2); max-width: 70ch; font-size: 16px; }
  .chips { display: flex; gap: 8px; flex-wrap: wrap; }
  .chip { font-size: 13px; padding: 6px 10px; border-radius: 999px; background: var(--surface); border: 1px solid var(--line); color: var(--ink2); }
  .chip b { color: var(--ink); font-weight: 600; }

  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 1px; background: var(--line);
          border: 1px solid var(--line); border-radius: 14px; overflow: hidden; }
  .kpi { background: var(--surface); padding: 16px 18px; display: grid; gap: 4px; align-content: start; }
  .kpi span { font-size: 13px; color: var(--muted); }
  .kpi b { font-size: 26px; font-weight: 500; letter-spacing: -0.02em; line-height: 1.15; }
  .kpi small { font-size: 12.5px; color: var(--ink2); }

  /* painéis */
  .grade2 { display: grid; grid-template-columns: 1.5fr 1fr; gap: 16px; }
  @media (max-width: 820px) { .grade2 { grid-template-columns: 1fr; } }
  .painel { background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: 18px; display: grid; gap: 12px; align-content: start; min-width: 0; }
  .painel h2 { font: 600 17px/1.25 var(--display); margin: 0; letter-spacing: -0.01em; }
  .painel .sub { margin: -6px 0 0; font-size: 13.5px; color: var(--muted); }
  .grafico { width: 100%; position: relative; }
  .grafico svg { display: block; width: 100%; height: auto; overflow: visible; }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px 16px; margin: 0; }
  .stats div { display: grid; gap: 2px; }
  .stats dt { font-size: 12.5px; color: var(--muted); }
  .stats dd { margin: 0; font: 500 15px/1.3 var(--mono); }
  .stats dd small { font: 12px var(--body); color: var(--ink2); display: block; }


  .top5 { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; }
  .top5 li { display: grid; grid-template-columns: 1fr auto; gap: 10px; align-items: baseline; font-size: 14px; padding-bottom: 8px; border-bottom: 1px solid var(--grid); }
  .top5 li:last-child { border-bottom: 0; padding-bottom: 0; }
  .top5 a { color: var(--ink); text-decoration: none; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
  .top5 a:hover { text-decoration: underline; }
  .top5 small { color: var(--muted); font-size: 12px; display: block; }

  .tabela { overflow-x: auto; border: 1px solid var(--line); border-radius: 14px; background: var(--surface); }
  table { border-collapse: collapse; width: 100%; min-width: 760px; font-size: 14px; }
  th, td { padding: 10px 12px; border-bottom: 1px solid var(--grid); text-align: left; }
  tbody tr:last-child td { border-bottom: 0; }
  th { font: 600 11.5px/1 var(--mono); letter-spacing: .06em; text-transform: uppercase; color: var(--muted); cursor: pointer; white-space: nowrap; user-select: none; }
  th.n, td.n { text-align: right; }
  th[aria-sort="ascending"]::after { content: " ↑"; } th[aria-sort="descending"]::after { content: " ↓"; }
  td.t { max-width: 420px; }
  td.t .t-tit { display: block; color: var(--ink); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  td.t .t-link { display: block; margin-top: 2px; font: 12px/1.4 var(--mono); color: var(--accent); text-decoration: none;
                 overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  td.t a.t-link:hover { text-decoration: underline; }
  td.t .t-link.vazio { color: var(--muted); }
  .vazio { color: var(--muted); }

  #dica { position: absolute; pointer-events: none; background: var(--ink); color: var(--bg); font-size: 12.5px; line-height: 1.4;
          padding: 7px 9px; border-radius: 8px; max-width: 260px; z-index: 5; transform: translate(-50%, calc(-100% - 10px)); }
  #dica b { font-family: var(--mono); font-weight: 500; }
  .nota { font-size: 12.5px; color: var(--muted); margin: 0; max-width: 80ch; }
  @media (prefers-reduced-motion: no-preference) { .kpi b, .laudo h1 { transition: color .2s; } }
  /* v2 */
  .insights { background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: 18px; display: grid; gap: 10px; }
  .insights h2 { font: 600 17px/1.25 var(--display); margin: 0; }
  .insights ul { margin: 0; padding: 0; list-style: none; display: grid; gap: 10px; }
  .insights li { display: grid; grid-template-columns: 22px 1fr; gap: 8px; font-size: 15px; line-height: 1.45; color: var(--ink); }
  .insights li::before { content: ""; width: 10px; height: 10px; margin-top: 6px; border-radius: 2px; background: var(--accent); transform: rotate(45deg); justify-self: center; }
  .insights li b { font-weight: 600; }
  .conclusao { margin: 0; font-size: 14.5px; color: var(--ink); background: var(--accent-soft); border-radius: 10px; padding: 10px 12px; }
  .conclusao b { color: var(--hot); }
  .grande { font: 600 44px/1 var(--display); letter-spacing: -0.03em; color: var(--accent); }
  .grande small { display: block; font: 13px/1.4 var(--body); color: var(--muted); letter-spacing: 0; margin-top: 6px; max-width: 30ch; }
  .seg-bloco { display: grid; grid-template-columns: auto 1fr; gap: 20px; align-items: center; }
  @media (max-width: 560px) { .seg-bloco { grid-template-columns: 1fr; } }
  .vezes { font: 500 13px/1 var(--mono); color: var(--hot); }
  .hora { color: var(--muted); font-size: 12px; }
</style>

<div class="wrap">
  <header class="top">
    <div class="marca-bloco">
      <div class="marca"><i aria-hidden="true"></i>Raio-X Kwai</div>
      <div class="autoria">Criado por <b>Carolina Campelo</b> · setembro de 2026</div>
    </div>
    <small>Cole o link de qualquer perfil do Kwai e veja quando a conta posta e quando engaja</small>
  </header>

  <section class="entrada" aria-label="Analisar um perfil">
    <div class="linha-form">
      <input type="text" id="perfil" placeholder="Cole o link do perfil, ex.: https://www.kwai.com/@Lulaoficial" autocomplete="off">
      <select id="qtd" aria-label="Quantos vídeos analisar">
        <option value="20">Últimos 20</option>
        <option value="40" selected>Últimos 40</option>
        <option value="80">Últimos 80</option>
      </select>
      <input type="number" id="seg" min="0" placeholder="Seguidores (opcional)" aria-label="Seguidores (opcional)">
    </div>
    <div class="acao">
      <button class="btn pri" type="button" id="analisar">Analisar perfil</button>
      <span class="status-txt" id="status"></span>
    </div>
    <div class="barra" id="barra" hidden><i id="progresso"></i></div>
    <div class="aviso" id="sem-robo" hidden>
      <b>O servidor de coleta não respondeu.</b> Atualize a página em alguns segundos e tente de novo.
    </div>
    <div id="msg" class="erro" hidden></div>
  </section>

  <section class="laudo" aria-live="polite">
    <div class="eyebrow"><span id="lb-conta">@conta</span><span id="lb-exemplo" class="tag">Dados de exemplo</span><span id="lb-coleta"></span></div>
    <h1 id="manchete"></h1>
    <p class="resumo" id="resumo"></p>
    <div class="chips" id="chips"></div>
  </section>

  <div class="kpis" id="kpis"></div>

  <section class="insights">
    <h2>O que levar pra conversa</h2>
    <ul id="insights"></ul>
  </section>

  <div class="grade2">
    <section class="painel">
      <h2>Dia em que mais posta vs dia em que mais engaja</h2>
      <p class="sub">Esquerda: quantos vídeos saíram em cada dia. Direita: interações medianas por vídeo publicado naquele dia (curtidas + comentários).</p>
      <div class="grafico" id="g-dias"></div>
      <p class="conclusao" id="c-dias"></p>
    </section>
    <section class="painel">
      <h2>Horário em que mais posta vs horário em que mais engaja</h2>
      <p class="sub">Mesma comparação, por faixa do dia (horário de Brasília).</p>
      <div class="grafico" id="g-horas"></div>
      <p class="conclusao" id="c-horas"></p>
    </section>
  </div>

  <section class="painel">
    <h2>Engajamento de cada vídeo ao longo do tempo</h2>
    <p class="sub" id="sub-eng"></p>
    <div class="grafico" id="g-eng"></div>
    <dl class="stats" id="stats-eng"></dl>
  </section>

  <div class="grade2">
    <section class="painel">
      <h2>Publicações por semana</h2>
      <p class="sub" id="sub-semanas"></p>
      <div class="grafico" id="g-semanas"></div>
      <dl class="stats" id="stats-freq"></dl>
    </section>
    <section class="painel">
      <h2>Engajamento em relação aos seguidores</h2>
      <p class="sub">Quanto da base de seguidores interage com um vídeo típico</p>
      <div class="seg-bloco">
        <div class="grande" id="seg-num"></div>
        <dl class="stats" id="stats-seg"></dl>
      </div>
    </section>
  </div>

  <section class="painel">
    <h2>Vídeos que mais engajaram</h2>
    <p class="sub">Interações de cada vídeo comparadas com a mediana do perfil</p>
    <ol class="top5" id="top-eng"></ol>
  </section>

  <section class="painel" style="padding:0;border:0;background:none">
    <h2>Todos os vídeos analisados</h2>
    <div class="tabela">
      <table>
        <thead><tr>
          <th data-k="data" tabindex="0">Publicado</th>
          <th data-k="titulo" tabindex="0">Vídeo</th>
          <th data-k="likes" class="n" tabindex="0">Curtidas</th>
          <th data-k="coms" class="n" tabindex="0">Coment.</th>
          <th data-k="int" class="n" tabindex="0">Interações</th>
          <th data-k="rel" class="n" tabindex="0">vs mediana</th>
          <th data-k="pseg" class="n" tabindex="0">% seguidores</th>
        </tr></thead>
        <tbody id="linhas"></tbody>
      </table>
    </div>
    <p class="nota" id="nota-final"></p>
  </section>
</div>
<div id="dica" hidden></div>

<script>
(() => {
const $ = s => document.querySelector(s);
const DIA = 864e5;
const DIAS_CURTO = ['seg', 'ter', 'qua', 'qui', 'sex', 'sáb', 'dom'];
const DIAS_NOS = ['às segundas', 'às terças', 'às quartas', 'às quintas', 'às sextas', 'aos sábados', 'aos domingos'];
const FAIXAS = [{rot: 'madrug.', nome: 'de madrugada', ini: 0}, {rot: 'manhã', nome: 'de manhã', ini: 6},
                {rot: 'tarde', nome: 'à tarde', ini: 12}, {rot: 'noite', nome: 'à noite', ini: 18}];
const MESES = ['jan', 'fev', 'mar', 'abr', 'mai', 'jun', 'jul', 'ago', 'set', 'out', 'nov', 'dez'];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const nf = (n, d = 0) => Number(n).toLocaleString('pt-BR', {maximumFractionDigits: d, minimumFractionDigits: 0});
function compacto(n) {
  if (n === null || n === undefined || !isFinite(n)) return '–';
  const a = Math.abs(n);
  if (a >= 1e6) return nf(n / 1e6, a >= 1e7 ? 0 : 1) + ' mi';
  if (a >= 1e4) return nf(n / 1e3, 0) + ' mil';
  if (a >= 1e3) return nf(n / 1e3, 1) + ' mil';
  return nf(n, 0);
}
const pct = (x, d = 1) => (x === null || x === undefined || !isFinite(x)) ? '–' : nf(x * 100, d) + '%';
const dataCurta = d => `${d.getDate()} ${MESES[d.getMonth()]}`;
const dataLonga = d => `${d.getDate()} ${MESES[d.getMonth()]} ${d.getFullYear()}`;
function dataDe(v) { const m = String(v || '').match(/(\d{4})-(\d{2})-(\d{2})/); return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null; }
function numero(v) {
  if (v === undefined || v === null) return null;
  const s = String(v).trim().toLowerCase().replace(/\s/g, ''); if (!s) return null;
  const m = s.match(/^(\d+(?:[.,]\d+)*)(k|m|mil|mi)?$/); if (!m) return null;
  if (m[2]) return Math.round(parseFloat(m[1].replace(',', '.')) * ({k: 1e3, mil: 1e3, m: 1e6, mi: 1e6}[m[2]]));
  return parseInt(m[1].replace(/[.,]/g, ''), 10);
}
const mediana = a => { if (!a.length) return null; const b = [...a].sort((x, y) => x - y); const m = b.length >> 1; return b.length % 2 ? b[m] : (b[m - 1] + b[m]) / 2; };
const segunda = d => { const x = new Date(d); x.setDate(x.getDate() - ((x.getDay() + 6) % 7)); x.setHours(0, 0, 0, 0); return x; };
const faixaDe = h => h === null || h === undefined ? null : (h < 6 ? 0 : h < 12 ? 1 : h < 18 ? 2 : 3);

// ---------- exemplo (claramente marcado) ----------
function exemplo() {
  let s = 11; const rnd = () => (s = (s * 16807) % 2147483647) / 2147483647;
  const hoje = new Date(); hoje.setHours(0, 0, 0, 0);
  const out = []; let d = new Date(hoje);
  for (let i = 0; i < 40; i++) {
    const dia = (d.getDay() + 6) % 7, hora = [8, 11, 13, 19, 20, 21, 22][Math.floor(rnd() * 7)];
    const base = 900 * (dia === 4 ? 1.9 : dia === 0 ? 0.8 : 1) * (hora >= 18 ? 1.4 : 0.9) * (0.4 + rnd() * 1.4) * (rnd() > 0.93 ? 3 : 1);
    const likes = Math.round(base);
    out.push({id: String(5200000000000000000 + i), link: '', titulo: `Vídeo de exemplo ${40 - i}`, data: new Date(d), hora,
      likes, coms: Math.round(likes * (0.02 + rnd() * 0.06)), conta: 'contaexemplo', seguidores: 185000, coleta: ''});
    d = new Date(d.getTime() - Math.floor(rnd() * rnd() * 5) * DIA);
  }
  return out;
}

// ---------- análise ----------
function grupos(vids, chave, n) {
  const g = Array.from({length: n}, () => ({n: 0, ints: []}));
  vids.forEach(v => { const k = chave(v); if (k === null) return; g[k].n++; if (v.int !== null) g[k].ints.push(v.int); });
  g.forEach(x => { x.med = mediana(x.ints); });
  const maisPosta = g.reduce((b, x, i) => x.n > g[b].n ? i : b, 0);
  const minimo = g.some(x => x.ints.length >= 2) ? 2 : 1;
  let melhor = null;
  g.forEach((x, i) => { if (x.ints.length >= minimo && (melhor === null || x.med > g[melhor].med)) melhor = i; });
  return {g, maisPosta, melhor, minimo};
}

function analisar(todos, qtd, segManual) {
  const comData = todos.filter(v => v.data).sort((a, b) => b.data - a.data);
  const vids = (qtd > 0 ? comData.slice(0, qtd) : comData).reverse();
  if (!vids.length) return null;
  vids.forEach(v => { v.int = v.likes !== null && v.likes !== undefined ? v.likes + (v.coms || 0) : null; });
  const n = vids.length, antigo = vids[0].data, recente = vids[n - 1].data;
  const dias = Math.round((recente - antigo) / DIA) + 1, porSemana = n / (dias / 7);

  const semanas = [];
  for (let w = segunda(antigo); w <= recente; w = new Date(w.getTime() + 7 * DIA)) semanas.push({ini: w, n: 0, ints: []});
  vids.forEach(v => { const k = Math.round((segunda(v.data) - semanas[0].ini) / (7 * DIA)); if (semanas[k]) { semanas[k].n++; if (v.int !== null) semanas[k].ints.push(v.int); } });
  const intervalos = [];
  for (let i = 1; i < n; i++) intervalos.push({d: Math.round((vids[i].data - vids[i - 1].data) / DIA), de: vids[i - 1].data, ate: vids[i].data});
  const maiorHiato = intervalos.reduce((a, b) => (!a || b.d > a.d ? b : a), null);

  const ints = vids.map(v => v.int).filter(x => x !== null);
  const medInt = mediana(ints);
  const L = vids.reduce((a, v) => a + (v.likes || 0), 0), C = vids.reduce((a, v) => a + (v.coms || 0), 0);
  vids.forEach(v => { v.rel = v.int !== null && medInt ? v.int / medInt : null; });

  const dias7 = grupos(vids, v => (v.data.getDay() + 6) % 7, 7);
  const comHora = vids.filter(v => v.hora !== null && v.hora !== undefined).length;
  const faixas = comHora >= Math.max(3, n * 0.5) ? grupos(vids, v => faixaDe(v.hora), 4) : null;

  // tendência: últimos k vs k anteriores
  let tend = null;
  const comInt = vids.filter(v => v.int !== null);
  if (comInt.length >= 8) {
    const k = Math.min(10, Math.floor(comInt.length / 2));
    const rec = mediana(comInt.slice(-k).map(v => v.int)), ant = mediana(comInt.slice(-2 * k, -k).map(v => v.int));
    if (ant) tend = {k, rec, ant, var: rec / ant - 1};
  }
  // frequência x engajamento: semanas cheias vs semanas leves
  let freqEng = null;
  const semComPost = semanas.filter(s => s.ints.length);
  if (semComPost.length >= 4) {
    const corte = mediana(semComPost.map(s => s.n));
    const cheias = semComPost.filter(s => s.n > corte).flatMap(s => s.ints), leves = semComPost.filter(s => s.n <= corte).flatMap(s => s.ints);
    if (cheias.length >= 3 && leves.length >= 3) freqEng = {corte, cheias: mediana(cheias), leves: mediana(leves)};
  }
  const segArq = Math.max(0, ...todos.map(v => v.seguidores || 0)) || null;
  const seguidores = segManual || segArq;
  vids.forEach(v => { v.pseg = seguidores && v.int !== null ? v.int / seguidores : null; });
  const fora = vids.filter(v => v.rel !== null && v.rel >= 2);
  const campeao = comInt.reduce((a, b) => (!a || b.int > a.int ? b : a), null);

  return {vids, n, antigo, recente, dias, porSemana, semanas, semanasVazias: semanas.filter(s => !s.n).length, intervalos, maiorHiato,
    diasComPost: new Set(vids.map(v => v.data.toDateString())).size, medInt, L, C, dias7, faixas, comHora, tend, freqEng,
    seguidores, fonteSeg: segManual ? 'informado' : (segArq ? 'perfil' : null), fora, campeao, nInt: ints.length,
    conta: (todos.find(v => v.conta) || {}).conta || '', coleta: (todos.find(v => v.coleta) || {}).coleta || ''};
}
function ritmo(ps) {
  if (ps >= 7) return 'posta todo dia ou mais';
  if (ps >= 3) return 'posta com alta frequência';
  if (ps >= 1) return 'posta com regularidade semanal';
  return 'posta de forma esporádica';
}
const variacao = x => (x >= 0 ? '+' : '−') + nf(Math.abs(x) * 100, 0) + '%';

function conclusaoGrupo(G, nomes, quando) {
  if (!G || G.melhor === null) return 'Ainda não há vídeos suficientes com curtidas para comparar.';
  const p = G.g[G.maisPosta], m = G.g[G.melhor];
  if (G.maisPosta === G.melhor)
    return `A conta posta mais ${nomes[G.maisPosta]} e é também quando engaja mais: <b>${compacto(m.med)} interações</b> por vídeo (mediana). Estratégia alinhada.`;
  const ganho = p.med ? m.med / p.med - 1 : null;
  return `Posta mais ${nomes[G.maisPosta]} (${p.n} vídeos), mas engaja mais ${nomes[G.melhor]}: <b>${compacto(m.med)} interações</b> por vídeo` +
    (ganho !== null && isFinite(ganho) ? `, <b>${variacao(ganho)}</b> que ${nomes[G.maisPosta]}` : '') +
    `. ${m.n <= 2 ? `Só ${m.n} vídeo(s) ${quando} ${nomes[G.melhor]}: vale testar mais antes de concluir.` : 'Vale concentrar mais posts aí.'}`;
}

// ---------- gráficos ----------
const dica = $('#dica');
function ligarDicas(el) {
  el.querySelectorAll('[data-dica]').forEach(m => {
    const ver = () => { const r = m.getBoundingClientRect(); dica.innerHTML = m.dataset.dica; dica.hidden = false;
      dica.style.left = (r.left + r.width / 2 + scrollX) + 'px'; dica.style.top = (r.top + scrollY) + 'px'; };
    m.addEventListener('mouseenter', ver); m.addEventListener('focus', ver);
    m.addEventListener('mouseleave', () => dica.hidden = true); m.addEventListener('blur', () => dica.hidden = true);
  });
}
function escalaBonita(max, passos = 4) {
  if (!max || max <= 0) return {max: 1, ticks: [0, 1]};
  const bruto = max / passos, mag = Math.pow(10, Math.floor(Math.log10(bruto)));
  const passo = [1, 2, 2.5, 5, 10].map(f => f * mag).find(p => p >= bruto);
  const topo = Math.ceil(max / passo) * passo; const ticks = [];
  for (let t = 0; t <= topo + 1e-9; t += passo) ticks.push(t);
  return {max: topo, ticks};
}
const larg = el => Math.max(280, Math.floor(el.clientWidth || el.parentElement.clientWidth || 560));

// duas colunas lado a lado, cada uma com sua própria escala: vídeos postados | interações medianas
function barrasPar(el, G, rotulos) {
  if (!G) { el.innerHTML = '<p class="vazio" style="margin:0">O Kwai não informou o horário de publicação desses vídeos.</p>'; return; }
  const W = larg(el), l = 52, gap = 22, r = 6, lh = 28, topo = 22, H = topo + rotulos.length * lh + 4;
  const pw = (W - l - gap - r) / 2, num = 58, bw = pw - num;
  const maxN = Math.max(...G.g.map(x => x.n), 1), maxM = Math.max(...G.g.map(x => x.med || 0), 1);
  let s = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Vídeos postados e interações medianas">
    <text x="${l}" y="12" font-size="11.5" fill="var(--muted)">vídeos postados</text>
    <text x="${l + pw + gap}" y="12" font-size="11.5" fill="var(--muted)">interações medianas</text>`;
  rotulos.forEach((rot, i) => {
    const x = G.g[i], y = topo + i * lh;
    const w1 = x.n ? Math.max(4, x.n / maxN * bw) : 0, w2 = x.med ? Math.max(4, x.med / maxM * bw) : 0;
    const c1 = i === G.maisPosta ? 'var(--hot)' : 'var(--accent)', c2 = i === G.melhor ? 'var(--hot)' : 'var(--accent)';
    const x2 = l + pw + gap;
    s += `<text x="${l - 8}" y="${y + 15}" text-anchor="end" font-size="12" fill="var(--ink2)">${rot}</text>
      <rect x="${l}" y="${y + 4}" width="${bw}" height="15" rx="4" fill="var(--sunken)"/>
      <rect x="${x2}" y="${y + 4}" width="${bw}" height="15" rx="4" fill="var(--sunken)"/>`;
    if (w1) s += `<rect x="${l}" y="${y + 4}" width="${w1}" height="15" rx="4" fill="${c1}"/>`;
    if (w2) s += `<rect x="${x2}" y="${y + 4}" width="${w2}" height="15" rx="4" fill="${c2}" ${x.ints.length < G.minimo ? 'fill-opacity=".45"' : ''}/>`;
    s += `<text x="${l + w1 + 6}" y="${y + 16}" font-size="12" fill="var(--ink)" font-family="var(--mono)">${x.n}</text>
      <text x="${x2 + w2 + 6}" y="${y + 16}" font-size="12" fill="var(--ink)" font-family="var(--mono)">${x.med !== null ? compacto(x.med) : '–'}</text>
      <rect x="0" y="${y}" width="${W}" height="${lh}" fill="transparent" tabindex="0"
        data-dica="${esc(`${rot}: <b>${x.n}</b> vídeo(s) · mediana de <b>${x.med !== null ? nf(x.med) : '–'}</b> interações`)}"/>`;
  });
  el.innerHTML = s + '</svg>'; ligarDicas(el);
}

function graficoSemanas(el, A) {
  const W = larg(el), H = 210, m = {t: 14, r: 12, b: 28, l: 30}, iw = W - m.l - m.r, ih = H - m.t - m.b;
  const e = escalaBonita(Math.max(...A.semanas.map(s => s.n), Math.ceil(A.porSemana)));
  const y = v => m.t + ih - (v / e.max) * ih, bw = iw / A.semanas.length, gap = Math.min(4, bw * 0.25);
  const cada = Math.ceil(A.semanas.length / Math.max(2, Math.floor(iw / 58)));
  let s = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Publicações por semana">`;
  e.ticks.forEach(t => { s += `<line x1="${m.l}" x2="${W - m.r}" y1="${y(t)}" y2="${y(t)}" stroke="var(--grid)"/><text x="${m.l - 8}" y="${y(t) + 4}" text-anchor="end" font-size="11" fill="var(--muted)" font-family="var(--mono)">${t}</text>`; });
  A.semanas.forEach((w, i) => {
    const x = m.l + i * bw + gap / 2, fim = new Date(w.ini.getTime() + 6 * DIA);
    if (w.n) s += `<path d="M${x},${m.t + ih} V${y(w.n) + 3} q0,-3 3,-3 h${bw - gap - 6} q3,0 3,3 V${m.t + ih} Z" fill="var(--accent)"/>`;
    else s += `<rect x="${x}" y="${m.t + ih - 2}" width="${bw - gap}" height="2" fill="var(--line)"/>`;
    s += `<rect x="${m.l + i * bw}" y="${m.t}" width="${bw}" height="${ih}" fill="transparent" tabindex="0" data-dica="${esc(`Semana de ${dataCurta(w.ini)} a ${dataCurta(fim)}<br><b>${w.n}</b> vídeo(s)`)}"/>`;
    if (i % cada === 0) s += `<text x="${x + (bw - gap) / 2}" y="${H - 8}" text-anchor="middle" font-size="11" fill="var(--muted)">${dataCurta(w.ini)}</text>`;
  });
  const ym = y(A.porSemana);
  s += `<line x1="${m.l}" x2="${W - m.r}" y1="${ym}" y2="${ym}" stroke="var(--hot)" stroke-width="1.5" stroke-dasharray="4 4"/>
    <text x="${W - m.r}" y="${ym - 6}" text-anchor="end" font-size="11.5" fill="var(--ink)" font-weight="600">média ${nf(A.porSemana, 1)}/semana</text>`;
  el.innerHTML = s + '</svg>'; ligarDicas(el);
}

function graficoEng(el, A) {
  const pts = A.vids.filter(v => v.int !== null);
  if (!pts.length) { el.innerHTML = '<p class="vazio">Sem curtidas nesses vídeos.</p>'; return; }
  const W = larg(el), H = 260, m = {t: 16, r: 14, b: 28, l: 52}, iw = W - m.l - m.r, ih = H - m.t - m.b;
  const e = escalaBonita(Math.max(...pts.map(v => v.int)));
  const t0 = A.antigo.getTime(), t1 = Math.max(A.recente.getTime(), t0 + DIA);
  const x = d => m.l + ((d.getTime() - t0) / (t1 - t0)) * iw, y = v => m.t + ih - (v / e.max) * ih;
  let s = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Interações por vídeo ao longo do tempo">`;
  e.ticks.forEach(t => { s += `<line x1="${m.l}" x2="${W - m.r}" y1="${y(t)}" y2="${y(t)}" stroke="var(--grid)"/><text x="${m.l - 8}" y="${y(t) + 4}" text-anchor="end" font-size="11" fill="var(--muted)" font-family="var(--mono)">${compacto(t)}</text>`; });
  const nM = Math.max(2, Math.floor(iw / 90));
  for (let i = 0; i <= nM; i++) { const d = new Date(t0 + (t1 - t0) * i / nM);
    s += `<text x="${x(d)}" y="${H - 8}" text-anchor="${i === 0 ? 'start' : i === nM ? 'end' : 'middle'}" font-size="11" fill="var(--muted)">${dataCurta(d)}</text>`; }
  const mm = pts.map((v, i) => { const j = pts.slice(Math.max(0, i - 4), i + 1); return [x(v.data), y(mediana(j.map(q => q.int)))]; });
  if (mm.length > 2) s += `<path d="${mm.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ')}" fill="none" stroke="var(--accent)" stroke-width="2" stroke-opacity=".4" stroke-linejoin="round"/>`;
  const ym = y(A.medInt);
  s += `<line x1="${m.l}" x2="${W - m.r}" y1="${ym}" y2="${ym}" stroke="var(--hot)" stroke-width="1.5" stroke-dasharray="4 4"/>
    <text x="${m.l + 6}" y="${ym - 6}" font-size="11.5" fill="var(--ink)" font-weight="600">mediana ${compacto(A.medInt)}</text>`;
  pts.forEach(v => {
    const alto = v.rel >= 2;
    const d = `${esc(v.titulo.slice(0, 80))}<br>${dataLonga(v.data)} · <b>${nf(v.int)}</b> interações` + (v.rel ? ` · ${nf(v.rel, 1)}× a mediana` : '');
    s += `<circle cx="${x(v.data)}" cy="${y(v.int)}" r="${alto ? 6 : 4.5}" fill="${alto ? 'var(--hot)' : 'var(--accent)'}" stroke="var(--surface)" stroke-width="2"/>
      <circle cx="${x(v.data)}" cy="${y(v.int)}" r="11" fill="transparent" tabindex="0" data-dica="${esc(d)}"/>`;
  });
  el.innerHTML = s + '</svg>'; ligarDicas(el);
}

// ---------- render ----------
let TODOS = exemplo(), EXEMPLO = true, ORDEM = {k: 'data', dir: 'desc'};
function erro(t) { const m = $('#msg'); m.textContent = t; m.hidden = !t; }
function normPerfil(t) {
  t = String(t || '').trim().replace(/\$\d+$/, '');
  const m = t.match(/@([\w.\-]+)/); if (m) return '@' + m[1].replace(/\.$/, '');
  return /^[\w.\-]+$/.test(t) ? '@' + t : '';
}
function desenhar(A) {
  barrasPar($('#g-dias'), A.dias7, DIAS_CURTO);
  barrasPar($('#g-horas'), A.faixas, FAIXAS.map(f => f.rot));
  graficoEng($('#g-eng'), A); graficoSemanas($('#g-semanas'), A);
}

function render(aoVivo) {
  const A = analisar(TODOS, +$('#qtd').value, numero($('#seg').value));
  if (!A) { if (!aoVivo) erro('Nenhum vídeo tem data de publicação — sem data não dá pra analisar.'); return; }
  window.__A = A;
  const conta = A.conta || (normPerfil($('#perfil').value) || '').replace(/^@/, '') || 'conta';
  $('#lb-conta').textContent = '@' + conta;
  $('#lb-exemplo').hidden = !EXEMPLO;
  $('#lb-coleta').textContent = A.coleta ? `coletado em ${dataLonga(dataDe(A.coleta))}` : '';
  const D = A.dias7;

  let man = `${A.n} vídeos em ${A.dias} dias: <em>${nf(A.porSemana, 1)} por semana</em>.`;
  if (D.melhor !== null && D.melhor !== D.maisPosta) man += ` Posta mais ${DIAS_NOS[D.maisPosta]}, mas engaja mais ${DIAS_NOS[D.melhor]}.`;
  else if (D.melhor !== null) man += ` Posta e engaja mais ${DIAS_NOS[D.melhor]}.`;
  $('#manchete').innerHTML = man;
  $('#resumo').textContent = `Do vídeo mais antigo (${dataLonga(A.antigo)}) ao mais recente (${dataLonga(A.recente)}), a conta ${ritmo(A.porSemana)}. ` +
    (A.medInt !== null ? `Um vídeo típico recebe ${compacto(A.medInt)} interações (curtidas + comentários).` : '') +
    (A.seguidores && A.medInt !== null ? ` Com ${compacto(A.seguidores)} seguidores, isso é ${pct(A.medInt / A.seguidores, 2)} da base por vídeo.` : '');

  $('#chips').innerHTML = [
    `Ritmo: <b>${ritmo(A.porSemana).replace('posta ', '')}</b>`,
    A.maiorHiato ? `Maior pausa: <b>${A.maiorHiato.d} dias</b>` : '',
    A.tend ? `Tendência: <b>${variacao(A.tend.var)}</b> nos últimos ${A.tend.k} vídeos` : '',
    `Fora da curva: <b>${A.fora.length} vídeo(s)</b> com 2× a mediana`,
  ].filter(Boolean).map(c => `<span class="chip">${c}</span>`).join('');

  const por1000 = A.seguidores && A.medInt !== null ? A.medInt / A.seguidores * 1000 : null;
  $('#kpis').innerHTML = [
    ['Período analisado', `${dataCurta(A.antigo)} → ${dataCurta(A.recente)}`, `${A.dias} dias · ${A.n} vídeos`],
    ['Publicações por semana', nf(A.porSemana, 1), `${nf(A.porSemana / 7 * 30, 0)} por mês`],
    ['Interações por vídeo', compacto(A.medInt), 'mediana de curtidas + comentários'],
    ['A cada 1.000 seguidores', por1000 !== null ? nf(por1000, por1000 < 10 ? 1 : 0) : '–', por1000 !== null ? 'interagem com um vídeo típico' : 'precisa dos seguidores'],
    ['Seguidores', A.seguidores ? compacto(A.seguidores) : '–', A.seguidores ? (A.fonteSeg === 'informado' ? 'informado por você' : 'lido do perfil') : 'informe no campo acima'],
  ].map(([r, v, s]) => `<div class="kpi"><span>${r}</span><b>${v}</b><small>${s}</small></div>`).join('');

  // insights
  const ins = [];
  if (D.melhor !== null && D.melhor !== D.maisPosta) {
    const g = D.g[D.melhor].med / (D.g[D.maisPosta].med || 1) - 1;
    ins.push(`<b>Dia:</b> a conta concentra posts ${DIAS_NOS[D.maisPosta]}, mas os vídeos ${DIAS_NOS[D.melhor]} engajam ${variacao(g)}. Mudar o calendário é um ganho rápido.`);
  }
  if (A.faixas && A.faixas.melhor !== null && A.faixas.melhor !== A.faixas.maisPosta) {
    const F = A.faixas, g = F.g[F.melhor].med / (F.g[F.maisPosta].med || 1) - 1;
    ins.push(`<b>Horário:</b> a maioria dos vídeos sai ${FAIXAS[F.maisPosta].nome}, mas quem posta ${FAIXAS[F.melhor].nome} engaja ${variacao(g)}.`);
  }
  if (A.tend) ins.push(`<b>Tendência:</b> os últimos ${A.tend.k} vídeos têm mediana de ${compacto(A.tend.rec)} interações, contra ${compacto(A.tend.ant)} dos ${A.tend.k} anteriores (${variacao(A.tend.var)}). ${A.tend.var < -0.15 ? 'O engajamento está caindo.' : A.tend.var > 0.15 ? 'O engajamento está subindo.' : 'Estável.'}`);
  if (A.freqEng) ins.push(`<b>Volume x engajamento:</b> nas semanas com mais de ${nf(A.freqEng.corte)} posts, cada vídeo teve ${compacto(A.freqEng.cheias)} interações (mediana), contra ${compacto(A.freqEng.leves)} nas semanas mais leves. ${A.freqEng.cheias < A.freqEng.leves * 0.85 ? 'Postar mais está diluindo o engajamento.' : A.freqEng.cheias > A.freqEng.leves * 1.15 ? 'Postar mais está puxando o engajamento pra cima.' : 'Postar mais não mudou o engajamento por vídeo.'}`);
  if (A.maiorHiato && A.maiorHiato.d >= 5) ins.push(`<b>Pausa:</b> ficou ${A.maiorHiato.d} dias sem postar (${dataCurta(A.maiorHiato.de)} → ${dataCurta(A.maiorHiato.ate)}).`);
  if (A.L) { const c100 = A.C / A.L * 100; ins.push(`<b>Conversa:</b> ${nf(c100, 1)} comentários a cada 100 curtidas. ${c100 < 3 ? 'O público curte, mas pouco conversa: perguntas diretas e chamadas pra comentar ajudam.' : c100 > 8 ? 'Público que conversa bastante: bom sinal de comunidade.' : 'Nível de conversa moderado.'}`); }
  if (A.campeao && A.campeao.rel) ins.push(`<b>O que funciona:</b> o melhor vídeo (${dataCurta(A.campeao.data)}) teve ${nf(A.campeao.rel, 1)}× a mediana. ${A.fora.length > 1 ? `Outros ${A.fora.length - 1} também passaram de 2×: vale ver o que eles têm em comum.` : 'Vale entender o que ele fez de diferente.'}`);
  $('#insights').innerHTML = ins.map(t => `<li><span>${t}</span></li>`).join('') || '<li><span>Dados insuficientes pra conclusões.</span></li>';

  $('#c-dias').innerHTML = conclusaoGrupo(D, DIAS_NOS, 'publicado(s)');
  $('#c-horas').innerHTML = A.faixas ? conclusaoGrupo(A.faixas, FAIXAS.map(f => f.nome), 'publicado(s)') : `Só ${A.comHora} de ${A.n} vídeos trouxeram o horário — pouco pra comparar.`;
  $('#c-horas').hidden = !A.faixas && A.comHora === 0;

  $('#sub-eng').textContent = 'Cada ponto é um vídeo (curtidas + comentários) · ponto maior = 2× a mediana ou mais · linha clara = mediana dos últimos 5 vídeos';
  $('#stats-eng').innerHTML = [
    ['Vídeo típico', compacto(A.medInt), 'mediana de interações'],
    ['Tendência', A.tend ? variacao(A.tend.var) : '–', A.tend ? `últimos ${A.tend.k} vs ${A.tend.k} anteriores` : 'precisa de 8+ vídeos'],
    ['Fora da curva', `${A.fora.length} vídeo(s)`, '2× a mediana ou mais'],
    ['Comentários por 100 curtidas', A.L ? nf(A.C / A.L * 100, 1) : '–', 'nível de conversa'],
  ].map(([t, v, s]) => `<div><dt>${t}</dt><dd>${v}<small>${s}</small></dd></div>`).join('');

  $('#sub-semanas').textContent = `${A.semanas.length} semanas · linha tracejada = média do período`;
  $('#stats-freq').innerHTML = [
    ['Intervalo típico', A.intervalos.length ? `${nf(mediana(A.intervalos.map(i => i.d)), 1)} dia(s)` : '–', 'entre um post e outro'],
    ['Maior pausa', A.maiorHiato ? `${A.maiorHiato.d} dia(s)` : '–', A.maiorHiato ? `${dataCurta(A.maiorHiato.de)} → ${dataCurta(A.maiorHiato.ate)}` : ''],
    ['Semanas sem post', `${A.semanasVazias} de ${A.semanas.length}`, ''],
  ].map(([t, v, s]) => `<div><dt>${t}</dt><dd>${v}<small>${s}</small></dd></div>`).join('');

  $('#seg-num').innerHTML = por1000 !== null ? `${nf(por1000, por1000 < 10 ? 1 : 0)}<small>de cada 1.000 seguidores interagem com um vídeo típico</small>`
    : `–<small>informe os seguidores no campo acima</small>`;
  const melhorSeg = A.seguidores && A.campeao ? A.campeao.int / A.seguidores : null;
  $('#stats-seg').innerHTML = [
    ['Taxa de engajamento', A.seguidores ? pct(A.medInt / A.seguidores, 2) : '–', 'vídeo típico ÷ seguidores'],
    ['Melhor vídeo', melhorSeg !== null ? pct(melhorSeg, 2) : '–', 'da base interagiu'],
    ['Curtidas por vídeo', A.n ? compacto(A.L / A.n) : '–', 'média'],
    ['Comentários por vídeo', A.n ? compacto(A.C / A.n) : '–', 'média'],
  ].map(([t, v, s]) => `<div><dt>${t}</dt><dd>${v}<small>${s}</small></dd></div>`).join('');

  const top = A.vids.filter(v => v.int !== null).sort((a, b) => b.int - a.int).slice(0, 5);
  $('#top-eng').innerHTML = top.map(v => `<li><div style="min-width:0">${v.link ? `<a href="${esc(v.link)}" target="_blank" rel="noopener">${esc(v.titulo)}</a>` : `<span>${esc(v.titulo)}</span>`}
    <small>${dataLonga(v.data)}${v.hora !== null && v.hora !== undefined ? ` · ${v.hora}h` : ''} · ${DIAS_CURTO[(v.data.getDay() + 6) % 7]} · ${nf(v.int)} interações</small></div>
    <b class="vezes">${v.rel ? nf(v.rel, 1) + '×' : ''}</b></li>`).join('') || '<li class="vazio">Sem vídeos com curtidas.</li>';

  tabela(A); desenhar(A);
  const notas = [];
  if (EXEMPLO) notas.push('Números de exemplo: cole o link de um perfil e clique em Analisar.');
  notas.push('Interações = curtidas + comentários. Usamos a mediana (o vídeo do meio) para um viral não distorcer o resultado.');
  $('#nota-final').textContent = notas.join(' ');
}

function tabela(A) {
  const {k, dir} = ORDEM, f = dir === 'asc' ? 1 : -1;
  const rows = [...A.vids].sort((a, b) => { const x = a[k], y = b[k];
    if (x === null || x === undefined) return 1; if (y === null || y === undefined) return -1;
    return (typeof x === 'string' ? x.localeCompare(y) : x - y) * f; });
  const cel = v => v !== null && v !== undefined ? nf(v) : '<span class="vazio">–</span>';
  $('#linhas').innerHTML = rows.map(v => `<tr>
    <td class="mono" style="white-space:nowrap">${dataLonga(v.data)}<br><span class="hora">${DIAS_CURTO[(v.data.getDay() + 6) % 7]}${v.hora !== null && v.hora !== undefined ? ` · ${v.hora}h` : ''}</span></td>
    <td class="t"><span class="t-tit" title="${esc(v.titulo)}">${esc(v.titulo)}</span>${v.link
      ? `<a class="t-link" href="${esc(v.link)}" target="_blank" rel="noopener">${esc(v.link.replace(/^https?:\/\/(www\.)?/, ''))} ↗</a>`
      : `<span class="t-link vazio">sem link (vídeo de exemplo)</span>`}</td>
    <td class="n">${cel(v.likes)}</td><td class="n">${cel(v.coms)}</td><td class="n">${cel(v.int)}</td>
    <td class="n">${v.rel ? nf(v.rel, 1) + '×' : '–'}</td><td class="n">${v.pseg !== null ? pct(v.pseg, 2) : '–'}</td></tr>`).join('');
  document.querySelectorAll('th[data-k]').forEach(th => th.setAttribute('aria-sort', th.dataset.k === k ? (dir === 'asc' ? 'ascending' : 'descending') : 'none'));
}
document.querySelectorAll('th[data-k]').forEach(th => {
  const ir = () => { const k = th.dataset.k; ORDEM = ORDEM.k === k ? {k, dir: ORDEM.dir === 'asc' ? 'desc' : 'asc'} : {k, dir: k === 'titulo' ? 'asc' : 'desc'}; tabela(window.__A); };
  th.addEventListener('click', ir); th.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); ir(); } });
});
$('#perfil').addEventListener('input', () => { if (EXEMPLO) render(); });
$('#qtd').addEventListener('change', () => render());
$('#seg').addEventListener('input', () => render());
let tempo; new ResizeObserver(() => { clearTimeout(tempo); tempo = setTimeout(() => window.__A && desenhar(window.__A), 120); }).observe($('.wrap'));

// ---------- coleta: o servidor puxa os dados do perfil ----------
let ROBO = false, rodando = false;
if (window.fetch) fetch('api/ping').then(r => r.ok ? r.json() : null).then(j => { ROBO = !!(j && j.ok); }).catch(() => {});
function status(t, girando) { $('#status').innerHTML = (girando ? '<span class="spin"></span>' : '') + esc(t); }
function deJob(job) {
  const seg = job.perfil_info && job.perfil_info.seguidores;
  return (job.itens || []).map(it => ({
    id: it.id_video, link: it.link, titulo: it.titulo || it.legenda || 'Sem legenda',
    data: dataDe(it.data_publicacao), hora: it.hora_publicacao ?? null, likes: it.curtidas ?? null, coms: it.comentarios ?? null,
    conta: job.conta, seguidores: seg ?? null, coleta: (job.inicio || '').slice(0, 10),
  }));
}
async function analisarPerfil() {
  if (rodando) return;
  const perfil = normPerfil($('#perfil').value);
  if (!perfil) { erro('Cole o link do perfil, por exemplo https://www.kwai.com/@Lulaoficial'); return; }
  if (!ROBO) { $('#sem-robo').hidden = false; erro(''); return; }
  erro(''); rodando = true; $('#analisar').disabled = true; $('#barra').hidden = false; $('#progresso').style.width = '4%';
  status('Iniciando…', true);
  try {
    const r = await fetch('api/coletar', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({perfil: 'https://www.kwai.com/' + perfil, max: +$('#qtd').value || 40})});
    const j = await r.json();
    if (!r.ok) throw new Error(j.erro || 'o servidor recusou o pedido');
    for (;;) {
      await new Promise(ok => setTimeout(ok, 1200));
      const job = await (await fetch('api/job/' + j.job)).json();
      if (job.erro) throw new Error(job.erro);
      const itens = job.itens || [], feitos = itens.filter(i => i.estado !== 'pendente').length;
      $('#progresso').style.width = itens.length ? (15 + 85 * feitos / itens.length) + '%' : '10%';
      const conv = deJob(job);
      if (conv.some(v => v.data)) { TODOS = conv; EXEMPLO = false; render(true); }
      if (job.status !== 'rodando') {
        if (job.status === 'erro') { erro(job.mensagem); status(''); }
        else { status(job.mensagem); $('#progresso').style.width = '100%'; render(); }
        break;
      }
      status(job.mensagem, true);
    }
  } catch (e) {
    erro(e.message || 'Não consegui falar com o servidor. Tente de novo em instantes.'); status('');
  }
  rodando = false; $('#analisar').disabled = false;
}
$('#analisar').addEventListener('click', analisarPerfil);
$('#perfil').addEventListener('keydown', e => { if (e.key === 'Enter') analisarPerfil(); });
render();
})();
</script>

</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "7860")), threaded=True)
