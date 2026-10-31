# Open issues

## ACM-001: ACM Queue rejects automated retrieval

- **Status:** Open
- **Recorded:** 2026-10-31
- **Priority:** Low; does not block the first brief generator.
- **Affected source:** ACM Queue — https://queue.acm.org/

### Observed behavior

Local discovery returned HTTP `403 Forbidden` for the ACM Queue homepage. The previously
checked feed URLs returned `410 Gone` and `404 Not Found`. Other publishers continued
successfully; the generator recorded the source failure and skipped ACM.

Local reproduction: run `.venv/bin/daily-tech --discover-only`. The reported run is
`output/20261031T203926292704Z/run.json` (generated locally and excluded from Git).

### Current handling

Leave ACM configured and skip inaccessible content. The retriever supports public pages
and does not implement institutional login. A 403 alone does not establish whether
credentials or a subscription would resolve access.

### Follow-up

- Investigate an officially supported public feed, API, or other permitted retrieval method.
- Verify discovery and full article retrieval from the deployment environment.
- If reliable access is unavailable, disable the source with a documented reason.

### Completion criteria

Either ACM supplies discoverable articles with readable content and valid metadata through
a supported access method, or the source is explicitly disabled with its limitation documented.
