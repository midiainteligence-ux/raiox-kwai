---
title: Raio-X Kwai
emoji: 📊
colorFrom: yellow
colorTo: red
sdk: docker
app_port: 7860
pinned: false
---

# Raio-X Kwai

Cole o link de qualquer perfil do Kwai e veja como a conta posta e engaja:
período analisado, publicações por semana, dia preferido, maior pausa,
visualizações de cada vídeo, chance de interação por view e seguidores.

## Publicar grátis no Hugging Face Spaces

1. Crie uma conta em https://huggingface.co (grátis, sem cartão).
2. Clique em **New → Space**. Dê um nome (ex.: `raiox-kwai`), escolha
   **Docker** como SDK, **Blank** como template, e **Public**.
3. Na aba **Files** do Space, clique em **Add file → Upload files** e suba os
   3 arquivos desta pasta: `app.py`, `Dockerfile`, `requirements.txt`
   (e substitua o `README.md` por este).
4. Espere o build (~3–5 min). Quando aparecer **Running**, o site está no ar em
   `https://<seu-usuario>-raiox-kwai.hf.space` — é esse link que você manda
   pra qualquer pessoa.

## Ajustes (Settings → Variables do Space)

| Variável | Padrão | O que faz |
|---|---|---|
| `COLETAS_SIMULTANEAS` | 2 | quantos perfis são analisados ao mesmo tempo (o resto entra na fila) |
| `CACHE_HORAS` | 6 | por quanto tempo o resultado de um perfil é reaproveitado |
| `MAX_POR_IP` | 2 | análises em andamento por pessoa |

## Rodar no próprio computador

    pip install -r requirements.txt
    python app.py        # abre em http://127.0.0.1:7860 (precisa do Chrome instalado)
