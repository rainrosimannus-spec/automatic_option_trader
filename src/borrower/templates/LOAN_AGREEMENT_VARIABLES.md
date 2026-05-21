# Loan Agreement Template — Variables Reference

This document maps template variables to Bruno data fields and operator inputs.

## From the Counterparty record (the lender)

| Template variable | Bruno field |
|---|---|
| `counterparty.name` | `Counterparty.name` |
| `counterparty.type` | `Counterparty.type` (`individual` / `company`) |
| `counterparty.registration_number` | `Counterparty.registration_number` |
| `counterparty.legal_form` | `Counterparty.legal_form` |
| `counterparty.address` | `Counterparty.address` |
| `counterparty.iban` | `Counterparty.iban` |
| `counterparty.contact_email` | `Counterparty.contact_email` |
| `counterparty.represented_by` | NEW FIELD NEEDED on Counterparty (for company lenders, the authorized representative) |
| `counterparty.represented_by_title` | NEW FIELD NEEDED on Counterparty |

## From the Loan record

| Template variable | Bruno field |
|---|---|
| `principal_max` | `Loan.principal_max` |
| `currency` | `Loan.currency` |
| `interest_rate_pct` | `Loan.interest_rate_annual * 100` |
| `interest_rate_type` | `Loan.interest_rate_type` (`fixed` / `variable_plus_spread`) |
| `day_count_convention` | `Loan.day_count_convention` (`act_360` / `act_365` / `30_360`) |
| `interest_treatment` | `Loan.interest_treatment` |
| `repayment_structure` | `Loan.repayment_structure` |
| `payment_frequency` | `Loan.payment_frequency` |
| `payment_day_of_month` | `Loan.payment_day_of_month` |
| `installment_amount` | `Loan.installment_amount` (amortizing only) |
| `installment_count` | Computed from term and payment_frequency |
| `contract_date` | `Loan.contract_date` |
| `origination_date` | `Loan.origination_date` |
| `maturity_date` | `Loan.maturity_date` |
| `early_repayment_allowed` | `Loan.early_repayment_allowed` |
| `early_repayment_notice_days` | `Loan.early_repayment_notice_days` |
| `purpose_description` | Derived from `Loan.purpose` + free text |
| `first_interest_payment_date` | Computed from origination + payment_frequency |
| `first_payment_date` | Same as above for amortizing |
| `payment_frequency_unit` | "month" / "quarter" / "year" derived from `payment_frequency` |

## From operator input (added at draft generation time)

| Template variable | Source |
|---|---|
| `borrower.represented_by` | Per-loan input — who signs for MesiCap (usually Rain) |
| `borrower.title` | Per-loan input — usually "Director" or "Board Member" |
| `borrower.notice_email` | Configurable; defaults to MesiCap's official contact email |
| `place_of_signing` | Per-loan input — defaults to "Tallinn, Estonia" or "Luxembourg" |
| `default_cure_days` | Per-loan input or template default of 15 |
| `minimum_net_worth` | Per-loan input or default of "2x outstanding principal" |
| `shareholder_loan_aggregate` | Computed from Bruno's existing shareholder loans (sum of outstanding) |
| `shareholder_loan_count` | Computed from Bruno (count of shareholder loans) |
| `shareholder_loan_currency` | Default to EUR for the aggregate disclosure |

## Schema additions needed in Bruno

Two new fields on `Counterparty`:
- `represented_by` (text, optional) — for companies, the authorized signatory
- `represented_by_title` (text, optional) — e.g., "Director", "Managing Member"

These can be added via a simple migration when ready to ship contract generation.
