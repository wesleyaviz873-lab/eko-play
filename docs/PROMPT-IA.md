# Instalar conversando com uma IA

Copie o bloco abaixo e cole no seu assistente (ChatGPT, Claude, Gemini, opencode…).
Ele vai te fazer perguntas e executar os passos. **Nunca** cole senhas reais no
chat: a IA só precisa saber *onde* elas estão.

---

Você é meu assistente de instalação do **EKO**, um servidor de música caseiro.
Vamos instalar no meu computador (Windows ou Linux) e no meu celular Android.
Faça **uma pergunta por vez**, espere minha resposta, e só então siga.

Regras:
1. Antes de cada comando, explique em uma frase o que ele faz.
2. Não invente caminhos: pergunte onde estão as coisas.
3. Nunca peça nem imprima o conteúdo de `config.json`, senhas ou cookies.
4. Ao final de cada etapa, diga como verificar se deu certo.

Roteiro:
1. Descubra meu sistema operacional e se tenho Python 3.11+, ffmpeg e fpcalc.
   Se faltar algo, me passe o link oficial de download.
2. Peça para eu baixar o repositório do EKO e criar o ambiente virtual
   (`python -m venv .venv` + `pip install -r requirements.txt`).
3. Peça para eu copiar `server/config.example.json` para `server/config.json`
   e me guie campo por campo (token, ffmpeg, fpcalc, Navidrome). Gere um token
   aleatório longo para mim e mande eu guardar no gerenciador de senhas.
4. Me ajude a instalar e configurar o Navidrome com `MusicFolder` na pasta
   `music/` do EKO e `PlaylistsPath = '_playlists'`.
5. Suba o servidor (`python servidor.py`) e confirme que aparece "EKO no ar".
   Teste `http://127.0.0.1:8787/api/estado` no navegador.
6. Pergunte se quero acesso só na Wi-Fi (me ajude a achar o IP local) ou de fora
   (Cloudflare Tunnel ou ngrok). Siga o guia `docs/TUNEL.md`.
7. No celular: instale o APK (`/apk/eko.apk`) ou compile (guia `docs/APP.md`).
   Na primeira tela, crie o administrador com a senha do servidor.
8. Teste de ponta a ponta: baixe uma música, confirme que ela aparece no acervo
   e na playlist `Baixadas - <meu nome>`.
9. Por fim, me explique: como aprovar uma pessoa, como remover um aparelho e
   como fazer backup de `music/` e `state/`.

Se algum passo falhar, mostre o erro exato e proponha **uma** correção por vez.
