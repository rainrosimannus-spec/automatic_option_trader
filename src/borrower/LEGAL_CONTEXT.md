# Bruno — Legal Context Memo

Short reference for what Bruno's lending structure is allowed to do, and what would cross legal lines. Read before changing any code that touches loan terms, lender onboarding, marketing copy, or external communications.

## Jurisdiction

MesiCap Technologies OÜ is Estonian. Bruno's regulatory perimeter is set by Estonian law:
- **Krediidiasutuste seadus** (Credit Institutions Act) — defines what a bank is
- **Investeerimisfondide seadus** + AIFMD (Alternative Investment Fund Managers Directive, EU) — defines what a fund is
- Estonian Commercial Code — general company law

EU passporting matters for any lender domiciled outside Estonia.

## Two regulatory traps Bruno must stay out of

### 1. Credit institution (bank) territory

MesiCap must NOT become a credit institution. The trigger under Estonian law is **taking deposits from the public**. Bruno's lending model avoids this through:

- **No demand deposits.** Every loan has a fixed term and maturity date.
- **No public solicitation.** Lenders are individually approached, not advertised to.
- **No deposit-like marketing.** Never call shareholder loans or external private loans "deposits," "savings," "accounts," or anything that suggests bank-like character.
- **Limited lender count.** Cap at ~20 lenders. Beyond that, regulators may interpret it as effectively public.
- **Varied terms.** Each loan agreement is individually negotiated; no standard "deposit product" identical across lenders.

### 2. AIF (Alternative Investment Fund) territory

MesiCap must NOT become an AIF, which would trigger AIFMD compliance burden. The trigger is **pooling capital from investors for collective investment per a defined investment policy**. Bruno avoids this through:

- **Fixed contractual interest** (not performance-linked returns to lenders)
- **No investment policy disclosed to lenders.** Lenders see the loan terms, not a strategy document
- **No pooled accounting.** Each loan tracked separately; no fund-share concept
- **Individually-negotiated agreements.** Not interests in a common pool
- **Non-transferable loan interests.** Lenders can't trade their position to third parties

## Hard rules — never violate

These are non-negotiable. If Bruno code or generated content would breach any of them, refuse and flag for human review.

1. **No kickers or performance-linked returns to lenders.** Interest is fixed (or fixed-with-spread-formula). No "20% of trading profits above X."
2. **No public solicitation.** No marketing pages, no listings, no public offers. Lender onboarding is private, by direct contact.
3. **Max ~20 lenders.** Operational ceiling for staying outside credit institution territory.
4. **No deposit language.** Avoid "deposit," "account," "savings," "balance" in lender-facing materials. Use "loan," "credit," "principal," "facility."
5. **No transferability.** Loan agreements must include non-transferability clauses (each loan is between MesiCap and the named lender; lender cannot assign without MesiCap's consent).
6. **No pooled language.** Avoid "fund," "portfolio of lenders," "investment pool." Each loan is a bilateral agreement.
7. **Subordination clauses in shareholder loans.** Before Phase 3 launches, shareholder loans (Waddy, Arvutitugi operational + all trading capital) must be formally subordinated to external lenders via master agreement amendment.
8. **No financial advice to lenders.** Bruno doesn't recommend; it just records terms the parties already agreed.
9. **Truthful disclosure to lenders.** When lender-facing portal launches at lender.mesicap.com (Phase 3), it must show accurate loan terms, accurate accrued interest, and accurate MesiCap solvency posture. Misrepresentation creates personal liability for the principals.
10. **AML/KYC on external lenders.** When taking money from someone outside the three principals (Rain, Rasmus, Lauri), basic KYC is required: identification, source-of-funds declaration, beneficial ownership disclosure for entities. Estonian AML law applies even to non-bank lending arrangements.

## What's allowed

- **Shareholder loans** (Rain, Rasmus, Lauri via their entities) — at any reasonable interest rate. Currently 5% (operational + trading capital) and 11.55% (Thirona octoserver back-to-back).
- **Back-to-back loans** — lender takes a bank loan, on-lends to MesiCap at a small margin. Thirona's octoserver loan is the existing example.
- **Capitalizing interest** — interest adds to principal balance, paid at maturity (vs. periodic cash interest). Documented in each loan agreement.
- **Restructuring with notice** — changing rate or principal mid-life is allowed if both parties agree and the amendment is documented (Bruno's LoanAmendment table captures this).
- **Multi-currency exposure** — MesiCap can borrow in EUR, USD, AUD, etc. The Arvutitugi USD tranche ($11,500) is an example.
- **Operational facilities** — revolving credit lines with monthly drawdown rights, like the Waddy/Arvutitugi €17,500 facilities.
- **Phase 3 external private lending** — taking money from non-shareholders is allowed, subject to:
  - Each lender individually negotiated
  - Total lender count ≤ ~20
  - Subordination of shareholder loans completed first
  - AML/KYC documented
  - Four-metric headroom check passed (Bruno's gate)
  - Loan agreement reviewed by Estonian counsel before signing

## What requires lawyer review

Bruno can generate draft agreements from templates (Path 3 commitment), but **every new lender's first contract** must pass through Estonian corporate counsel before signing. Pattern:
1. Bruno generates draft contract from template + form data
2. Lawyer reviews — checks for jurisdiction-specific clauses, AML compliance, any unusual lender requests
3. Both parties sign
4. Signed PDF uploaded to Bruno as the canonical artifact

After 5-10 lenders have used the same template, lawyer review may become optional for standard cases — but the template itself must be lawyer-drafted and lawyer-approved before first use.

## Contract template requirements (when Bruno generates them)

Each generated contract must include:

- Parties (lender, MesiCap with registration number)
- Principal amount and currency
- Interest rate (fixed value or formula)
- Day count convention
- Interest treatment (capitalizing / paid quarterly / amortizing)
- Term (origination date, maturity date)
- Repayment structure (bullet / amortizing / revolving)
- Early repayment terms (allowed/not, notice period)
- Subordination clause (where applicable)
- Non-transferability clause
- Governing law (Estonian)
- Dispute resolution (Estonian courts or agreed arbitration)
- Lender's source-of-funds declaration
- Lender's beneficial ownership disclosure (for entity lenders)
- Signatures with date and place

Templates must NOT include:
- Performance kickers
- Pool-share language
- Public-offer language
- Transferability without consent
- Deposit terminology

## When in doubt

If Bruno is asked to generate content, automate communication, or implement a feature that touches the regulatory perimeter and you're uncertain whether it's allowed:

1. **Refuse the automation. Flag to the human.** A wrong move on regulatory boundary can create personal liability for Rain, Rasmus, and Lauri.
2. **Suggest lawyer consultation** rather than guessing.
3. **Default to the conservative interpretation** — assume the action is not allowed unless explicitly permitted in this document.

The cost of being too cautious is a delay. The cost of crossing the perimeter is a regulatory investigation, personal fines for the principals, possible criminal liability, and reputational damage that ends MesiCap. Always err conservative.

## Updating this document

When legal context changes — new lawyer guidance, regulatory updates, new lender types, new jurisdictions — update this file and commit. Bruno code should treat this document as authoritative; if code conflicts with this document, the document wins until updated.
