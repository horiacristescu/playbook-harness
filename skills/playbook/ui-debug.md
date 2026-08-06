# UI Debug — Playwright Reference

## Starter Template

Copy to `ui/e2e/debug-<name>.spec.ts` and adapt:

```typescript
import { test, expect } from "@playwright/test";

test("debug: <describe what's broken>", async ({ page, request }) => {
  // 1. Prefetch API — is server data correct?
  const res = await request.get("http://localhost:5173/api/<endpoint>");
  const data = await res.json();
  console.log("API:", JSON.stringify(data, null, 2));

  // 2. Script app to broken state
  await page.goto("http://localhost:5173/<route>");
  await page.waitForSelector("<container>", { timeout: 5000 });
  await page.waitForTimeout(500);

  // 3. Probe DOM — collect actual state
  const items = await page.$$eval("<selector>", (els) =>
    els.map((el, i) => ({
      index: i,
      className: el.className,
      bg: getComputedStyle(el).backgroundColor,
      text: el.textContent?.substring(0, 60) ?? "",
    }))
  );
  for (const item of items) console.log(item);

  // 4. Screenshot
  await page.screenshot({ path: "/tmp/debug-<name>.png", fullPage: true });

  // 5. Diagnose — add assertions after root cause is found
});
```

Run: `cd ui && npx playwright test e2e/debug-<name>.spec.ts --reporter=list`

## Key Techniques

- **Bulk DOM inspection:** `page.$$eval(".selector", els => els.map(el => ({ className: el.className, bg: getComputedStyle(el).backgroundColor })))`
- **Single element style:** `page.locator(".selector").evaluate(el => getComputedStyle(el).backgroundColor)`
- **Debug data attributes:** Add `data-*` to rendered elements to expose render-time values (indices, flags, state). Read back with `el.getAttribute("data-my-attr")`. This caught React StrictMode double-invocation in task 108 — `data-gate-idx` showed 1,3,5 instead of 0,1,2.
- **API prefetch in test:** `request.get("http://localhost:5173/api/...")` — verify server data before blaming the frontend.
- **Scripting interactions:** Click sidebar items, scroll to elements, wait for WebSocket updates, type into inputs — Playwright is a full browser.
- **Screenshot:** `page.screenshot({ path: "/tmp/debug.png", fullPage: true })`

## Reference Example

`ui/e2e/task-highlight.spec.ts` — converted from task 108 debug session. Shows: API prefetch to find a task with pending gates, DOM probe for gate classes and computed background, assertion that exactly one gate has `gate-current` with amber background.
