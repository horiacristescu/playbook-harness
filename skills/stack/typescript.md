# TypeScript

- **TypeScript** `strict: true` + `noUncheckedIndexedAccess` + path aliases (`@/*`)
- **Vite** — build/dev
- **Svelte** — UI (unless a strong reason for React: unsupervised agent work or ecosystem)
- **Zod** — validation at boundaries ⚑
- **Biome** — format + lint ⚑
- **Vitest** — unit/component tests
- **Playwright** — browser/e2e tests
- **SPA** — unless you actually need SSR
- **Tailwind v4** — if you want a system ⚑

Layout: `web/` with its own `package.json`.

⚑ Pin in AGENTS.md (post-cutoff): **Svelte 5 + runes** (`$state`/`$derived`/`$effect`),
**Biome / ESLint flat config** (`eslint.config.js`), **Zod** at every boundary, **Tailwind v4**.
