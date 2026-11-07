# Good first issues

Seeded issues for new contributors. File each one on GitHub with the
`good first issue` and `help wanted` labels.

## 1. Support CSV input

Title: Accept ratecon and invoice as CSV files

Description: Accept a CSV file for each input with columns
load_number, linehaul, fuel_surcharge, detention, lumper,
other_accessorials, so dispatchers can feed a spreadsheet export
directly. Other accessorials in CSV form is a semicolon separated
list of name=amount pairs, for example "tarp fee=65;pallet fee=40".
Empty cells mean the line is zero.

Difficulty: Easy

Acceptance criteria: a CSV example pair under examples/ reconciles
to the same findings as the equivalent JSON pair; a test covers CSV
input; README documents the CSV columns.

## 2. Add a per-load summary table

Title: Print a compact summary table in text output

Description: After the per-load detail, print a compact table with
one row per load (load number, ratecon total, invoice total,
difference, status) aligned in columns, so the report is easy to
paste into a broker email.

Difficulty: Easy

Acceptance criteria: text output contains the table with all loads;
amounts match the existing detail lines; JSON output is unchanged;
a test checks the table rows.

## 3. Support a ratecon text dump as input

Title: Parse a plain-text rate confirmation into a ratecon JSON

Description: Add a --ratecon-text flag that reads a broker rate
confirmation pasted as plain text (load number, linehaul, fuel
surcharge, detention, lumper lines) and converts it to the JSON
shape this tool already reconciles, so a dispatcher who only has
the emailed ratecon can still run the check. Reuse the parsing
approach of the ratecon-reader project where it fits.

Difficulty: Medium

Acceptance criteria: a sample ratecon text file under examples/
converts to the same load the JSON sample describes; a test covers
the conversion; README documents the flag.

## 4. Add a broker payment shortfall report

Title: Export a shortfall report as CSV

Description: Add a --shortfall-out flag that writes one CSV row per
underbilled or missing line (load number, line, ratecon amount,
invoice amount, shortfall) plus a totals row, ready to attach to a
broker payment dispute.

Difficulty: Easy

Acceptance criteria: the CSV rows match the text report findings to
the cent; matched loads produce no rows; invoice-only charges are
excluded; a test round-trips the export; README documents the flag.
