/**
 * HLS Proxy Worker - Cloudflare Worker
 * 
 * Proxy genérico para streams HLS que:
 *   - Agrega CORS (Access-Control-Allow-Origin: *)
 *   - Setea Referer / User-Agent server-side (bypass forbidded headers)
 *   - Rewrite automático de playlists M3U8 (segmentos → proxy)
 *   - Streams segmentos .ts/.m4s
 *   - Soporta EXT-X-KEY
 *   - Preserva query strings (tokens, nimblesessionid, etc.)
 * 
 * USO:
 *   ?url=<encoded_url>&ref=<referer_domain>&ua=<user_agent>
 * 
 * Ejemplo:
 *   /proxy?url=https%3A%2F%2Flivetrx01.vodgc.net%2Feltrecetv%2Findex.m3u8&ref=vodgc.net
 * 
 * DEPLOY:
 *   npm install -g wrangler
 *   wrangler login
 *   wrangler deploy
 */

// Headers por defecto para upstream
const DEFAULT_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

export default {
  async fetch(request) {
    try {
      const url = new URL(request.url)

      // Status / health endpoint
      if (url.pathname === '/' || url.pathname === '/status') {
        return new Response(JSON.stringify({ ok: true, service: 'hls-proxy' }), {
          headers: { 'content-type': 'application/json', 'access-control-allow-origin': '*' },
        })
      }

      const targetUrl = url.searchParams.get('url')
      if (!targetUrl) {
        return new Response('Missing ?url parameter', { status: 400 })
      }

      const decodedTarget = decodeURIComponent(targetUrl)
      const upstreamUrl = new URL(decodedTarget)

      // Headers para upstream
      const ref = url.searchParams.get('ref') || upstreamUrl.origin
      const ua = url.searchParams.get('ua') || DEFAULT_UA

      const upstreamHeaders = {
        'User-Agent': ua,
        'Referer': ref.startsWith('http') ? ref : 'https://' + ref + '/',
        'Origin': upstreamUrl.origin,
      }

      const upstreamResponse = await fetch(upstreamUrl.toString(), {
        headers: upstreamHeaders,
      })

      if (!upstreamResponse.ok) {
        return new Response('Upstream error: ' + upstreamResponse.status, {
          status: upstreamResponse.status,
        })
      }

      const contentType = upstreamResponse.headers.get('content-type') || ''

      // ─── M3U8 Playlist: rewrite URLs ───
      if (
        contentType.includes('vnd.apple.mpegurl') ||
        contentType.includes('x-mpegURL') ||
        upstreamUrl.pathname.endsWith('.m3u8')
      ) {
        const finalUrl = upstreamResponse.url
        const baseUrl = finalUrl.substring(0, finalUrl.lastIndexOf('/') + 1)
        let text = await upstreamResponse.text()

        // Rewrite every non-comment line (relative → absolute → proxy)
        text = text.replace(/^([^#][^\r\n]*)/gm, (line) => {
          line = line.trim()
          if (!line) return line

          // If it's already an absolute URL pointing to our proxy, skip
          if (line.includes('/proxy?url=')) return line

          // Resolve relative → absolute
          let absolute
          try {
            absolute = new URL(line, baseUrl).toString()
          } catch {
            return line
          }

          // Encode and proxy (update ref to match target hostname)
          const proxyUrl = new URL(request.url)
          proxyUrl.searchParams.set('url', absolute)
          try {
            proxyUrl.searchParams.set('ref', new URL(absolute).hostname)
          } catch {}
          return proxyUrl.toString()
        })

        // Rewrite EXT-X-KEY URIs too
        text = text.replace(
          /(URI=["'])([^"']+)(["'])/gi,
          (match, pre, uri, post) => {
            let absolute
            try {
              absolute = new URL(uri, baseUrl).toString()
            } catch {
              return match
            }
            const proxyUrl = new URL(request.url)
            proxyUrl.searchParams.set('url', absolute)
            try {
              proxyUrl.searchParams.set('ref', new URL(absolute).hostname)
            } catch {}
            return pre + proxyUrl.toString() + post
          }
        )

        return new Response(text, {
          headers: {
            'Content-Type': 'application/vnd.apple.mpegurl',
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Headers': '*',
            'Cache-Control': 'public, max-age=5',
          },
        })
      }

      // ─── Segment / Binary response ───
      return new Response(upstreamResponse.body, {
        headers: {
          'Content-Type': contentType || 'application/octet-stream',
          'Access-Control-Allow-Origin': '*',
          'Cache-Control': 'public, max-age=30',
        },
      })
    } catch (err) {
      return new Response('Proxy error: ' + err.message, { status: 500 })
    }
  },
}
