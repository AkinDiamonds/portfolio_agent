# Plan 05 — Token Budget & History Truncation

## Goal
Implement `app/budget.py` — the module that enforces the context window ceiling before every LLM call. This is a pure logic module with no I/O: it takes a list of messages and returns a (possibly trimmed) list that fits within the effective input budget.

## Scope
- `app/budget.py` — token estimation + history truncation
- `app/agent.py` — integrate `build_messages()` call before the LLM loop

## Depends On
- Plan 01 (`Settings`, `effective_input_budget`)
- Plan 04 (`agent.py` — insertion point)

---

## Files to Create / Modify

| File | Action | Purpose |
|---|---|---|
| `app/budget.py` | Create | Token estimation and message trimming |
| `app/agent.py` | Modify | Call `build_messages()` before the loop |

---

## `app/budget.py` — Design

### Token estimation
Use a character-length heuristic: `len(text) // 4`. This is intentionally approximate — exact tokenizer parity with every possible plugged-in model is not worth the dependency cost. The heuristic errs on the side of slightly over-counting, which is the safe direction (slightly more aggressive trimming rather than slightly less).

For a message object, estimate tokens as the sum of the role string length and content length, both divided by 4, plus a small fixed overhead (e.g. 4 tokens) per message to account for the message envelope.

### `estimate_tokens(text: str) -> int`
Returns `max(1, len(text) // 4)`. Returns at least 1 to avoid a zero-token message being counted as free.

### `estimate_message_tokens(message: dict) -> int`
Returns the token estimate for a single message dict (with `role` and `content` fields). Adds the per-message overhead constant.

### `build_messages(system_prompt, tool_schema, history, message, budget) -> list[dict]`

**Inputs:**
- `system_prompt: str` — the fixed system prompt text
- `tool_schema: dict` — the GitHub tool schema (treated as a fixed cost, serialized to JSON for size estimation)
- `history: list[dict]` — the client-supplied conversation history (oldest first)
- `message: str` — the current user message
- `budget: int` — the effective input token budget (from `settings.effective_input_budget`)

**Algorithm:**

1. Compute fixed costs:
   - System prompt token estimate
   - Tool schema token estimate (serialize to JSON string, then estimate)
   - Current message token estimate

2. Remaining budget = `budget − fixed_costs`. If remaining budget is already negative or zero after fixed costs, log a WARNING and proceed with an empty history. This is an edge case (system prompt alone exceeds the budget) that should never happen in normal configuration.

3. Walk the history in **reverse** (newest first), greedily accumulating messages that fit. Stop when the next message would exceed the remaining budget.

4. Reverse the accumulated history back to chronological order and assemble the final list:
   - System message (`role: "system"`)
   - Trimmed history items
   - Current user message (`role: "user"`)

5. If any history was dropped, log at INFO level how many turns were dropped. This surfaces the trimming in logs so operators know when it happens.

**Return:** the assembled `list[dict]` ready to be passed to the LLM client.

### Startup logging
`app/main.py` should call `estimate_tokens(system_prompt)` at startup and log the result at INFO level so the operator can see the fixed cost of their prompt immediately.

---

## `app/agent.py` Integration

Replace the ad-hoc message assembly in `handle_turn()` with a call to `build_messages()`. The loop itself does not change — only the message list fed into it changes. Any additional messages appended during the loop (assistant tool-call message + tool result message) are appended directly and are not subject to re-trimming mid-turn. This is intentional: mid-turn messages are always small and bounded by `MAX_TOOL_ITERATIONS`.

---

## Guardrails

| Risk | Mitigation |
|---|---|
| History grows unbounded | Greedy reverse walk drops oldest turns first until the budget fits |
| System prompt alone exceeds budget | Detected at startup via log; also detected at runtime with a WARNING log — proceeds with empty history rather than crashing |
| Token estimate is wrong (model uses a different tokenizer) | Heuristic errs on the side of over-counting → slightly more trimming, never under-trimming → safe |
| Tool schema cost forgotten | Included in fixed cost calculation, not just the prompt |
| Mid-turn messages blowing budget | Bounded by `MAX_TOOL_ITERATIONS × (tool_call_size + tool_result_size)`, which is always small relative to the budget |

---

## Verification Checklist (unit-testable)

- [ ] A history that fits entirely within the budget is returned unchanged (no drops).
- [ ] A history that exceeds the budget has the oldest turn(s) dropped until it fits.
- [ ] The returned list always starts with the system message and ends with the current user message.
- [ ] A system prompt that alone exceeds the budget logs a WARNING and returns `[system_msg, current_msg]` — does not raise.
- [ ] `estimate_tokens("")` returns 1 (never zero).
- [ ] The startup log includes the system prompt's fixed token cost.

## Definition of Done
`build_messages()` is the single message-assembly function used by `agent.py`. Unit tests cover trimming behavior with synthetic oversized histories. No code in the repo assembles messages manually outside this function.
