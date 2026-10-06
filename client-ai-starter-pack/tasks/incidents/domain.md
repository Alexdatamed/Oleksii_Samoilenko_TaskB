# Incident-to-patch assistant: exercise rules

These fictional rules are the source of truth for this assignment. No industry research is required.

1. The supplied module calculates a unit price from a total in integer cents and a whole-number quantity. For quantity zero, it must return zero. Negative arguments raise ValueError.
2. For positive quantities, divide total cents by quantity and round to the nearest cent, with halves rounded up.
3. The seed has one zero-quantity defect. Keep the original module and reference tests unchanged. Apply a proposed patch only to a separate copy of the module.
4. Use the fixed check command to compare baseline and proposed code. The assistant must read the files and call a model at runtime; hardcoding the known fix does not complete the task.

## Worked example

For total_cents=0 and quantity=0, the required result is 0. The supplied baseline raises ZeroDivisionError. Its other four reference cases pass.

## Extend the starter

Use the supplied defect and checks. Add a few event messages or input examples without changing the specified behavior. Freeze the final fixture before running the repair assistant.

Record any unresolved ambiguity in your README. Do not silently add domain rules. Keep seed cases and their expected results so the reviewer can run the same checks.

## Fixed check command

From this task folder, run `python3 check.py baseline.py`. For a proposed copy, run `python3 check.py candidate.py`. Keep the check script and reference cases unchanged. Use a timeout when your assistant runs this command.
