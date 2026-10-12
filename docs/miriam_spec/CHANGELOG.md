# Miriam Spec Changelog

## v1.2 (2026-09-16)

**Changes:** Initial canonical specification v1.2
- Created with all 8 content areas from RAI-113
- Added 20+ good/bad example pairs with concrete violations
- Implemented machine-checkable registry and examples
- Added regression testing gates in CI
- Updated all citations in code to reference v1.2

**Acceptance Criteria:**
- Every high-risk behavior has explicit rule
- Spec version referenced by agent and evaluation suite
- Changes to spec require benchmark regression testing
- New engineer can implement Miriam behavior from this document alone

**Benchmark Run:** Current baseline (262 tests passed)

## v1.1 (unpublished)
Reference only - citations from code point to this unissued version. All citations updated to v1.2 in this implementation.

## v1.0 (unreleased)
Base specification for testing.

## Change Control Policy

### Required Changes for Spec Updates

1. **Version Bump**: Increment semantic version (v1.2 → v1.3 or v2.0)
2. **Changelog Entry**: Document changes with:
   - Clear description of modifications
   - Acceptance criteria updates
   - Current benchmark run status
   - Testing impact assessment
3. **Regression Testing**: Run full test suite including:
   - Spec validation tests
   - Quality lint tests  
   - Onboarding evaluation replay
   - All existing functionality tests
4. **PR Checklist**: Include:
   - Spec diff summary
   - Regression test results
   - Version bump justification
   - Impact on existing functionality

### Approval Workflow

- **Minimal Change**: Spec edits within existing structure (typos, clarifications)
  - Reviewer: Any engineer
  - Process: Update spec, run regression tests, PR

- **Behavioral Change**: New rules, removed restrictions, significant rewording
  - Reviewer: Senior engineer + product owner
  - Process: Spec update, full regression testing, manual review of behavioral impact

- **Breaking Change**: Removed functionality, rule reversal, version bump
  - Reviewer: Product owner + technical lead
  - Process: Spec update with migration guide, comprehensive testing, user impact assessment

### Versioning Strategy

- **Patch (v1.2 → v1.3)**: Fixes, clarifications, minor improvements
- **Minor (v1.2 → v2.0)**: New sections, behavioral rule changes
- **Major (v2.0 → v3.0)**: Removed functionality, breaking changes

Always maintain backward compatibility where possible. Document any known limitations or areas requiring gradual migration.
