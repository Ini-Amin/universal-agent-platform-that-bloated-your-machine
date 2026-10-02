---
name: api-recon
description: Map an API's attack surface before any exploitation attempt
domain: bbp
required_capabilities: http.request, dns.lookup, document.read
version: 1
---

# API Reconnaissance

## When to use
An authorized engagement exposes an HTTP API and the goal is to understand its
surface before testing for vulnerabilities. Recon only -- this skill does not
perform exploitation.

## Method
1. **Confirm authorization and scope.** Record the in-scope hosts and the
   rules of engagement before sending a single request.
2. **Fingerprint the stack.** Capture server headers, framework hints, and
   error shapes; note any gateway or WAF in front of the origin.
3. **Enumerate endpoints** from documentation, OpenAPI/Swagger specs, client
   bundles, and observed traffic. Keep a deduplicated endpoint inventory.
4. **Classify by auth model.** For each endpoint record: required auth, token
   type, and whether object identifiers appear in the path or body.
5. **Probe methods and content types** per endpoint, watching status codes for
   differences between unauthenticated, authenticated, and privileged calls.
6. **Rate-limit politely.** Throttle requests and honour `Retry-After`; recon
   must not degrade the service.
7. **Write up the surface**: endpoints, auth requirements, parameters, and the
   highest-value candidates for deeper testing.

## Checks
- Every request stayed inside the authorized scope.
- The endpoint inventory is deduplicated and reproducible.
- Authentication differences are recorded per endpoint, not assumed globally.

## Pitfalls
- Sending destructive or state-changing requests during recon.
- Treating a WAF 403 as "endpoint does not exist".
- Assuming an endpoint is safe because the UI never links to it.
- Missing object identifiers in nested or batch endpoints.
