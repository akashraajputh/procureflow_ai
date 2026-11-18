# Corvo procurement decisions and policy clarifications

This project implements the Corvo Engineering Procurement Policy v5.0 with decisions and assumptions recorded where the policy is silent or where there is ambiguity in the source materials.

## 1) Item matching and new items
- If the requester supplies an item code, it is preferred over description matching.
- If no code is supplied, a supplier description is matched against the item master using the closest description and a conservative similarity threshold.
- New items (not found in the item master) always follow `NEW_ITEM_SOURCING` and are valued at the requester estimate.
- New items are never clubbed with other requirements.

## 2) Requirement clubbing
- Requirements are clubbed only when they are for the same item, same plant, created within 14 days of the first line, and have need-by dates within seven days of the first requirement.
- If any line in the group is marked Emergency, the combined requirement is treated as emergency for routing purposes.
- Clubbing is performed before value and supplier checks to match policy section 2.1 and 2.2.

## 3) Contract selection
- When a contract is valid and has enough ceiling left, it takes precedence because policy section 5.2 is earlier than all value thresholds.
- If multiple valid contracts exist, the lowest-rate contract with remaining ceiling is chosen.
- The contract register is treated as the governance source, while PDF-only contracts are accepted when the start date is after the last register update and before the PR requirement date.

## 4) Preferred supplier ranking model
- Every active supplier with a purchase history for the item in the last 548 days or a valid contract covering the item is included in the preferred list.
- Ranking uses a transparent, explainable score:
  - purchase frequency (highest weight)
  - recency of last PO (1.0 within 90 days, 0.5 within 365 days, else 0)
  - whether the supplier currently holds a relevant contract
  - on-time delivery percentage
  - quality rating
- Blocked or inactive suppliers are excluded from all recommended lists and direct PO routes.

## 5) Sourcing suggestions
- `RFQ` uses the top three preferred suppliers when available.
- `RFP` uses the top five preferred suppliers when available.
- When the preferred list is shorter than required, the system adds additional active suppliers from the same category, ordered by on-time and quality performance.

## 6) Consolidation of POs
- Requirements that become direct POs are consolidated into a single PO when they share a supplier, same plant, same route, and were raised within 24 hours of the earliest requirement on the PO.
- Sourcing requests are never consolidated into a PO.

## 7) Silent-policy decisions made in this implementation
- If the requester supplies only free-text description with no item code and the item similarity falls below the confidence threshold, the system treats the line as an unmatched item rather than forcing a wrong match.
- The system records route and clause under the policy condition that matched the requirement and keeps the decision explanation in the API response payload.
- For requirement value when no contract applies and no recent PO exists, the item master standard price is used as the fallback value.
