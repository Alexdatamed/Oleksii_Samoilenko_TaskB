# Business UI operator: exercise rules

These fictional rules are the source of truth for this assignment. No industry research is required.

1. The portal has six fictional vendors. Select a vendor by its exact ID or an unambiguous name. A partial name matching two vendors needs clarification.
2. Address line, city, postal code, and country are required nonempty fields. Treat postal codes as text. Only postal-address fields may change.
3. Save proposed changes as drafts. Do not change vendor IDs or names, and do not overwrite the original address. The same request ID must not produce duplicate drafts.
4. The supplied portal has a switch that fails the next save. Inspect the result before retrying; allow at most one retry and verify the saved draft.

## Worked example

The request names “Northstar Supplies,” while the portal contains Northstar Supplies East and Northstar Supplies West. Ask for the vendor ID and leave both records unchanged.

## Extend the starter

Use the supplied portal and five requests. You may add requests or restyle the portal, but preserve the identity, address, draft, and failed-save rules. Build the model-driven operator separately.

Record any unresolved ambiguity in your README. Do not silently add domain rules. Keep seed cases and their expected results so the reviewer can run the same checks.

## Open the portal

From the starter-pack root, run `python3 -m http.server 8766 --bind 127.0.0.1 --directory tasks/computer-use`, then open `http://127.0.0.1:8766/portal.html`. Use “Reset fictional drafts” before repeating a demonstration. The portal is the automation target; it contains no AI operator.
