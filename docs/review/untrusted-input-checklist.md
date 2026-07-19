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
where the same classes below surfaced across **nine** Codex rounds because no
pre-open lens covered them. Fold them in the first push instead.

Each item: the check, the failure it prevents, and the fix pattern. Cite `file:line`.

---

## 1. SSRF / fetch-destination control

- [ ] **Scheme allow-list** — only `http`/`https`; reject everything else.
- [ ] **Resolve + reject non-public IPs** — resolve the host (non-blocking) and reject
      if **any** resolved address is non-global. `not ipaddress.ip_address(x).is_global`
      is the base check (covers loopback / RFC1918 / link-local `169.254` incl.
      cloud-metadata / CGNAT `100.64/10` / unspecified), but it is **not sufficient alone**:
      also reject **`is_multicast`** and **`is_reserved`** explicitly — `is_global` can be
      *true* for special-purpose IPv6 whose `is_reserved` is true. And for **IPv6 that
      embeds an IPv4** — IPv4-mapped `::ffff:a.b.c.d`, IPv4-compatible `::a.b.c.d`, NAT64
      `64:ff9b::/96` — extract the embedded IPv4 (`.ipv4_mapped`, prefix-detect the others)
      and re-run the full public-check on it, or an attacker reaches internal IPv4 through a
      "global" IPv6 literal. Validate **every** A/AAAA record, not just the first.
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
- [ ] **Reject an incomplete/truncated compressed stream** — a truncated gzip/deflate body
      can `decompress()`+`flush()` to *partial* output without raising, leaving
      `decompressor.eof` False. Check `eof` (and `unconsumed_tail`) after flushing and fail
      closed, or you feed a silently-truncated page to the extractor.
- [ ] **No catastrophic-backtracking regex on untrusted input (ReDoS)** — a pattern with
      `.*`/`[^x]*` scanning HTML/text can go **quadratic** on a pathological payload (e.g.
      `"<script " * n` with no `>`), exhausting the CPU of the process (which here also runs
      the roast controller). Use linear `str.find`/single-pass scanners for tag/markup
      stripping, not backtracking regex; if a regex stays, prove it can't rescan a
      no-terminator suffix.
- [ ] **Bound the work count** — redirect chains, retries, per-item loops all have limits.
- [ ] **Cap the INBOUND request body too, not just the fetched response** — the response
      cap protects against a hostile *upstream*, but a new external-input endpoint also takes
      a client request; on a direct ASGI/Uvicorn deployment with no fronting proxy, an
      oversized/deeply-nested JSON or form body is buffered + parsed before your handler runs.
      **Enforce it BEFORE the framework parses the body** — a check inside the route handler is
      too late for a FastAPI endpoint with a Pydantic body param (the framework buffers +
      deserializes first). Use ASGI middleware (inspect `Content-Length` / bound the receive
      stream) or a server limit (`uvicorn --limit-max-request-size`-equivalent), plus a parse-
      depth bound.
- [ ] **Concurrency bound that REJECTS, not one that queues** — a plain `async with
      semaphore:` *waits* when exhausted, so callers pile up unbounded (memory/latency) and it
      never returns 429. Use a **non-blocking / bounded acquire** (`locked()` check, or acquire
      within a short `asyncio.timeout` → 429 on failure) on any billable/expensive endpoint, so
      excess load is rejected rather than accumulated.
- [ ] **A rate/spend bound is NOT the same as a concurrency bound** — rejecting only *while
      another call is in flight* still lets a client serially fire back-to-back requests (burning
      provider spend) as fast as each finishes. A billable endpoint needs a rate/spend limit
      (token bucket / N-per-window) in addition to the concurrency cap; if that's an app-wide
      policy decision (e.g. an unauthenticated LAN tool), name it and track it, don't silently
      leave it uncapped.

## 4. Fail-soft — no unhandled exception becomes a 500

- [ ] **Every parse/convert/decode on untrusted input maps to a typed error** the route
      handler translates (e.g. → `BeanFetchError`/`BeanExtractionError` → 422), never an
      unhandled 500. Audit **every** `urlsplit`/`urljoin`/`httpx.URL`/`.port`/`int()`/
      `ipaddress.ip_address`/`getaddrinfo`/`.decode(charset)`/provider-SDK call in the path.
      Two parsers rarely agree — `urlsplit` accepts inputs `httpx.URL` rejects (NUL in path →
      `httpx.InvalidURL`); `getaddrinfo` raises `UnicodeError` (not `OSError`) on un-IDNA-able
      hosts; `bytes.decode(response_charset)` raises `LookupError` on an unknown charset name
      (`errors="replace"` does NOT cover an unknown *codec*). **Don't assume a library
      pre-validates** — verify it, or guard the call; a class-sweep that *asserted*
      `response.encoding` couldn't raise was wrong, and shipped a 500.
- [ ] The module's fail-soft **docstring promise matches reality** — if it says "never
      raises an unhandled exception", prove it with a test per escape path.
- [ ] **Distinguish client-input errors (4xx) from dependency failures (5xx).** A parse/decode
      failure on attacker-influenced input is a **4xx** (bad request). But a provider timeout,
      rate-limit, credential rejection, or outage is an **operational** failure — return
      **502/503**, not 4xx; misclassifying an outage as "bad input" hides real incidents and
      lies to the caller. Map by *origin of the failure*, not by "any exception → 4xx".
- [ ] **Use a SAFE deserializer for every untrusted format.** SSRF/fail-soft/resource checks
      all pass while the parser itself executes code or fetches entities: `yaml.safe_load` (never
      `yaml.load`), `defusedxml` / external-entities-disabled for XML (XXE), **never `pickle`/
      `marshal`/`eval`** on untrusted bytes. Name the parser and confirm it's the safe variant.

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
- [ ] **CPU-heavy synchronous parsing counts as contention too, not just provider calls.**
      The same process runs the roast controller, so a synchronous HTML/markup/decompression
      parse (see the ReDoS + compression-bomb items in §3) blocks the event loop and can stall
      the advisor tick even with no provider call involved. Keep such parsing linear/bounded,
      and for anything genuinely heavy run it off the loop (thread/executor) — the §3 resource
      caps and this contention guard are the same concern viewed from the roast loop.

## 7. LLM prompt-injection & tool boundary

When fetched/decoded **attacker-controlled content** (a vendor page, a webhook payload)
flows into an LLM prompt, the content is untrusted *instructions*, not just data:

- [ ] **The LLM path has no write tools / no privileged actions.** A prompt-injected page
      must not be able to make the model call a tool, mutate state, or reach the roaster —
      the advisor invariant (advisor never gets MCP write tools) applies here too. The
      extraction agent returns typed data only.
- [ ] **Treat the model's output as untrusted** — it can be steered by injected page text, so
      the same normalization + provenance-verification (class 5) + human review gate the value
      before it is used. An injected "origin: Jamaica Blue Mountain" is caught by the
      evidence/containment check, not trusted because the model said so.
- [ ] **Don't concatenate page text into a system/instruction role** — keep fetched content in
      a clearly-delimited data slot, never where it reads as operator instructions.

## 8. Invariant separation

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
