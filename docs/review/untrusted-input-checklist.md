# Untrusted-input hardening checklist

**When this applies.** Any change that, on the server, does one or more of:

- **fetches a URL / opens a connection** whose target is influenced by operator or
  external input (a pasted URL, a redirect `Location`, a webhook, a config value);
- **parses or decodes untrusted bytes/strings** (URL parsing, HTML/JSON/charset
  decode, number/port parsing, deserialization);
- **calls the shared LLM/model provider** from a *new* code path (anything that can
  contend with the roast advisor for the same backend);
- accepts a new **external-input endpoint** (a route taking client-supplied data).

If a diff matches, run this list pre-open and route it to **`security-reviewer`**.
This checklist is the codified retro of PR #587 (the bean-sourcing fetch endpoint),
where the same seven classes below surfaced across **five** Codex rounds because no
pre-open lens covered them. Fold them in the first push instead.

Each item: the check, the failure it prevents, and the fix pattern. Cite `file:line`.

---

## 1. SSRF / fetch-destination control

- [ ] **Scheme allow-list** — only `http`/`https`; reject everything else.
- [ ] **Resolve + reject non-public IPs** — resolve the host (non-blocking) and reject
      if **any** resolved address is non-global. Use `not ipaddress.ip_address(x).is_global`
      (covers loopback / RFC1918 / link-local `169.254` incl. cloud-metadata / CGNAT
      `100.64/10` / reserved / unspecified) **plus an explicit `is_multicast`** (which
      `is_global` does *not* exclude). Validate **every** A/AAAA record, not just the first.
- [ ] **Revalidate every redirect hop** — keep `follow_redirects=False`, follow manually,
      re-run the full destination check on each `Location`, bound the hop count.
- [ ] **Pin the validated IP into the connection** (DNS-rebinding / TOCTOU) — connect to
      the validated IP literal (preserve `Host` + TLS SNI), or a socket-level custom
      transport, so the connection can't re-resolve to a poisoned address between check
      and connect. (Live 2026 CVE class: CVE-2026-27826, Prefect #21591.)
- [ ] **Disable env proxies** on the pinned client (`trust_env=False`) — an `HTTPS_PROXY`
      CONNECT tunnel bypasses the pin and TLS-verifies against the wrong host.
- [ ] **Prefer an allow-list of expected hosts** where the input domain is small
      (defence-in-depth over the denylist).

## 2. Secret / PII hygiene

- [ ] **Reject `userinfo@` and URL `#fragment` before any logging, fetch, or storage** —
      both can carry credentials/tokens (`user:pass@`, `#access_token=`). Log only a
      **redacted** URL; never persist the raw value in a field returned to the client.
- [ ] No secret (API key, token, credential) reaches a log line, an error message, or a
      response body.

## 3. Resource exhaustion / DoS

- [ ] **End-to-end deadline** — wrap the whole operation (all hops + body + any provider
      call) in one `asyncio.timeout`, *in addition* to per-op transport timeouts (per-op
      can't stop a slow-drip body).
- [ ] **Cap the response body** — enforce a byte cap **before** growing the buffer.
- [ ] **Bound decompression** — a compressed body decodes to far more than its wire size.
      Stream **raw** bytes with a raw cap, decompress with a bounded decoder
      (`zlib.decompressobj(...).decompress(data, max_length)`), fail closed past the cap.
      Constrain `Accept-Encoding` to codecs you can bound.
- [ ] **Bound the work count** — redirect chains, retries, per-item loops all have limits.
- [ ] **Concurrency bound** on any billable/expensive endpoint (semaphore → 429), so a
      caller can't fan out unbounded paid calls.

## 4. Fail-soft — no unhandled exception becomes a 500

- [ ] **Every parse/convert/decode on untrusted input maps to a typed error** the route
      handler translates (e.g. → `BeanFetchError`/`BeanExtractionError` → 422), never an
      unhandled 500. Audit **every** `urlsplit`/`urljoin`/`httpx.URL`/`.port`/`int()`/
      `ipaddress.ip_address`/`.decode()`/provider-SDK call in the path. Two parsers rarely
      agree — `urlsplit` accepts inputs `httpx.URL` rejects (NUL in path → `httpx.InvalidURL`).
- [ ] The module's fail-soft **docstring promise matches reality** — if it says "never
      raises an unhandled exception", prove it with a test per escape path.

## 5. Data-contract & normalization consistency

- [ ] **Normalize every extracted value before it is used** — tag provenance, feed a
      required-field check, or build a model *after* stripping/normalizing, not on the raw
      value (a whitespace-only string is truthy and silently wins fallbacks or falsely
      reads as "present").
- [ ] **Tri-state honesty** — if "absent" and "explicitly false/empty" must be
      distinguishable (provenance, honest imputation), the field is nullable, not a
      defaulted bool/str.

## 6. Cross-feature contention (safety-adjacent — escalate to `safety-reviewer` too)

- [ ] **A new provider-calling path must not be able to run during an active roast** (or
      must not contend with advisor availability). The roast loop's advice calls time out
      and a few consecutive failures trip the safety fallback — a background extraction on
      the same backend can starve it. Guard on the active-run signal (→ 409), and make the
      guard **race-free**: check + do the work under the same lock the roast-start path uses,
      or a concurrent start can interleave after the check.

## 7. Invariant separation

- [ ] An external-input module stays **outside the safety envelope** — no
      `controller`/`safety`/`mcp_client` imports (assert with a direct + transitive
      import test). It returns typed data; it never touches the roaster.

---

### How this is enforced

- **`pr-preflight` step 4** triggers this checklist + `security-reviewer` when the diff
  matches the "When this applies" test — pre-open, so findings fold into the first push.
- **AGENTS.md routing** is capability-based (fetch/parse untrusted input, or a new
  provider-calling path), not just file-based — because #587 touched none of the
  file-based triggers yet was the highest-risk surface in the batch.
- The goal is **findings-caught-pre-open trending up**; a reviewer still catching a real
  defect post-open is the system working, not a regression.
