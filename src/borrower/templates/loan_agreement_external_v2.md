<!--
LOAN AGREEMENT TEMPLATE — External Private Lender — v2 (REVIEWED)

Status: REVIEWED. Promoted from v1-draft to the reviewed production template on
2026-06-06, on the confirmation of MesiCap principal Rain Rosimannus that
Estonian counsel has reviewed the wording. Because the active template is now
reviewed, Bruno renders it WITHOUT the draft banner/watermark (gated on
agreements.TEMPLATE_REVIEWED), so the generated PDF is clean and signable. If a
future template is introduced that has NOT been reviewed, give it a version
ending in "-draft" to re-engage the guard.

Template language: English (lender-facing). Production version is bilingual
Estonian + English; this is the English side. The Estonian translation is
maintained in parallel and controls in case of discrepancy (§15.6).

Variables in {{ double_braces }} are populated by Bruno from the New Loan form.
Variables in {{ counterparty.* }} are populated from the lender's Counterparty
record. See LOAN_AGREEMENT_VARIABLES.md (below) for the full mapping.

Inline "LAWYER REVIEW:" markers below are retained as a record of the clauses
counsel scrutinised during the v1→v2 review; they are stripped from rendered
output and never reach the lender-facing document. (This sentence must not
contain literal comment delimiters, or it would close this block early.)
-->

# LOAN AGREEMENT

**Between**

**MesiCap Technologies OÜ** ("Borrower")
Estonian commercial registry code 17323813
Registered office: Suur-Liiva tn 15-13, Haapsalu linn, 90503, Estonia
Represented by: {{ borrower.represented_by }}

**and**

**{{ counterparty.name }}** ("Lender")
{% if counterparty.type == 'company' %}
Registry code: {{ counterparty.registration_number }}
Legal form: {{ counterparty.legal_form }}
{% endif %}
Address: {{ counterparty.address }}
{% if counterparty.contact_email %}Email: {{ counterparty.contact_email }}{% endif %}
{% if counterparty.represented_by %}Represented by: {{ counterparty.represented_by }}{% endif %}

(each a "Party", together the "Parties")

Date of agreement: {{ contract_date }}
Place of signing: {{ place_of_signing }}

---

## 1. Subject and Principal

1.1. The Lender agrees to make available to the Borrower a loan in the principal amount of **{{ principal_max | currency_words }} ({{ "{:,.2f}".format(principal_max) }} {{ currency }})** (the "Loan").

1.2. The Loan is for the purpose of {{ purpose_description }}. The Borrower may use the proceeds for general corporate purposes consistent with this purpose, including but not limited to working capital, investment activities, and operational expenses.

1.3. The Loan is **non-transferable**. The Lender may not assign, sell, pledge, or otherwise transfer this Loan or any rights hereunder to any third party without the prior written consent of the Borrower. Any purported transfer without such consent is void.

---

## 2. Disbursement

2.1. The Lender shall transfer the principal amount to the Borrower's account at AS LHV Pank (IBAN: EE187700771012126780) on or before {{ origination_date }} (the "Origination Date").

2.2. Disbursement is conditioned on:
 (a) execution of this Agreement by both Parties;
 (b) the Lender having provided the source-of-funds and beneficial-ownership documentation required under Section 12;
 (c) no material adverse change in the Borrower's circumstances between signing and disbursement. Material adverse change means a change that materially impairs the Borrower's ability to perform its payment obligations under this Agreement.

2.3. Failure to disburse by {{ origination_date }} entitles the Borrower to terminate this Agreement without penalty.

---

## 3. Interest

3.1. The Loan bears interest at the rate of **{{ "%.2f" | format(interest_rate_pct) }}% per annum** ({{ interest_rate_type }}), calculated on the {{ day_count_convention }} day-count convention.

3.2. Interest accrues from the Origination Date until the Loan is repaid in full.

{% if interest_treatment == 'capitalizing' %}
3.3. Interest **capitalizes**: accrued interest is added to the principal balance on a daily basis and becomes part of the principal owed. The entire balance (principal plus capitalized interest) is repaid at the Maturity Date per Section 4.
{% elif interest_treatment == 'paid_periodically' %}
3.3. Interest is **paid periodically** in cash. Interest payments are due {{ payment_frequency }}, on or before the {{ payment_day_of_month }} of each {{ payment_frequency_unit }}. The first interest payment is due {{ first_interest_payment_date }}.

3.4. Late interest payments accrue default interest at {{ "%.2f" | format(interest_rate_pct + 2) }}% per annum (the contractual rate plus 2 percentage points) until paid.
{% elif interest_treatment == 'amortizing' %}
3.3. The Loan is **amortizing**: principal and interest are repaid in equal installments per the schedule in Section 4.
{% endif %}

3.5. Interest is computed on the actual outstanding principal balance.

---

## 4. Repayment

{% if repayment_structure == 'bullet' %}
4.1. The Loan is repaid in a single payment on {{ maturity_date }} (the "Maturity Date"). The repayment amount equals the outstanding principal plus any accrued and unpaid interest as of that date.

4.2. The Borrower shall transfer the repayment amount to the Lender's designated account (IBAN: {{ counterparty.iban }}) on or before the Maturity Date.

{% elif repayment_structure == 'amortizing' %}
4.1. The Loan is repaid in {{ installment_count }} equal monthly installments of **{{ "{:,.2f}".format(installment_amount) }} {{ currency }}** each, due on the {{ payment_day_of_month }} of each month starting {{ first_payment_date }}.

4.2. The final installment is due on {{ maturity_date }}.

4.3. Each installment covers principal and interest per a standard amortization schedule, attached as Schedule A.
{% endif %}

4.4. All payments are made in {{ currency }} by bank transfer to the Lender's designated account. The Borrower bears the cost of the bank transfer.

---

## 5. Early Repayment

{% if early_repayment_allowed %}
5.1. The Borrower may repay the Loan in whole or in part at any time, subject to {{ early_repayment_notice_days }} days' prior written notice to the Lender.

5.2. Early repayment includes:
 (a) the principal amount being repaid;
 (b) accrued and unpaid interest up to the early repayment date;
 (c) no early repayment penalty or premium.
{% else %}
5.1. Early repayment is not permitted under this Agreement except with the Lender's prior written consent. The Lender is under no obligation to consent.
{% endif %}

---

## 6. Representations and Warranties

6.1. The Borrower represents and warrants that:
 (a) it is duly incorporated and validly existing under Estonian law;
 (b) it has full corporate authority to enter into this Agreement;
 (c) this Agreement constitutes legal, valid, and binding obligations on the Borrower;
 (d) no event of default (as defined in Section 7) has occurred or is continuing as of the date of this Agreement;
 (e) the Borrower's most recent financial position as disclosed to the Lender is accurate in all material respects.

6.2. The Lender represents and warrants that:
 (a) it is lawfully entitled to enter into this Agreement;
 (b) the funds being loaned originate from lawful sources and are owned beneficially by the Lender (or by the persons disclosed in the beneficial-ownership documentation under Section 12);
 (c) the Loan does not constitute the proceeds of any criminal activity;
 (d) the Lender has obtained any approvals or consents required under its own jurisdiction's law to make this Loan.

---

## 7. Events of Default

7.1. Each of the following constitutes an "Event of Default":
 (a) the Borrower fails to pay any amount due under this Agreement when due, and such failure continues for {{ default_cure_days | default(15) }} business days after written notice from the Lender;
 (b) the Borrower breaches any material representation, warranty, or covenant under this Agreement and such breach (if curable) continues uncured for {{ default_cure_days | default(15) }} business days after written notice;
 (c) the Borrower commences voluntary insolvency proceedings or is the subject of involuntary insolvency proceedings that are not dismissed within 60 days;
 (d) the Borrower ceases to carry on substantially all of its business;
 (e) any judgment in excess of €50,000 is entered against the Borrower and is not satisfied or stayed within 60 days.

7.2. The Lender's failure to declare an Event of Default does not constitute a waiver of the Lender's rights.

<!-- LAWYER REVIEW: Estonian Bankruptcy Act and Reorganisation Act have
specific timing and threshold provisions. Cure periods, judgment thresholds,
and "material" definitions should be aligned with statutory defaults. -->

---

## 8. Acceleration and Remedies

8.1. Upon an Event of Default that is continuing:
 (a) the Lender may, by written notice to the Borrower, declare the entire outstanding principal and accrued interest immediately due and payable;
 (b) the Lender may exercise any other rights and remedies available under this Agreement, Estonian law, or in equity.

8.2. The Lender's remedies are cumulative and non-exclusive. Exercising one remedy does not preclude any other.

<!-- LAWYER REVIEW: acceleration provisions must align with Estonian Law of
Obligations Act §§ on contractual termination and damages. -->

---

## 9. Covenants

9.1. While any amount remains outstanding under this Agreement, the Borrower shall:
 (a) maintain its corporate existence in good standing;
 (b) comply in all material respects with all applicable laws, including Estonian commercial, tax, and AML laws;
 (c) maintain accurate books and records of its financial position;
 (d) provide the Lender with quarterly financial summaries within sixty (60) days after the end of each calendar quarter, covering: (i) outstanding loan obligations to all lenders; (ii) current net asset value; and (iii) any material changes in the Borrower's circumstances. The Borrower shall not be required to prepare audited financial statements or disclose confidential commercial information, proprietary trading strategies, customer-specific information, employee compensation information, or other commercially sensitive information for the purposes of this Section;
 (e) promptly notify the Lender of any Event of Default or any circumstance reasonably expected to result in an Event of Default.

9.2. While any amount remains outstanding, the Borrower shall not, without the Lender's prior written consent:
 (a) incur new senior secured debt that ranks ahead of this Loan;
 (b) make distributions, dividends, or share repurchases that would cause the Borrower's net worth to fall below {{ minimum_net_worth | default("one and a half times (1.5x) the outstanding Loan principal") }};
 (c) merge with or be acquired by another entity (other than a wholly-owned subsidiary structure).

<!-- LAWYER REVIEW: covenant (a) "senior secured debt" — define precisely.
Covenant (b) — net worth definition needs to match Bruno's headroom math
(gross unencumbered assets minus debt obligations, etc.). -->

---

## 10. Reporting and Information Rights

10.1. The Borrower grants the Lender access to a dedicated lender portal (currently planned to be hosted at lender.mesicap.com) where the Lender can view, as it relates to this Loan and the Borrower's overall financial position:
 (a) current outstanding principal and accrued interest;
 (b) payment history;
 (c) the Borrower's aggregate debt-to-asset ratio;
 (d) status of any covenant tests under Section 9.

10.2. The Borrower shall use reasonable efforts to keep the portal current. Information provided through the portal is for the Lender's information only and does not modify or supersede this Agreement.

10.3. The Lender's information rights do not extend to confidential commercial information of the Borrower (e.g., specific trading positions, customer lists, employee compensation).

---

## 11. Subordination Acknowledgment

11.1. The Lender acknowledges and agrees that this Loan ranks **senior** in right of payment to any loans extended to the Borrower by the Borrower's shareholders or shareholder-affiliated entities (collectively, "Shareholder Loans"). The Shareholder Loans are subordinated to this Loan.

11.2. In any liquidation, insolvency, or winding-up of the Borrower:
 (a) the Lender (together with any other senior external lenders) is paid in full before any payment is made on the Shareholder Loans;
 (b) the Shareholder Loans receive payment only from assets remaining after all senior external lenders are satisfied.

11.3. The Borrower represents that as of the date of this Agreement, the Shareholder Loans aggregate approximately {{ shareholder_loan_aggregate | default("[to be filled at draft time]") }} {{ shareholder_loan_currency | default(currency) }} in principal across {{ shareholder_loan_count | default("[to be filled at draft time]") }} loan agreements, all of which are or will be formally subordinated to this Loan via subordination agreements with the respective shareholder lenders.

<!-- LAWYER REVIEW: Section 11 is critical for lender protection and Bruno's
LTV math. The subordination must be enforceable in Estonian insolvency. Verify
that Bruno's data on shareholder loans accurately reflects the subordination
status at draft time. -->

---

## 12. AML, Source of Funds, and Beneficial Ownership

12.1. The Lender confirms that the funds being lent under this Agreement:
 (a) are derived from lawful sources;
 (b) do not constitute proceeds of crime;
 (c) are not subject to any pending legal proceedings, claims, or encumbrances that would prevent the Lender from lending them.

12.2. The Lender shall provide to the Borrower, before disbursement:
 (a) for individuals: passport or ID copy, proof of address, written declaration of source of funds with supporting documentation;
 (b) for entities: certificate of incorporation, list of beneficial owners (any natural person owning ≥ 25%), copy of authorized representative's ID, written declaration of source of funds;
 (c) any additional documentation reasonably requested by the Borrower to comply with Estonian AML obligations.

12.3. The Borrower shall maintain this documentation for the duration of the Loan plus five years thereafter, in compliance with Estonian Money Laundering and Terrorist Financing Prevention Act.

12.4. The Lender agrees to provide updated documentation upon reasonable request if circumstances change (e.g., change of beneficial ownership of the Lender entity).

<!-- LAWYER REVIEW: Section 12 must align with Estonian AML Act § text.
Specifically the thresholds for beneficial ownership disclosure and the
record-keeping period. -->

---

## 13. Governing Law and Dispute Resolution

13.1. This Agreement is governed by the laws of the Republic of Estonia, excluding its conflict-of-laws principles.

13.2. Any dispute arising out of or in connection with this Agreement shall be resolved by the courts of Estonia, with the **Tallinn District Court** (Harju Maakohus) having exclusive first-instance jurisdiction.

13.3. Notwithstanding Section 13.2, the Parties may by mutual written agreement refer any specific dispute to arbitration under the rules of the Estonian Chamber of Commerce and Industry, in which case the seat of arbitration is Tallinn, language is Estonian (or English by agreement), and the arbitral tribunal consists of one arbitrator unless the Parties agree otherwise.

<!-- LAWYER REVIEW: Confirm venue for non-Estonian-resident lenders.
Some lenders may negotiate for different venues; this draft assumes Estonian
preference. -->

---

## 14. Notices

14.1. All notices, requests, and other communications under this Agreement shall be in writing and delivered:
 (a) by hand, with acknowledgment of receipt;
 (b) by registered mail with return receipt;
 (c) by email to the address specified below, with read receipt or replied acknowledgment.

14.2. Notice addresses:

**For the Borrower:**
MesiCap Technologies OÜ
Suur-Liiva tn 15-13, Haapsalu linn, 90503, Estonia
Email: {{ borrower.notice_email }}

**For the Lender:**
{{ counterparty.name }}
{{ counterparty.address }}
{% if counterparty.contact_email %}Email: {{ counterparty.contact_email }}{% endif %}

14.3. Either Party may change its notice address by written notice to the other.

---

## 15. Miscellaneous

15.1. **Entire Agreement.** This Agreement, together with any schedules and any subsequent written amendments signed by both Parties, constitutes the entire agreement between the Parties regarding the Loan and supersedes all prior negotiations, representations, and agreements.

15.2. **Amendment.** This Agreement may be amended only by written instrument signed by both Parties. Amendments are recorded in the Borrower's loan management system as part of the Loan's audit trail.

15.3. **Severability.** If any provision of this Agreement is held invalid or unenforceable, the remaining provisions remain in full force.

15.4. **Waiver.** No failure or delay by either Party in exercising any right constitutes a waiver of that right. Any waiver must be in writing to be effective.

15.5. **Counterparts.** This Agreement may be signed in counterparts (including electronic counterparts), each of which is an original.

15.6. **Language.** This Agreement is executed in both Estonian and English. **In case of any discrepancy, the Estonian version controls.**

15.7. **Confidentiality.** Each Party shall keep the terms of this Agreement confidential, except as required by law, regulation, or court order, or with the other Party's written consent. The Lender may disclose the existence and basic terms of the Loan to its tax advisors, auditors, and beneficial owners on a need-to-know basis.

15.8. **Force Majeure.** No Party shall be deemed in breach of this Agreement or in default of any obligation (other than payment obligations already due and payable) to the extent performance is prevented, delayed, or made impracticable by circumstances beyond its reasonable control, including natural disasters, war, civil unrest, governmental actions, sanctions, failures of banking or payment systems, telecommunications outages, cyber incidents, or similar events. Any affected deadline shall be automatically extended for the duration of such event, and the affected Party shall notify the other Party as soon as reasonably practicable.

15.9. In no event shall either Party be liable for indirect, consequential, special, or punitive damages arising from this Agreement.

15.10. **"Net Worth"** means the Borrower's total assets minus total liabilities, calculated on a non-consolidated basis in accordance with the Borrower's ordinary accounting practices consistently applied, as reflected in the most recent management accounts.

---

## Signatures

**For the Borrower:**

MesiCap Technologies OÜ

_____________________________
{{ borrower.represented_by }}
Title: {{ borrower.title }}
Date: ___________
Place: ___________

**For the Lender:**

{{ counterparty.name }}

_____________________________
{{ counterparty.represented_by | default(counterparty.name) }}
{% if counterparty.type == 'company' %}Title: {{ counterparty.represented_by_title }}{% endif %}
Date: ___________
Place: ___________

---

## Schedule A: Amortization Schedule

{% if repayment_structure == 'amortizing' %}
[Bruno populates this with the per-installment principal/interest breakdown
calculated from the loan parameters.]
{% else %}
Not applicable (Loan is not amortizing).
{% endif %}

---

<!--
END OF CONTRACT TEMPLATE

The following sections are not part of the contract — they are documentation
for Bruno developers and operators.
-->
