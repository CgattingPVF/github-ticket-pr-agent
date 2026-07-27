# Escalation Pipeline Logic

## Testing Lab (Automated QA)

The Testing Lab runs a **fixed escalation pipeline** that starts with Haiku and escalates upward when evidence gates fail.

### Pipeline Order
```
Haiku → Sonnet → Luna → Opus → Sol Low → Sol High
```

### How It Works

1. **Initial Run**: Testing starts with Haiku
2. **Evidence Gate**: After each run, the QA system scores the "integrity" of the results
   - If all checks pass or pass rate is acceptable → **STOP, report success**
   - If integrity is below threshold or critical failures detected → **ESCALATE**
3. **Next Tier**: Move to Sonnet, then Luna, and re-run the same tests
4. **Repeat**: Continue escalating through each tier until:
   - Tests pass with acceptable integrity, OR
   - Reach Sol High (final tier) and report the best result

### Why This Order?

- **Haiku**: Fast, cost-effective, good for straightforward requirements
- **Sonnet**: Balanced speed/capability, catches mid-complexity issues
- **Luna (GPT-5.6)**: Stronger reasoning, handles intricate test scenarios
- **Opus**: High-capability Anthropic model for complex edge cases
- **Sol Low/High (GPT-5.6 reasoning)**: Maximum reasoning power for the hardest proofs

### Pipeline Implementation

**Testing Phase** (`/testing/run`):
```python
# Hardcoded to start with Haiku (which triggers escalation)
job = store.create({
    'workflow_profile': 'testing_only',
    'agent_provider': 'claude',
    'approval_mode': 'auto',   # No human gates
})
```

**JavaScript Tracking** (`testing.html`):
```javascript
const PIPELINE_TIER_ORDER = [
  'claude-haiku-4-5',      // Tier 0 (start here)
  'claude-sonnet-5',       // Tier 1
  'gpt-5.6-luna',          // Tier 2
  'claude-opus-4-7',       // Tier 3
  'gpt-5.6-sol',           // Tier 4 (Sol Low)
  'gpt-5.6-sol-high',      // Tier 5 (Sol High, final)
];
```

When a job is running, the UI reads `job.stage` (e.g., "Running autonomous QA with claude-sonnet-5") and highlights the active tier in the pipeline gauge.

### Failure Handling

If a run **fails**:
1. Operator sees red X on that tier
2. "Fix + retest" button launches a **QA Fix workflow**
3. Fix workflow also escalates: `qa_fix` profile creates a new job that runs the full automation ladder

If all tiers are exhausted:
- Report the best result achieved
- Operator can manually override test outcomes with justification
