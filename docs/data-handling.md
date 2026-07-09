# Data handling & privacy

This page answers the question every records or security team eventually asks:
**"If we connect this to Claude, what does Claude actually see, and where does
our document content go?"**

It's written to be shared with a non-technical stakeholder and to hold up to a
technical review. Nothing here is marketing — where a claim depends on your
Anthropic plan or agreement, we say so and point you to the source.

## The short version

- The extension **does not upload, index, sync, or crawl** your repository.
  Nothing is copied into a database or sent anywhere in bulk.
- It fetches **only the specific entries a query needs**, and **only what your
  configured service account has permission to see**. Read-only by default.
- To reason over a document, Claude **does send that document's content to
  Anthropic's API** — the model runs in Anthropic's cloud, not on the machine.
  This is inherent to using any cloud AI assistant.
- What Anthropic may do with that content **depends on your plan/agreement**.
  Under commercial (API / Team / Enterprise) terms, inputs are not used to train
  models; consumer plans differ. Verify your own terms — see [below](#what-anthropic-does-with-it).
- Your strongest control is the **service account**: scope it to only the folders
  you're willing to expose, and Claude physically cannot reach the rest.

## How the data actually flows

```
You ask a question in Claude
        │
        ▼
Claude decides which tool to call (e.g. "search", "read this entry")
        │
        ▼
laserfiche-mcp  ──►  your Laserfiche Repository API   (runs LOCALLY on the
(local process)      (your server, your credentials)   user's machine; direct
        │                                               connection, no middleman)
        ▼
Tool result (the specific entries / fields / extracted text)
        │
        ▼
Sent to Anthropic's API so the model can read it and answer  ◄── content leaves
                                                                  the machine HERE
```

Two facts sit side by side, and honesty requires stating both:

1. **The connection is local and permission-scoped.** The server runs on the
   user's own machine and talks straight to your Laserfiche server with the
   credentials you gave it. No third-party service sits in the middle, and no
   copy of your repository is made.

2. **The content Claude reasons over transits to Anthropic.** "The server is
   local" does *not* mean the data stays local. Whatever a tool returns for a
   given question — search hits, field values, extracted document text — is sent
   to Anthropic's API to run the model. Anyone technical knows this, so we state
   it plainly rather than imply local-only privacy.

## What Claude sees — and doesn't

| | |
|---|---|
| **Pull-based, per-query** | It retrieves only what a specific question needs. Ask about one lease, it fetches that one lease. It never holds the repository. |
| **Permission-scoped** | It inherits exactly the service account's Laserfiche permissions — no more. Folders that account can't open, Claude can't open. |
| **Read-only by default** | It can search and read; it cannot change, move, or delete anything unless an administrator deliberately enables write mode. |
| **Not an index** | Nothing is ingested, embedded, cached to disk, or uploaded ahead of time. There is no background sync. |
| **No bulk export** | There is no "download everything" path. Document reads are capped (`LF_EDOC_MAX_BYTES`, default 25 MB) and results are paginated. |

So the common fear — *"Claude is now looking through all our documents"* — is not
how it works. It sees a specific document only when a person asks a question that
requires that specific document, and only if the service account is allowed to.

## What Anthropic does with it

This depends on **which Claude product and plan** the person is using, and it
changes over time — so don't take a vendor-neutral summary as your compliance
answer. Read the primary sources for *your* agreement:

- Anthropic [Privacy Center](https://privacy.anthropic.com/) and
  [Commercial Terms of Service](https://www.anthropic.com/legal/commercial-terms)
- For an organization: your Data Processing Addendum (DPA)

The general shape, to be verified against your own terms:

- **Commercial (API / Team / Enterprise):** inputs and outputs are **not used to
  train** Anthropic's models, and **Zero Data Retention** is available for
  qualifying commercial use.
- **Consumer (Free / Pro / Max):** different terms, with training opt-in/out
  controls that have changed over time. If sensitive records are in scope, a
  personal Pro/Max subscription is the wrong vehicle — use a commercial or
  Enterprise agreement so the data terms are contractual.

> [!NOTE]
> This project is a community MCP server. It has no visibility into and makes no
> promises about Anthropic's data practices — those are governed by the agreement
> between you and Anthropic. This page explains what the *software* does; the rest
> is your contract with your AI provider.

## What you control (the mitigations that matter)

Ordered by how much protection they give:

1. **Scope the service account — the strongest control.** Create a Laserfiche
   service account whose permissions cover **only** the folders you're willing to
   expose to AI, and point the extension at that account (`LF_USERNAME` /
   `LF_PASSWORD`). Claude then *physically cannot* retrieve anything outside that
   scope — it's enforced by Laserfiche's own access control, not by trust or by
   this software. Keep your sensitive/regulated records outside that account's
   reach and they never enter the picture.

2. **Stay read-only.** The default (`LF_READ_ONLY=true`) means nothing can be
   changed or deleted. Only turn it off with the write-mode fences in place — see
   the [Safety model](../README.md#safety-model).

3. **Segment by repository.** Don't connect your most sensitive repositories if
   they don't need AI access. Connect a purpose-built one.

4. **Choose the right AI agreement.** For anything regulated (PHI, privileged
   legal material, regulated PII), use a commercial/Enterprise Anthropic
   agreement — and consider Zero Data Retention — rather than a consumer plan.

5. **Know when the answer is "not this."** For the most sensitive content, the
   honest position is that a cloud LLM may not be appropriate at all. Keeping that
   content out of the service account's scope (control #1) is how you enforce that
   decision technically, not just by policy.

## A paragraph you can hand to a stakeholder

> The Laserfiche Assistant does not upload or index our repository. It fetches
> only the specific document needed to answer a specific question, only what its
> configured service account is permitted to see, and read-only. We point it at a
> service account scoped to just the folders we've chosen to expose, so it cannot
> reach our sensitive records at all. The content Claude reasons over is sent to
> Anthropic's API to run the model — that is true of any AI assistant — and under
> our commercial/Enterprise agreement that content is not used to train models.
> For our most sensitive records, we simply keep them outside the service
> account's scope.

## Related

- [Safety model](../README.md#safety-model) — write-mode guardrails
- [SECURITY.md](../SECURITY.md) — reporting a vulnerability
- [Desktop extension guide](desktop-extension.md) — install & configuration
- [Remote HTTP & OAuth](remote-http.md) — per-user auth for web deployments
