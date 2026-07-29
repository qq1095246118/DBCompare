# Asymmetric Member Transfers Design

## Problem

For one token, Bubblemaps may return a transfer involving member B only when the
transfers endpoint is queried for member A. The current pipeline builds final
member views from the union of all responses, but each clean member file contains
only that member's own endpoint response. Final validation therefore fails when
the merged member view is more complete than the member's individual clean file.

## Selected Behavior

Use the accepted, exact-chain and exact-token transfer union from all ordinary
Cluster member responses as the source for final member views. Deduplicate the
union with the existing transfer identity rules. Store every accepted transfer in
the clean file of each ordinary Cluster member that appears as its sender or
receiver.

Raw response files remain unchanged. A final member transfer is valid when it can
be traced to any raw member response for the same chain and token. It does not
need to appear in that member's own raw response.

## Boundaries

- Do not change Cluster reconstruction or Cluster ranking.
- Do not add external or unranked addresses as members.
- Do not change exact chain and token filtering.
- Do not fetch additional API data.
- Do not change Supernode handling.
- Preserve existing transfer deduplication, ordering, completeness checks, and
  partial-success warnings.
- Continue to reject a final transfer that appears in none of the token's raw
  member responses.

## Data Flow

1. Persist each official response unchanged in the raw layer.
2. Persist the initially filtered per-request response in the clean layer.
3. Read all clean member files and build the deduplicated token-wide transfer
   union.
4. Derive each ordinary member's complete transfer view from that union.
5. Replace the clean member documents with those derived views.
6. Write the final token summary and validate final member transfers against the
   union of all raw member responses for that token.

## Failure Handling

The batch still fails when a final transfer cannot be traced to any raw response,
when identity deduplication is ambiguous, or when existing strict completeness
checks fail. Endpoint asymmetry by itself is not an error or partial-success
condition because no accepted transfer is missing from the captured raw union.

## Verification

- Add a CLI regression test where a transfer involving member B appears only in
  member A's response; both final member files must contain it and publication
  must succeed.
- Keep a negative validation test proving that a fabricated final transfer not
  present in the raw union is rejected.
- Replay the latest failed generation and verify the affected member changes from
  three to five persisted transfers.
- Run the complete test suite and compile checks.
