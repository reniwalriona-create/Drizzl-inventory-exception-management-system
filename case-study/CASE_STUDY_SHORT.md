# Short Case Study: Inventory and Exception Management for a Growing Beverage Company

## How I transformed fragmented spreadsheets into a deployed operating system

**Role:** Product & Business Analyst / Project Lead<br>
**Additional ownership:** Workflow and data design, UX, AI-assisted implementation, validation, and deployment<br>
**Status:** Deployed August 26, 2026; in a controlled two-month pilot<br>
**Product:** Internal operations application built with Python, Flask, and PostgreSQL

> This case study uses fictional identifiers, synthetic documents, and sanitized screenshots. It discusses Drizzl's operating workflow without exposing production records or private company materials.

[**Watch the 2:27 demo walkthrough →**](https://youtu.be/jKcDjJRn7pU)

[Read the long case study](../README.md) · [Read the technical documentation](../TECHNICAL_README.md)

The video demonstrates the end-to-end workflow with synthetic data and concise feature callouts; the case study below explains the decisions behind the product.

## Executive summary

Drizzl is an early-stage probiotic soda company with seven products. It supplies consumer-facing commerce platforms through a B2B fulfillment process, sells to retailers, cafés, and corporate customers, and moves stock through flea markets, promotions, consignment partners, samples, and internal transfers. Two team members coordinate these operations across Drizzl's warehouses in Mumbai and Bangalore and approximately 15 customer fulfillment locations across India.

The operating process was split across several spreadsheets, delivery schedules, email, and commerce-platform portals. Informal movements were not consistently recorded, Goods Received Notices could be missed, and discrepancy notes and customer debits were rarely reconciled. The spreadsheet could show sufficient inventory while the warehouse was unable to fulfill an order.

I converted this ambiguous process into a deployed inventory and exception-management system. It connects B2B purchase orders, receipts, shortfalls, discrepancy notes, and debits while also capturing production, transfers, direct sales, and losses. Instead of maintaining an editable stock total, the product calculates inventory by product and location from a traceable history of movements.

![Stock visualization showing inventory by product and location](assets/13-stock-visualizations.png)

*Using synthetic demonstration data, the reporting layer calculates and filters stock by canonical product and location.*

## The problem I uncovered

Discovery with a Drizzl cofounder and the employee responsible for inventory and fulfillment revealed two connected inventory flows:

- **B2B fulfillment:** Commerce platforms send purchase orders (POs); Drizzl ships from one of its warehouses; a Goods Received Notice (GRN) records what the customer received; and a later discrepancy note explains rejected or missing units and any debit.
- **Manual operations:** Production, transfers, flea markets, promotions, samples, testing, consignment stock, direct sales, damage, and expiry change physical inventory without generating the same document chain.

Neither flow was captured completely. One spreadsheet attempted to connect POs and GRNs, another tracked delivery schedules, and new orders were usually discovered through email. Because GRNs arrived later, older documents could be forgotten. Employees or cofounders could also take cans for an event or trial without a suitable place to record the movement.

This created three immediate business risks:

1. Orders could not be fulfilled even when the spreadsheet showed available stock.
2. Shortfalls and customer debits were not consistently tracked.
3. Founders spent significant time reconstructing records instead of managing operations.

The original request was simply to improve the PO–GRN spreadsheet and provide a clearer view of inventory. Discovery expanded the scope: a reliable answer required manual movement capture, cross-platform product mapping, discrepancy and debit reconciliation, and an accountable correction process—not just a better spreadsheet.

## How the product works

Platform documents follow a controlled path before they affect official records:

```text
PO → validate products and assign the supplying warehouse → post commitment
GRN → compare ordered and received quantities → record fulfillment and shortfall
Discrepancy → classify the existing shortfall and attach its debit value
```

Manual movements capture the rest of the operation: production and opening balances add stock, transfers relocate it, and sales or losses remove it. Both paths use the same internal products, locations, movement history, and reporting layer.

![Staged PO review showing validation and warehouse assignment](assets/04-po-review.png)

*Imports remain staged until mapping and reference problems are resolved and a source warehouse is assigned.*

## Four decisions that made the system trustworthy

### 1. One product identity across every channel

The same Drizzl product can have different SKUs on different commerce platforms. I created a canonical Master Product model so those external identifiers resolve to one internal product. Unknown mappings block posting rather than silently creating fragmented stock pools.

### 2. Review imported documents before posting

An uploaded file never writes directly to inventory. Users first review duplicates, missing references, product mappings, quantities, and exceptions. An explicit posting action then creates the official commitment or movement. This separates potentially messy source data from trusted operational records.

### 3. Separate commitment, physical movement, and financial explanation

A PO reserves stock but does not remove it because an order alone does not prove fulfillment. The matching GRN triggers the physical movement: received units become a sale, while the difference between ordered and received units becomes an unclassified shortfall. A later discrepancy note classifies that existing shortfall and its debit without deducting inventory twice.

Rejected, excess, or incorrect units are not currently returned when collection would cost more than the stock; under the agreed operating rule, they remain classified as a loss rather than re-entering available warehouse inventory.

### 4. Correct history instead of deleting it

Operational records can be voided, restored, or superseded. Current calculations remain correct while the original record, responsible user, reason, and correction path remain visible.

![Fulfillment tracker connecting POs, GRNs, and discrepancies](assets/07-fulfillment-tracker.png)

*The tracker replaces manual document searches with one view of commitments, receipts, shortfalls, and discrepancy status.*

## My ownership and product strategy

I owned problem framing, requirements, business rules, workflow and data design, UX, implementation, testing, and deployment. Drizzl's stakeholders supplied operational knowledge and reviewed the evolving workflow; I remained responsible for product and implementation decisions. I used AI-assisted development tools while retaining responsibility for the architecture, logic, validation, and final output.

The project began August 7. Through August 12, I interviewed stakeholders, mapped the workflow, collected historical PO/GRN/discrepancy samples, and defined acceptance criteria. I then designed, built, and tested iteratively, with weekly reviews and WhatsApp feedback across time zones. From August 22–26, I completed end-to-end verification, shared a full workflow video, led a guided screen-share walkthrough with both intended users, and deployed the product.

| Delivery area | Approach |
|---|---|
| Stakeholders | Cofounder and employee responsible for inventory and fulfillment |
| Requirements | Interview notes, process maps, sample-document analysis, and acceptance criteria |
| Scope decision | Prioritize operational truth and exception management before commercial analytics |
| Key risks | Changing CSV formats, incorrect product mappings, omitted manual movements, and unsafe testing |
| Mitigations | Staged validation, explicit SKU mapping, dedicated manual entry, user attribution, and disposable test databases |
| Rollout | Workflow video, guided two-user walkthrough, deployment, and controlled two-month pilot |

I deliberately sequenced the roadmap:

- **Phase 1:** Establish trustworthy products, locations, movements, commitments, and PO-to-GRN fulfillment.
- **Phase 2:** Add shortfall classification, debit reporting, document lookup, activity history, and non-destructive corrections.
- **Deferred:** Revenue, margin, and broader commercial analytics. Those metrics would not be credible until the operational data was trustworthy.

## Tools and implementation

I built the product with Python, Flask, PostgreSQL and SQL, psycopg2, HTML/CSS, JavaScript, and Chart.js. Flask-Login, Werkzeug, and Flask-WTF support authentication and secure forms, while Gunicorn serves the deployed application.

## From operational data to decision support

```text
Platform CSVs + manual entries
             ↓
Normalize, validate, and map external SKUs
             ↓
Canonical products, locations, and document relationships
             ↓
Commitments + inventory movements + discrepancy classifications
             ↓
Stock, fulfillment, shortfall, debit, and audit reporting
```

I defined the core measures in business terms: **on hand** is physical stock at a location; **committed** is stock reserved for open POs; **uncommitted** is what remains available; **shortfall** is ordered minus received units; **shortfall rate** is shortfall divided by ordered units; and **debit value** is the amount charged to Drizzl for the exception. A PO is **awaiting discrepancy** only after its GRN posts with a positive shortfall—an exact receipt requires no discrepancy note.

Current reports can be examined by product, location, PO, business date, fulfillment status, and discrepancy cause. This supports operational decisions about available stock, unresolved orders, and where units or debit value are being lost. Product popularity by city, route-level damage patterns, and profitability are intentionally deferred analytical opportunities, not capabilities claimed for the current release.

## Validation and results to date

Workflow testing changed the product design. In one test, a production entry used the same location as both source and destination, causing the inflow and outflow to cancel. I added interface constraints, server-side validation, and regression coverage: production can only have a destination, sales and losses can only have a source, and transfers cannot use the same location on both sides.

The verification gate covers product identity, imports, posting, ledger behavior, corrections, security, reporting, and database integrity. Mutating tests use disposable PostgreSQL databases rather than operational records.

During the guided user walkthrough, the team initially questioned when “Needs discrepancy” should increase. I clarified that the state begins only after a GRN posts with a shortfall, not when the PO is created and not when the received quantity matches exactly. The walkthrough confirmed the core acceptance criteria: documents could be staged and matched, invalid records were blocked, inventory moved once, shortfalls appeared at the correct stage, and corrections preserved history.

![Debits and Losses report showing classified exceptions](assets/09-debits-and-losses.png)

*Shortfall causes, affected units, and customer debit values are consolidated without creating a second stock deduction.*

The deployed prototype replaces disconnected records with one traceable operating model:

| Before | With the deployed prototype |
|---|---|
| Inventory depended on incomplete spreadsheet entries. | Stock is calculated from product- and location-level movements. |
| PO, GRN, and discrepancy records required manual reconstruction. | One tracker connects the fulfillment chain and its current state. |
| External SKUs could fragment the same physical product. | Platform SKUs resolve to one canonical product identity. |
| Shortfalls and debits lacked a consolidated view. | Exceptions are classified and connected to debit reporting. |
| Corrections risked destroying context. | Void, restore, and supersession preserve history. |

Because the controlled pilot has just begun, I do not yet claim measured business impact. Over the two-month pilot, the team will evaluate reconciliation time, missing-document frequency, shortfalls and debit value, inventory warnings, manual-movement adoption, and user feedback.

## Business recommendations

1. **Make manual movement logging part of daily operations** so production, transfers, events, samples, office consumption, sales, and losses are recorded when they happen.
2. **Review incomplete document chains weekly and escalate aging POs.** Any PO still without a Goods Received Notice after two weeks should be checked for expiry, non-shipment, rejection, or a replacement reference.
3. **Separate unshipped units from post-dispatch losses** so inventory and exception reporting reflect what physically occurred.
4. **Monitor shortfalls, damage, and debit value** by platform, receiving location, product, and eventually route as sufficient data accumulates.
5. **Strengthen controls as usage grows** through role-based permissions, self-service SKU administration, automated backups, and clearer separation of duties.

## Next steps

Phase 3 will expand the product from operational control into commercial intelligence:

- Imported versus manually recorded sales
- Revenue and gross-margin views
- Inventory value lost to damage, expiry, and short delivery
- Estimated sales value affected by stock loss
- Product and channel performance
- Production-planning signals

This phase remains intentionally postponed until the pilot produces enough reliable operational data to support meaningful analysis.
