## Summary

Briefly describe the architectural scope or defect remediation introduced in this Pull Request.

- **Closes**: #<!-- Issue Number -->
- **Branch Type**: `feat/*` | `fix/*` | `test/*` | `docs/*`

---

## Key Changes

- 
- 
- 

---

## Engineering & Contract Checklist

- [ ] **Deterministic Fact Grounding**: Prices, SKU specs, and after-sales eligibility are strictly derived from structured source-of-truth records (`0` hallucinated coupons or unverified claims).
- [ ] **Fail-Closed Safety & SLA Fallback**: External API timeouts (`>800ms` for System One Jev, circuit breaker trips for primary LLM) gracefully degrade to deterministic local engines.
- [ ] **272K Prompt Cache Alignment**: Static constitution above `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` remains byte-identical across turns (`static_prefix_md5` invariant).
- [ ] **Clean Workspace**: Zero `.env` secrets, zero `__pycache__` artifacts, and all file encodings verified as UTF-8.

---

## Verification & Reproducible Test Output

```bash
# Paste exact verification commands and exit codes executed with `python -B`
python -B scripts/validate_catalog.py
```
