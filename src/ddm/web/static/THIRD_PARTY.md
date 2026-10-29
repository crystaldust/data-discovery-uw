# Third-party static files

Served from here rather than a CDN, so the pages need no third-party origin and the
Content-Security-Policy can stay `'self'`. Files are byte-for-byte the published builds;
the SHA-256 values match jsDelivr's package listings.

| File | Package | License | SHA-256 (base64) |
|---|---|---|---|
| `pico.min.css` | `@picocss/pico@2.1.1` `/css/pico.min.css` | MIT (header in file) | `+8mmP8n8n3LRL9f8mAbhH6n3euT5ytFGsnADoRGbo9s=` |
| `htmx.min.js` | `htmx.org@2.0.11` `/dist/htmx.min.js` | 0BSD | `1v3HXyBOa976mbab8ebUrGm4o2T3eSn0XBNHa0AA9xc=` |
| `swagger/swagger-ui-bundle.js` | `swagger-ui-dist@5.17.14` | Apache-2.0 (`swagger/LICENSE`) | `wuSp7wgUSDn/R8FCAgY+z+TlnnCk5xVKJr1Q2IDIi6E=` |
| `swagger/swagger-ui.css` | `swagger-ui-dist@5.17.14` | Apache-2.0 | `QBcPDuhZ0X+SExunBzKaiKBw5PZodNETZemnfSMvYRc=` |
| `swagger/favicon-32x32.png` | `swagger-ui-dist@5.17.14` | Apache-2.0 | `PtYS9B4FDKXnAAytbxy+fn2jn2X8qZwC6Z5lkQVuWDc=` |
