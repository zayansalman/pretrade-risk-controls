# PTRM capability map — prediction markets

What venue, vendor, and regulatory pre-trade risk systems do — mapped onto
`pretrade-gate`, with a verdict on each capability: already covered, worth
taking, or deliberately out of scope.

Reference systems surveyed: Nasdaq PTRM / Nordic PRM / EQRC, HKEX PTRM (and
PTRM 2.0), Borsa İstanbul BISTECH PTRM, LME PTRM, Eurex Advanced Risk
Protection, CME Globex Credit Controls (GC2, Kill Switch, Cancel-on-Disconnect,
Inline Credit Controls), Exegy SREX, Fixnetix iX-eCute, HPR Riskbot / Omnibot /
Softbot, SEC Rule 15c3-5, MiFID II RTS 6 Art. 12/15/16.

> **Venue-class scope.** This library targets **prediction markets**. The
> capabilities below are stated in prediction-market terms and priced for that
> venue class. Retail-equity-via-broker-API capabilities — LULD bands, short-sale
> controls, trading sessions, corporate actions, broker rate limits, and the
> rest — are deferred to the issue backlog under the `equities` label rather
> than carried here. See §5.

---

## 1. The shape of a venue-grade PTRM

Every system above decomposes the same way. `pretrade-gate` today implements
parts of rows 1 and 2 and almost none of rows 3–5.

| Layer | What it means | pretrade-gate today |
|---|---|---|
| 1. Order-level checks | Reject *this* order on its own merits (size, price, duplication) | 4 of ~12 checks |
| 2. Aggregate checks | Reject because of accumulated state (exposure, loss, count, rate) | loss halt + daily notional |
| 3. Threshold structure | Staggered watch → warn → throttle → block, with utilisation reporting | binary block/allow |
| 4. Breach lifecycle | Latch, escalate, mass-cancel, explicit operator release | no latch, no cancel, auto re-arm |
| 5. Control plane & evidence | Scoped limit hierarchy, segregation of duties, audit trail, alerting | flat keys, no audit, no alerts |

Two structural observations drive most of the recommendations below:

- **Everything in `block_reason()` is binary.** Every surveyed system is
  graded. Nasdaq exposes three alert thresholds (watch / warn / action); Eurex
  stages alert → throttle → stop-button. Grading is what turns a risk gate into
  an operator instrument instead of a tripwire.
- **Nothing latches.** In HKEX, Eurex, and CME a tripped control stays tripped
  until a human with the right role releases it. `pretrade-gate` recomputes
  from live state on every call, so a breach silently un-breaches.

---

## 2. Capability inventory

`✔` covered · `~` partially covered · `✘` absent.
"Value" is value *to this library on prediction markets*, not to a clearing
member.

### 2.1 Order-level checks

| # | Capability | Where it comes from | Here | Value | Notes |
|---|---|---|---|---|---|
| 1 | Max order size / fat-finger | universal | ✔ | — | `max_trade_usd` + shares override; the runtime override matches venue intraday limit changes |
| 2 | Max order value (notional) | universal | ✔ | — | same check, dollar-denominated |
| 3 | Price collar / price banding | RTS 6 Art. 15, Nasdaq, CME | ~ | **high** | The slippage guard is one-sided and relative to the signal price only. Venues band *both* sides against a reference: an ask far *below* the signal is a data error, not a gift. Add absolute bounds too — Polymarket limit prices must be within `0.01`–`0.99`, and paying `0.97` for a binary is a different trade from paying `0.55` regardless of what the signal said |
| 4 | Duplicate / repeated-order check | Nasdaq single-order checks, HPR, DMA gateways | ✘ | **high** | Same market/side/price/size inside N seconds ⇒ block. Catches retry storms and double-fire loop bugs — the failure the one-position rule only catches *after* a fill is visible |
| 5 | Order-size vs liquidity (% ADV, market impact) | Nasdaq (single-order ADV, market impact check) | ✘ | **high** | `best_ask` alone ignores *size at* that ask. Prediction-market books are thin enough that anything above a minimal clip sweeps levels the slippage guard never sees |
| 6 | Instrument permission / restricted list | RTS 6 Art. 15(5), Nasdaq restricted & hard-to-borrow lists | ✘ | **high** | An operator deny-list of market/token IDs, consulted inside `block_reason` from a refreshed set. Build the mechanism once — it is shared with #38 |
| 7 | Instrument state / tradeability | all venue systems reject in non-tradeable states | ✘ | **high** | The CLOB market object already carries `accepting_orders`, `active`, `closed`, `archived`. Feed the tradeability verdict in on `EntryRequest` and block on it — cheap, and it prevents a whole class of venue rejects |
| 8 | Stale market data guard | gateway convention; ties to the `feedwatch` sibling | ✘ | **highest cheap win** | A quote age on the request plus `max_quote_age_ms`. Today an arbitrarily stale `best_ask` passes the slippage guard silently |
| 9 | Self-match prevention | CME STP | ✘ | low → deferred | Largely subsumed by the one-position rule while the gate stays single-position. Filed under `equities`, where multi-symbol and multi-strategy make it real |
| 10 | Positive-notional / well-formedness | universal sanity | ✔ | — | Economic well-formedness only; see #39 for structural |
| 38 | Time-to-resolution guard | prediction-market-specific; the analogue of expiry/session-end controls | ✘ | **highest domain win** | Refuse entries within N hours of resolution. Resolution is not instantaneous: a proposal opens a ~2-hour challenge window, and a dispute escalates to a 24–48h UMA vote, during which the position is un-exitable and the outcome is genuinely uncertain. No venue in the survey has this because no surveyed venue resolves by oracle |
| 39 | Structural order validity (tick size, min size) | Nasdaq order-type checks; venue order validation | ✘ | medium-high | The CLOB rejects `INVALID_ORDER_MIN_TICK_SIZE` and `INVALID_ORDER_MIN_SIZE`. The trap: **tick size changes dynamically** as price moves past `0.96` or below `0.04`, so a clip that was valid at `0.95` is rejected at `0.97` with no code change. Cheap to check, and it feeds #40 |
| 40 | Repeated-reject throttle | 15c3-5 erroneous-order controls; RTS 6 Art. 15 | ✘ | **high** | N venue rejects in a rolling window ⇒ latch a halt and require an operator release. Note the gate currently has **no inbound channel for order outcomes at all** — `record_realized_pnl` and `record_buy_notional` are the only writes — so this needs a new feedback method |

### 2.2 Aggregate checks

| # | Capability | Where it comes from | Here | Value | Notes |
|---|---|---|---|---|---|
| 11 | Daily loss limit | Nasdaq PRM loss limits | ✔ | — | Trailing from a high-water mark, which is stronger than the fixed floors most venues ship |
| 12 | Gross executed exposure | Nasdaq aggregate checks | ✔ | — | `daily_buy_notional` is exactly this, buy-side only |
| 13 | Gross **open** exposure (mark-to-market) | Nasdaq, Eurex, CME ICC | ✘ | **high** | The halt counts only *realized* PnL. A position bleeding out unrealized does not move the halt until it closes — and prediction-market positions are often held toward resolution rather than flipped intraday, so the blind spot is wide here. An optional `unrealized_pnl` input keeps the gate pure |
| 14 | Position limits (per instrument) | HKEX (position delta), CME ICC, universal | ~ | medium | The one-position rule is a crude binary version; a max-shares-per-market limit is the real analogue |
| 15 | Credit / capital threshold | **SEC 15c3-5**, CME GC2 | ✘ | **high** | "Order must fit inside available collateral." Distinct from the bankroll cap: that is a policy budget, this is solvency. Prevents venue rejects and negative-balance states |
| 16 | Message / order rate throttle | **RTS 6 Art. 15**, Nasdaq port message rate threshold | ✘ | **high** | The only runaway-loop defence today is cumulative dollars. A loop emitting hundreds of *blocked or tiny* orders is invisible to it. Reserve headroom for cancels: a bot that exhausts its rate budget loses the ability to cancel, not just to send. Needs the clock seam (#37) |
| 17 | Repeated-automated-execution throttle | **RTS 6 Art. 15** (explicit) | ✘ | **high** | Max entries per market per day; max consecutive losing entries before a human is required. The regulation's own words for "stop the strategy re-firing without human intervention" |
| 18 | Persisted counters across restart | venue systems persist intraday | ✔ | — | Genuine strength; most homegrown gates get this wrong |

### 2.3 Threshold structure

| # | Capability | Where it comes from | Here | Value | Notes |
|---|---|---|---|---|---|
| 19 | Staggered levels (watch / warn / action) | **Nasdaq (3 alert thresholds), Eurex (3 staggered limits)** | ✘ | **highest** | Add `gate.evaluate(req) -> Decision` carrying severity + per-limit utilisation; keep `block_reason()` as a one-line wrapper so nothing breaks. This is the single change that unlocks the rows below |
| 20 | Utilisation / headroom reporting | Eurex utilisation %, all venue GUIs | ~ | **high** | `loss_halt_headroom` proves the idea — generalise it so *every* limit reports `used / limit`. Straight into `botdeck` as gauges |
| 21 | Throttle-as-an-action (not just reject) | Eurex level 2 | ✘ | medium | "Halve the clip" is often the right response to 80% utilisation |
| 22 | Per-limit configurable breach action | Nasdaq (reject new / reject + cancel all), CME | ✘ | medium | Requires the `Decision` object first |
| 23 | Limit config validation | venue GUIs validate on entry | ✘ | low-medium | `GateConfig` currently accepts negative caps and `max_entry_slippage=5.0`. Cheap `__post_init__` |

### 2.4 Breach lifecycle

| # | Capability | Where it comes from | Here | Value | Notes |
|---|---|---|---|---|---|
| 24 | **Latching breach + explicit release** | **Eurex stop-button, HKEX block/unblock, CME kill switch** | ✘ | **highest** | Every surveyed system requires a deliberate human release. Here a halt is recomputed live, so any state change lifting PnL back over the floor silently re-arms trading. Persist a sticky `gate.halted` flag; only `reset_daily_loss_halt` clears it |
| 25 | Kill = block **+ mass cancel** | **HKEX ("block plus mass order cancellation"), CME ("block all new order entry and cancel all working orders")** | ~ | **high** | The gate blocks new entries but never signals that the resting entry order should be pulled. `mark_kill_handled` is the right hook — extend it to demand a cancel acknowledgement |
| 26 | Admin-only kill re-enable | CME (risk admin re-enables), HKEX | ✘ | medium-high | `rm KILL` silently re-arms the bot. Venues never allow that |
| 27 | Liquidation-only / close-only mode | venue trading states, Eurex | ✘ | **high** | A halted gate should still permit *risk-reducing* orders. Today the gate is entry-only, so exits are simply un-gated — the same blind spot from the other direction |
| 28 | Cancel-on-disconnect / deadman | **CME COD** | ✘ | medium-high | If the strategy loop stops calling the gate for N seconds, treat it as failure: require re-arm, and let the EMS pull orders. The mirror image of the kill switch |
| 29 | Fail-closed on risk-system staleness | 15c3-5 posture; venue heartbeats | ~ | **high** | State defaults fail safe, but a store that stops answering just leaves cached values in place forever. Rule: last successful `refresh_*` older than N seconds ⇒ block |

### 2.5 Control plane and evidence

| # | Capability | Where it comes from | Here | Value | Notes |
|---|---|---|---|---|---|
| 30 | Scoped limit hierarchy | **HKEX PTLG, CME (LCE → exec firm → sender comp), Nasdaq (firm/trader/session)** | ✘ | **high** | Directly answers the README's "single process, single instrument" limitation: per-market child gates plus a parent holding aggregate limits. Prerequisite for trading several markets concurrently, and for #9 |
| 31 | Scoped kill switches | CME's three independent kill levels | ~ | medium | One global file today; per-scope files are nearly free once scopes exist |
| 32 | Intraday limit change, no restart | universal | ✔ | — | `refresh_runtime_limits` is exactly this |
| 33 | Segregated control (trader can't set own limits) | **15c3-5 "direct and exclusive control"** | ~ | medium | The control plane is a separate module, but the bot process can write the same keys. A read-only store view for the gate closes it |
| 34 | **Audit trail of limit changes and breaches** | 15c3-5, RTS 6 record-keeping, every venue GUI | ✘ | **high** | Biggest credibility gap: toggling the loss-halt bypass leaves no trace of who, when, or why. An append-only event stream (blocks, breaches, override writes, releases) costs little and makes the library defensible |
| 35 | Alerting on approach and breach | Nasdaq alert thresholds, Eurex level-1 alert | ✘ | **high** | Optional `on_event` callback; `botdeck` and `feedwatch` are already the consumers |
| 36 | Post-trade reconciliation vs limits | **RTS 6 Art. 16** | ✘ | **high** | The gate trusts the caller's `position_open` boolean absolutely. If it also derived its own view from `record_*` calls, a disagreement between the two would be a blockable condition — that catches EMS bugs no individual limit can |
| 37 | Injectable clock | venue session calendars | ✘ | **high** (as a seam) | `_roll_daily_window` hardcodes `datetime.now(UTC)`. UTC midnight is defensible for a 24/7 prediction market, so the *calendar* is not the point — the **seam** is: #4, #16, #17 and #40 all need a clock they can control, and injecting one also removes wall-clock dependence from the test suite |

---

## 3. Recommended order

**Tier 1 — cheap, no architectural change, keeps `block_reason()` pure**

1. `evaluate() -> Decision` with severity levels and per-limit utilisation (#19, #20)
2. Latching halt with explicit operator release (#24)
3. Injectable clock (#37) — unblocks 4, 5 below
4. Stale-quote guard + stale-refresh fail-closed (#8, #29)
5. Duplicate-order window (#4)
6. Rate throttle + repeated-execution throttle (#16, #17)
7. Two-sided price collar and absolute price bounds (#3)
8. Event stream for blocks, breaches, and override writes (#34, #35)
9. `GateConfig` validation (#23)

**Tier 2 — new inputs or a wider contract**

10. Time-to-resolution guard (#38) — highest domain-specific value
11. Market tradeability flags and operator deny-list (#7, #6)
12. Available-collateral check (#15)
13. Structural order validity, incl. the dynamic tick-size boundary (#39)
14. Repeated-reject throttle, and with it an order-outcome feedback channel (#40)
15. Depth-aware size check (#5)
16. Unrealized PnL in the halt (#13)
17. Kill and halt emit a mass-cancel action requiring acknowledgement (#25, #26)
18. Self-reconciliation of position state vs the caller's flag (#36)
19. Liquidation-only mode; gate exits as well as entries (#27)

**Tier 3 — structural**

20. Scoped gate hierarchy with aggregate parent limits (#30, #31)
21. Per-limit breach-action policy, including throttle (#21, #22)
22. Deadman / heartbeat (#28)

---

## 4. Deliberately out of scope

Real capabilities of the surveyed systems that should **not** come here:

- **Margin- and VaR-based risk metrics** (Eurex ARP prices limits off total
  margin requirement). Requires a clearing model this library has no business
  owning.
- **FPGA / nanosecond latency engineering** (Fixnetix iX-eCute, HPR Riskbot's
  ~340ns, Omnibot's switch-parallel checks). The correct *design* lesson —
  fixed-cost, no-I/O checks on the hot path — is already implemented; the
  hardware is irrelevant at bot cadence.
- **Multi-asset instrument reference data, borrow lists, clearing-member credit
  hierarchies, regulatory reporting.** Broker and venue obligations, not a
  single-bot concern.
- **Mass-cancel *execution*.** The gate should *decide* and emit the action;
  actually pulling orders belongs to the EMS.

---

## 5. Deferred: retail equity via broker API

Trading equities through a retail broker API pulls in a second, largely disjoint
set of controls. They are real and they are tracked — as backlog issues under
the **`equities`** label — but they are not built here while the library stays
scoped to prediction markets.

Deferred there: multi-symbol scoped hierarchy as a hard prerequisite, buying
power / intraday margin excess, short-sale controls (locate, hard-to-borrow,
SSR), LULD bands and halt-auction states, trading-session awareness, broker API
rate limits and 429 backpressure, structural order validity under equity tick
and lot rules, restricted-symbol lists, corporate actions, exchange calendars,
and self-match prevention across strategies.

One finding worth recording so it is not rediscovered wrongly: **do not build a
pattern-day-trader day-trade counter.** The SEC approved FINRA's amendments to
Rule 4210 on 14 April 2026, effective 4 June 2026, eliminating both the PDT
designation and the $25,000 minimum equity requirement and replacing them with a
real-time intraday margin excess standard, with an 18-month broker phase-in to
20 October 2027. The durable check is buying power, not a trade count.

Several items in §2 are venue-neutral and stay here rather than being deferred:
the rate throttle (#16), the clock seam (#37), the repeated-reject throttle
(#40), the deny-list mechanism (#6), and the whole of §2.3–§2.5. The `equities`
issues layer venue-specific rules on top of those; they do not re-implement them.
