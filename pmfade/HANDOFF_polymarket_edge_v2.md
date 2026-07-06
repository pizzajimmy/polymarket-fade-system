# HANDOFF — Polymarket Edge Engine v2
## Sophistication layer for existing 30-min scanner (longshot + news-fade signals)

**Audience:** Claude Code, working inside the existing repo.
**Prime directive:** Extend, don't rewrite. The scaffold works — server, 30-minute scan cadence, longshot signal, news-fade signal. Read the existing code first, map its data layer and signal interfaces, then bolt the modules below onto it. Where this doc's assumptions conflict with what you find in the repo, flag it in your plan before coding.

---

## 0. What v1 does vs. what v2 adds

| | v1 (exists) | v2 (this handoff) |
|---|---|---|
| Longshot signal | Price-based (cheap Yes = fade candidate) | Anchored: structural probability model per market family + empirical calibration prior; edge scored net of fees, spread-at-size, carry hurdle, oracle risk |
| News fade | Spike detection | Discrimination layer: input-change classification, cooldown protocol, adverse-selection checks, hold-to-resolution viability gate |
| Execution | (confirm with operator — see §12) | Passive maker-only: resting limit orders at fair-value bands, repricing on anchor decay, cancel-on-news |
| Exits | Hold to resolution (implied) | Convergence exits: harvest calibration decay and 72-hr reversion without holding to expiry |
| Risk | (confirm) | Sizing caps, correlation groups, oracle-risk budget, paper-trade gate |

---

## 1. Conceptual model (read before coding)

The system trades one quantity: **edge = market_implied_probability − fair_value**, where fair_value blends (a) a structural anchor (procedural/base-rate model of the specific market) and (b) an empirical calibration prior (how mispriced this category × horizon × price-bucket has historically been).

Two harvest modes share the same fair-value machinery:

1. **Carry-to-convergence.** Favorite-longshot bias on Polymarket concentrates at long horizons: published calibration slopes run ~0.99 within 1 hour of resolution but ~1.32 beyond one month, with Politics the most underconfident domain (~1.31). Slope > 1 ⇒ longshots overpriced / favorites underpriced, and the mispricing *decays toward zero as resolution approaches*. So: enter No on overpriced longshots >30d out, exit into the convergence (sell No at a higher price weeks later) rather than locking capital to expiry.
2. **Spike reversion.** Sentiment-driven news spikes in political/geopolitical markets tend to overshoot and mean-revert over ~24–72 hours *when the news changed no structural input*. Canonical specimen: Iran ceasefire market (Apr 2026) spiked 35→68 in 8 minutes on a rumor, settled ~58 within 2 hours, reverted over the next day; the documented fade sold No at 64 and exited at 48 over 36h. The fatal failure mode is fading information (cf. Dutch election Oct 2025: "spike" at exit polls was reality arriving after weeks of underreaction — fading it was instant zero). Discrimination, not detection, is the whole game.

**Epistemic warning to encode, not just note:** public sources contradict each other on longshot calibration (one analytics site claims sub-10% events resolve Yes ~14% of the time — underpricing — while trade-tape and academic calibration studies find overpricing that worsens with horizon). The discrepancy is likely horizon/measurement methodology. Therefore Module A computes our own calibration surface from raw resolved-market data, and all published numbers in this doc are **defaults-to-verify**, not gospel.

---

## 2. Module A — Calibration engine (empirical prior)

**Purpose:** Compute our own calibration surface so fair-value priors come from our data, not conflicting blog posts.

**Inputs:** Historical resolved markets via Gamma API (market metadata, final outcome, category/tags, end date) + price history (Gamma/CLOB timeseries; use whatever the scaffold already ingests, extend if needed).

**Method:**
1. For each resolved binary market, sample implied probability at fixed horizons before resolution: `{1h, 24h, 7d, 30d, 90d}` (snapshot the Yes mid, or last trade if no book data).
2. Bucket by `category × horizon × price_decile`. Compute realized frequency per bucket and a calibration slope per `category × horizon` cell (logistic regression of outcome on logit(price) is fine; report slope + intercept + n).
3. Output artifact: `calibration_surface.parquet` (or JSON) with per-cell: realized_freq, implied_mean, slope, n, last_computed. Expose a function `calibration_prior(price, category, days_to_resolution) -> fair_value_prior` (interpolate between horizon buckets).
4. Recompute weekly via existing scheduler. Log drift vs. prior computation.

**Acceptance:** Surface reproduces the qualitative published pattern (slope near 1 at short horizon, materially >1 at >30d for politics/geopolitics) OR clearly documents that our data disagrees — either result is a pass; silent failure is not. Minimum n per cell = 50; cells below that fall back to the category × horizon marginal.

---

## 3. Module B — Anchor registry (structural models per market family)

**Purpose:** Per-market structural probability, decomposed the way an rNPV/PoS model decomposes a biotech catalyst: explicit inputs, explicit math, deterministic time-decay.

**Design:**
- A `families/` directory. Each family = one YAML/JSON template: `match` rules (regex/keywords against market title + rules text), `inputs` (named parameters with sources), `anchor_fn` (probability as a function of inputs + days_remaining), `confidence_tier` (see below), `min_chain_days` (mechanical time floor where applicable).
- Market → family matching runs in the 30-min scan. Unmatched markets get `family: none` and are only eligible for calibration-prior-only scoring at reduced weight.
- **Operator override file** (`anchors_manual.yaml`): per-market hand-set anchor + note + expiry date. Manual overrides beat family models. Every override is logged.

**v1 families to implement (in priority order):**

1. **`dated_impeachment`** — "Will X be impeached by DATE." Inputs: chamber composition (majority party, margin), controlling party's stated posture, legislative session calendar, seating date of next Congress (US: Jan 3). Anchor logic: if controlling party opposed and no floor path exists inside the window → anchor ≤ 1%; if window spans a chamber flip, anchor jumps at the seating date, not the election date. (Live illustration at handoff time: "impeached by end of 2026" traded ~4–5¢ Yes while the seating-date math put structural probability well under 1% even under a Democratic midterm sweep.)
2. **`ceasefire_by_date`** — Inputs: signed-deal status (bool), definitional requirements from rules text (Polymarket's standard requires the ceasefire in effect AND holding **10 consecutive calendar days**), negotiation-stage assessment (operator-set enum). Mechanical floor: `min_chain_days = signing_buffer (2–4d) + 10d hold ≈ 12–14d`. If `days_remaining < min_chain_days` and no deal signed → anchor = 0.5–1% (never literal 0: residual for surprise + resolution ambiguity). For date ladders, anchors must be monotonic in date.
3. **`leader_out_by_date`** — "X out as President/PM by DATE." Decompose: removal (vote-threshold math; US removal = 67 Senate votes, base rate 0/3) + resignation (base rate ~1–2% per term, operator-adjustable) + death/incapacity (actuarial: period life table lookup by age/sex, pro-rated to window; flag that heads of state beat table averages). Sum components.
4. **`constitutional_impossibility`** — third terms, territorial acquisition, amendments. Inputs: required thresholds (2/3 both chambers + 38 states; treaty + counterparty consent + Senate ratification). Anchor = 0.5–2% depending on window length. These are the purest sentiment sponges.
5. **`legal_outcome_by_date`** — "X charged/convicted/jailed by DATE." Inputs: current process stage (none/investigation/charged/trial/sentenced), jurisdiction median stage-to-stage durations (operator-seeded lookup table). Anchor = P(remaining chain completes inside window). Note: this family overlaps entertainment markets — the least efficient category (~62% published accuracy) but also the least liquid; Module D's liquidity filters will kill most of these, which is correct.

**Confidence tiers** (drive the blend weight in Module D):
- `tier_1_mechanical` (calendar math, vote thresholds, chain floors): blend weight w = 0.8
- `tier_2_base_rate` (actuarial, historical frequencies): w = 0.6
- `tier_3_judgment` (operator-set, soft inputs): w = 0.4

**Time-decay:** anchors are functions of `days_remaining`, recomputed every scan. A dated-event anchor with an unmet precondition and a shrinking window decays deterministically — this is the convergence the exit logic harvests.

---

## 4. Module C — Resolution-rules hardness scorer

**Purpose:** Oracle/resolution risk is the dominant tail on a penny-collecting book. UMA disputes are not rare (1,150+ disputed markets in 2026 by mid-year; the Zelenskyy-suit market resolved No on $237M volume against broad media description; a $60M Strategy-BTC market turned on whether "sells by May 31" meant execution or disclosure).

**Method:** Parse each market's rules text. Score 0–100, hard = high:
- **Penalize:** "consensus of credible reporting", "credible reporting", undefined qualitative nouns (suit, meeting, major, official visit), resolution requiring interpretation of intent, multi-condition compound rules.
- **Reward:** single named official source (a filing, a roll-call vote, a government publication), explicit timezone-stamped deadlines, explicit edge-case handling in the rules.
- Keyword scoring is fine for v2.0. Optional v2.1: LLM pass (Claude API — the scaffold can call it) that reads the full rules and returns `{hardness_score, ambiguous_terms[], dispute_scenarios[]}`.

**Use:** hardness < threshold (default 60) ⇒ market excluded from execution, signal-only. Hardness enters Module D as a haircut on edge.

---

## 5. Module D — Edge scorer v2 (replaces naive longshot signal)

**Fair value:** `FV = w · anchor + (1 − w) · calibration_prior(price, category, horizon)`, w from the anchor's confidence tier (0 if family = none).

**Gross edge:** `edge_gross = |market_price − FV|` on the side we'd take (almost always buying No / the underpriced favorite).

**Cost model — subtract, in order:**
1. **Fees.** Polymarket taker fee per share ≈ `C × p × (1 − p)`, category coefficients (verify against docs.polymarket.com and the market object's `feesEnabled` flag — pre-fee-activation markets are exempt): geopolitics **0** (permanently fee-free), sports 0.03, politics/finance/tech/mentions 0.04, economics/culture/weather/other 0.05, crypto 0.07. At the price extremes we operate in, taker fees are ~0.1–0.3%; **maker fills are 0 and collect 20–25% daily rebates** — the execution module (F) makes maker the default, so fee cost in the scorer should reflect intended execution mode.
2. **Spread-at-size.** Never use mid. Walk the CLOB book to the intended clip size; cost = executable price − mid. If the book can't absorb the clip within a max-impact bound (default 1.5¢), scale the size down, don't relax the bound.
3. **Carry hurdle.** Annualize: `edge_net_annualized = edge_net / price_paid × (365 / expected_hold_days)`, where expected_hold uses the convergence exit assumption (Module F), not resolution date. Require ≥ hurdle (default 10%/yr — locked USDC earns nothing, so the hurdle is risk-free + oracle/tail premium).
4. **Hardness haircut.** `edge_net × (hardness / 100)`.

**Filters (hard gates, applied before ranking):**
- Liquidity: total volume ≥ $100K AND current book depth ≥ 3× intended clip on our side. (Sub-$100K markets show whale distortion — single $25K+ positions move them 5–15% — and published accuracy collapses toward coin-flip.)
- Price zone: Yes in [0.03, 0.20] for longshot fades (below 3¢ the spread eats everything; above 20¢ you're making a real directional call, route to operator review).
- Days to resolution ≥ 21 for carry entries (the bias lives at long horizon).

**Cheap structural screens (add to every scan, near-free alpha + sanity checks):**
- **Ladder monotonicity:** within a date-laddered event family, P(by Aug) ≤ P(by Oct) ≤ P(by Dec). Violations → alert (arb or data error).
- **NegRisk sum check:** mutually exclusive multi-outcome events should have ΣYes ≈ 1.0 (±fees/spread). Deviations → alert.

**Output:** ranked signal table per scan: market, side, FV, anchor, prior, edge_net_annualized, hardness, family, filters_passed, suggested clip. Persist every scan's table — this is the backtest substrate.

---

## 6. Module E — News discrimination layer (upgrades existing fade signal)

Wrap the existing spike detector with this protocol. A spike alone is never a trade.

1. **Cooldown:** on spike detection, start a 30–90 min timer (default 45). No action inside the window — the practitioner evidence says the first 30–90 minutes are the panic phase; price at timer-expiry is the fade reference, not the wick.
2. **Deviation threshold:** proceed only if post-cooldown price deviates ≥ 15 points (default; parametrize `fade_min_deviation_pts`) from Module D's FV for that market.
3. **Input-change classification — the core gate.** Each anchor family lists its inputs (§3). Feed the triggering headline/snippet(s) + the market's input list to a classifier returning `{input_changed: bool, which_input, direction, confidence}`. Implementation: Claude API call with a strict JSON-output prompt; fall back to operator alert if the news feed is empty or confidence < 0.7.
   - `input_changed = true` → **suppress fade**, flag market for re-anchor (operator task), optionally widen/cancel resting orders (Module F). A scheduled vote, a signed deal, an actual indictment = information. A rumor of talks, a filed-and-doomed resolution, an outrage cycle = sentiment.
   - `input_changed = false` → fade candidate, continue.
4. **Adverse-selection heuristics (v2.1, optional):** pre-spike 30–60 min net taker flow direction and wallet freshness from the data API. Rationale: the Iran-ceasefire spike was preceded by ~50 insider-flagged accounts — part of a spike can be informed flow that will not revert. If pre-spike flow was one-sided into the move, halve size or skip.
5. **Hold-to-resolution viability gate (non-negotiable):** binaries have no clean stops — exiting a failed fade means crossing a blown-out spread. Therefore a fade is only executable if the fill price is *also* +EV held to resolution per the anchor. The reversion is a free acceleration on a trade we'd own anyway, never a standalone timing bet.
6. **Exit:** take-profit at 50–70% retrace of the spike or 72h elapsed, whichever first; else it converts to a carry position under Module F's exit rules (which the viability gate guarantees is acceptable).

---

## 7. Module F — Passive maker execution & exits

**Philosophy:** don't chase spikes; pre-position. Resting limit orders at anchor-derived prices mean panics fill us at our number, with zero fees plus rebates, including overnight (operator is NZT; US political news breaks NZ overnight — the system must be fully autonomous in that window).

- **Order placement:** for each watchlist market passing Module D, rest a No bid (Yes ask) at `FV ± band` (default band 2–4¢ beyond FV in our favor, tuned per market liquidity). Maker-only enforcement: **never send an order that would cross the book.** No market orders anywhere in the system.
- **Repricing:** every scan, re-derive FV (anchors decay with `days_remaining`); move resting orders accordingly. Rate-limit re-quotes to avoid spam-cancels.
- **Cancel-on-news:** an `input_changed = true` event from Module E pulls all resting orders in that market's correlation group within seconds (this path must not wait for the 30-min scan — hook it to the spike detector's event stream).
- **Exits (carry positions):**
  - Convergence take-profit: exit when captured ≥ 60% of modeled edge, or when `days_to_resolution < 10` (near-resolution calibration is tight; residual edge rarely beats the exit spread) — hold to resolution only if remaining edge still clears the carry hurdle and hardness ≥ 80.
  - Exit-liquidity check at entry: confirm book depth on the exit side too; a position you can't sell is a hold-to-resolution position and must be sized as one.

---

## 8. Module G — Risk & portfolio

- **Sizing:** ≤ 2% of bankroll per market at entry (parametrize; 5% absolute ceiling), computed on capital at risk (price paid × shares).
- **Correlation groups:** markets sharing an underlying event (all Trump-exit variants; all rungs of one ceasefire ladder; impeachment + removal + out-by-date) form one group. Group exposure cap: 6% of bankroll. Nested date ladders are ~perfectly correlated — treat as one position.
- **Oracle-risk budget:** aggregate exposure to markets with hardness 60–80 capped at 20% of deployed capital. Hardness < 60 never executes (§4).
- **Insurance-book monitor:** this strategy is short-tail (many small wins, rare large losses — at 4–5¢ edges one loss erases ~20 winners). Dashboard must track: realized win rate vs. FV-implied win rate per family, rolling PnL, largest-loss-to-average-win ratio, and calibration of our own FVs (are our anchors themselves calibrated?). Sobering base rate to keep on the dashboard: only ~7.6% of Polymarket wallets are lifetime-profitable and the top 0.04% take >70% of PnL — the mechanical discipline above is the entire moat.
- **Kill switches:** daily loss limit (default 3% of bankroll → cancel all resting orders, alert operator); any UMA dispute opened on a held market → alert + freeze adds in that group.

---

## 9. Parameter defaults (single config file; every value below must be overridable)

```
scan_interval_min: 30            # existing
calib_horizons: [1h, 24h, 7d, 30d, 90d]
calib_min_cell_n: 50
blend_w: {tier1: 0.8, tier2: 0.6, tier3: 0.4, none: 0.0}
fee_C: {geopolitics: 0.0, sports: 0.03, politics: 0.04, finance: 0.04,
        tech: 0.04, mentions: 0.04, economics: 0.05, culture: 0.05,
        weather: 0.05, other: 0.05, crypto: 0.07}   # verify vs docs + feesEnabled
min_volume_usd: 100000
min_book_depth_multiple: 3
max_impact_cents: 1.5
longshot_yes_zone: [0.03, 0.20]
min_days_to_resolution_carry: 21
carry_hurdle_annualized: 0.10
hardness_execution_floor: 60
hardness_haircut: true
fade_cooldown_min: 45            # range 30–90
fade_min_deviation_pts: 15
fade_classifier_min_confidence: 0.7
fade_take_profit_retrace: 0.6
fade_max_hold_hours: 72
maker_band_cents: [2, 4]
convergence_tp_fraction: 0.6
near_resolution_exit_days: 10
max_position_pct: 0.02
max_position_pct_ceiling: 0.05
max_group_pct: 0.06
oracle_soft_bucket_cap_pct: 0.20
daily_loss_limit_pct: 0.03
```

---

## 10. API & data notes

- **Gamma API** (`gamma-api.polymarket.com`): market/event metadata, categories/tags, rules text, resolution status, `feesEnabled`. Primary source for Modules A–D.
- **CLOB API** (`clob.polymarket.com`) + `py-clob-client`: order book depth, trades, order placement/cancel (maker limit orders), websocket for the cancel-on-news fast path.
- **Data API** (holders/trades endpoints) for the v2.1 adverse-selection heuristics.
- Verify all endpoints/params against **docs.polymarket.com** at build time — fee schedule and API surface have both changed within the last year (fees rolled out in phases Jan–Apr 2026; geopolitics remains free). Reuse the scaffold's auth/wallet plumbing; do not introduce a second signing path.

---

## 11. Build order, backtests, acceptance gates

1. **Phase 1 (no execution):** Module A + C + D wired into the existing scan; signals logged alongside v1 signals for comparison. Acceptance: two weeks of parallel logs; own calibration surface computed and the §1 source-contradiction resolved empirically in a short written note.
2. **Phase 2:** Module B families 1–3 + Module E wrapping the existing fade detector. Acceptance: replay historical spikes from stored scans (and any archived price data) through the discrimination gate; report fade precision/recall vs. the naive detector; the Dutch-election-shaped cases (input-change spikes) must be suppressed.
3. **Phase 3:** Module F + G in **paper mode** (simulated fills at book-derived prices, real order lifecycle without submission). Acceptance: 30 days paper, realized-vs-modeled slippage and fill-rate report.
4. **Phase 4:** live, at 25% of configured size caps for the first month. Operator flips the switch manually; the system never self-promotes to live.

---

## 12. Open questions — ask the operator before building

1. Does the current scaffold place orders, or is it signal-only? If it trades: wallet/signing setup, current size logic, and where the bankroll figure lives.
2. Which news feed(s) power the spike detector, and are raw headlines/snippets stored (needed for Module E's classifier)?
3. Is historical price data archived locally, or must Module A backfill from the APIs?
4. Bankroll figure and whether the §9 caps are acceptable starting values.
5. Priority order of anchor families if timeboxed (default: impeachment → ceasefire → leader-out).
6. Claude API key availability on the server for Module C/E LLM passes, or keyword-only for v2.0?

---

## 13. Non-goals / guardrails (do not "improve" these away)

- No taker/market orders. No crossing the book, ever.
- No autonomous size increases, cap changes, or live-mode promotion.
- No trading markets with hardness < 60 regardless of edge size.
- No fading a spike that fails the hold-to-resolution viability gate.
- Don't rewrite the scanner loop or the existing signal outputs; v1 signals keep flowing for comparison.

## 14. References for context (fetch if deeper background needed)

- Domain-calibration study (slopes by category × horizon): arxiv.org/pdf/2602.19520
- Polymarket trade-tape database + longshot pricing facts: arxiv.org/pdf/2606.04217
- Microstructure / longshot-decile spreads: arxiv.org/pdf/2604.24366
- Fee schedule + feesEnabled semantics: docs.polymarket.com/trading/fees
- Oracle-dispute background (Zelenskyy suit; Strategy-BTC May sale): coverage via Decrypt / The Defiant, mid-2025 and mid-2026.
