# Polymarket Per-Category CLI Limit Design

## Goal

Allow operators to choose how many Polymarket selections are retained per
configured category without changing API pagination or candidate collection.

The new command-line option is:

```text
--per-category N
```

It defaults to `20` so existing commands keep their current behavior.

## Current State

`select_ranked_markets()` already accepts a positive `per_category` argument
and defaults it to `20`. The API collector currently calls the function without
passing that argument, so operators cannot change the limit from the command
line.

`--page-limit` controls only the number of markets requested per Gamma API
page. It does not control the number of selected records.

## CLI Contract

Add `--per-category` to
`getMarket.Polymarket.tool.export_polymarket_market` with these rules:

- the default is `20`;
- any positive integer is accepted;
- zero, negative values, decimal values, and non-numeric text are rejected by
  argument parsing;
- there is no upper bound; and
- `--page-limit` remains a separate option with its existing `1` through `20`
  validation.

Example:

```bash
.venv/bin/python -m getMarket.Polymarket.tool.export_polymarket_market \
  --per-category 10
```

Invalid CLI values fail during argument parsing, before a run directory or any
artifact is created.

## Data Flow

The collector passes the parsed value directly to the existing ranking layer:

```python
select_ranked_markets(
    merged.markets,
    per_category=args.per_category,
)
```

The option changes only how many selected rows the ranking result contains.
It does not stop API pagination early and does not reduce the candidate pool.

For a value of `10`:

- each configured category contributes at most 10 records to `final.json`;
- the six configured categories therefore contribute at most 60 category
  records in total;
- categories with fewer than 10 eligible markets contribute their available
  count;
- one market selected in multiple categories remains one record per category;
  and
- ranks within each category remain consecutive, starting at 1.

The existing metric fallback remains unchanged: liquidity is used first,
followed by dominant probability and then 24-hour volume when earlier metrics
do not fill the configured limit.

## Artifact Compatibility

`clean.json` remains the complete, globally unique candidate list and is not
truncated by `--per-category`.

`final.json` keeps the existing DB-aligned `{"records": [...]}` structure. Only
the number of selected records changes. Its 14 outer fields, 17 `content`
fields, 8 `extra_data` fields, time formats, null placeholders, category order,
and cross-category duplicate behavior remain unchanged.

Raw API pages and sanitized `error.json` behavior also remain unchanged. The DB
exporter is outside this feature's scope.

## Documentation

Update the Polymarket collector README and the global command guide to:

- include `--per-category` in the runnable examples and parameter list;
- state that it defaults to 20 and accepts any positive integer;
- distinguish it from `--page-limit`; and
- explain that it limits only `final.json`, not API collection or `clean.json`.

## Tests

Extend the Polymarket CLI tests to prove:

- parsing without the option returns `per_category == 20`;
- `--per-category 10` is accepted;
- zero, negative, decimal, and non-numeric values are rejected;
- the collector forwards the parsed value to the ranking layer;
- a limit of 10 produces at most 10 records per category with ranks 1 through
  10 in the complete fixture;
- the complete fixture still produces all 125 globally unique candidates in
  `clean.json`; and
- omitting the option preserves the existing 20-per-category result.

Run the complete offline Polymarket suite and the repository's full offline
regression suite after implementation.

## Non-Goals

This change does not:

- limit how many markets are downloaded from Gamma;
- alter category membership, crypto filtering, metric normalization, ranking
  priorities, or tie-breaking;
- change DB export behavior;
- add configuration-file or environment-variable support; or
- change any JSON field structure.
