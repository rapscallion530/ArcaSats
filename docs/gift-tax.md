# Gifting Bitcoin — US tax & the recipient basis statement

*Researched 2026-06-17. NOT tax advice — figures change yearly; verify against current IRS guidance before relying on this.*

## The gift itself is not a taxable event
Gifting crypto is **not** a sale. The **donor** recognizes no capital gain at transfer, and the
**recipient** recognizes no income on receipt. Tax happens later, when the recipient **sells/disposes**.
(IRS Virtual Currency FAQ; recipient reports nothing until disposal.)

## Donor reporting — Form 709
- File **Form 709 (US Gift Tax Return)** if total gifts to **one** recipient in a year exceed the
  **annual exclusion**: **$19,000 (2026)** per donor per recipient ($38,000 if married and splitting gifts).
- Filing usually means **no tax due** — it just draws down the **lifetime exemption: $15,000,000 (2026)**
  (made "permanent" + inflation-indexed by the One Big Beautiful Bill Act, signed 2025-07-04; was $13.99M in 2025).
- Form 709 is due with the income tax return (≈ April 15, extendable). Valuation = **FMV in USD at the gift date**.
- Below $19k/recipient/year → no 709 needed. (1040 digital-asset question still applies.)

## Recipient's cost basis — carryover & the dual-basis rule
- **FMV at gift ≥ donor's basis** (the usual case for appreciated BTC): recipient takes the donor's
  **adjusted cost basis (carryover)** and **tacks the donor's holding period** (so long-term status carries).
- **FMV at gift < donor's basis** (gifted at a loss): **dual basis** —
  - basis for computing a **gain** = donor's basis;
  - basis for computing a **loss** = **FMV at gift date**;
  - sale price between the two → **no gain or loss** (IRS Pub 551).
  - When the FMV/loss basis applies, the recipient's holding period starts at the gift date.
- **No documentation → IRS may assign ZERO basis** → entire proceeds taxable. Under the per-wallet rules
  (effective 2025-01-01), undocumented transfers break the basis chain. → The gift statement below is essential.

## What to give the recipient (the gift basis statement / "gift letter")
A bona fide-gift letter the recipient keeps for their records:
1. Donor full legal name (+ contact)
2. Recipient full legal name
3. Asset & amount (e.g. 0.25000000 BTC)
4. Gift date (and time)
5. On-chain transaction ID (chain of custody)
6. **Donor's acquisition date(s)** of the gifted coins (for holding-period tacking)
7. **Donor's adjusted cost basis** (the carryover basis) — USD
8. **FMV in USD at the gift date**
9. Gift tax paid by donor, if any (rare; can add to basis)
10. Statement that it is a **bona fide gift** — no repayment, goods, or services expected
11. Signature/date

→ Recipient then records: **basis-for-gain = donor basis**, **basis-for-loss = min(donor basis, FMV)**,
**holding period = tacked from donor** (gain case).

## How this app can auto-fill it
For a `transfer_out` flagged as a gift to a different owner, the app already knows:
- amount, date, txid (from the transaction);
- **carryover basis** = the FIFO basis the engine consumed for that transfer_out (`transfer_out_basis`);
- **FMV at gift date** = the backfilled USD value;
- donor (account owner) and recipient (destination account owner).
Donor supplies: recipient legal name, gift tax paid (usually $0). Acquisition date(s) need the engine to
expose the consumed lots' dates (small extension).

## Sources
- IRS Virtual Currency FAQ: https://www.irs.gov/individuals/international-taxpayers/frequently-asked-questions-on-virtual-currency-transactions
- IRS Pub 551 (Basis of Assets) — dual-basis rule for gifts.
- 2026 exemption ($15M) — Morgan Lewis: https://www.morganlewis.com/pubs/2025/10/irs-announces-increased-gift-and-estate-tax-exemption-amounts-for-2026
- Annual exclusion $19k (2026): https://wealthvieu.com/gift-tax-exclusion-limit/
- Crypto gift treatment + gift letter contents: https://www.taxbit.com/blogs/crypto-tax-prep-what-are-the-tax-implications-of-gifting-digital-assets · https://cointracking.info/tax-guides/united-states/crypto-gift-tax/
