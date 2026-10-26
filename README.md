# Spotube Spotify Metadata Plugin (Fixed Fork)

Fork del plugin de metadatos de **Spotify** para **Spotube v5+** (`sonic-liberation/spotube-plugin-spotify`) que corrige el bug crítico de **duplicación exponencial de canciones y congelamiento de memoria** en *"Canciones Favoritas" (Liked Songs)* y playlists.

## 🐛 ¿Qué problema resuelve este fix?

En **Spotube v5.1.x**, cuando el proveedor de metadatos devuelve `hasMore: true` al cargar las primeras 20 canciones de `savedTracks` o `playlist.tracks`:
1. `metadataPluginIsSavedTrackProvider` observa `metadataPluginSavedTracksProvider.future` y, al ver `hasMore == true`, invoca `notifier.fetchAll()`.
2. Dentro de `PaginatedAsyncNotifierMixin.fetchAll()`, cada página descargada actualiza `state = AsyncData(...)` mientras `hasMore` sigue siendo `true`.
3. Esa actualización de estado invalida `metadataPluginIsSavedTrackProvider` en Riverpod a mitad del ciclo, disparando **múltiples bucles `fetchAll()` concurrentes en paralelo ($2^N$)** junto con el `fetchMore()` del scroll.
4. Como resultado, una biblioteca de ~750 canciones se multiplica exponencialmente a **8,000+ canciones repetidas**, consumiendo más de **5 GB de RAM** y congelando la aplicación.

## 🛠️ Solución implementada

La API GraphQL interna de Spotify (`fetchLibraryTracks` y `fetchPlaylist`) soporta obtener hasta `5,000` canciones en una sola petición (`~0.4s` para ~750 canciones).

Este parche modifica:
- [`src/segments/user.ht`](./src/segments/user.ht) (`savedTracks`, `savedPlaylists`)
- [`src/segments/playlist.ht`](./src/segments/playlist.ht) (`tracks`)
- [`plugin.json`](./plugin.json) (`PatchedSpotifyPlugin` wrapper en `entryPoint` compatible directamente con el bytecode precompilado `plugin.out`)

Al solicitar la lista completa en la carga inicial (`offset: 0, limit: 5000`) y devolver `hasMore: false` y `nextOffset: null`:
- **Carga instantánea** de todas tus canciones favoritas y playlists en menos de medio segundo.
- **0 canciones duplicadas** (exactamente el número real de canciones de tu biblioteca).
- **Sin bucles infinitos ni fugas de RAM/CPU**, ya que evita por completo el bucle defectuoso `fetchAll()` de Spotube.

## 📦 Instalación en Spotube

1. Descarga el archivo **`plugin.smplug`** desde la sección de [**Releases**](https://github.com/julesprog963-stack/spotube-plugin-spotify/releases/latest) (o usa el archivo `plugin.smplug` incluido en la raíz de este repositorio).
2. En **Spotube**, ve a **Configuración → Plugins** (o **Metadatos**).
3. Sube el archivo `plugin.smplug` o pega directamente la URL de este repositorio:
   ```text
   https://github.com/julesprog963-stack/spotube-plugin-spotify
   ```
4. Actívalo como tu proveedor de metadatos predeterminado e inicia sesión con tu cuenta de Spotify.
