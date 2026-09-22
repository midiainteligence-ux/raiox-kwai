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


def montar_driver():
    """Mesmo navegador do seu robô de links, que já funciona: modo CELULAR
    (janela 430x900 + navegador Android)."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    opcoes = Options()
    opcoes.add_argument("--headless=new")
    opcoes.add_argument("--no-sandbox")
    opcoes.add_argument("--disable-dev-shm-usage")
    opcoes.add_argument("--disable-gpu")
    opcoes.add_argument("--window-size=430,900")
    opcoes.add_argument(f"--user-data-dir=/tmp/chrome-perfil-{random.randint(1, 999999)}")
    opcoes.add_argument(
        "user-agent=Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
    )
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


def ler_grade_do_perfil(url_perfil, conta, max_videos, avisar):
    """Pega os IDs dos vídeos com a MESMA lógica do seu robô de links (links na
    tela + /video/ID e photoId no código-fonte da página), rolando em 6 passos.
    Em paralelo, tenta ler o número de views de cada card (▷ 2.8K); se o card
    não trouxer, a view vem depois da página do vídeo."""
    avisar("Abrindo o navegador…")
    driver = montar_driver()
    ids, vistos, views_card, titulos, info = [], set(), {}, {}, {}
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

            try:
                for c in driver.execute_script(JS_LER_CARDS, conta) or []:
                    if c.get("views_texto"):
                        views_card.setdefault(c["id"], c["views_texto"])
                    if c.get("titulo_grade"):
                        titulos.setdefault(c["id"], c["titulo_grade"])
            except Exception:
                pass

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

            altura = driver.execute_script("return window.innerHeight;") or 900
            for _ in range(6):
                driver.execute_script(f"window.scrollBy(0, {altura // 2});")
                driver.execute_script("window.dispatchEvent(new Event('scroll'));")
                time.sleep(0.5)
            time.sleep(3)

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

    cards = [{"id": v, "href": "", "views_texto": views_card.get(v, ""),
              "titulo_grade": titulos.get(v, ""), "miniatura": ""} for v in ids[:max_videos]]
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
    candidatos = []
    p_hora = re.compile(r'\btime"?\s*:\s*"(\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}:\d{2}"')
    p_ts = re.compile(r'\btimestamp"?\s*:\s*(\d{10,13})\b')
    for m_id in re.finditer(re.escape(video_id), html):
        ini = max(0, m_id.start() - 800)
        janela = html[ini:m_id.end() + 800]
        pos = m_id.start() - ini
        for m in p_hora.finditer(janela):
            candidatos.append((abs(m.start() - pos), m.group(1)))
        for m in p_ts.finditer(janela):
            d = _ts_para_data(m.group(1))
            if d:
                candidatos.append((abs(m.start() - pos), d))
    if candidatos:
        return min(candidatos)[1], "html"
    url = _url_video(html)
    if url:
        m = re.search(r"/(\d{4})/(\d{2})/(\d{2})/\d{2}/", url)
        if m:
            return "-".join(m.groups()), "cdn"
    return "", ""


def metricas_da_pagina(link, video_id):
    r = requests.get(link, headers=HEADERS, timeout=25)
    html = r.text
    out = {"curtidas": None, "comentarios": None, "compartilhamentos": None,
           "views_pagina": None, "legenda": "", "data_publicacao": "", "fonte_data": ""}

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

    out["data_publicacao"], out["fonte_data"] = _data_publicacao(html, video_id)
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
                      "data_publicacao": "", "legenda": "", "estado": "pendente"})
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
                    for k in ("curtidas", "comentarios", "compartilhamentos", "data_publicacao", "legenda"):
                        item[k] = m[k]
                    if item["visualizacoes"] is None and m["views_pagina"] is not None:
                        item["visualizacoes"], item["fonte_views"] = m["views_pagina"], "página"
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
  .passos { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }
  .passo { background: var(--sunken); border-radius: 10px; padding: 12px 14px; font-size: 14px; color: var(--ink2); }
  .passo b { display: block; color: var(--ink); font-weight: 600; margin-bottom: 2px; }
  pre.cod { margin: 8px 0 0; background: var(--bg); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px;
            font: 12.5px/1.5 var(--mono); color: var(--ink); overflow-x: auto; white-space: pre; }
  .cod-barra { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-top: 8px; }
  [hidden] { display: none !important; }
  body { margin: 0; }
  .acao { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .status-txt { font-size: 14px; color: var(--ink2); display: inline-flex; align-items: center; gap: 8px; }
  .spin { width: 14px; height: 14px; border: 2px solid var(--line); border-top-color: var(--accent); border-radius: 50%; animation: gira .8s linear infinite; }
  @keyframes gira { to { transform: rotate(360deg); } }
  @media (prefers-reduced-motion: reduce) { .spin { animation-duration: 3s; } }
  .barra { height: 4px; border-radius: 4px; background: var(--grid); overflow: hidden; }
  .barra i { display: block; height: 100%; width: 0; background: var(--accent); transition: width .3s; }
  .csv-opc summary { cursor: pointer; font-size: 13.5px; color: var(--muted); }
  .csv-opc .zona { margin-top: 10px; }
  .zona { border: 1.5px dashed var(--line); border-radius: 10px; padding: 12px 14px; display: flex; align-items: center;
          gap: 12px; flex-wrap: wrap; color: var(--ink2); font-size: 14px; }
  .zona.arrastando { border-color: var(--accent); background: var(--accent-soft); }
  .zona input[type=file] { display: none; }
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

  .taxa { display: grid; grid-template-columns: auto 1fr; gap: 18px; align-items: center; }
  @media (max-width: 520px) { .taxa { grid-template-columns: 1fr; } }
  .taxa-num { font: 600 44px/1 var(--display); letter-spacing: -0.03em; color: var(--accent); }
  .taxa-num small { display: block; font: 13px/1.4 var(--body); color: var(--muted); letter-spacing: 0; margin-top: 6px; }
  .pessoas { display: grid; grid-template-columns: repeat(20, 1fr); gap: 3px; max-width: 360px; }
  .pessoas i { aspect-ratio: 1; border-radius: 50%; background: var(--grid); display: block; }
  .pessoas i.on { background: var(--accent); }
  .pessoas i.com { background: var(--hot); }
  .legenda { display: flex; gap: 14px; flex-wrap: wrap; font-size: 12.5px; color: var(--ink2); }
  .legenda span::before { content: ""; display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 6px; background: var(--c); vertical-align: 0; }

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
  td.t { max-width: 360px; }
  td.t a { color: var(--ink); text-decoration: none; display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  td.t a:hover { text-decoration: underline; }
  .vazio { color: var(--muted); }

  #dica { position: absolute; pointer-events: none; background: var(--ink); color: var(--bg); font-size: 12.5px; line-height: 1.4;
          padding: 7px 9px; border-radius: 8px; max-width: 260px; z-index: 5; transform: translate(-50%, calc(-100% - 10px)); }
  #dica b { font-family: var(--mono); font-weight: 500; }
  .nota { font-size: 12.5px; color: var(--muted); margin: 0; max-width: 80ch; }
  @media (prefers-reduced-motion: no-preference) { .kpi b, .laudo h1 { transition: color .2s; } }
</style>

<div class="wrap">
  <header class="top">
    <div class="marca"><i aria-hidden="true"></i>Raio-X Kwai</div>
    <small>Cole o link de qualquer perfil do Kwai e veja como a conta posta e engaja</small>
  </header>

  <section class="entrada" aria-label="Carregar uma conta">
    <div class="linha-form">
      <input type="text" id="perfil" placeholder="Cole o link do perfil, ex.: https://www.kwai.com/@augustocuryoficial" autocomplete="off">
      <select id="qtd" aria-label="Quantos vídeos analisar">
        <option value="20">Últimos 20</option>
        <option value="40" selected>Últimos 40</option>
        <option value="80">Últimos 80</option>
        
      </select>
      <input type="number" id="seg" min="0" placeholder="Seguidores" aria-label="Seguidores (opcional)">
    </div>
    <div class="acao">
      <button class="btn pri" type="button" id="analisar">Analisar perfil</button>
      <span class="status-txt" id="status"></span>
    </div>
    <div class="barra" id="barra" hidden><i id="progresso"></i></div>
    <div class="aviso" id="sem-robo" hidden>
      <b>O servidor de coleta não respondeu.</b> Atualize a página em alguns segundos e tente de novo.
    </div>
    <details class="csv-opc">
      <summary>Já tem um CSV da pasta Basedadosconteudo? Carregar arquivo</summary>
      <label class="zona" id="zona">
        <input type="file" id="arquivo" accept=".csv,text/csv">
        <span class="btn">Escolher CSV</span>
        <span id="zona-txt">ou arraste o arquivo para cá</span>
      </label>
    </details>
    <div id="msg" class="erro" hidden></div>
  </section>

  <section class="laudo" aria-live="polite">
    <div class="eyebrow"><span id="lb-conta">@conta</span><span id="lb-exemplo" class="tag">Dados de exemplo</span><span id="lb-coleta"></span></div>
    <h1 id="manchete"></h1>
    <p class="resumo" id="resumo"></p>
    <div class="chips" id="chips"></div>
  </section>

  <div class="kpis" id="kpis"></div>

  <div class="grade2">
    <section class="painel">
      <h2>Publicações por semana</h2>
      <p class="sub" id="sub-semanas"></p>
      <div class="grafico" id="g-semanas"></div>
      <dl class="stats" id="stats-freq"></dl>
    </section>
    <section class="painel">
      <h2>Em que dia a conta posta</h2>
      <p class="sub">Quantos dos vídeos analisados saíram em cada dia da semana</p>
      <div class="grafico" id="g-dias"></div>
    </section>
  </div>

  <section class="painel">
    <h2>Visualizações de cada vídeo ao longo do tempo</h2>
    <p class="sub" id="sub-views"></p>
    <div class="grafico" id="g-views"></div>
  </section>

  <div class="grade2">
    <section class="painel">
      <h2>Chance de interagir por visualização</h2>
      <p class="sub">De cada 100 pessoas que assistem, quantas curtem ou comentam</p>
      <div class="taxa">
        <div class="taxa-num" id="taxa-num"></div>
        <div style="display:grid;gap:8px">
          <div class="pessoas" id="pessoas" aria-hidden="true"></div>
          <div class="legenda">
            <span style="--c:var(--accent)">curtiu</span>
            <span style="--c:var(--hot)">comentou</span>
            <span style="--c:var(--grid)">só assistiu</span>
          </div>
        </div>
      </div>
      <dl class="stats" id="stats-eng"></dl>
    </section>
    <section class="painel">
      <h2>Vídeos que mais engajaram</h2>
      <p class="sub">Maior taxa de interação por view (vídeos com 100+ views)</p>
      <ol class="top5" id="top-eng"></ol>
    </section>
  </div>

  <section class="painel" style="padding:0;border:0;background:none">
    <h2>Todos os vídeos analisados</h2>
    <div class="tabela">
      <table>
        <thead><tr>
          <th data-k="data" tabindex="0">Publicado</th>
          <th data-k="titulo" tabindex="0">Vídeo</th>
          <th data-k="views" class="n" tabindex="0">Views</th>
          <th data-k="likes" class="n" tabindex="0">Curtidas</th>
          <th data-k="coms" class="n" tabindex="0">Coment.</th>
          <th data-k="taxa" class="n" tabindex="0">Interação/view</th>
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
const DIAS_LONGO = ['segunda', 'terça', 'quarta', 'quinta', 'sexta', 'sábado', 'domingo'];
const MESES = ['jan', 'fev', 'mar', 'abr', 'mai', 'jun', 'jul', 'ago', 'set', 'out', 'nov', 'dez'];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// ---------- formatação pt-BR ----------
const nf = (n, d = 0) => Number(n).toLocaleString('pt-BR', {maximumFractionDigits: d, minimumFractionDigits: 0});
function compacto(n) {
  if (n === null || n === undefined || !isFinite(n)) return '–';
  const a = Math.abs(n);
  if (a >= 1e6) return nf(n / 1e6, a >= 1e7 ? 0 : 1) + ' mi';
  if (a >= 1e4) return nf(n / 1e3, 0) + ' mil';
  if (a >= 1e3) return nf(n / 1e3, 1) + ' mil';
  return nf(n, 0);
}
const pct = (x, d = 1) => (x === null || !isFinite(x)) ? '–' : nf(x * 100, d) + '%';
const dataCurta = d => `${d.getDate()} ${MESES[d.getMonth()]}`;
const dataLonga = d => `${d.getDate()} ${MESES[d.getMonth()]} ${d.getFullYear()}`;
const iso = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;

// ---------- leitura de CSV (aspas, quebras de linha, ; ou ,) ----------
function lerCSV(texto) {
  texto = texto.replace(/^﻿/, '');
  const primeira = texto.slice(0, texto.indexOf('\n') > 0 ? texto.indexOf('\n') : texto.length);
  const sep = (primeira.split(';').length > primeira.split(',').length) ? ';' : ',';
  const linhas = []; let campo = '', linha = [], aspas = false;
  for (let i = 0; i < texto.length; i++) {
    const c = texto[i];
    if (aspas) {
      if (c === '"') { if (texto[i + 1] === '"') { campo += '"'; i++; } else aspas = false; }
      else campo += c;
    } else if (c === '"') aspas = true;
    else if (c === sep) { linha.push(campo); campo = ''; }
    else if (c === '\n' || c === '\r') {
      if (c === '\r' && texto[i + 1] === '\n') i++;
      linha.push(campo); campo = '';
      if (linha.some(x => x !== '')) linhas.push(linha);
      linha = [];
    } else campo += c;
  }
  linha.push(campo); if (linha.some(x => x !== '')) linhas.push(linha);
  const cab = (linhas.shift() || []).map(h => h.trim().toLowerCase());
  return linhas.map(l => Object.fromEntries(cab.map((h, i) => [h, (l[i] ?? '').trim()])));
}

function numero(v) {
  if (v === undefined || v === null) return null;
  let s = String(v).trim().toLowerCase().replace(/\s/g, '');
  if (!s) return null;
  const m = s.match(/^(\d+(?:[.,]\d+)*)(k|m|b|mil|mi|bi)?$/);
  if (!m) return null;
  if (m[2]) return Math.round(parseFloat(m[1].replace(',', '.')) * ({k: 1e3, mil: 1e3, m: 1e6, mi: 1e6, b: 1e9, bi: 1e9}[m[2]]));
  if (/^\d{1,3}(\.\d{3})+$/.test(m[1])) return parseInt(m[1].replace(/\./g, ''), 10);
  return Math.round(parseFloat(m[1].replace(',', '.')));
}
function dataDe(v) {
  const m = String(v || '').match(/(\d{4})-(\d{2})-(\d{2})/);
  return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null;
}
const pega = (r, ...nomes) => { for (const n of nomes) if (r[n] !== undefined && r[n] !== '') return r[n]; return ''; };

function normalizar(registros) {
  return registros.map(r => ({
    id: pega(r, 'id_video', 'id'),
    link: pega(r, 'link', 'link_video'),
    titulo: pega(r, 'titulo', 'titulo_legenda', 'legenda', 'titulo_grade') || 'Sem legenda',
    data: dataDe(pega(r, 'data_publicacao', 'data')),
    views: numero(pega(r, 'visualizacoes', 'views', 'view_count')),
    likes: numero(pega(r, 'curtidas', 'likes')),
    coms: numero(pega(r, 'comentarios', 'comentários', 'comments')),
    shares: numero(pega(r, 'compartilhamentos', 'shares')),
    conta: pega(r, 'conta', 'rotulo_perfil').replace(/^@/, ''),
    seguidores: numero(pega(r, 'seguidores', 'followers')),
    coleta: pega(r, 'data_coleta'),
  }));
}

// ---------- exemplo (claramente marcado) ----------
function exemplo() {
  let s = 7; const rnd = () => (s = (s * 16807) % 2147483647) / 2147483647;
  const hoje = new Date(); hoje.setHours(0, 0, 0, 0);
  const out = []; let d = new Date(hoje);
  for (let i = 0; i < 40; i++) {
    const views = Math.round(Math.exp(7.2 + rnd() * 2.6));
    const likes = Math.round(views * (0.015 + rnd() * 0.05));
    out.push({id: String(5200000000000000000 + i), link: '', titulo: `Vídeo de exemplo ${40 - i}`,
      data: new Date(d), views, likes, coms: Math.round(likes * (0.03 + rnd() * 0.08)), shares: null,
      conta: 'contaexemplo', seguidores: 48200, coleta: ''});
    d = new Date(d.getTime() - Math.floor(rnd() * rnd() * 5) * DIA);
  }
  return out;
}

// ---------- análise ----------
const mediana = a => { if (!a.length) return null; const b = [...a].sort((x, y) => x - y); const m = b.length >> 1; return b.length % 2 ? b[m] : (b[m - 1] + b[m]) / 2; };
const segunda = d => { const x = new Date(d); x.setDate(x.getDate() - ((x.getDay() + 6) % 7)); x.setHours(0, 0, 0, 0); return x; };

function analisar(todos, qtd, seguidoresManual) {
  const comData = todos.filter(v => v.data).sort((a, b) => b.data - a.data);
  const semData = todos.length - comData.length;
  const vids = (qtd > 0 ? comData.slice(0, qtd) : comData).reverse();   // do mais antigo ao mais recente
  if (!vids.length) return null;
  const n = vids.length, antigo = vids[0].data, recente = vids[n - 1].data;
  const dias = Math.round((recente - antigo) / DIA) + 1;
  const porSemana = n / (dias / 7);

  const semanas = [];
  for (let w = segunda(antigo); w <= recente; w = new Date(w.getTime() + 7 * DIA)) semanas.push({ini: w, n: 0});
  vids.forEach(v => { const k = Math.round((segunda(v.data) - semanas[0].ini) / (7 * DIA)); if (semanas[k]) semanas[k].n++; });
  const semanasVazias = semanas.filter(s => s.n === 0).length;

  const intervalos = [];
  for (let i = 1; i < n; i++) intervalos.push({d: Math.round((vids[i].data - vids[i - 1].data) / DIA), de: vids[i - 1].data, ate: vids[i].data});
  const maiorHiato = intervalos.reduce((a, b) => (b.d > (a ? a.d : -1) ? b : a), null);
  const diasComPost = new Set(vids.map(v => iso(v.data))).size;

  const porDia = [0, 0, 0, 0, 0, 0, 0];
  vids.forEach(v => porDia[(v.data.getDay() + 6) % 7]++);

  const comViews = vids.filter(v => v.views !== null);
  const views = comViews.map(v => v.views);
  const totalViews = views.reduce((a, b) => a + b, 0);
  const campeao = comViews.reduce((a, b) => (!a || b.views > a.views ? b : a), null);

  const eng = comViews.filter(v => v.views > 0 && v.likes !== null);
  const V = eng.reduce((a, v) => a + v.views, 0);
  const L = eng.reduce((a, v) => a + v.likes, 0);
  const C = eng.reduce((a, v) => a + (v.coms || 0), 0);
  vids.forEach(v => { v.taxa = (v.views > 0 && v.likes !== null) ? (v.likes + (v.coms || 0)) / v.views : null; });
  const taxas = vids.map(v => v.taxa).filter(t => t !== null);

  const segArquivo = Math.max(0, ...todos.map(v => v.seguidores || 0)) || null;
  const seguidores = seguidoresManual || segArquivo;
  const contas = [...new Set(todos.map(v => v.conta).filter(Boolean))];

  return {
    vids, n, antigo, recente, dias, porSemana, semanas, semanasVazias, intervalos, maiorHiato, diasComPost, porDia,
    comViews: comViews.length, totalViews, mediaViews: views.length ? totalViews / views.length : null,
    medianaViews: mediana(views), campeao, V, L, C,
    taxa: V ? (L + C) / V : null, taxaCurtida: V ? L / V : null, taxaComent: V ? C / V : null,
    medianaTaxa: mediana(taxas), nEng: eng.length,
    seguidores, fonteSeg: seguidoresManual ? 'informado' : (segArquivo ? 'arquivo' : null),
    conta: contas[0] || '', semData, totalArquivo: todos.length,
    coleta: todos.map(v => v.coleta).find(Boolean) || '',
  };
}

function ritmo(ps) {
  if (ps >= 7) return 'posta todo dia ou mais';
  if (ps >= 3) return 'posta com alta frequência';
  if (ps >= 1) return 'posta com regularidade semanal';
  return 'posta de forma esporádica';
}

// ---------- gráficos (SVG desenhado à mão) ----------
const dica = $('#dica');
function mostrarDica(ev, html) {
  const alvo = ev.currentTarget.getBoundingClientRect();
  dica.innerHTML = html; dica.hidden = false;
  dica.style.left = (alvo.left + alvo.width / 2 + scrollX) + 'px';
  dica.style.top = (alvo.top + scrollY) + 'px';
}
function ligarDicas(el) {
  el.querySelectorAll('[data-dica]').forEach(m => {
    m.addEventListener('mouseenter', e => mostrarDica(e, m.dataset.dica));
    m.addEventListener('focus', e => mostrarDica(e, m.dataset.dica));
    m.addEventListener('mouseleave', () => dica.hidden = true);
    m.addEventListener('blur', () => dica.hidden = true);
  });
}
function escalaBonita(max, passos = 4) {
  if (!max || max <= 0) return {max: 1, ticks: [0, 1]};
  const bruto = max / passos, mag = Math.pow(10, Math.floor(Math.log10(bruto)));
  const passo = [1, 2, 2.5, 5, 10].map(f => f * mag).find(p => p >= bruto);
  const topo = Math.ceil(max / passo) * passo;
  const ticks = []; for (let t = 0; t <= topo + 1e-9; t += passo) ticks.push(t);
  return {max: topo, ticks};
}
const larg = el => Math.max(280, Math.floor(el.clientWidth || el.parentElement.clientWidth || 600));

function graficoSemanas(el, A) {
  const W = larg(el), H = 220, m = {t: 14, r: 12, b: 28, l: 30};
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const esc_ = escalaBonita(Math.max(...A.semanas.map(s => s.n), Math.ceil(A.porSemana)));
  const y = v => m.t + ih - (v / esc_.max) * ih;
  const bw = iw / A.semanas.length, gap = Math.min(4, bw * 0.25);
  const cada = Math.ceil(A.semanas.length / Math.max(2, Math.floor(iw / 58)));
  let s = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Publicações por semana">`;
  esc_.ticks.forEach(t => { s += `<line x1="${m.l}" x2="${W - m.r}" y1="${y(t)}" y2="${y(t)}" stroke="var(--grid)" stroke-width="1"/>
    <text x="${m.l - 8}" y="${y(t) + 4}" text-anchor="end" font-size="11" fill="var(--muted)" font-family="var(--mono)">${t}</text>`; });
  A.semanas.forEach((w, i) => {
    const x = m.l + i * bw + gap / 2, h = ih - (y(w.n) - m.t);
    const fim = new Date(w.ini.getTime() + 6 * DIA);
    const d = `Semana de ${dataCurta(w.ini)} a ${dataCurta(fim)}<br><b>${w.n}</b> ${w.n === 1 ? 'vídeo' : 'vídeos'}`;
    if (w.n > 0) s += `<path d="M${x},${m.t + ih} V${y(w.n) + 3} q0,-3 3,-3 h${bw - gap - 6} q3,0 3,3 V${m.t + ih} Z" fill="var(--accent)"/>`;
    else s += `<rect x="${x}" y="${m.t + ih - 2}" width="${bw - gap}" height="2" fill="var(--line)"/>`;
    s += `<rect x="${m.l + i * bw}" y="${m.t}" width="${bw}" height="${ih}" fill="transparent" tabindex="0" data-dica="${esc(d)}"/>`;
    if (i % cada === 0) s += `<text x="${x + (bw - gap) / 2}" y="${H - 8}" text-anchor="middle" font-size="11" fill="var(--muted)">${dataCurta(w.ini)}</text>`;
  });
  const ym = y(A.porSemana);
  s += `<line x1="${m.l}" x2="${W - m.r}" y1="${ym}" y2="${ym}" stroke="var(--hot)" stroke-width="1.5" stroke-dasharray="4 4"/>
    <text x="${W - m.r}" y="${ym - 6}" text-anchor="end" font-size="11.5" fill="var(--ink)" font-weight="600">média ${nf(A.porSemana, 1)}/semana</text>`;
  el.innerHTML = s + '</svg>'; ligarDicas(el);
}

function graficoDias(el, A) {
  const W = larg(el), linha = 26, H = 7 * linha + 8, l = 40, r = 36;
  const max = Math.max(...A.porDia, 1), iw = W - l - r;
  const topo = A.porDia.indexOf(max);
  let s = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Vídeos por dia da semana">`;
  A.porDia.forEach((c, i) => {
    const yy = 4 + i * linha, w = (c / max) * iw;
    s += `<text x="${l - 8}" y="${yy + 15}" text-anchor="end" font-size="12" fill="var(--ink2)">${DIAS_CURTO[i]}</text>
      <rect x="${l}" y="${yy + 4}" width="${iw}" height="14" rx="4" fill="var(--sunken)"/>`;
    if (c) s += `<rect x="${l}" y="${yy + 4}" width="${Math.max(w, 6)}" height="14" rx="4" fill="${i === topo ? 'var(--hot)' : 'var(--accent)'}"/>`;
    s += `<text x="${l + Math.max(w, 6) + 6}" y="${yy + 15}" font-size="12" fill="var(--ink)" font-family="var(--mono)">${c}</text>`;
  });
  el.innerHTML = s + '</svg>';
}

function graficoViews(el, A) {
  const W = larg(el), H = 260, m = {t: 16, r: 14, b: 28, l: 52};
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const pts = A.vids.filter(v => v.views !== null);
  if (!pts.length) { el.innerHTML = '<p class="vazio">Este arquivo não traz visualizações.</p>'; return; }
  const esc_ = escalaBonita(Math.max(...pts.map(v => v.views)));
  const t0 = A.antigo.getTime(), t1 = Math.max(A.recente.getTime(), t0 + DIA);
  const x = d => m.l + ((d.getTime() - t0) / (t1 - t0)) * iw;
  const y = v => m.t + ih - (v / esc_.max) * ih;
  let s = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Visualizações por vídeo ao longo do tempo">`;
  esc_.ticks.forEach(t => { s += `<line x1="${m.l}" x2="${W - m.r}" y1="${y(t)}" y2="${y(t)}" stroke="var(--grid)"/>
    <text x="${m.l - 8}" y="${y(t) + 4}" text-anchor="end" font-size="11" fill="var(--muted)" font-family="var(--mono)">${compacto(t)}</text>`; });
  const nMarcas = Math.max(2, Math.floor(iw / 90));
  for (let i = 0; i <= nMarcas; i++) { const d = new Date(t0 + (t1 - t0) * i / nMarcas);
    s += `<text x="${x(d)}" y="${H - 8}" text-anchor="${i === 0 ? 'start' : i === nMarcas ? 'end' : 'middle'}" font-size="11" fill="var(--muted)">${dataCurta(d)}</text>`; }
  // linha de tendência: média móvel dos últimos 5 vídeos
  const mm = pts.map((v, i) => { const j = pts.slice(Math.max(0, i - 4), i + 1); return [x(v.data), y(j.reduce((a, b) => a + b.views, 0) / j.length)]; });
  if (mm.length > 2) s += `<path d="${mm.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ')}" fill="none" stroke="var(--accent)" stroke-width="2" stroke-opacity=".45" stroke-linejoin="round"/>`;
  const ymed = y(A.medianaViews);
  s += `<line x1="${m.l}" x2="${W - m.r}" y1="${ymed}" y2="${ymed}" stroke="var(--hot)" stroke-width="1.5" stroke-dasharray="4 4"/>
    <text x="${m.l + 6}" y="${ymed - 6}" font-size="11.5" fill="var(--ink)" font-weight="600">mediana ${compacto(A.medianaViews)}</text>`;
  pts.forEach(v => {
    const top = A.campeao && v.id === A.campeao.id;
    const d = `${esc(v.titulo.slice(0, 80))}<br>${dataLonga(v.data)} · <b>${nf(v.views)}</b> views` + (v.taxa !== null ? ` · ${pct(v.taxa)} interação` : '');
    s += `<circle cx="${x(v.data)}" cy="${y(v.views)}" r="${top ? 6 : 4.5}" fill="${top ? 'var(--hot)' : 'var(--accent)'}" stroke="var(--surface)" stroke-width="2"/>
      <circle cx="${x(v.data)}" cy="${y(v.views)}" r="11" fill="transparent" tabindex="0" data-dica="${esc(d)}"/>`;
  });
  el.innerHTML = s + '</svg>'; ligarDicas(el);
}

// ---------- render ----------
let TODOS = exemplo(), EXEMPLO = true, ORDEM = {k: 'data', dir: 'desc'};

function render(aoVivo) {
  const qtd = +$('#qtd').value, segManual = numero($('#seg').value);
  const A = analisar(TODOS, qtd, segManual);
  if (!A) { if (aoVivo) return; erro('Nenhum vídeo desse arquivo tem data de publicação — sem data não dá pra medir frequência.'); return; }
  window.__A = A;
  const conta = A.conta || (normPerfil($('#perfil').value) || '').replace(/^@/, '') || 'conta';
  $('#lb-conta').textContent = '@' + conta;
  $('#lb-exemplo').hidden = !EXEMPLO;
  $('#lb-coleta').textContent = A.coleta ? `coletado em ${dataLonga(dataDe(A.coleta))}` : '';

  const umEm = A.taxa ? Math.round(1 / A.taxa) : null;
  $('#manchete').innerHTML = `${A.n} vídeos em ${A.dias} dias: <em>${nf(A.porSemana, 1)} por semana</em>` +
    (umEm ? `, e 1 em cada ${nf(umEm)} views vira curtida ou comentário.` : '.');
  const segTxt = A.seguidores ? ` Com ${compacto(A.seguidores)} seguidores, cada vídeo alcança em média o equivalente a ${pct(A.mediaViews / A.seguidores, A.mediaViews / A.seguidores < 0.1 ? 1 : 0)} da base.` : '';
  $('#resumo').textContent = `Do mais antigo (${dataLonga(A.antigo)}) ao mais recente (${dataLonga(A.recente)}), a conta ${ritmo(A.porSemana)}. ` +
    (A.mediaViews !== null ? `Cada vídeo teve em média ${compacto(A.mediaViews)} views (mediana ${compacto(A.medianaViews)}).` : '') + segTxt;

  const topoDia = A.porDia.indexOf(Math.max(...A.porDia));
  $('#chips').innerHTML = [
    `Ritmo: <b>${ritmo(A.porSemana).replace('posta ', '')}</b>`,
    `Dia preferido: <b>${DIAS_LONGO[topoDia]}</b>`,
    `Semanas sem post: <b>${A.semanasVazias} de ${A.semanas.length}</b>`,
    A.campeao ? `Maior alcance: <b>${compacto(A.campeao.views)}</b> em ${dataCurta(A.campeao.data)}` : '',
  ].filter(Boolean).map(c => `<span class="chip">${c}</span>`).join('');

  $('#kpis').innerHTML = [
    ['Período analisado', `${dataCurta(A.antigo)} → ${dataCurta(A.recente)}`, `${A.dias} dias · ${A.n} vídeos`],
    ['Publicações por semana', nf(A.porSemana, 1), `${nf(A.porSemana / 7 * 30, 0)} por mês, em média`],
    ['Views por vídeo', A.mediaViews !== null ? compacto(A.mediaViews) : '–', A.comViews ? `${compacto(A.totalViews)} no total` : 'sem views no arquivo'],
    ['Interação por view', pct(A.taxa, 2), A.taxa ? `curtidas + comentários ÷ views` : 'sem curtidas no arquivo'],
    ['Seguidores', A.seguidores ? compacto(A.seguidores) : '–', A.seguidores ? (A.fonteSeg === 'informado' ? 'informado por você' : 'lido do perfil') : 'informe no campo acima'],
  ].map(([r, v, s]) => `<div class="kpi"><span>${r}</span><b>${v}</b><small>${s}</small></div>`).join('');

  $('#sub-semanas').textContent = `${A.semanas.length} semanas, de ${dataCurta(A.semanas[0].ini)} em diante · linha tracejada = média do período`;
  const hiato = A.maiorHiato;
  $('#stats-freq').innerHTML = [
    ['Intervalo típico entre posts', A.intervalos.length ? `${nf(mediana(A.intervalos.map(i => i.d)), 1)} dia(s)` : '–', 'mediana'],
    ['Maior pausa', hiato ? `${hiato.d} dia(s)` : '–', hiato ? `${dataCurta(hiato.de)} → ${dataCurta(hiato.ate)}` : ''],
    ['Dias com post', `${A.diasComPost} de ${A.dias}`, `${nf(A.n / A.diasComPost, 1)} vídeo(s) por dia postado`],
  ].map(([t, v, s]) => `<div><dt>${t}</dt><dd>${v}<small>${s}</small></dd></div>`).join('');

  $('#sub-views').textContent = `Cada ponto é um vídeo · ponto maior e mais escuro = maior alcance · linha clara = média dos últimos 5 vídeos`;

  // taxa
  const por100 = A.taxa !== null ? A.taxa * 100 : null;
  $('#taxa-num').innerHTML = por100 !== null ? `${nf(por100, por100 < 10 ? 1 : 0)}<small>de cada 100 views<br>viram interação</small>` : '–<small>sem curtidas no arquivo</small>';
  const curt = A.taxaCurtida !== null ? Math.round(A.taxaCurtida * 100) : 0;
  const com = A.taxaComent !== null ? Math.max(A.taxaComent > 0 ? 1 : 0, Math.round(A.taxaComent * 100)) : 0;
  $('#pessoas').innerHTML = Array.from({length: 100}, (_, i) => `<i class="${i < com ? 'com' : i < com + curt ? 'on' : ''}"></i>`).join('');
  $('#stats-eng').innerHTML = [
    ['Chance de curtir', pct(A.taxaCurtida, 2), A.taxaCurtida ? `1 a cada ${nf(1 / A.taxaCurtida)} views` : ''],
    ['Chance de comentar', pct(A.taxaComent, 2), A.taxaComent ? `1 a cada ${nf(1 / A.taxaComent)} views` : ''],
    ['Vídeo típico', pct(A.medianaTaxa, 2), 'mediana da taxa por vídeo'],
    ['Curtidas por seguidor', A.seguidores && A.n ? nf(A.L / A.n / A.seguidores * 100, 2) + '%' : '–', A.seguidores ? 'média por vídeo ÷ seguidores' : 'precisa dos seguidores'],
  ].map(([t, v, s]) => `<div><dt>${t}</dt><dd>${v}<small>${s}</small></dd></div>`).join('');

  const top = A.vids.filter(v => v.taxa !== null && v.views >= 100).sort((a, b) => b.taxa - a.taxa).slice(0, 5);
  $('#top-eng').innerHTML = top.length ? top.map(v => `<li><div style="min-width:0">${v.link ? `<a href="${esc(v.link)}" target="_blank" rel="noopener">${esc(v.titulo)}</a>` : `<span>${esc(v.titulo)}</span>`}<small>${dataLonga(v.data)} · ${compacto(v.views)} views</small></div><b class="mono">${pct(v.taxa)}</b></li>`).join('')
    : '<li class="vazio">Sem vídeos com views e curtidas suficientes.</li>';

  tabela(A);
  graficoSemanas($('#g-semanas'), A); graficoDias($('#g-dias'), A); graficoViews($('#g-views'), A);

  const notas = [];
  if (EXEMPLO) notas.push('Números de exemplo: cole o link de um perfil e clique em Analisar.');
  if (A.semData) notas.push(`${A.semData} vídeo(s) do arquivo ficaram de fora por não terem data de publicação.`);
  if (A.comViews < A.n) notas.push(`${A.n - A.comViews} vídeo(s) sem visualizações — entram na frequência, mas não nas médias de views.`);
  if (A.dias < 14) notas.push('Período curto (menos de 2 semanas): a média semanal oscila muito.');
  notas.push('Interação por view = (curtidas + comentários) ÷ views somados dos vídeos que têm os três números.');
  $('#nota-final').textContent = notas.join(' ');
}

function tabela(A) {
  const {k, dir} = ORDEM, f = dir === 'asc' ? 1 : -1;
  const rows = [...A.vids].sort((a, b) => {
    const x = a[k], y = b[k];
    if (x === null || x === undefined) return 1; if (y === null || y === undefined) return -1;
    return (typeof x === 'string' ? x.localeCompare(y) : x - y) * f;
  });
  $('#linhas').innerHTML = rows.map(v => `<tr>
    <td class="mono" style="white-space:nowrap">${dataLonga(v.data)}</td>
    <td class="t">${v.link ? `<a href="${esc(v.link)}" target="_blank" rel="noopener" title="${esc(v.titulo)}">${esc(v.titulo)}</a>` : esc(v.titulo)}</td>
    <td class="n">${v.views !== null ? nf(v.views) : '<span class="vazio">–</span>'}</td>
    <td class="n">${v.likes !== null ? nf(v.likes) : '<span class="vazio">–</span>'}</td>
    <td class="n">${v.coms !== null ? nf(v.coms) : '<span class="vazio">–</span>'}</td>
    <td class="n">${pct(v.taxa, 2)}</td></tr>`).join('');
  document.querySelectorAll('th[data-k]').forEach(th => th.setAttribute('aria-sort', th.dataset.k === k ? (dir === 'asc' ? 'ascending' : 'descending') : 'none'));
}
document.querySelectorAll('th[data-k]').forEach(th => {
  const ir = () => { const k = th.dataset.k; ORDEM = ORDEM.k === k ? {k, dir: ORDEM.dir === 'asc' ? 'desc' : 'asc'} : {k, dir: k === 'titulo' ? 'asc' : 'desc'}; tabela(window.__A); };
  th.addEventListener('click', ir); th.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); ir(); } });
});

// ---------- entrada ----------
function normPerfil(t) {
  t = String(t || '').trim().replace(/\$\d+$/, '');
  const m = t.match(/@([\w.\-]+)/); if (m) return '@' + m[1].replace(/\.$/, '');
  return /^[\w.\-]+$/.test(t) ? '@' + t : '';
}
function atualizarCodigo() {}
function erro(t) { const m = $('#msg'); m.textContent = t; m.hidden = !t; }

$('#perfil').addEventListener('input', () => { if (EXEMPLO) render(); });
$('#qtd').addEventListener('change', () => { atualizarCodigo(); render(); });
$('#seg').addEventListener('input', render);
function carregarArquivo(f) {
  if (!f) return;
  const r = new FileReader();
  r.onload = () => {
    try {
      const vids = normalizar(lerCSV(String(r.result)));
      if (!vids.length) return erro('O arquivo está vazio ou não é um CSV.');
      if (!vids.some(v => v.data)) return erro('Não achei a coluna data_publicacao com datas nesse CSV. Use o CSV do coletar_csv ou da pasta Basedadosconteudo.');
      if (!vids.some(v => v.views !== null || v.likes !== null)) erro('Aviso: esse CSV não tem visualizações nem curtidas — só a frequência vai aparecer.');
      else erro('');
      TODOS = vids; EXEMPLO = false;
      if (vids[0].conta && !normPerfil($('#perfil').value)) $('#perfil').value = 'https://www.kwai.com/@' + vids[0].conta;
      $('#zona-txt').textContent = `${f.name} · ${vids.length} vídeo(s)`;
      atualizarCodigo(); render();
    } catch (e) { erro('Não consegui ler esse arquivo: ' + e.message); }
  };
  r.readAsText(f, 'utf-8');
}
$('#arquivo').addEventListener('change', e => carregarArquivo(e.target.files[0]));
const zona = $('#zona');
['dragenter', 'dragover'].forEach(t => zona.addEventListener(t, e => { e.preventDefault(); zona.classList.add('arrastando'); }));
['dragleave', 'drop'].forEach(t => zona.addEventListener(t, e => { e.preventDefault(); zona.classList.remove('arrastando'); }));
zona.addEventListener('drop', e => carregarArquivo(e.dataTransfer.files[0]));

let tempo; new ResizeObserver(() => { clearTimeout(tempo); tempo = setTimeout(() => window.__A && (graficoSemanas($('#g-semanas'), window.__A), graficoDias($('#g-dias'), window.__A), graficoViews($('#g-views'), window.__A)), 120); }).observe($('.wrap'));

// ---------- coleta: o servidor puxa os dados do perfil ----------
let ROBO = false, rodando = false;
fetch('api/ping').then(r => r.ok ? r.json() : null).then(j => { ROBO = !!(j && j.ok); }).catch(() => {});

function status(t, girando) { $('#status').innerHTML = (girando ? '<span class="spin"></span>' : '') + esc(t); }
function deJob(job) {
  const seg = job.perfil_info && job.perfil_info.seguidores;
  return (job.itens || []).map(it => ({
    id: it.id_video, link: it.link, titulo: it.titulo || it.legenda || 'Sem legenda',
    data: dataDe(it.data_publicacao), views: it.visualizacoes ?? null, likes: it.curtidas ?? null,
    coms: it.comentarios ?? null, shares: it.compartilhamentos ?? null,
    conta: job.conta, seguidores: seg ?? null, coleta: (job.inicio || '').slice(0, 10),
  }));
}
async function analisarPerfil() {
  if (rodando) return;
  const perfil = normPerfil($('#perfil').value);
  if (!perfil) { erro('Cole o link do perfil, por exemplo https://www.kwai.com/@augustocuryoficial'); return; }
  if (!ROBO) { $('#sem-robo').hidden = false; erro(''); return; }
  erro(''); rodando = true; $('#analisar').disabled = true; $('#barra').hidden = false; $('#progresso').style.width = '4%';
  status('Iniciando…', true);
  try {
    const r = await fetch('api/coletar', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({perfil: 'https://www.kwai.com/' + perfil, max: (+$('#qtd').value) || 150})});
    const j = await r.json();
    if (!r.ok) throw new Error(j.erro || 'o robô recusou o pedido');
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
