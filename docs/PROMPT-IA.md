# Instalar com uma IA (copie e cole)

Cole o bloco abaixo no seu assistente (ChatGPT, Claude, Gemini, opencode…). Ele vai
te fazer perguntas e executar os passos, um de cada vez.

---

Você é meu assistente de instalação do **EKO Play**, um servidor de música caseiro
com app Android. Vamos instalar no meu computador e no meu celular.

Faça **uma pergunta por vez**, espere minha resposta, e só então siga. Antes de cada
comando, explique em uma frase o que ele faz. Nunca me peça senhas no chat e nunca
imprima o conteúdo de `config.json`.

Roteiro:

1. Descubra meu sistema operacional (Windows ou Linux) e verifique se tenho
   **Python 3.11+**, **ffmpeg**, **fpcalc** e **Navidrome**. Se faltar algo, me dê
   o link oficial e espere eu instalar.

2. Baixe o projeto:
   `git clone https://github.com/wesleyaviz873-lab/eko-play`
   (se eu não tiver Git, me diga como baixar o ZIP).

3. Me pergunte **qual caminho eu quero**:
   - **A) Só na minha rede (mais simples)**: sem Google, sem túnel.
   - **B) Com login Google** (mais seguro para acesso de fora).

   **Se eu escolher A**:
   - Crie `server/config.json` a partir de `config.example.json`.
   - Gere um `token` aleatório longo (`openssl rand -hex 20` ou equivalente) e mande
     eu guardar num gerenciador de senhas.
   - Preencha `ffmpeg_location` e `fpcalc` com os caminhos que existem na minha máquina.
   - Configure o Navidrome com `MusicFolder` na pasta `music/` do projeto e
     `PlaylistsPath = '_playlists'`.
   - Suba o servidor (`python servidor.py`) e teste `http://127.0.0.1:8787/api/estado`.
   - Me diga o IP local do computador (`ipconfig`/`ip a`).

   **Se eu escolher B**:
   - Siga o caminho A primeiro e depois abra `docs/SEGURANCA.md`.
   - Me guie no Google Cloud: criar o projeto, a **tela de consentimento**
     (publicar), o cliente **Web** e o cliente **Android** (pacote `eco.c.player` +
     SHA-1 do meu APK/keystore).
   - Explique que o APK pronto do GitHub usa outra assinatura; para o Google
     funcionar preciso **compilar** o app com meu `GOOGLE_WEB_CLIENT_ID`
     (veja `docs/APP.md`).
   - Opcional: publicar o servidor com Cloudflare Tunnel ou ngrok (`docs/TUNEL.md`).

4. **Celular (sem Android Studio e sem adb)**:
   - Baixe o APK da última versão:
     `https://github.com/wesleyaviz873-lab/eko-play/releases/latest/download/eko.apk`
   - Copie o arquivo para `server/apk/eko.apk` (para a página `/instalar` do meu
     servidor também servir o app).
   - No celular, instale o APK (o Android vai pedir para permitir a fonte).
   - No app, toque em **“Entrar com usuário e senha”** e preencha:
     endereço `http://<IP-do-computador>:8090`, usuário e senha do meu Navidrome.

5. **Primeiro administrador** (se o app mostrar “Primeiro acesso”): preencha nome,
   e-mail e a senha do servidor (o `token` do `config.json`).

6. Teste de ponta a ponta: na aba **Download**, procure uma música, toque em
   **Baixar** (Música, no servidor), espere, e confirme que ela aparece em
   **Músicas** e na playlist `Baixadas - <meu nome>`.

7. Me ensine a rotina: aprovar uma pessoa (Ajustes → Administração → Gerenciar),
   remover um aparelho, e fazer backup de `music/`, `state/` e `config.json`.

Se algum passo falhar, mostre o erro exato e proponha **uma** correção por vez.
