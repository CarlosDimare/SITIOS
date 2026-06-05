/**
 * HLS Proxy Server — Bun
 *
 * Proxy local para streams HLS:
 *   - CORS en todas las responses
 *   - Referer/User-Agent server-side (bypasea forbidden headers del browser)
 *   - Rewrite automático de playlists M3U8 (segmentos → proxy)
 *   - EXT-X-KEY URI rewrite
 *   - Binary streaming de .ts/.m4s
 *
 * USO:
 *   bun run hls-proxy.ts
 *   bun run hls-proxy.ts --port 4040
 *
 * Luego en tv.html activás Proxy HLS y ponés:
 *   http://localhost:3030
 */

const DEFAULT_PORT = 3030
const DEFAULT_UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

const portArg = process.argv.find((a) => a.startsWith('--port='))
const port = portArg
  ? parseInt(portArg.split('=')[1], 10)
  : DEFAULT_PORT

function info(msg: string) {
  console.log(`\x1b[33m[HLS]\x1b[0m ${msg}`)
}

function warn(msg: string) {
  console.log(`\x1b[31m[HLS]\x1b[0m ${msg}`)
}

Bun.serve({
  port,
  async fetch(request) {
    const url = new URL(request.url)

    // ─── Status page ───
    if (url.pathname === '/' || url.pathname === '/status') {
      return new Response(
        JSON.stringify(
          {
            ok: true,
            service: 'hls-proxy',
            usage: 'GET /proxy?url=<encoded-hls-url>&ref=<referer>&ua=<user-agent>',
            proxies: request.headers.get('x-forwarded-for') || 'direct',
          },
          null,
          2,
        ),
        {
          headers: {
            'content-type': 'application/json',
            'access-control-allow-origin': '*',
          },
        },
      )
    }

    // ─── Proxy endpoint ───
    if (url.pathname !== '/proxy') {
      return new Response('Use /proxy?url=...', { status: 404 })
    }

    const targetUrl = url.searchParams.get('url')
    if (!targetUrl) {
      return new Response('Missing ?url parameter', { status: 400 })
    }

    const decodedTarget = decodeURIComponent(targetUrl)
    const upstreamUrl = new URL(decodedTarget)
    const ref = url.searchParams.get('ref') || upstreamUrl.origin
    const ua = url.searchParams.get('ua') || DEFAULT_UA

    info(`→ ${upstreamUrl.hostname}${upstreamUrl.pathname.substring(0, 60)}`)

    const upstreamResponse = await fetch(upstreamUrl.toString(), {
      headers: {
        'User-Agent': ua,
        'Referer': ref.startsWith('http') ? ref : `https://${ref}/`,
        Origin: upstreamUrl.origin,
      },
    })

    if (!upstreamResponse.ok) {
      warn(`↑ ${upstreamResponse.status} from ${upstreamUrl.hostname}`)
      const body = await upstreamResponse.text().catch(() => '')
      return new Response(body || `Upstream error: ${upstreamResponse.status}`, {
        status: upstreamResponse.status,
        headers: {
          'content-type': upstreamResponse.headers.get('content-type') || 'text/plain',
          'access-control-allow-origin': '*',
          'access-control-allow-headers': '*',
        },
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
      const originalLength = text.length

      // Rewrite every non-comment line (relative → absolute → proxy)
      text = text.replace(/^([^#][^\r\n]*)/gm, (line) => {
        line = line.trim()
        if (!line) return line
        if (line.includes('/proxy?url=')) return line

        let absolute: string
        try {
          absolute = new URL(line, baseUrl).toString()
        } catch {
          return line
        }

        const proxyUrl = new URL(request.url)
        proxyUrl.searchParams.set('url', absolute)
        try {
          proxyUrl.searchParams.set('ref', new URL(absolute).hostname)
        } catch {}
        return proxyUrl.toString()
      })

      // Rewrite EXT-X-KEY URIs
      text = text.replace(/(URI=["'])([^"']+)(["'])/gi, (match, pre, uri, post) => {
        let absolute: string
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
      })

      const rewrittenLines = text.split('\n').length
      info(`  manifest ${originalLength}B → ${rewrittenLines} lines`)

      return new Response(text, {
        headers: {
          'content-type': 'application/vnd.apple.mpegurl',
          'access-control-allow-origin': '*',
          'access-control-allow-headers': '*',
          'cache-control': 'public, max-age=5',
        },
      })
    }

    // ─── Segment / Binary response ───
    info(`  segment ${contentType || 'application/octet-stream'}`)
    return new Response(upstreamResponse.body, {
      headers: {
        'content-type': contentType || 'application/octet-stream',
        'access-control-allow-origin': '*',
        'cache-control': 'public, max-age=30',
      },
    })
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      warn(`error: ${msg}`)
      return new Response(`Proxy error: ${msg}`, {
        status: 500,
        headers: { 'access-control-allow-origin': '*', 'content-type': 'text/plain' },
      })
    }
  },
})

console.log(`\n  ${'='.repeat(40)}`)
console.log(`  \x1b[33mHLS Proxy\x1b[0m running on \x1b[1mhttp://localhost:${port}\x1b[0m`)
console.log(`  ${'='.repeat(40)}\n`)
