# Miriam Memory — Per-Person Architecture

Status: implemented.
Scope: how long-term memory is scoped, written, grounded, and read, and why one
person must end up with **one connected memory**, not several clusters.

---

## 1. The problem this fixes

A person's memory was landing in one container but many **disconnected
documents**, so the graph read as several "Tobiloba" clusters instead of one
memory. Three independent causes:

1. **A new document per web session.** The chat path keyed Supermemory ingest to
   the client's `conversation_id`. A client that starts a new conversation each
   session handed Supermemory a new document each time.
2. **The busiest surface wrote nothing.** `/chat/stream` returned its `done`
   event without persisting the turn or ingesting it. Whichever surface the UI
   used, one of them was silent.
3. **The texting channel wrote nothing.** `/chat/spectrum` (iMessage / WhatsApp /
   terminal) only wrote to the local SQL store. Nothing reached the graph.

Isolation was never the bug: `container_tag_for(user_id)` has always mapped one
person to one container. Cohesion was.

---

## 2. The model

Two boundaries, doing different jobs:

| Boundary | Identifier | Job |
|---|---|---|
| Container tag | `container_tag_for(user_id)` | **Isolation.** Tobiloba's memory is unreachable from John's. Never changes per session. |
| Document / conversation id | `conversation_scope_for(user_id, channel)` | **Cohesion.** One document per person per channel, so dreaming links a person's facts instead of seeing a new stranger each session. |

Container grounding (`person_entity_context`) is set once per container with
`entityContext` plus the person's display `name`:

- `entityContext` is what stops extraction drifting on unanchored pronouns.
  Without it, "I'm saving for a house" is a fact about nobody in particular.
  Supermemory reads it while processing documents in the tag, so it grounds
  every write, not just the one it was set from.
- `name` is what the console shows instead of a raw user id.

This is the same shape the reference products use: Supermemory (one container
per person + `entityContext` + one profile), Mem0 (entity scope
`user_id`/`agent_id`/`run_id` + profile + graph links), Zep (one temporal
context graph per entity + per-user summary). In all three, the isolation scope
is stable and **one** entity maps to **one** graph.

### Scopes in use

| Channel | Scope |
|---|---|
| Web chat / money turns | `miriam:web:<container>` |
| Conversational onboarding | `miriam:onboarding:<container>` |
| Spectrum gateway | `miriam:<channel>:<container>` (`imessage` / `whatsapp` / `terminal`) |

`conversation_scope_for` sanitises the channel and derives the trailing token
from `container_tag_for`, so a scope can never cross the isolation boundary.

---

## 3. Write paths

Every surface that talks to a person writes to the same container.

| Surface | Entry | Scope | Notes |
|---|---|---|---|
| `POST /chat` | `_finalize_turn` | `miriam:web:*` | turn + profile read |
| `POST /chat` (money) | `_finalize_money_turn` | `miriam:web:*` | receipt + reply |
| `POST /chat/stream` | `event_stream` | `miriam:web:*` | persisted on `done`, using the final streamed content |
| Onboarding turns | `_finish_onboarding_turn` / stream | `miriam:onboarding:*` | |
| Onboarding settled facts | `OnboardingService._remember` | `miriam:onboarding:*` | appended as system notes, not new documents |
| Spectrum | `spectrum._remember` | `miriam:<channel>:*` | |

Write rules:

- **Conversation → `ingest_turn`.** Both turns go in under the stable scope.
- **A fact Miriam already knows exactly → `ingest_note`.** It appends a system
  message to the same document. It deliberately does **not** use
  `create_memories`, which mints a lightweight source document per call and
  would re-fragment the graph.
- **A permanent trait → `remember_person_fact`.** Reserved for the few anchors
  where `isStatic` matters; currently the person's name, so the always-on
  profile's static list carries it.
- **Grounding → `ensure_container`.** After the first turn lands (the settings
  call 404s until the container exists), and never again: the result is memoised
  per process. A failure cools down for 5 minutes instead of retrying every turn.

Everything is fail-open. Without `SUPERMEMORY_API_KEY`, every call is a no-op
and the local SQL store still holds the conversation.

---

## 4. Read paths

- `build_memory_facts` reads the always-on profile (static + dynamic + buckets)
  plus a query-scoped search with one hop of related edges.
- `api/chat._load_memory_facts` prefers Supermemory and falls back to the local
  store.
- `voice/memory.read_facts` gives the money layers personality (facts about the
  person, never balances).

Reads are scoped to the person's container, which is why consolidating the
writes is what makes retrieval work: the profile now aggregates a coherent
picture instead of the fragments that happened to match the query.

---

## 5. What to know when changing this

- **Do not key Supermemory ingest to a session id.** Use
  `conversation_scope_for`. A new scope per session is the original bug.
- **Do not reach for `create_memories` in a hot path.** One call, one source
  document. Use `ingest_note` for facts that arrive mid-conversation.
- **Do not add a container per channel or per project.** Channels are scopes
  within the person's container; only a new *person* gets a new container.
- **Keep documents medium-sized.** If a single channel document grows unwieldy,
  window it by time (`miriam:web:<container>:<YYYY-MM>`) rather than returning
  to per-session documents.

## 6. Existing data (pre-fix clusters)

This change governs new writes; documents already scattered stay where they are
until re-ingested. Two options when that matters:

- **Merge containers** if one person genuinely ended up in more than one space:
  Supermemory's merge-container-tags endpoint folds the sources into a target
  tag.
- **Backfill** by re-ingesting the person's turns under
  `conversation_scope_for(...)`, which lets dreaming relink the memories.

Both are operational, not code paths, and are not run automatically.

## 7. Not done here

- **A correction surface.** `forget` / `update` exist on the client but no user
  path calls them ("that's not right anymore"). Wiring one is additive; it was
  left out because it is a memory *write* tool, and the agent loop's registry is
  read-only by design.
- **Spectrum reads.** The gateway now writes memory but its replies are
  deterministic templates and do not yet consume facts.
