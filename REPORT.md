# Design report: Computer-Use Automation System

The system turns one LLM-driven run against a legacy UI into a typed, versioned
**capability** that replays deterministically with no model in the loop. Discovery is the
expensive, occasional path; replay is the production path an AI agent invokes. Everything
below is implemented and tested (144 tests, 37 of them end-to-end against the running app) unless marked as design.

## Architecture

A single Python process, synchronous, with clear seams:

```
            discover (occasional)                         replay (production)
goal --> Planner --> discovery loop --> Recorder --> Capability --> Replayer --> ReplayResult
          (mock | Claude)    |                       (JSON file)      |
                             +------- Policy + Redactor + RunLog ------+
                             +---------------- Surface ----------------+--> the app
                                                                       +--> Operator (handoff)
```

- **Surface** (`cua/surface/`): the only code that touches the app. A small contract
  (go to a route, find a target and click/fill/select/read it, is X visible, describe the
  screen, screenshot). `WebSurface` implements it with Playwright on the **accessibility
  tree**: roles and visible names, not CSS selectors and not pixels. Accessibility trees
  survive markup changes, carry meaning ("button Search"), and also exist for desktop apps.
- **Planner** (`cua/agent/`): the only place an LLM sits. A scripted mock and a Claude
  planner share one interface; the loop can't tell them apart.
- **Recorder**: a pure function from *verified* actions to a capability; no I/O, so it is
  tested exhaustively offline.
- **Replayer** (`cua/replay/`): runs a capability with no planner, under the policy, with
  redacted evidence, returning a strict result contract.

Key trade-offs. *Python + Playwright + Pydantic*: Pydantic gives one definition for types,
runtime validation and exported JSON Schema (the format LLM tools use). *Hand-written agent loop, no framework*: every step of observe, decide, act and record is our own readable code that I can defend line by line, rather than a framework's internals. *Synchronous
single process*: simplest correct thing; the seams (surface, planner, operator, secret
store) are where scale would plug in, but no queueing or multi-tenant infrastructure is
built, by design. *A purpose-built target app* (a fake credit union with tables, no IDs, no
test IDs, unlabelled fields, and deterministic faults) so every error path is reproducible.

## Artifact schema

Two document types (`cua/schema.py`, exported to `schemas/`):

- **App profile**, one per vendor product: sign-on steps (secrets referenced by *name*,
  never value), app-wide **interstitials** (popup, expired session, outage) with their
  recovery, sensitive screen labels, and timing. Hand-written and reviewed: it is
  low-volume, touches credentials, and grows from real incidents.
- **Capability**, one per task, shaped like a function: `meta` (id, version, status,
  app profile), typed `inputs` (with pattern and sensitivity), typed `outputs`, ordered
  `steps`, declared business `outcomes`, a `success` condition, and `provenance` that links
  to the discovery run without embedding its transcript.

Each step carries a human-readable `intent`, an action, a **ranked list of locators with a
written rationale**, a mandatory `risk` (`read_only` / `reversible` / `irreversible`; no
default, so a forgotten label fails validation instead of looking safe), an optional `expect`
checkpoint, and the business outcomes possible at that step. Locator strategies, most robust
first: `role_name`, `role_in_scope` (an unnamed field inside its label's *innermost* row),
`table_cell` (row label x column header), `labelled_value` (the cell beside a label), and
`nth_of_role` (position; last-resort fallback, logged as drift when used).

Design choices: values are always placeholders (`{{inputs.member_id}}`), so no real data is
ever stored; `schema_version` (format) is separate from `meta.version` (recipe); files are
named `<id>@<version>.json` for side-by-side review; `extra="forbid"` makes a typo an error,
not a silently ignored field; cross-field validation rejects undeclared inputs, outcomes,
outputs and any `secrets.*` reference in a capability. Comparing the discovered
`member.savings_balance` with the hand-authored `member.lookup_balance` shows exactly what
review adds: business outcomes, input patterns, and generalization (`account_type` as input).

## Determinism & error handling

**Determinism.** A locator must match **exactly one** element; zero means "not yet" (re-check
every 100 ms until the timeout), two or more means ambiguous and is **refused, never
guessed**. Ranked fallbacks keep a run working while flagging drift. Every click/navigation
has a checkpoint: we never assume an action worked. Values are read by meaning: a member
whose accounts are listed in a different order is the regression test proving it.

**Result contract.** Exactly one of `success` (with outputs), `business_outcome` (stable
code, e.g. `MEMBER_NOT_FOUND`), `failed` (category, step, intent, expected vs observed,
masked screenshot, and a `retryable` flag derived from the category), or `needs_human`.
Recoveries, drift warnings and human handoffs are listed on every result.

**Taxonomy, in practice.** When a checkpoint isn't met, replay asks what *known* state
explains the screen, most specific first: this step's declared business outcomes, then the
app's interstitials. Recoverable ones (popup, expired session) are handled and the run
continues; hard ones (`app_error`) stop with evidence; nothing known means
`checkpoint_failed`. A pending page load at the deadline is a `timeout` (retryable),
distinguished from "loaded the wrong thing" by tracking in-flight navigations. Bad inputs
fail before the browser opens, without echoing the value.

**Re-verify before retry.** After any recovery or handback, the step's expectation is checked
first; if it holds, the action is **not repeated** (signing on again after a session timeout
lands on the target page; the search is not re-run). An action is repeated only if the
policy's effective risk is not irreversible. Transient failures are retried at the capability
boundary by the caller, where repeating a read-only capability is known to be safe.

**UI drift** (secondary here, since these UIs change slowly) surfaces as fallback-locator
warnings and checkpoint failures with before/after evidence. A bug found this way: a row
locator matched a page-layout table that wraps the whole panel, and worked on one screen
only by accident; it now targets the innermost matching container.

## Heterogeneity & multi-tenant

*Design; not implemented beyond the seams noted.*

**Surfaces.** The seam is the `Surface` contract plus the locator vocabulary, which describes
controls by role, name and labels rather than by markup. A **desktop surface** implements the
same operations over UI Automation (Windows) or the AX API (macOS), which expose the same
role/name tree; the artifacts and replay engine are unchanged. **Legacy web** with frames
adds frame-aware scoping inside the web surface (a scope anchor may name a frame). Apps with
**no accessibility tree** (Citrix, remote desktops) need a visual surface: OCR text anchors
plus vision-model grounding, recorded as a new, lower-ranked locator strategy with the same
exactly-one rule. Terminal (3270) screens map naturally to labelled fields. Two optional
surface hooks already exist for this (`navigation_pending`, operator capture), with safe
defaults for surfaces that can't support them.

**Reuse across tenants.** Artifacts are already tenant-neutral: routes are relative, values
are placeholders, credentials are named. Layering would be: **capability** (task) → **app
profile** per vendor product *and version range* (e.g. `harbor-member-console@4.x`) →
a small **tenant overlay** holding the base URL, secret names, allowed origins, and
**overrides** (e.g. this institution relabels "Member ID:" as "Account Number:", or hides an
account type). The effective capability is resolved at run time, so one reviewed capability
serves every tenant on that product, and a tenant-specific change is an overlay diff, not a
re-recording.

**Detecting per-tenant/version drift.** Signals already produced: fallback-locator
warnings, checkpoint failures, unexplained screens. Planned additions: a fingerprint of key
screens' labels and roles captured when a capability is approved and compared per tenant;
scheduled read-only **canary replays** per tenant-app; and aggregation by tenant and product
version, so a vendor upgrade shows up as a cluster. A drifted tenant's capability is marked
for review for that tenant only; the fix is an override or a new profile version.

## Escalation & handoff

**Detecting "stuck."** Discovery: the planner gives up (it is encouraged to rather than
guess), the same action fails three times in a row, the step budget runs out, or an action
needs approval. Replay: a step requiring approval, or a condition it can't recover from
(returned as `needs_human` / `failed` with full context for unattended runs).

**Control model.** A **control lease** has exactly one holder: automation, *awaiting an
operator*, an operator, or ended; only listed transitions are legal, and every change is
logged with who and why. Automation checks the lease before **every** action. Crucially, the
lease is also enforced **in the browser**: while nobody holds control, the page is shielded
and the request guard blocks every page load, so nothing can be committed by accident.

**Handoff.** The operator receives a request (capability, step, intent, reason, route, masked
screenshot) and chooses **approve** (automation performs the step, the maker-checker pattern),
**take control** (the person works in the *same live session* while their actions are
recorded: which control, by role and name; typing as a character count, never the value),
or **reject**. On handback, re-verify-before-retry applies, so an operator's Confirm is
never repeated. The operator UI is a minimal console (`--operator console`); a scripted
operator drives the tests. A production console (web UI, work queue, streamed browser) would
be another implementation of the same `Operator` interface.

**Found by doing it by hand.** Three defects the automated tests didn't show: the browser
froze while the console waited for input (the request guard runs in Python), a crash after
handback left an irreversible action's outcome unreported, and a click during review went
through once control changed hands. Each is fixed (responsive waiting, safe handback checks,
in-browser lease enforcement) and covered by a regression test, with the bank's own data,
not our logs, proving no account is ever double-created.

## Safety

- **Allowlist, default-deny** (`policy.yaml`): origins, routes (segment-aware globs, so `*`
  never crosses `/`) and action types; anything unlisted is refused, so `/logout` needs no rule.
- **Two enforcement layers**: every step is checked before acting, and every browser request
  is checked in Playwright's network layer, catching unpredicted links and redirects.
- **Risk handling**: irreversible actions require human approval (block would make the work
  impossible; flagging would be too late). Risk can only be **raised**: a click whose visible
  name says Confirm, Submit, Transfer or Delete is treated as irreversible whatever the
  artifact claims, so an artifact can't talk its way out of approval.
- **The planner's vocabulary is restricted**: no position targets, no `secrets.*`, a stated
  risk on every click, and explicit `finish`/`give_up`. It never sees credentials (sign-on
  happens first) or real values (redacted screens, input placeholders).
- **Redaction** in three layers: registered values by sensitivity (secrets matched anywhere,
  identifiers masked as whole tokens), labelled screen fields, and patterns (SSN, card, date,
  money, email). It is applied **at the sink**, the one class all logging goes through, and
  to everything sent to the LLM; screenshots are masked. Secrets are never caller inputs.
- **Self-checks**: the evidence generator scans every file it writes for raw values; it
  caught a member ID in hand-written index text, and its own ordering bug.

**Limits.** Keyword-based risk raising has false positives (by design) and can miss
dangerous controls with innocuous names. Redaction can miss unlabelled free text in an
unusual format. The browser shield is a courtesy (the request guard is the enforcement), and
nothing stops actions outside the browser. Evidence retention and access control are out of
scope here. Credentials come from environment variables, a stand-in for a vault.

## Cuts

**Deliberately left out, and why.** Multi-tenant overlays, desktop and visual surfaces,
and frames: designed above, not built, as the brief prefers sound seams over infrastructure.
A production operator console: a console UI makes the control-transfer model real at a
fraction of the cost. **The live Claude planner was not run against the real API**: it is
implemented and tested offline with a fake client; all evidence uses the screen-aware mock,
and I'd want real-model runs before claiming discovery quality. Live handoff is wired into
replay; discovery produces the same intervention requests but resolves them unattended.
Interruption handling is duplicated between discovery and the replay engine, chosen over
restructuring a tested engine late.

**Next, in order.** Real-model discovery runs and a comparison against the hand-authored
artifacts. Automatic outcome probing: replay a draft with probe inputs (unknown ID, bad
format) and propose the error texts as business outcomes for review. A draft → approved
workflow with replay stability scoring (run N times). Isolating every live test with its own
app instance. A shared session layer for interruption handling. Then the tenant overlay and
a desktop surface behind the existing contract.