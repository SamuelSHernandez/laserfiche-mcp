# UX principles for this project

Working notes, not a public doc. We're not an application and barely an
interface — most of what a person experiences here is a terminal prompt,
a four-field form, or Claude's own chat window. That's exactly why this
matters: there's no visual polish to hide behind, so every remaining
decision (what we ask for, when, in what order, and what we do with the
answer) carries more weight per interaction, not less. The person setting
this up, the person chatting with Claude day to day, and — a genuinely
unusual "user" worth naming — the model itself reading tool docstrings and
error messages, are all first-class citizens of this design, not an
afterthought bolted onto a working backend.

Source: [lawsofux.com](https://lawsofux.com/). Each entry below: the law,
what it concretely means in this codebase (with a real example, not a
hypothetical), and a check question to run any new friction-point proposal
through before we build it.

See also: [`TESTING_PLAN.md`](TESTING_PLAN.md) for the verification side of
the current work; this doc is the design lens, that one is the confidence
lens.

---

## How to use this

When a new "reduce friction" idea comes up, run it through the relevant
checks below *before* proposing an implementation. The web_url redesign is
the worked example throughout — first proposal (diff two pasted links)
failed the Tesler's Law and Occam's Razor checks; the corrected version
(system already knows the anchor entry, user supplies only the one URL)
passes both. If a proposal doesn't have a good answer to at least one
check, it's probably solving the wrong problem, the way the first web_url
proposal was.

---

### Tesler's Law (conservation of complexity)
*"For any system there is a certain amount of complexity which cannot be
reduced — it can only be moved."*

**Here:** every setup field represents complexity that has to live
*somewhere*. The question is never "how do we eliminate this," it's "which
side of the interaction already holds this information, and does our
design put the burden there instead of on the person." The web_url feature
needs to correlate a specific entry to a specific URL — that correlation
can't be skipped. It can be done by the system (which already has a
verified connection and can pick a known entry) instead of the user
(who'd have to recall or look something up).

**Check:** for this ask, who already holds the information — us or them?
If it's us, we shouldn't be asking.

---

### Postel's Law
*"Be liberal in what you accept, and conservative in what you send."*

**Here:** already real in this codebase, not aspirational —
`_url_problem` accepts any `http(s)://host` shape without demanding an
exact path, `_env_quote`/`_dotenv_unstorable` absorb passwords with
special characters instead of rejecting them, and the API-version prompt
normalizes `v1`/`V1`/`1` to the same value. Every new input field should
inherit this by default: trim whitespace, tolerate a missing scheme,
strip stray quote marks a browser's copy added — don't make the one thing
we're asking for also need to be pasted perfectly.

**Check:** does this field reject anything a reasonable person would
plausibly paste, that we could just as easily normalize instead?

---

### Doherty Threshold
*"Productivity soars when a computer and its user interact at a pace
(<400ms) that ensures neither has to wait on the other."*

**Here:** `_run_diagnose`'s docstring already reasons about this by name
without citing the law — probes run with `retry_attempts=0` specifically
because the default exponential backoff would turn an unreachable server
into "minutes of silence before the UNREACHABLE line ever printed."
Anything we add to the setup path (e.g. auto-picking a sample entry to
anchor the web_url ask) inherits this constraint: one cheap call, not a
walk, especially right before we're about to ask the person for something.

**Check:** does this step have a bounded, fast worst case — and if not,
does it say something while it waits?

---

### Peak-End Rule
*"People judge an experience largely based on how they felt at its peak
and at its end."*

**Here:** this is the argument for prioritizing the `.mcpb` diagnostic gap
over smaller polish. For someone with a wrong password, the *end* of
their first encounter with this tool is currently a bare tool-call error
inside a Claude chat, instead of `diagnose`'s deliberately-built advice
(which actively re-probes the other API version and names the fix). No
amount of polish earlier in the flow outweighs a bad last moment — so that
gap gets fixed before further refining steps that already work.

**Check:** if this flow fails, what's the *last* thing the person sees —
and is it as good as the best thing we know how to say?

---

### Zeigarnik Effect
*"People remember uncompleted or interrupted tasks better than completed
tasks."*

**Here:** the setup wizard's cancel path — *"Setup cancelled — nothing
was written"* — is doing real work by closing the loop explicitly. Without
it, a person who Ctrl-C's out is left holding an open question ("did that
half-save something wrong?") that lingers longer than if they'd never
started. Every place we can interrupt should say, plainly, what state
things are actually left in.

**Check:** if the person stops here, do they know for certain whether
anything changed?

---

### Jakob's Law + Mental Model
*"Users spend most of their time on other [products]"* and bring a
*"compressed model of what we think we know about a system"* with them.

**Here:** the wizard asks for "Repository name or ID (the repository you
pick when signing into Laserfiche Web Access)," not `LF_REPOSITORY_ID`.
It's borrowing vocabulary the person already has from a Laserfiche
product they've used, instead of teaching new jargon for the same
concept. Same
reasoning applies to `env`-var config files, a `setup`/`diagnose`
subcommand split, and `.env` — all patterns borrowed from the wider dev
tool ecosystem rather than invented fresh.

**Check:** does this reuse a word/pattern the person already has from
Laserfiche itself or from other dev tools, or are we introducing new
vocabulary for something they already have a name for?

---

### Occam's Razor
*"Among competing hypotheses that predict equally well, the one with the
fewest assumptions should be selected."*

**Here:** the formal name for the correction on the first web_url
proposal. Diffing two pasted links and reverse-engineering the system's
already-known-entry version both solve the same problem; the second one
assumes less (no need for the person to find two documents, no diff
edge cases to reason about) and should win on that basis alone, independent
of either being "clever."

**Check:** if there are two designs that solve this equally well, are we
picking the one with fewer moving parts, or the more interesting one?

---

### Hick's Law
*"The time it takes to make a decision increases with the number and
complexity of choices."*

**Here:** the README forks immediately into two paths — "for everyone"
(`.mcpb`, no terminal) vs. "for developers" (`uvx`/`pip`) — rather than
presenting one page of every install option and letting the reader sort
it out. The `.mcpb` form itself holds to 4 fields. Any new setup question
should default to invisible/optional (skippable, auto-resolved) rather
than adding a fifth visible decision to a flow that currently has four.

**Check:** does this add a new decision the person has to make, or does
it resolve automatically with an escape hatch only if it's wrong?

---

### Goal-Gradient Effect
*"The tendency to approach a goal increases with proximity to the goal."*

**Here:** the wizard's own structure — question → question → *Saved.
Checking the connection...* → *You're connected. Try:* — is already a
small momentum arc even without an explicit progress bar. Five questions
is short enough that a literal "step 2 of 5" indicator would likely be
overhead (see Miller's Law: within working-memory range without help).
Worth re-checking only if the flow grows past what someone can hold in
their head unaided.

**Check:** does the person doing this get a visible sense of getting
closer, right up to the actual finish?

---

## Open question

Should this checklist gate every future onboarding change, or just the
non-obvious ones? Leaning toward: run it explicitly for anything touching
`setup`/`diagnose`/the `.mcpb` form, skip the ceremony for pure bug fixes.
