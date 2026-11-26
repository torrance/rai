---
description: "Check new or changed claims in one draft file against the library"
agent: assistant
---
Check the claims for $ARGUMENTS. Call read_notes("claims") and take the claims for
that file whose status is "unchecked". For each: search the library, read the
relevant passages with read_location, then record the result with update_claim
(status and the evidence you read). Finish with a short summary in the chat of what
changed: contradicted claims first, then unsupported, then the rest.
