# App no celular

O app é o **EKO Play** (Kotlin + Compose), em `app/`.

## Instalar sem Android Studio e sem adb (recomendado)

1. Baixe o APK da última versão:
   `https://github.com/wesleyaviz873-lab/eko-play/releases/latest/download/eko.apk`
2. Abra o arquivo no celular e permita instalar de fonte desconhecida quando o
   Android pedir.
3. No app, use **“Entrar com usuário e senha”**:
   - Endereço: `http://<IP-do-seu-computador>:8090` (Navidrome) ou o endereço HTTPS
     do seu túnel.
   - Usuário e senha: a conta do Navidrome (por padrão `admin` e a senha que você
     definiu no `navidrome.toml`).
4. Se o servidor for novo (sem administrador), o app mostra **“Primeiro acesso”**:
   nome, e-mail e a senha do servidor (o `token` do `config.json`).

Para servir o APK pelo seu próprio servidor, copie o arquivo para
`server/apk/eko.apk`. A página `/instalar` passa a mostrar o botão **Baixar** com a
versão atual.

## Compilar (para usar login Google ou personalizar)

Requisitos: Android Studio (ou JDK 17+ e SDK 35) e Gradle 8.9.

1. Copie `local.properties.example` para `local.properties`:
   ```properties
   sdk.dir=/caminho/Android/Sdk
   EKO_SERVER_URL=https://seu-endereco
   GOOGLE_WEB_CLIENT_ID=seu-web-client-id.apps.googleusercontent.com
   ```
2. `./gradlew assembleDebug` e instale o APK gerado.
3. Para distribuir, crie `signing.properties` com a sua keystore e rode
   `./gradlew assembleRelease`.

> O cliente **Android** do Google precisa do pacote `eco.c.player` e do SHA-1 da
> sua keystore. Sem isso o login Google retorna `28444`/`28433`.

## Como usar

1. **Download**: é o YouTube dentro do app. Procure, abra o vídeo e toque em
   **Baixar**.
2. Escolha **Música** (vai para o servidor) ou **Vídeo**, e **Playlist inteira**
   quando a página for uma playlist.
3. O que foi baixado aparece no acervo e, para quem baixou, na playlist
   `Baixadas - <nome>`.
4. **Ajustes → Administração** (só admin): pedidos, pessoas, compartilhar, login
   do YouTube.

## Privacidade

- O app fala **somente** com o endereço configurado (o seu servidor).
- Não há SDK de anúncio nem de analytics.
