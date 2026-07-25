# PTRM capability map

What venue, vendor, and regulatory pre-trade risk systems do — mapped onto
`pretrade-gate`, with a verdict on each capability: already covered, worth
taking, or deliberately out of scope.

Reference systems surveyed: Nasdaq PTRM / Nordic PRM / EQRC, HKEX PTRM (and
PTRM 2.0), Borsa İstanbul BISTECH PTRM, LME PTRM, Eurex Advanced Risk
Protection, CME Globex Credit Controls (GC2, Kill Switch, Cancel-on-Disconnect,
Inline Credit Controls), Exegy SREX, Fixnetix iX-eCute, HPR Riskbot / Omnibot /
Softbot, SEC Rule 15c3-5, MiFID II RTS 6 Art. 12/15/16.

---

## 1. The shape of a venue-grade PTRM

Every system above decomposes the same way. `pretrade-gate` today implements
parts of rows 1 and 2 and almost none of rows 3–5.

| Layer | What it means | pretrade-gate today |
|---|---|---|
| 1. Order-level checks | Reject *this* order on its own merits (size, price, duplication) | 4 of ~10 checks |
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
"Value" is value *to this library*, not to a clearing member.

### 2.1 Order-level checks

| # | Capability | Where it comes from | Here | Value | Notes |
|---|---|---|---|---|---|
| 1 | Max order size / fat-finger | universal | ✔ | — | `max_trade_usd` + shares override; the runtime override matches venue intraday limit changes |
| 2 | Max order value (notional) | universal | ✔ | — | same check, dollar-denominated |
| 3 | Price collar / price banding | RTS 6 Art. 15, Nasdaq, CME | ~ | **high** | Slippage guard is one-sided and relative to the signal price only. Venues band *both* sides against a reference: an ask far *below* the signal is a data error, not a gift. Add absolute bounds too (never pay > 0.95 or < 0.02 on a binary) |
| 4 | Duplicate / repeated-order check | Nasdaq single-order checks, HPR, DMA gateways | ✘ | **high** | Same market/side/price/size inside N seconds ⇒ block. Catches retry storms and double-fire loop bugs — the failure your one-position rule only catches *after* a fill is visible |
| 5 | Order-size vs liquidity (% ADV, market impact) | Nasdaq (single-order ADV, market impact check) | ✘ | medium-high | `best_ask` alone ignores *size at* that ask. On thin books your clip sweeps levels the slippage guard never sees |
| 6 | Instrument permission / restricted list | RTS 6 Art. 15(5), Nasdaq restricted & hard-to-borrow lists | ✘ | **high** | Deny-list of market IDs, plus a minimum time-to-resolution rule — trading a market minutes from resolution is the domain-specific version of "no permission to trade this instrument" |
| 7 | Instrument state / halt awareness | all venue systems reject in non-tradeable states | ✘ | **high** | Market paused / closed / resolving ⇒ block. Cheap: one flag on `EntryRequest` |
| 8 | Stale market data guard | gateway convention; ties to your `feedwatch` sibling | ✘ | **highest cheap win** | A quote age on the request plus `max_quote_age_ms`. Today an arbitrarily stale `best_ask` passes the slippage guard silently |
| 9 | Self-match prevention | CME STP | ✘ | low | Largely subsumed by the one-position rule |
| 10 | Positive-notional / well-formedness | universal sanity | ✔ | — | |

### 2.2 Aggregate checks

| # | Capability | Where it comes from | Here | Value | Notes |
|---|---|---|---|---|---|
| 11 | Daily loss limit | Nasdaq PRM loss limits | ✔ | — | Yours is *trailing* from a high-water mark, which is stronger than the fixed floors most venues ship |
| 12 | Gross executed exposure | Nasdaq aggregate checks | ✔ | — | `daily_buy_notional` is exactly this, buy-side only |
| 13 | Gross **open** exposure (mark-to-market) | Nasdaq, Eurex, CME ICC | ✘ | medium-high | The halt only counts *realized* PnL. A position bleeding out unrealized does not move the halt until it closes. Optional `unrealized_pnl` input keeps the gate pure |
| 14 | Position limits (per instrument, long/short) | HKEX (position delta), CME ICC, universal | ~ | medium | One-position rule is a crude binary version; a max-shares-per-market limit is the real analogue |
| 15 | Credit / capital threshold | **SEC 15c3-5**, CME GC2 | ✘ | **high** | "Order must fit inside available collateral." Distinct from the bankroll cap: that's a policy budget, this is solvency. Prevents venue rejects and negative-balance states |
| 16 | Message / order rate throttle | **RTS 6 Art. 15**, Nasdaq port message rate threshold | ✘ | **high** | Your only runaway-loop defence is cumulative dollars. A loop emitting hundreds of *blocked or tiny* orders is invisible to it. Needs a timestamp window and an injectable clock |
| 17 | Repeated-automated-execution throttle | **RTS 6 Art. 15** (explicit) | ✘ | **high** | Max entries per market per day; max consecutive losing entries before a human is required. The regulation's own words for "stop the strategy re-firing without human intervention" |
| 18 | Persisted counters across restart | venue systems persist intraday | ✔ | — | Genuine strength; most homegrown gates get this wrong |

### 2.3 Threshold structure

| # | Capability | Where it comes from | Here | Value | Notes |
|---|---|---|---|---|---|
| 19 | Staggered levels (watch / warn / action) | **Nasdaq (3 alert thresholds), Eurex (3 staggered limits)** | ✘ | **highest** | Add `gate.evaluate(req) -> Decision` carrying severity + per-limit utilisation; keep `block_reason()` as a one-line wrapper so nothing breaks. This is the single change that unlocks rows below |
| 20 | Utilisation / headroom reporting | Eurex utilisation %, all venue GUIs | ~ | **high** | `loss_halt_headroom` proves the idea — generalise it so *every* limit reports `used / limit`. Straight into `botdeck` as gauges |
| 21 | Throttle-as-an-action (not just reject) | Eurex level 2 | ✘ | medium | "Halve the clip" is often the right response to 80% utilisation |
| 22 | Per-limit configurable breach action | Nasdaq (reject new / reject + cancel all), CME | ✘ | medium | Requires the `Decision` object first |
| 23 | Limit config validation | venue GUIs validate on entry | ✘ | low-medium | `GateConfig` currently accepts negative caps and `max_entry_slippage=5.0`. Cheap `__post_init__` |

### 2.4 Breach lifecycle

| # | Capability | Where it comes from | Here | Value | Notes |
|---|---|---|---|---|---|
| 24 | **Latching breach + explicit release** | **Eurex stop-button, HKEX block/unblock, CME kill switch** | ✘ | **highest** | Every surveyed system requires a deliberate human release. Here, a halt is recomputed live: any state change that lifts PnL back over the floor silently re-arms trading. Persist a sticky `gate.halted` flag; only `reset_daily_loss_halt` clears it |
| 25 | Kill = block **+ mass cancel** | **HKEX ("block plus mass order cancellation"), CME ("block all new order entry and cancel all working orders")** | ~ | **high** | Yours blocks new entries but never tells anyone to pull the resting entry order. `mark_kill_handled` is the right hook — extend it to demand a cancel acknowledgement |
| 26 | Admin-only kill re-enable | CME (risk admin re-enables), HKEX | ✘ | medium-high | Removing the file silently re-arms the bot. Venues never allow that |
| 27 | Liquidation-only / close-only mode | venue trading states, Eurex | ✘ | **high** | A halted gate should still permit *risk-reducing* orders. Today the gate is entry-only, so exits are simply un-gated — the same blind spot from the other direction |
| 28 | Cancel-on-disconnect / deadman | **CME COD** | ✘ | medium-high | If the strategy loop stops calling the gate for N seconds, treat it as failure: require re-arm, and let the EMS pull orders. The mirror image of the kill switch |
| 29 | Fail-closed on risk-system staleness | 15c3-5 posture; venue heartbeats | ~ | **high** | State defaults fail safe, but a store that stops answering just leaves cached values in place forever. Rule: last successful `refresh_*` older than N seconds ⇒ block |

### 2.5 Control plane and evidence

| # | Capability | Where it comes from | Here | Value | Notes |
|---|---|---|---|---|---|
| 30 | Scoped limit hierarchy | **HKEX PTLG, CME (LCE → exec firm → sender comp), Nasdaq (firm/trader/session)** | ✘ | **high** | Directly answers the README's "single process, single instrument" limitation: per-market/per-strategy child gates plus a parent holding aggregate limits |
| 31 | Scoped kill switches | CME's three independent kill levels | ~ | medium | One global file today; per-scope files are nearly free once scopes exist |
| 32 | Intraday limit change, no restart | universal | ✔ | — | `refresh_runtime_limits` is exactly this |
| 33 | Segregated control (trader can't set own limits) | **15c3-5 "direct and exclusive control"** | ~ | medium | Control plane is a separate module, but the bot process can write the same keys. A read-only store view for the gate closes it |
| 34 | **Audit trail of limit changes and breaches** | 15c3-5, RTS 6 record-keeping, every venue GUI | ✘ | **high** | Biggest credibility gap: toggling the loss-halt bypass leaves no trace of who, when, or why. An append-only event stream (block events, override writes, breaches, releases) costs little and makes the library defensible |
| 35 | Alerting on approach and breach | Nasdaq alert thresholds, Eurex level-1 alert | ✘ | **high** | Optional `on_event` callback; `botdeck` and `feedwatch` are already the consumers |
| 36 | Post-trade reconciliation vs limits | **RTS 6 Art. 16** | ✘ | **high** | The gate trusts the caller's `position_open` boolean absolutely. If it also derived its own view from `record_*` calls, a disagreement between the two would be a blockable condition — that catches EMS bugs no individual limit can |
| 37 | Configurable business-day boundary | venue session calendars | ✘ | medium | UTC midnight is hardcoded via `datetime.now(UTC)`. An injectable clock buys a configurable boundary *and* removes wall-clock dependence from the tests |

---

## 3. Recommended order

**Tier 1 — cheap, no architectural change, keeps `block_reason()` pure**

1. `evaluate() -> Decision` with severity levels and per-limit utilisation (#19, #20)
2. Latching halt with explicit operator release (#24)
3. Stale-quote guard + stale-refresh fail-closed (#8, #29)
4. Duplicate-order window (#4)
5. Rate throttle + repeated-execution throttle (#16, #17) — needs an injectable clock (#37)
6. Two-sided price collar and absolute price bounds (#3)
7. Event stream for blocks, breaches, and override writes (#34, #35)
8. `GateConfig` validation (#23)

**Tier 2 — new inputs or a wider contract**

9. Available-collateral check (#15)
10. Market-state and deny-list / time-to-resolution checks (#6, #7)
11. Depth-aware size check (#5)
12. Unrealized PnL in the halt (#13)
13. Kill and halt emit a mass-cancel action requiring acknowledgement (#25, #26)
14. Self-reconciliation of position state vs the caller's flag (#36)
15. Liquidation-only mode; gate exits as well as entries (#27)

**Tier 3 — structural**

16. Scoped gate hierarchy with aggregate parent limits (#30, #31)
17. Per-limit breach-action policy, including throttle (#21, #22)
18. Deadman / heartbeat (#28)

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
  hierarchies, regulatory reporting.** These are broker/venue obligations, not
  a single-bot concern.
- **Mass-cancel *execution*.** The gate should *decide* and emit the action;
  actually pulling orders belongs to the EMS.
