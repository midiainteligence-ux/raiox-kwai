# -*- coding: utf-8 -*-
"""
RAIO-X KWAI — site público
==========================
Qualquer pessoa abre o site, cola o link de um perfil do Kwai e clica em
Analisar. O servidor puxa os links dos últimos vídeos (mesma coleta do robô
de links, em modo celular), lê views/curtidas/comentários/data de cada um e
a página monta o laudo na hora. Depois, em segundo plano, baixa o áudio
de cada vídeo e TRANSCREVE localmente com o Whisper (mesma lógica do
notebook do Colab) — a transcrição vai aparecendo na tabela e no CSV.

Proteções pra uso público:
  - FILA: no máximo COLETAS_SIMULTANEAS navegadores ao mesmo tempo; quem
    chega depois vê "você é o nº X da fila".
  - CACHE: o mesmo perfil analisado há menos de CACHE_HORAS devolve o
    resultado pronto, sem abrir o navegador de novo.
  - LIMITE POR PESSOA: no máximo MAX_POR_IP análises em andamento por IP.

Transcrição: precisa de `openai-whisper` (pip) e do `ffmpeg` (sistema).
  TRANSCREVER=0 desliga · MODELO_WHISPER=tiny/base/small (padrão: base).
  MAX_TRANSCRICOES=10: só os 10 vídeos mais recentes são transcritos; o
  resto do perfil fica só com as métricas.
  Os textos ficam em PASTA_TRANSCRICOES (cache: vídeo já transcrito não
  é transcrito de novo).

Rodar local:  pip install -r requirements.txt && python app.py
Publicar:     veja o README.md (Hugging Face Spaces, grátis).
"""

import os
import random
import re
import shutil
import subprocess
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
TRANSCREVER = os.environ.get("TRANSCREVER", "1") != "0"
MODELO_WHISPER = os.environ.get("MODELO_WHISPER", "base")      # tiny / base / small / medium
PASTA_TRANSCRICOES = os.environ.get("PASTA_TRANSCRICOES", "transcricoes")
PASTA_TMP = os.environ.get("PASTA_TMP", "/tmp/kwai_audio")
MAX_MB_VIDEO = 300
MAX_TRANSCRICOES = int(os.environ.get("MAX_TRANSCRICOES", "10"))   # só os N vídeos mais recentes são transcritos
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
    out["url_video"] = _url_video(html)
    return out


# ----------------------------------------------------------------------
# 3) TRANSCRIÇÃO LOCAL (Whisper) — mesma lógica do notebook do Colab:
#    baixa o .mp4 → ffmpeg extrai o áudio → Whisper transcreve
# ----------------------------------------------------------------------

_MODELO_WHISPER = None
TRAVA_WHISPER = threading.Lock()      # um áudio por vez (Whisper pesa e não é thread-safe)


def transcricao_disponivel():
    if not TRANSCREVER or shutil.which("ffmpeg") is None:
        return False
    try:
        import whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _modelo():
    global _MODELO_WHISPER
    if _MODELO_WHISPER is None:
        import whisper
        _MODELO_WHISPER = whisper.load_model(MODELO_WHISPER)
    return _MODELO_WHISPER


def _cache_transcricao(video_id):
    return os.path.join(PASTA_TRANSCRICOES, f"kwai_{video_id}.txt")


def transcrever_video(video_id, url_video):
    """Devolve o texto falado no vídeo. Usa o cache em disco se já existir."""
    cache = _cache_transcricao(video_id)
    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as f:
            return f.read().strip()
    if not url_video:
        raise RuntimeError("a página não trouxe o arquivo de vídeo")

    os.makedirs(PASTA_TMP, exist_ok=True)
    mp4 = os.path.join(PASTA_TMP, f"{video_id}_{uuid.uuid4().hex[:6]}.mp4")
    wav = mp4[:-4] + ".wav"
    try:
        with requests.get(url_video, headers=HEADERS, stream=True, timeout=60) as r:
            r.raise_for_status()
            baixado = 0
            with open(mp4, "wb") as f:
                for pedaco in r.iter_content(1024 * 256):
                    baixado += len(pedaco)
                    if baixado > MAX_MB_VIDEO * 1024 * 1024:
                        raise RuntimeError("vídeo grande demais")
                    f.write(pedaco)
        # áudio mono 16 kHz: é o formato que o Whisper usa internamente
        ok = subprocess.run(["ffmpeg", "-y", "-i", mp4, "-vn", "-ac", "1", "-ar", "16000", wav],
                            capture_output=True).returncode == 0
        if not ok:
            raise RuntimeError("o ffmpeg não conseguiu extrair o áudio")
        with TRAVA_WHISPER:
            texto = _modelo().transcribe(wav, language="pt", fp16=False)["text"].strip()
    finally:
        for arq in (mp4, wav):
            if os.path.exists(arq):
                os.remove(arq)

    os.makedirs(PASTA_TRANSCRICOES, exist_ok=True)
    with open(cache, "w", encoding="utf-8") as f:
        f.write(texto)
    return texto






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

    transcrever = transcricao_disponivel()
    urls_video = {}
    itens = []
    for pos, c in enumerate(cards, start=1):
        views = contagem_para_int(c["views_texto"])
        itens.append({"posicao": pos, "id_video": c["id"],
                      "link": f"https://www.kwai.com/@{conta}/video/{c['id']}",
                      "titulo": c["titulo_grade"], "visualizacoes": views,
                      "fonte_views": "grade" if views is not None else "",
                      "curtidas": None, "comentarios": None, "compartilhamentos": None,
                      "data_publicacao": "", "hora_publicacao": None, "legenda": "", "estado": "pendente",
                      "transcricao": "", "estado_transcricao": "pendente" if transcrever else "desligado"})
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
                    urls_video[item["id_video"]] = m.get("url_video")
                    item["estado"] = "ok"
                feitos += 1
                JOBS[job_id]["mensagem"] = f"Lendo os vídeos: {feitos}/{len(itens)}"

    if not transcrever:
        _atualizar(job_id, status="concluido", transcrevendo=False,
                   mensagem=f"Pronto: {len(itens)} vídeo(s) de @{conta}.")
        with TRAVA:
            CACHE[(conta.lower(), max_videos)] = (time.time(), job_id)
        return

    # métricas prontas: o laudo já aparece; as transcrições chegam em seguida
    _atualizar(job_id, status="concluido", transcrevendo=True,
               mensagem=f"Dados prontos. Transcrevendo os {min(len(itens), MAX_TRANSCRICOES) if MAX_TRANSCRICOES > 0 else len(itens)} vídeos mais recentes: 0…")
    with TRAVA:
        CACHE[(conta.lower(), max_videos)] = (time.time(), job_id)

    # só os N mais recentes (pela data de publicação; sem data, pela ordem da grade)
    recentes = sorted(itens, key=lambda i: (i["data_publicacao"] or "", i.get("hora_publicacao") or 0,
                                            -i["posicao"]), reverse=True)
    alvo = recentes[:MAX_TRANSCRICOES] if MAX_TRANSCRICOES > 0 else recentes
    with TRAVA:
        ids_alvo = {i["id_video"] for i in alvo}
        for item in itens:
            if item["id_video"] not in ids_alvo:
                item["estado_transcricao"] = "fora"
    total = len(alvo)

    feitos = ok = 0
    for item in alvo:
        try:
            texto = transcrever_video(item["id_video"], urls_video.get(item["id_video"]))
            estado = "ok" if texto else "vazio"
        except Exception:
            texto, estado = "", "erro"
        with TRAVA:
            item["transcricao"], item["estado_transcricao"] = texto, estado
            feitos += 1
            ok += estado == "ok"
            JOBS[job_id]["mensagem"] = f"Dados prontos. Transcrevendo os {total} vídeos mais recentes: {feitos}/{total}…"
    _atualizar(job_id, transcrevendo=False,
               mensagem=f"Pronto: {len(itens)} vídeo(s) de @{conta} · {ok} transcrição(ões).")


def _limpar_antigos():
    limite = time.time() - CACHE_HORAS * 3600
    with TRAVA:
        for chave, (hora, jid) in list(CACHE.items()):
            if hora < limite:
                CACHE.pop(chave, None)
        vivos = {jid for _, jid in CACHE.values()}
        for jid in list(JOBS):
            j = JOBS[jid]
            if j["status"] != "rodando" and not j.get("transcrevendo") and jid not in vivos and j["criado"] < limite:
                JOBS.pop(jid, None)


# ----------------------------------------------------------------------
# ROTAS
# ----------------------------------------------------------------------

@app.get("/")
def pagina():
    return Response(HTML, mimetype="text/html")


@app.get("/api/ping")
def ping():
    return jsonify({"ok": True, "transcricao": transcricao_disponivel()})


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
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap">
<style>
  :root {
    --bg: #eef1fa; --surface: #ffffff; --sunken: #f0f3fb;
    --ink: #161a2c; --ink2: #4a5070; --muted: #8a90aa; --line: #e2e7f4; --grid: #edf0f8;
    --accent: #6482d6; --accent-soft: #dfe6fa; --accent-ink: #ffffff;
    --hot: #3c58b4; --warn: #9a6a12; --warn-soft: #f4eecb; --err: #c0392b;
    --peach: #f3dccf; --butter: #f4eecb;
    --display: "Plus Jakarta Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
    --body: "Plus Jakarta Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
    --mono: "Plus Jakarta Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
    --r-lg: 28px; --r-md: 20px;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --bg: #0f1220; --surface: #181c2d; --sunken: #20263b;
      --ink: #eef1fb; --ink2: #b8bfdb; --muted: #858ca9; --line: #262c43; --grid: #22283d;
      --accent: #8aa2ee; --accent-soft: #263152; --accent-ink: #0e1220;
      --hot: #bccbff; --warn: #f0c878; --warn-soft: #3a331e; --err: #ff8a7a;
      --peach: #3d2c26; --butter: #39341f;
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --bg: #0f1220; --surface: #181c2d; --sunken: #20263b;
    --ink: #eef1fb; --ink2: #b8bfdb; --muted: #858ca9; --line: #262c43; --grid: #22283d;
    --accent: #8aa2ee; --accent-soft: #263152; --accent-ink: #0e1220;
    --hot: #bccbff; --warn: #f0c878; --warn-soft: #3a331e; --err: #ff8a7a;
    --peach: #3d2c26; --butter: #39341f;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.55 var(--body); -webkit-font-smoothing: antialiased; }
  .wrap { max-width: 1120px; margin: 0 auto; padding-inline: 16px; padding-block: 20px 72px; display: grid; gap: 22px; }
  .num, td.n, .kpi b, .mono { font-variant-numeric: tabular-nums; }
  [hidden] { display: none !important; }

  /* cabeçalho estilo app */
  header.top { display: flex; align-items: center; justify-content: space-between; gap: 14px; flex-wrap: wrap; padding: 4px 0; }
  .logos { display: flex; align-items: center; gap: 16px; min-width: 0; background: #ffffff; border-radius: 999px; padding: 8px 22px 8px 14px; }
  .logos img { display: block; height: auto; }
  .logo-vitoria { width: 200px; max-width: 44vw; }
  .logo-due { width: 84px; max-width: 20vw; }
  .logos .sep { width: 1px; height: 30px; background: #e2e7f4; }
  .autora { display: flex; align-items: center; gap: 10px; background: var(--surface); border-radius: 999px; padding: 6px 16px 6px 6px; }
  .avatar { width: 40px; height: 40px; border-radius: 50%; display: grid; place-items: center; flex: none;
            background: var(--accent); color: var(--accent-ink); font: 700 14px/1 var(--display); letter-spacing: .02em; }
  .autora .quem { display: grid; line-height: 1.25; }
  .autora small { font-size: 12px; color: var(--muted); }
  .autora b { font-size: 14.5px; font-weight: 700; color: var(--ink); }
  .autora .quando { font-size: 12px; color: var(--muted); padding-left: 10px; margin-left: 2px; border-left: 1px solid var(--line); white-space: nowrap; }

  .hero { display: grid; gap: 6px; padding: 6px 2px 0; }
  .hero .saud { font-size: 14px; color: var(--muted); }
  .hero h2 { margin: 0; font: 700 clamp(22px, 3vw, 30px)/1.2 var(--display); letter-spacing: -0.02em; max-width: 28ch; text-wrap: balance; }

  /* entrada */
  .entrada { background: var(--surface); border-radius: var(--r-lg); padding: 20px; display: grid; gap: 14px; }
  .linha-form { display: flex; gap: 10px; flex-wrap: wrap; }
  .linha-form input[type=text] { flex: 1 1 320px; min-width: 0; }
  input[type=text], input[type=number], select { font: inherit; color: var(--ink); background: var(--sunken); border: 1px solid transparent;
         border-radius: 999px; padding: 12px 18px; }
  input::placeholder { color: var(--muted); }
  input[type=number] { flex: 1 1 210px; min-width: 0; }
  select { flex: 0 1 170px; padding-right: 34px; }
  .btn { font: 600 14px/1 var(--body); padding: 14px 22px; border-radius: 999px; border: 0;
         background: var(--sunken); color: var(--ink); cursor: pointer; display: inline-flex; align-items: center; gap: 8px; }
  .btn.pri { background: var(--accent); color: var(--accent-ink); }
  .btn:hover { filter: brightness(1.05); }
  .btn:disabled { opacity: .6; cursor: progress; }
  input:focus-visible, select:focus-visible, .btn:focus-visible, th:focus-visible, summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .acao { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .status-txt { font-size: 14px; color: var(--ink2); display: inline-flex; align-items: center; gap: 8px; }
  .spin { width: 14px; height: 14px; border: 2px solid var(--accent-soft); border-top-color: var(--accent); border-radius: 50%; animation: gira .8s linear infinite; }
  @keyframes gira { to { transform: rotate(360deg); } }
  @media (prefers-reduced-motion: reduce) { .spin { animation-duration: 3s; } }
  .barra { height: 8px; border-radius: 999px; background: var(--sunken); overflow: hidden; }
  .barra i { display: block; height: 100%; width: 0; background: var(--accent); border-radius: 999px; transition: width .3s; }
  .aviso { font-size: 13.5px; padding: 12px 16px; border-radius: var(--r-md); background: var(--warn-soft); color: var(--ink); }
  .aviso b { color: var(--warn); }
  .erro { color: var(--err); font-size: 14px; }

  /* laudo */
  .laudo { display: grid; gap: 12px; padding: 8px 2px 0; }
  .eyebrow { font-size: 13px; color: var(--muted); display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
  #lb-conta { font-weight: 700; color: var(--ink); }
  .tag { font: 600 11.5px/1 var(--body); padding: 6px 10px; border-radius: 999px; background: var(--warn-soft); color: var(--warn); }
  .laudo h1 { font: 700 clamp(24px, 3.4vw, 36px)/1.18 var(--display); letter-spacing: -0.025em; margin: 0; text-wrap: balance; max-width: 30ch; }
  .laudo h1 em { font-style: normal; color: var(--accent); }
  .laudo p.resumo { margin: 0; color: var(--ink2); max-width: 70ch; font-size: 16px; }
  .chips { display: flex; gap: 8px; flex-wrap: wrap; }
  .chip { font-size: 13px; padding: 8px 14px; border-radius: 999px; background: var(--surface); color: var(--ink2); }
  .chip b { color: var(--ink); font-weight: 700; }

  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; }
  .kpi { background: var(--surface); border-radius: var(--r-md); padding: 18px 20px; display: grid; gap: 6px; align-content: start; }
  .kpi:nth-child(1) { background: var(--accent-soft); }
  .kpi:nth-child(3) { background: var(--butter); }
  .kpi:nth-child(4) { background: var(--peach); }
  .kpi span { font-size: 13px; color: var(--ink2); }
  .kpi b { font: 700 25px/1.15 var(--display); letter-spacing: -0.02em; }
  .kpi small { font-size: 12.5px; color: var(--muted); }

  /* painéis */
  .grade2 { display: grid; grid-template-columns: 1.5fr 1fr; gap: 16px; }
  @media (max-width: 820px) { .grade2 { grid-template-columns: 1fr; } }
  .painel { background: var(--surface); border-radius: var(--r-lg); padding: 22px; display: grid; gap: 14px; align-content: start; min-width: 0; }
  .painel h2 { font: 700 18px/1.25 var(--display); margin: 0; letter-spacing: -0.015em; }
  .painel .sub { margin: -8px 0 0; font-size: 13.5px; color: var(--muted); }
  .grafico { width: 100%; position: relative; }
  .grafico svg { display: block; width: 100%; height: auto; overflow: visible; }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin: 0; }
  .stats div { display: grid; gap: 2px; background: var(--sunken); border-radius: 16px; padding: 12px 14px; }
  .stats dt { font-size: 12.5px; color: var(--muted); }
  .stats dd { margin: 0; font: 700 16px/1.3 var(--display); font-variant-numeric: tabular-nums; }
  .stats dd small { font: 400 12px var(--body); color: var(--ink2); display: block; }

  .top5 { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; }
  .top5 li { display: grid; grid-template-columns: 1fr auto; gap: 12px; align-items: center; font-size: 14px;
             background: var(--sunken); border-radius: 18px; padding: 12px 16px; }
  .top5 a { color: var(--ink); font-weight: 600; text-decoration: none; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; display: block; }
  .top5 a:hover { color: var(--accent); }
  .top5 small { color: var(--muted); font-size: 12px; display: block; }

  .tabela { overflow-x: auto; border-radius: var(--r-lg); background: var(--surface); padding: 6px 8px; }
  table { border-collapse: collapse; width: 100%; min-width: 760px; font-size: 14px; }
  th, td { padding: 12px 14px; border-bottom: 1px solid var(--grid); text-align: left; vertical-align: top; }
  tbody tr:last-child td { border-bottom: 0; }
  th { font: 600 12px/1 var(--body); color: var(--muted); cursor: pointer; white-space: nowrap; user-select: none; }
  td.n { font-weight: 600; }
  th.n, td.n { text-align: right; }
  th[aria-sort="ascending"]::after { content: " ↑"; } th[aria-sort="descending"]::after { content: " ↓"; }
  th[aria-sort="ascending"], th[aria-sort="descending"] { color: var(--accent); }
  td.t { max-width: 420px; }
  td.t .t-tit { display: block; color: var(--ink); font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  td.t .t-link { display: block; margin-top: 2px; font-size: 12px; color: var(--accent); text-decoration: none;
                 overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  td.t a.t-link:hover { text-decoration: underline; }
  td.t .t-link.vazio { color: var(--muted); }
  .vazio { color: var(--muted); }
  details.tr { margin-top: 8px; }
  details.tr summary { cursor: pointer; font-size: 12.5px; font-weight: 600; color: var(--accent); width: max-content; }
  details.tr p { margin: 8px 0 2px; font-size: 13.5px; line-height: 1.55; color: var(--ink); white-space: normal;
                 max-height: 220px; overflow-y: auto; background: var(--sunken); border-radius: 14px; padding: 10px 14px; }
  .tr-esp { display: block; margin-top: 6px; font-size: 12px; color: var(--muted); }

  #dica { position: absolute; pointer-events: none; background: var(--ink); color: var(--bg); font-size: 12.5px; line-height: 1.4;
          padding: 8px 11px; border-radius: 12px; max-width: 260px; z-index: 5; transform: translate(-50%, calc(-100% - 10px)); }
  #dica b { font-weight: 700; }
  .nota { font-size: 12.5px; color: var(--muted); margin: 0; max-width: 80ch; padding: 0 6px; }

  .insights { background: var(--surface); border-radius: var(--r-lg); padding: 22px; display: grid; gap: 12px; }
  .insights h2 { font: 700 18px/1.25 var(--display); margin: 0; }
  .insights ul { margin: 0; padding: 0; list-style: none; display: grid; gap: 10px; }
  .insights li { display: grid; grid-template-columns: 22px 1fr; gap: 8px; font-size: 15px; line-height: 1.5; color: var(--ink); }
  .insights li::before { content: ""; width: 8px; height: 8px; margin-top: 8px; border-radius: 50%; background: var(--accent); justify-self: center; }
  .insights li b { font-weight: 700; }
  .conclusao { margin: 0; font-size: 14.5px; color: var(--ink); background: var(--accent-soft); border-radius: var(--r-md); padding: 12px 16px; }
  .conclusao b { color: var(--hot); }
  .grande { font: 700 46px/1 var(--display); letter-spacing: -0.03em; color: var(--accent); }
  .grande small { display: block; font: 13px/1.4 var(--body); color: var(--muted); letter-spacing: 0; margin-top: 8px; max-width: 30ch; }
  .seg-bloco { display: grid; grid-template-columns: auto 1fr; gap: 20px; align-items: center; }
  @media (max-width: 560px) { .seg-bloco { grid-template-columns: 1fr; } .autora .quando { display: none; } }
  .vezes { font: 700 13px/1 var(--body); color: var(--accent-ink); background: var(--accent); border-radius: 999px; padding: 6px 10px; }
  .vezes:empty { display: none; }
  .hora { color: var(--muted); font-size: 12px; }
</style>

<div class="wrap">
  <header class="top">
    <div class="logos">
      <img class="logo-vitoria" src="data:image/webp;base64,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" alt="VitórIA" width="520" height="165">
      <span class="sep" aria-hidden="true"></span>
      <img class="logo-due" src="data:image/webp;base64,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" alt="DUE" width="352" height="106">
    </div>
    <div class="autora" aria-label="Criado por Carolina Campelo em setembro de 2026">
      <span class="avatar" aria-hidden="true">CC</span>
      <span class="quem"><small>Criado por</small><b>@carolinacampelo</b></span>
      <span class="quando">set 2026</span>
    </div>
  </header>

  <section class="hero">
    <span class="saud">Raio-X Kwai</span>
    <h2>Cole o link de qualquer perfil do Kwai e veja quando a conta posta e quando engaja</h2>
  </section>

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
      <button class="btn" type="button" id="baixar" hidden>Baixar CSV (com transcrições)</button>
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
      <rect x="${l}" y="${y + 4}" width="${bw}" height="15" rx="7.5" fill="var(--sunken)"/>
      <rect x="${x2}" y="${y + 4}" width="${bw}" height="15" rx="7.5" fill="var(--sunken)"/>`;
    if (w1) s += `<rect x="${l}" y="${y + 4}" width="${w1}" height="15" rx="7.5" fill="${c1}"/>`;
    if (w2) s += `<rect x="${x2}" y="${y + 4}" width="${w2}" height="15" rx="7.5" fill="${c2}" ${x.ints.length < G.minimo ? 'fill-opacity=".45"' : ''}/>`;
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

function celTranscricao(v) {
  if (v.transcricao) return `<details class="tr"><summary>ver transcrição</summary><p>${esc(v.transcricao)}</p></details>`;
  if (v.estTr === 'pendente') return '<span class="tr-esp">transcrevendo…</span>';
  if (v.estTr === 'erro') return '<span class="tr-esp">não deu pra transcrever</span>';
  if (v.estTr === 'vazio') return '<span class="tr-esp">sem fala no áudio</span>';
  return '';
}
function baixarCSV() {
  const cols = [['data', v => v.data ? v.data.toISOString().slice(0, 10) : ''], ['hora', v => v.hora ?? ''],
    ['link', v => v.link], ['legenda', v => v.legenda || v.titulo], ['curtidas', v => v.likes ?? ''],
    ['comentarios', v => v.coms ?? ''], ['transcricao', v => v.transcricao || '']];
  const q = x => '"' + String(x ?? '').replace(/"/g, '""').replace(/\r?\n/g, ' ') + '"';
  const linhas = [cols.map(c => c[0]).join(';'), ...TODOS.map(v => cols.map(c => q(c[1](v))).join(';'))];
  const blob = new Blob(['\ufeff' + linhas.join('\r\n')], {type: 'text/csv;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `kwai_${(TODOS[0] && TODOS[0].conta) || 'perfil'}_${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
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
      : `<span class="t-link vazio">sem link (vídeo de exemplo)</span>`}${celTranscricao(v)}</td>
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
    legenda: it.legenda || '', transcricao: it.transcricao || '', estTr: it.estado_transcricao || '',
    conta: job.conta, seguidores: seg ?? null, coleta: (job.inicio || '').slice(0, 10),
  }));
}
async function analisarPerfil() {
  if (rodando) return;
  const perfil = normPerfil($('#perfil').value);
  if (!perfil) { erro('Cole o link do perfil, por exemplo https://www.kwai.com/@Lulaoficial'); return; }
  if (!ROBO) { $('#sem-robo').hidden = false; erro(''); return; }
  erro(''); rodando = true; $('#analisar').disabled = true; $('#baixar').hidden = true; let assinatura = ''; $('#barra').hidden = false; $('#progresso').style.width = '4%';
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
      const transcritos = itens.filter(i => i.estado_transcricao && i.estado_transcricao !== 'pendente').length;
      $('#progresso').style.width = !itens.length ? '10%'
        : job.transcrevendo ? (100 * transcritos / itens.length) + '%' : (15 + 85 * feitos / itens.length) + '%';   // "fora" já conta como feito
      const conv = deJob(job), nova = `${feitos}/${transcritos}/${job.status}`;
      if (nova !== assinatura && conv.some(v => v.data)) {        // só redesenha quando algo mudou
        assinatura = nova; TODOS = conv; EXEMPLO = false; render(true);
        $('#baixar').hidden = job.status === 'rodando';
      }
      if (job.status !== 'rodando' && !job.transcrevendo) {
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
$('#baixar').addEventListener('click', baixarCSV);
$('#perfil').addEventListener('keydown', e => { if (e.key === 'Enter') analisarPerfil(); });
render();
})();
</script>

</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "7860")), threaded=True)