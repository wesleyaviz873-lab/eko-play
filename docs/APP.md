# App no celular

O app é o **Resonance** (Kotlin + Compose), já dentro deste repositório em `app/`.

## Instalar sem compilar

Baixe o APK no seu servidor: `https://SEU-ENDERECO/apk/eko.apk` (a página inicial
também tem o botão). Instale e abra.

## Compilar

Requisitos: Android Studio (ou JDK 17+ e Android SDK 35), Gradle 8.9.

1. Copie `local.properties.example` para `local.properties` e ajuste:
   ```properties
   sdk.dir=/caminho/Android/Sdk
   EKO_SERVER_URL=https://seu-endereco
   GOOGLE_WEB_CLIENT_ID=seu-web-client-id.apps.googleusercontent.com
   ```
2. `./gradlew assembleDebug` e instale o APK em `app/build/outputs/apk/debug/`.

Para assinar um release, crie `keystore.properties`:

```properties
storeFile=../signing/eko-release.jks
storePassword=...
keyAlias=eko
keyPassword=...
```

## Como usar

1. **Download** (aba de baixo): é o YouTube dentro do app. Procure, abra o vídeo e
   toque em **Baixar**.
2. Escolha **Música** (vai para o servidor) ou **Vídeo**, e **Playlist inteira**
   quando a página for uma playlist.
3. O que foi baixado aparece no acervo e, para quem baixou, na playlist
   `Baixadas - <nome>`.
4. **Ajustes → Administração** (só admin): pedidos, pessoas, compartilhar, login
   do YouTube.

## Privacidade

- O app fala **somente** com o endereço configurado (o seu servidor).
- O navegador embutido é o YouTube; não há SDK de anúncio nem de analytics.
- O login Google (opcional) é o oficial do Android; o servidor valida o token e
  guarda apenas o seu e-mail.
