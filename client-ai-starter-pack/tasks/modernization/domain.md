# Legacy behavior and migration: exercise rules

These fictional rules are the source of truth for this assignment. No industry research is required.

1. The supplied order_total(quantity, unit_cents, member) function accepts nonnegative integer quantities and prices. Negative values raise ValueError. A zero quantity returns zero with no delivery charge.
2. For members, deduct 10% from quantity times unit price. Round the discount to the nearest cent, with halves rounded up.
3. Apply the delivery threshold after the discount: delivery is free when the discounted subtotal is at least 10,000 cents; otherwise add 500 cents.
4. Refactor this module without changing its language, dependencies, interface, or behavior. Keep the baseline and comparison cases unchanged. Document any discrepancy between the description and source.

## Worked example

A member orders one item at 11,000 cents. The discount leaves 9,900, below the free-delivery threshold. Adding 500 gives 10,400 cents. Applying the threshold before the discount would change the result.

## Extend the starter

Use the supplied module, rules, and five cases. Add boundary cases if useful. Create a separate deliberately incorrect candidate to show that the comparison detects a changed rule.

Record any unresolved ambiguity in your README. Do not silently add domain rules. Keep seed cases and their expected results so the reviewer can run the same checks.

## Fixed check command

From this task folder, run `python3 check.py baseline.py`. For a proposed copy, run `python3 check.py candidate.py`. Keep the check script and reference cases unchanged. Use a timeout when your assistant runs this command.
